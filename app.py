import os
import json
import math
import anthropic
import fitz  # PyMuPDF
from flask import Flask, request, jsonify, render_template_string
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MAX_PAGES_PER_CHUNK = 80   # stay under the 100-page API limit
MAX_FILE_BYTES = 900 * 1024 * 1024  # 900 MB

# ── System prompt ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an expert appellate attorney and legal analyst specializing in reviewing and summarizing court of appeals records. Your task is to thoroughly analyze the uploaded court record and produce a structured, comprehensive summary.

Return ONLY valid JSON — no markdown fences, no preamble, nothing else.

The JSON must have exactly these keys:
{
  "caseTitle": "string — case name/number if identifiable",
  "pleadings": [...],
  "witnesses": [...],
  "dispositiveMotions": [...],
  "ordersAndJudgments": [...],
  "trial": { ... },
  "pretrialMatters": [...],
  "trialObjections": [...]
}

For each array item, include a "date" field (string, e.g. "March 5, 2021" or "unknown") and a "title" field.

pleadings: each item: { date, title, type (e.g. "Petition","Answer","Affirmative Defense","Third-Party Petition", etc.), summary }

witnesses: each item: { name, date, type ("Affidavit"|"Declaration"|"Deposition"|"Trial Testimony"), keyTestimony (array of strings — bullet points of most important testimony) }

dispositiveMotions: each item: { date, title, type ("Motion to Dismiss"|"Motion for Summary Judgment"|"Plea to the Jurisdiction"|"Motion for Directed Verdict"|other), filedBy, mainArguments (array of strings), courtRuling, rulingDate }

ordersAndJudgments: each item: { date, title, type ("Interlocutory Order"|"Final Judgment"|"Partial Summary Judgment"|"Order on Motion"|other), summary }

trial: {
  occurred: boolean,
  startDate: string,
  endDate: string,
  days: [ { dayNumber, date, description, witnesses: [string], significantEvents: [string] } ],
  exhibits: [ { id, description, offeredBy, date, admitted: boolean, reason } ],
  summary: string
}

pretrialMatters: each item: { date, title, type ("Motion in Limine"|"Pretrial Order"|"Scheduling Order"|"Pretrial Conference"|other), summary, ruling }

trialObjections: each item: { date, objectingParty, basis, evidenceOrTestimony, courtRuling, sustained: boolean }

Be exhaustive. If a section has no relevant documents, return an empty array (or for trial, occurred: false). For dates, use the format found in the document."""

MERGE_PROMPT = """You are an expert appellate attorney. You have analyzed a large court record in multiple sections and now have partial summaries from each section. 

Merge all the partial summaries below into one comprehensive, deduplicated summary. Combine entries that refer to the same document, witness, or event. Keep all unique entries.

Return ONLY valid JSON with exactly these keys: caseTitle, pleadings, witnesses, dispositiveMotions, ordersAndJudgments, trial, pretrialMatters, trialObjections.

For the trial object: if any section shows a trial occurred, set occurred: true and merge the days, exhibits, and summary from all sections.

Partial summaries to merge:
"""

# ── HTML page ────────────────────────────────────────────────────────────────

HTML = open(os.path.join(os.path.dirname(__file__), "index.html")).read()

@app.route("/")
def index():
    return render_template_string(HTML)

# ── Analyze endpoint ─────────────────────────────────────────────────────────

@app.route("/analyze", methods=["POST"])
def analyze():
    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "Server is missing ANTHROPIC_API_KEY. Contact the site administrator."}), 500

    if "file" not in request.files:
        return jsonify({"error": "No file uploaded."}), 400

    f = request.files["file"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Please upload a PDF file."}), 400

    pdf_bytes = f.read()
    if len(pdf_bytes) > MAX_FILE_BYTES:
        mb = len(pdf_bytes) / 1048576
        return jsonify({"error": f"File is {mb:.0f} MB. Maximum allowed size is 900 MB."}), 400

    # Open PDF and count pages
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        return jsonify({"error": f"Could not open PDF: {str(e)}"}), 400

    total_pages = len(doc)

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # If small enough, send as a single PDF document via the Files API
    if total_pages <= MAX_PAGES_PER_CHUNK and len(pdf_bytes) <= 32 * 1024 * 1024:
        doc.close()
        try:
            result = analyze_pdf_bytes(client, pdf_bytes)
            return jsonify(result)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # Large document: extract text, split into chunks, analyze each, merge
    try:
        partial_results = analyze_in_chunks(client, doc, total_pages)
        doc.close()
        if len(partial_results) == 1:
            return jsonify(partial_results[0])
        merged = merge_results(client, partial_results)
        return jsonify(merged)
    except Exception as e:
        doc.close()
        return jsonify({"error": str(e)}), 500


def analyze_pdf_bytes(client, pdf_bytes):
    """Send a small PDF directly to the API as base64."""
    import base64
    b64 = base64.b64encode(pdf_bytes).decode("utf-8")
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=8000,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "document",
                    "source": {"type": "base64", "media_type": "application/pdf", "data": b64}
                },
                {
                    "type": "text",
                    "text": "Please analyze this appellate court record and return the structured JSON summary as instructed."
                }
            ]
        }]
    )
    raw = response.content[0].text
    return parse_json(raw)


def analyze_in_chunks(client, doc, total_pages):
    """Extract text page by page, split into chunks, analyze each chunk."""
    num_chunks = math.ceil(total_pages / MAX_PAGES_PER_CHUNK)
    results = []

    for chunk_idx in range(num_chunks):
        start = chunk_idx * MAX_PAGES_PER_CHUNK
        end = min(start + MAX_PAGES_PER_CHUNK, total_pages)

        # Extract text for these pages
        text_parts = []
        for page_num in range(start, end):
            page = doc[page_num]
            text = page.get_text("text")
            if text.strip():
                text_parts.append(f"[Page {page_num + 1}]\n{text}")

        chunk_text = "\n\n".join(text_parts)

        if not chunk_text.strip():
            continue  # skip blank chunks

        label = f"pages {start + 1}–{end} of {total_pages}"
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=8000,
            system=SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": f"Analyze this section of the appellate court record ({label}) and return the structured JSON summary.\n\n{chunk_text}"
            }]
        )
        raw = response.content[0].text
        try:
            results.append(parse_json(raw))
        except Exception:
            pass  # skip unparseable chunks

    return results


def merge_results(client, partials):
    """Ask Claude to merge multiple partial JSON summaries into one."""
    combined = json.dumps(partials, indent=2)
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=8000,
        system=MERGE_PROMPT + combined,
        messages=[{
            "role": "user",
            "content": "Please merge these partial summaries into one comprehensive JSON summary."
        }]
    )
    raw = response.content[0].text
    return parse_json(raw)


def parse_json(raw):
    clean = raw.strip()
    if clean.startswith("```"):
        clean = clean.split("```")[1]
        if clean.startswith("json"):
            clean = clean[4:]
    clean = clean.strip().rstrip("`").strip()
    return json.loads(clean)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
