import os
import json
import math
import base64
import traceback
import anthropic
import fitz  # PyMuPDF
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
 
app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)
 
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MAX_PAGES_PER_CHUNK = 80
MAX_FILE_BYTES = 900 * 1024 * 1024  # 900 MB
 
# ── Increase Flask's max content length to 900 MB ────────────────────────────
app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_BYTES
 
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
 
# ── Routes ───────────────────────────────────────────────────────────────────
 
@app.route("/")
def index():
    return send_from_directory(".", "index.html")
 
@app.route("/health")
def health():
    return jsonify({"status": "ok"})
 
@app.route("/analyze", methods=["POST"])
def analyze():
    try:
        return _analyze()
    except Exception as e:
        tb = traceback.format_exc()
        print("UNHANDLED ERROR:", tb)
        return jsonify({"error": f"Unexpected server error: {str(e)}"}), 500
 
def _analyze():
    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "Server is missing ANTHROPIC_API_KEY. Add it in the Render dashboard under Environment."}), 500
 
    if "file" not in request.files:
        return jsonify({"error": "No file received. Please select a PDF and try again."}), 400
 
    f = request.files["file"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Please upload a PDF file."}), 400
 
    # Read file in chunks to handle large uploads
    try:
        pdf_bytes = f.read()
    except Exception as e:
        return jsonify({"error": f"Failed to read uploaded file: {str(e)}"}), 400
 
    if not pdf_bytes:
        return jsonify({"error": "The uploaded file appears to be empty."}), 400
 
    mb = len(pdf_bytes) / 1048576
    print(f"Received file: {f.filename}, size: {mb:.1f} MB")
 
    if len(pdf_bytes) > MAX_FILE_BYTES:
        return jsonify({"error": f"File is {mb:.0f} MB. Maximum allowed size is 900 MB."}), 400
 
    # Open PDF
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        return jsonify({"error": f"Could not open PDF (it may be corrupted or password-protected): {str(e)}"}), 400
 
    total_pages = len(doc)
    print(f"PDF opened: {total_pages} pages")
 
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
 
    # Small PDF: send as base64 directly
    if total_pages <= MAX_PAGES_PER_CHUNK and len(pdf_bytes) <= 32 * 1024 * 1024:
        doc.close()
        print("Using direct PDF analysis")
        try:
            result = analyze_pdf_bytes(client, pdf_bytes)
            return jsonify(result)
        except Exception as e:
            print("Direct analysis error:", traceback.format_exc())
            return jsonify({"error": f"Analysis failed: {str(e)}"}), 500
 
    # Large PDF: extract text, chunk, analyze, merge
    print(f"Using chunked text analysis ({math.ceil(total_pages / MAX_PAGES_PER_CHUNK)} chunks)")
    try:
        partial_results = analyze_in_chunks(client, doc, total_pages)
        doc.close()
    except Exception as e:
        doc.close()
        print("Chunked analysis error:", traceback.format_exc())
        return jsonify({"error": f"Analysis failed during chunked processing: {str(e)}"}), 500
 
    if not partial_results:
        return jsonify({"error": "Could not extract any readable text from this PDF. It may be a scanned image — please use a PDF with selectable text."}), 400
 
    try:
        if len(partial_results) == 1:
            return jsonify(partial_results[0])
        merged = merge_results(client, partial_results)
        return jsonify(merged)
    except Exception as e:
        print("Merge error:", traceback.format_exc())
        return jsonify({"error": f"Analysis failed during merge: {str(e)}"}), 500
 
 
def analyze_pdf_bytes(client, pdf_bytes):
    b64 = base64.b64encode(pdf_bytes).decode("utf-8")
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=8000,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64}},
                {"type": "text", "text": "Please analyze this appellate court record and return the structured JSON summary as instructed."}
            ]
        }]
    )
    return parse_json(response.content[0].text)
 
 
def analyze_in_chunks(client, doc, total_pages):
    num_chunks = math.ceil(total_pages / MAX_PAGES_PER_CHUNK)
    results = []
 
    for chunk_idx in range(num_chunks):
        start = chunk_idx * MAX_PAGES_PER_CHUNK
        end = min(start + MAX_PAGES_PER_CHUNK, total_pages)
        label = f"pages {start + 1}-{end} of {total_pages}"
        print(f"Analyzing chunk {chunk_idx + 1}/{num_chunks}: {label}")
 
        text_parts = []
        for page_num in range(start, end):
            text = doc[page_num].get_text("text")
            if text.strip():
                text_parts.append(f"[Page {page_num + 1}]\n{text}")
 
        chunk_text = "\n\n".join(text_parts)
        if not chunk_text.strip():
            print(f"  Chunk {chunk_idx + 1} has no extractable text, skipping")
            continue
 
        try:
            response = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=8000,
                system=SYSTEM_PROMPT,
                messages=[{
                    "role": "user",
                    "content": f"Analyze this section of the appellate court record ({label}) and return the structured JSON summary.\n\n{chunk_text}"
                }]
            )
            parsed = parse_json(response.content[0].text)
            results.append(parsed)
            print(f"  Chunk {chunk_idx + 1} analyzed successfully")
        except Exception as e:
            print(f"  Chunk {chunk_idx + 1} failed: {e}")
            # Continue with remaining chunks
 
    return results
 
 
def merge_results(client, partials):
    combined = json.dumps(partials, indent=2)
    print(f"Merging {len(partials)} partial results")
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=8000,
        system=MERGE_PROMPT + combined,
        messages=[{
            "role": "user",
            "content": "Please merge these partial summaries into one comprehensive JSON summary."
        }]
    )
    return parse_json(response.content[0].text)
 
 
def parse_json(raw):
    clean = raw.strip()
    # Strip markdown code fences if present
    if "```" in clean:
        parts = clean.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            try:
                return json.loads(part)
            except Exception:
                continue
    return json.loads(clean)
 
 
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
