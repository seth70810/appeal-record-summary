"""
MyAppealCoach.com — Oral Argument Tools
Routes to be registered on the main Flask app in app.py.

Integration instructions (two steps):
  1. At the top of app.py, add:
       from oral_arguments_routes import register_oral_routes
  2. After `app = Flask(__name__)`, add:
       register_oral_routes(app)

That's it. All routes live under /oral-arguments and /api/oral/.
"""

import os
import io
import json
import re
import fitz  # PyMuPDF
import anthropic

from flask import Blueprint, render_template_string, request, jsonify, Response, stream_with_context

oral_bp = Blueprint("oral", __name__)
_client = None


def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    return _client


def register_oral_routes(app):
    app.register_blueprint(oral_bp)


# ─────────────────────────────────────────────────────────────────────────────
# PAGE ROUTE
# ─────────────────────────────────────────────────────────────────────────────

@oral_bp.route("/oral-arguments")
def oral_arguments_page():
    return render_template_string(ORAL_HTML)


# ─────────────────────────────────────────────────────────────────────────────
# API: MOOT COURT — INITIALIZE SESSION
# ─────────────────────────────────────────────────────────────────────────────

@oral_bp.route("/api/oral/moot/init", methods=["POST"])
def moot_init():
    """
    Accepts a brief PDF, returns a JSON panel description and
    the first judge question based on frequency/intensity settings.
    """
    if "brief" not in request.files:
        return jsonify({"error": "No brief uploaded"}), 400

    frequency = int(request.form.get("frequency", 5))
    brief_file = request.files["brief"]
    brief_bytes = brief_file.read()

    # Extract text from PDF
    brief_text = _extract_pdf_text(brief_bytes, max_pages=60)
    if not brief_text.strip():
        return jsonify({"error": "Could not extract text from brief. Ensure the PDF has selectable text."}), 400

    # Determine panel behavior based on frequency
    freq_desc = _frequency_description(frequency)

    system = """You are a panel of three federal appellate judges conducting oral argument.
You are universally intense, probing, and skeptical — always. Your job is to test the advocate's mastery of the record, the law, and the weaknesses in their case.

You will be given a brief. Your tasks:
1. Identify the three judges by name and briefly characterize each judge's personality/focus area (record-focused, policy-focused, precedent-focused, etc.)
2. Generate a list of at least 20 tough questions drawn from the brief's weakest points, record gaps, contrary authority, and policy implications.
3. Return ONLY a JSON object with this exact structure:
{
  "panel": [
    {"name": "Judge [Lastname]", "focus": "one-sentence description"},
    {"name": "Judge [Lastname]", "focus": "one-sentence description"},
    {"name": "Judge [Lastname]", "focus": "one-sentence description"}
  ],
  "questions": [
    {"judge": "Judge [Lastname]", "question": "..."},
    ...
  ]
}

Use realistic judge names (not famous real judges). Make questions pointed, specific to the brief, and genuinely difficult. Do not include any preamble or explanation outside the JSON."""

    user = f"""Here is the appellant's brief (first 60 pages extracted):

{brief_text[:12000]}

Frequency setting: {frequency}/10 — {freq_desc}

Generate the panel and question bank now."""

    try:
        response = _get_client().messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4000,
            system=system,
            messages=[{"role": "user", "content": user}]
        )
        raw = response.content[0].text.strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
        data = json.loads(raw)
        data["frequency"] = frequency
        data["brief_excerpt"] = brief_text[:8000]  # Store for later critique calls
        return jsonify(data)
    except json.JSONDecodeError as e:
        return jsonify({"error": f"Failed to parse panel response: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────────────────────────────────────
# API: MOOT COURT — CRITIQUE ANSWER
# ─────────────────────────────────────────────────────────────────────────────

@oral_bp.route("/api/oral/moot/critique", methods=["POST"])
def moot_critique():
    """
    Accepts a judge's question + advocate's transcribed answer.
    Returns critique + suggested stronger answer.
    """
    data = request.get_json()
    question = data.get("question", "")
    answer = data.get("answer", "")
    brief_excerpt = data.get("brief_excerpt", "")
    judge_name = data.get("judge", "The Court")

    if not question or not answer:
        return jsonify({"error": "Missing question or answer"}), 400

    system = """You are a senior appellate advocacy coach reviewing an attorney's oral argument performance.
Analyze the advocate's spoken answer to a judge's question.
Return ONLY a JSON object with this structure:
{
  "score": <integer 1-10>,
  "critique": "<2-3 sentence honest assessment — what worked, what didn't>",
  "fatal_flaws": ["<flaw 1>", "<flaw 2>"],
  "stronger_answer": "<A model answer the advocate should have given — conversational, concise, under 90 words>"
}
Be direct and demanding. Do not sugarcoat weaknesses."""

    user = f"""Brief context:
{brief_excerpt[:4000]}

Judge's question ({judge_name}):
{question}

Advocate's answer (transcribed from speech):
{answer}

Evaluate the answer and provide coaching."""

    try:
        response = _get_client().messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            system=system,
            messages=[{"role": "user", "content": user}]
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
        return jsonify(json.loads(raw))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────────────────────────────────────
# API: MOOT COURT — REBUTTAL REPORT
# ─────────────────────────────────────────────────────────────────────────────

@oral_bp.route("/api/oral/moot/rebuttal", methods=["POST"])
def moot_rebuttal():
    """
    Accepts the full session log (questions + answers + scores).
    Returns a targeted rebuttal coaching report.
    """
    data = request.get_json()
    session_log = data.get("session_log", [])
    brief_excerpt = data.get("brief_excerpt", "")

    if not session_log:
        return jsonify({"error": "No session data provided"}), 400

    # Find weakest answers
    sorted_log = sorted(session_log, key=lambda x: x.get("score", 10))
    weakest = sorted_log[:5]

    log_text = "\n\n".join([
        f"Q ({item['judge']}): {item['question']}\nA: {item['answer']}\nScore: {item.get('score','N/A')}/10\nFlaws: {', '.join(item.get('fatal_flaws', []))}"
        for item in weakest
    ])

    system = """You are a senior appellate advocacy coach writing a post-argument rebuttal coaching report.
Return ONLY a JSON object with this structure:
{
  "summary": "<2-3 sentence overall assessment of the argument session>",
  "rebuttal_points": [
    {
      "trigger": "<what judge question or challenge triggered this rebuttal need>",
      "rebuttal_strategy": "<how to handle this on rebuttal — specific language and approach>",
      "key_phrase": "<one powerful sentence to use in rebuttal>"
    }
  ],
  "overall_recommendations": ["<recommendation 1>", "<recommendation 2>", "<recommendation 3>"]
}
Focus on actionable rebuttal strategies, not general praise."""

    user = f"""Brief context:
{brief_excerpt[:3000]}

Weakest answers from the moot court session:
{log_text}

Generate a targeted rebuttal coaching report."""

    try:
        response = _get_client().messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            system=system,
            messages=[{"role": "user", "content": user}]
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
        return jsonify(json.loads(raw))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────────────────────────────────────
# API: RECORD CITE CHECKER
# ─────────────────────────────────────────────────────────────────────────────

@oral_bp.route("/api/oral/cite-check", methods=["POST"])
def cite_check():
    """
    Accepts brief PDF + record PDF.
    Extracts record citations from brief, verifies each appears in the record.
    Returns verified, unverified, and not-found citations.
    """
    if "brief" not in request.files or "record" not in request.files:
        return jsonify({"error": "Both brief and record PDFs are required"}), 400

    brief_bytes = request.files["brief"].read()
    record_bytes = request.files["record"].read()

    brief_text = _extract_pdf_text(brief_bytes, max_pages=100)
    record_text = _extract_pdf_text(record_bytes, max_pages=200)

    if not brief_text.strip():
        return jsonify({"error": "Could not extract text from brief"}), 400
    if not record_text.strip():
        return jsonify({"error": "Could not extract text from record"}), 400

    # Step 1: Extract citations from brief
    extract_system = """You are an appellate record citation extractor.
Extract every record citation from the brief. Texas appellate record citations use formats like:
- RR [volume]:[page] (Reporter's Record)
- CR [page] (Clerk's Record)
- 1 RR 45, 2 RR 12-15, CR 100
- Supp. CR, Supp. RR variants

Return ONLY a JSON array of citation strings exactly as they appear in the brief:
["RR 2:45", "CR 100", "3 RR 12", ...]

If no citations found, return []."""

    extract_user = f"Brief text:\n{brief_text[:10000]}\n\nExtract all record citations."

    try:
        extract_resp = _get_client().messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            system=extract_system,
            messages=[{"role": "user", "content": extract_user}]
        )
        raw = extract_resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
        citations = json.loads(raw)
    except Exception as e:
        return jsonify({"error": f"Citation extraction failed: {str(e)}"}), 500

    if not citations:
        return jsonify({
            "citations_found": 0,
            "verified": [],
            "not_found": [],
            "summary": "No record citations detected in the brief."
        })

    # Step 2: Verify each citation against record text
    verify_system = """You are an appellate record citation verifier.
You have the text of an appellate record. For each citation provided, determine whether the content it references likely appears in the record text.

Return ONLY a JSON array with this structure:
[
  {"citation": "RR 2:45", "status": "verified", "note": "Page 45 of vol 2 contains testimony about..."},
  {"citation": "CR 200", "status": "not_found", "note": "No content matching CR page 200 found in record text"},
  {"citation": "3 RR 12", "status": "uncertain", "note": "Record text is truncated and may not include this page"}
]
Status must be exactly: "verified", "not_found", or "uncertain"."""

    verify_user = f"""Citations to verify:
{json.dumps(citations)}

Record text (may be truncated):
{record_text[:15000]}

Verify each citation."""

    try:
        verify_resp = _get_client().messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            system=verify_system,
            messages=[{"role": "user", "content": verify_user}]
        )
        raw = verify_resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
        results = json.loads(raw)

        verified = [r for r in results if r["status"] == "verified"]
        not_found = [r for r in results if r["status"] == "not_found"]
        uncertain = [r for r in results if r["status"] == "uncertain"]

        return jsonify({
            "citations_found": len(citations),
            "verified": verified,
            "not_found": not_found,
            "uncertain": uncertain,
            "summary": f"{len(verified)} verified, {len(not_found)} not found, {len(uncertain)} uncertain out of {len(citations)} total citations."
        })
    except Exception as e:
        return jsonify({"error": f"Verification failed: {str(e)}"}), 500


# ─────────────────────────────────────────────────────────────────────────────
# API: HOT BENCH PREP
# ─────────────────────────────────────────────────────────────────────────────

@oral_bp.route("/api/oral/hot-bench", methods=["POST"])
def hot_bench():
    """
    Accepts brief PDF.
    Returns at least 10 most dangerous questions with recommended answers.
    Streams the response for better UX on large briefs.
    """
    if "brief" not in request.files:
        return jsonify({"error": "No brief uploaded"}), 400

    brief_bytes = request.files["brief"].read()
    brief_text = _extract_pdf_text(brief_bytes, max_pages=80)

    if not brief_text.strip():
        return jsonify({"error": "Could not extract text from brief"}), 400

    system = """You are a senior appellate advocate preparing a client for the most dangerous judicial questions they will face.
Analyze the brief and identify AT LEAST 10 questions (aim for 12-15) that represent the greatest threats to the appellant's position.

Focus on:
- Weaknesses in the standard of review argument
- Gaps or conflicts in the record
- Contrary authority the brief doesn't adequately distinguish
- Policy implications the court will worry about
- Jurisdictional or preservation problems
- Factual assertions unsupported by record citations

Return ONLY a JSON object:
{
  "case_summary": "<2 sentence summary of what this appeal is about>",
  "primary_vulnerability": "<the single biggest weakness in the appellant's position>",
  "dangerous_questions": [
    {
      "rank": 1,
      "category": "<Standard of Review | Record | Authority | Policy | Jurisdiction | Preservation>",
      "question": "<the dangerous question exactly as a judge might ask it>",
      "why_dangerous": "<one sentence — why this question is hard to answer well>",
      "recommended_answer": "<how to answer this question — specific, under 100 words, conversational appellate register>"
    }
  ]
}"""

    user = f"""Brief text:
{brief_text[:14000]}

Identify the most dangerous questions this advocate will face at oral argument."""

    try:
        response = _get_client().messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=5000,
            system=system,
            messages=[{"role": "user", "content": user}]
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
        data = json.loads(raw)
        return jsonify(data)
    except json.JSONDecodeError as e:
        return jsonify({"error": f"Failed to parse response: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _extract_pdf_text(pdf_bytes: bytes, max_pages: int = 100) -> str:
    """Extract text from a PDF, handling portfolios/collections."""
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")

        # Handle PDF portfolios
        if doc.is_pdf and doc.embfile_count() > 0:
            all_text = []
            for i in range(doc.embfile_count()):
                info = doc.embfile_info(i)
                if info.get("filename", "").lower().endswith(".pdf"):
                    embedded = doc.embfile_get(i)
                    sub_doc = fitz.open(stream=embedded, filetype="pdf")
                    for page in sub_doc:
                        all_text.append(page.get_text())
                    sub_doc.close()
            if all_text:
                return "\n".join(all_text)

        # Standard PDF
        pages = min(len(doc), max_pages)
        text_parts = []
        for i in range(pages):
            text_parts.append(doc[i].get_text())
        doc.close()
        return "\n".join(text_parts)
    except Exception as e:
        return ""


def _frequency_description(freq: int) -> str:
    if freq == 1:
        return "Cold Panel — no interruptions during argument"
    elif freq <= 3:
        return "Quiet bench — rare questions, long stretches uninterrupted"
    elif freq <= 5:
        return "Moderately active bench — questions every few minutes"
    elif freq <= 7:
        return "Active bench — regular interruptions throughout argument"
    elif freq <= 9:
        return "Hot bench — frequent interruptions, advocate rarely finishes a thought"
    else:
        return "Extremely hot bench — question approximately every 15 seconds"


# ─────────────────────────────────────────────────────────────────────────────
# HTML PAGE
# ─────────────────────────────────────────────────────────────────────────────

ORAL_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MyAppealCoach — Oral Argument Tools</title>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;0,600;0,700;1,300;1,400;1,600&family=DM+Mono:wght@300;400;500&display=swap" rel="stylesheet">
<style>
  :root {
    --ink:        #0e0e0e;
    --paper:      #f7f4ef;
    --cream:      #ede9e0;
    --gold:       #b8963e;
    --gold-light: #d4b06a;
    --red:        #8b1a1a;
    --red-light:  #c0392b;
    --muted:      #6b6560;
    --border:     #d0c8bc;
    --card-bg:    #faf8f4;
    --success:    #2d6a4f;
    --warn:       #b8963e;
    --danger:     #8b1a1a;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background: var(--paper);
    color: var(--ink);
    font-family: 'Cormorant Garamond', Georgia, serif;
    min-height: 100vh;
  }

  /* ── Header ── */
  header {
    background: var(--ink);
    color: var(--paper);
    padding: 0;
    position: relative;
    overflow: hidden;
  }
  .header-inner {
    max-width: 900px;
    margin: 0 auto;
    padding: 48px 32px 40px;
    position: relative;
    z-index: 1;
  }
  .header-rule {
    width: 48px;
    height: 2px;
    background: var(--gold);
    margin-bottom: 20px;
  }
  header h1 {
    font-size: clamp(2rem, 5vw, 3.2rem);
    font-weight: 300;
    letter-spacing: 0.02em;
    line-height: 1.1;
  }
  header h1 em {
    font-style: italic;
    color: var(--gold-light);
  }
  header p.subtitle {
    margin-top: 12px;
    font-size: 1.1rem;
    font-weight: 300;
    color: #b0a898;
    font-style: italic;
  }
  .header-bg-text {
    position: absolute;
    right: -20px;
    top: 50%;
    transform: translateY(-50%);
    font-size: 180px;
    font-weight: 700;
    color: rgba(255,255,255,0.03);
    font-family: 'DM Mono', monospace;
    line-height: 1;
    pointer-events: none;
  }

  /* ── Nav Tabs ── */
  .tab-nav {
    background: var(--cream);
    border-bottom: 1px solid var(--border);
    display: flex;
    overflow-x: auto;
  }
  .tab-nav button {
    background: none;
    border: none;
    border-bottom: 3px solid transparent;
    padding: 16px 28px;
    font-family: 'Cormorant Garamond', serif;
    font-size: 1rem;
    font-weight: 600;
    color: var(--muted);
    cursor: pointer;
    white-space: nowrap;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    transition: all 0.2s;
  }
  .tab-nav button:hover { color: var(--ink); }
  .tab-nav button.active {
    color: var(--ink);
    border-bottom-color: var(--gold);
  }

  /* ── Main ── */
  main {
    max-width: 900px;
    margin: 0 auto;
    padding: 40px 32px 80px;
  }

  .tool-panel { display: none; }
  .tool-panel.active { display: block; }

  /* ── Cards ── */
  .card {
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 2px;
    padding: 32px;
    margin-bottom: 24px;
  }
  .card h2 {
    font-size: 1.5rem;
    font-weight: 400;
    margin-bottom: 8px;
    letter-spacing: 0.01em;
  }
  .card p.desc {
    font-size: 1rem;
    color: var(--muted);
    font-style: italic;
    line-height: 1.6;
    margin-bottom: 24px;
  }

  /* ── Form elements ── */
  label {
    display: block;
    font-size: 0.78rem;
    font-family: 'DM Mono', monospace;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--muted);
    margin-bottom: 6px;
  }
  .upload-zone {
    border: 1.5px dashed var(--border);
    border-radius: 2px;
    padding: 28px;
    text-align: center;
    cursor: pointer;
    transition: border-color 0.2s, background 0.2s;
    position: relative;
    margin-bottom: 20px;
  }
  .upload-zone:hover, .upload-zone.dragover {
    border-color: var(--gold);
    background: rgba(184,150,62,0.04);
  }
  .upload-zone input[type=file] {
    position: absolute; inset: 0; opacity: 0; cursor: pointer; width: 100%;
  }
  .upload-zone .upload-label {
    font-size: 0.95rem;
    color: var(--muted);
    font-style: italic;
  }
  .upload-zone .upload-label strong {
    color: var(--gold);
    font-style: normal;
    font-weight: 600;
  }
  .upload-zone .file-name {
    font-family: 'DM Mono', monospace;
    font-size: 0.85rem;
    color: var(--success);
    margin-top: 8px;
  }

  /* Slider */
  .slider-row {
    display: flex;
    align-items: center;
    gap: 16px;
    margin-bottom: 20px;
  }
  .slider-row input[type=range] {
    flex: 1;
    height: 4px;
    -webkit-appearance: none;
    background: var(--border);
    border-radius: 2px;
    outline: none;
  }
  .slider-row input[type=range]::-webkit-slider-thumb {
    -webkit-appearance: none;
    width: 18px;
    height: 18px;
    background: var(--gold);
    border-radius: 50%;
    cursor: pointer;
  }
  .slider-value {
    font-family: 'DM Mono', monospace;
    font-size: 1.1rem;
    font-weight: 500;
    color: var(--ink);
    min-width: 28px;
    text-align: center;
  }
  .slider-labels {
    display: flex;
    justify-content: space-between;
    font-size: 0.72rem;
    font-family: 'DM Mono', monospace;
    color: var(--muted);
    margin-top: -14px;
    margin-bottom: 20px;
    letter-spacing: 0.05em;
  }

  /* Buttons */
  .btn {
    display: inline-block;
    background: var(--ink);
    color: var(--paper);
    border: none;
    padding: 14px 32px;
    font-family: 'Cormorant Garamond', serif;
    font-size: 1rem;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    cursor: pointer;
    transition: background 0.2s;
    border-radius: 1px;
  }
  .btn:hover { background: #2a2a2a; }
  .btn:disabled { background: var(--border); color: var(--muted); cursor: not-allowed; }
  .btn-gold {
    background: var(--gold);
    color: white;
  }
  .btn-gold:hover { background: #9a7a2e; }
  .btn-outline {
    background: transparent;
    border: 1.5px solid var(--ink);
    color: var(--ink);
  }
  .btn-outline:hover { background: var(--ink); color: var(--paper); }
  .btn-sm { padding: 9px 20px; font-size: 0.88rem; }
  .btn-danger { background: var(--red); }
  .btn-danger:hover { background: var(--red-light); }

  /* ── Moot Court Simulator UI ── */
  #moot-setup, #moot-session, #moot-rebuttal { }

  .panel-display {
    display: flex;
    gap: 16px;
    margin-bottom: 24px;
    flex-wrap: wrap;
  }
  .judge-card {
    flex: 1;
    min-width: 200px;
    background: var(--ink);
    color: var(--paper);
    padding: 16px 20px;
    border-radius: 2px;
  }
  .judge-card .judge-name {
    font-size: 1.05rem;
    font-weight: 600;
    margin-bottom: 4px;
    color: var(--gold-light);
  }
  .judge-card .judge-focus {
    font-size: 0.88rem;
    font-style: italic;
    color: #b0a898;
    line-height: 1.4;
  }

  /* Argument transcript area */
  .argument-area {
    background: #fdfcf9;
    border: 1px solid var(--border);
    border-radius: 2px;
    padding: 24px;
    min-height: 180px;
    margin-bottom: 20px;
    position: relative;
  }
  .argument-transcript {
    font-size: 1rem;
    line-height: 1.8;
    color: var(--ink);
    min-height: 120px;
    white-space: pre-wrap;
  }
  .transcript-placeholder {
    color: var(--muted);
    font-style: italic;
  }
  .mic-status {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-top: 12px;
    font-family: 'DM Mono', monospace;
    font-size: 0.8rem;
    color: var(--muted);
  }
  .mic-dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: var(--border);
    transition: background 0.3s;
  }
  .mic-dot.recording { background: var(--red-light); animation: pulse 1s infinite; }
  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.3; }
  }

  /* Question interrupt overlay */
  .question-overlay {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(14,14,14,0.85);
    z-index: 100;
    align-items: center;
    justify-content: center;
    padding: 24px;
  }
  .question-overlay.show { display: flex; }
  .question-modal {
    background: var(--paper);
    border: 2px solid var(--gold);
    border-radius: 2px;
    padding: 40px;
    max-width: 640px;
    width: 100%;
  }
  .question-modal .judge-label {
    font-family: 'DM Mono', monospace;
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--gold);
    margin-bottom: 12px;
  }
  .question-modal .question-text {
    font-size: 1.4rem;
    font-weight: 400;
    line-height: 1.5;
    margin-bottom: 28px;
    font-style: italic;
  }
  .question-modal .answer-area {
    background: #fdfcf9;
    border: 1px solid var(--border);
    padding: 20px;
    min-height: 100px;
    font-size: 1rem;
    line-height: 1.7;
    margin-bottom: 20px;
    white-space: pre-wrap;
    font-style: italic;
    color: var(--muted);
  }
  .modal-btn-row {
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
  }

  /* Critique display */
  .critique-box {
    margin-top: 20px;
    padding: 20px 24px;
    border-left: 3px solid var(--gold);
    background: rgba(184,150,62,0.05);
  }
  .critique-box .score-badge {
    display: inline-block;
    font-family: 'DM Mono', monospace;
    font-size: 0.8rem;
    padding: 3px 10px;
    border-radius: 1px;
    margin-bottom: 10px;
    font-weight: 500;
  }
  .score-high { background: #d4edda; color: #2d6a4f; }
  .score-mid  { background: #fff3cd; color: #856404; }
  .score-low  { background: #f8d7da; color: #8b1a1a; }
  .critique-text { font-size: 1rem; line-height: 1.7; margin-bottom: 10px; }
  .stronger-answer {
    background: var(--ink);
    color: var(--paper);
    padding: 16px 20px;
    font-size: 0.95rem;
    line-height: 1.7;
    font-style: italic;
    margin-top: 12px;
  }
  .stronger-answer strong {
    display: block;
    font-family: 'DM Mono', monospace;
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--gold-light);
    margin-bottom: 6px;
    font-style: normal;
  }

  /* Progress */
  .progress-bar-outer {
    background: var(--border);
    height: 4px;
    border-radius: 2px;
    margin-bottom: 24px;
    overflow: hidden;
  }
  .progress-bar-inner {
    height: 100%;
    background: var(--gold);
    transition: width 0.5s;
  }
  .progress-label {
    font-family: 'DM Mono', monospace;
    font-size: 0.78rem;
    color: var(--muted);
    margin-bottom: 6px;
  }

  /* ── Hot Bench / Cite Check results ── */
  .question-list { list-style: none; }
  .question-item {
    border: 1px solid var(--border);
    border-radius: 2px;
    margin-bottom: 16px;
    overflow: hidden;
  }
  .question-item-header {
    display: flex;
    align-items: flex-start;
    gap: 16px;
    padding: 20px 24px;
    background: var(--card-bg);
    cursor: pointer;
  }
  .question-item-header:hover { background: var(--cream); }
  .q-rank {
    font-family: 'DM Mono', monospace;
    font-size: 1.4rem;
    font-weight: 300;
    color: var(--gold);
    min-width: 36px;
    line-height: 1;
    margin-top: 2px;
  }
  .q-content { flex: 1; }
  .q-category {
    font-family: 'DM Mono', monospace;
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--muted);
    margin-bottom: 6px;
  }
  .q-text {
    font-size: 1.1rem;
    font-style: italic;
    line-height: 1.5;
    margin-bottom: 4px;
  }
  .q-danger {
    font-size: 0.88rem;
    color: var(--red);
  }
  .question-item-body {
    display: none;
    padding: 20px 24px;
    border-top: 1px solid var(--border);
    background: #fdfcf9;
  }
  .question-item-body.open { display: block; }
  .recommended-answer-label {
    font-family: 'DM Mono', monospace;
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--gold);
    margin-bottom: 8px;
  }
  .recommended-answer-text {
    font-size: 1rem;
    line-height: 1.7;
    font-style: italic;
  }

  /* Cite check results */
  .cite-group { margin-bottom: 24px; }
  .cite-group h3 {
    font-size: 1rem;
    font-family: 'DM Mono', monospace;
    text-transform: uppercase;
    letter-spacing: 0.07em;
    margin-bottom: 12px;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .cite-badge {
    font-size: 0.72rem;
    padding: 2px 8px;
    border-radius: 1px;
    font-family: 'DM Mono', monospace;
  }
  .badge-ok { background: #d4edda; color: #2d6a4f; }
  .badge-warn { background: #fff3cd; color: #856404; }
  .badge-err { background: #f8d7da; color: #8b1a1a; }
  .cite-item {
    display: flex;
    gap: 12px;
    padding: 10px 16px;
    border: 1px solid var(--border);
    margin-bottom: 8px;
    background: var(--card-bg);
    font-size: 0.95rem;
    border-radius: 1px;
  }
  .cite-ref {
    font-family: 'DM Mono', monospace;
    font-size: 0.85rem;
    color: var(--ink);
    min-width: 100px;
    font-weight: 500;
  }
  .cite-note { color: var(--muted); font-style: italic; flex: 1; }

  /* Rebuttal report */
  .rebuttal-item {
    border: 1px solid var(--border);
    padding: 20px 24px;
    margin-bottom: 16px;
    background: var(--card-bg);
  }
  .rebuttal-trigger {
    font-size: 0.85rem;
    color: var(--muted);
    font-style: italic;
    margin-bottom: 8px;
  }
  .rebuttal-strategy {
    font-size: 1rem;
    line-height: 1.7;
    margin-bottom: 12px;
  }
  .rebuttal-key-phrase {
    background: var(--ink);
    color: var(--gold-light);
    padding: 12px 16px;
    font-style: italic;
    font-size: 1rem;
    border-left: 3px solid var(--gold);
  }

  /* Loading */
  .loading-state {
    text-align: center;
    padding: 48px;
    color: var(--muted);
    font-style: italic;
  }
  .spinner {
    display: inline-block;
    width: 32px;
    height: 32px;
    border: 2px solid var(--border);
    border-top-color: var(--gold);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    margin-bottom: 16px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* Utility */
  .hidden { display: none !important; }
  .mt16 { margin-top: 16px; }
  .mt24 { margin-top: 24px; }
  .gap12 { display: flex; gap: 12px; flex-wrap: wrap; }
  .summary-banner {
    background: var(--cream);
    border: 1px solid var(--border);
    padding: 16px 24px;
    margin-bottom: 24px;
    font-size: 1rem;
    line-height: 1.6;
    font-style: italic;
    color: var(--ink);
  }
  .vulnerability-banner {
    background: rgba(139,26,26,0.06);
    border: 1px solid rgba(139,26,26,0.2);
    border-left: 3px solid var(--red);
    padding: 14px 20px;
    margin-bottom: 24px;
    font-size: 0.95rem;
    color: var(--red);
    line-height: 1.6;
  }

  @media (max-width: 600px) {
    main { padding: 24px 16px 60px; }
    .header-inner { padding: 32px 20px 28px; }
    .card { padding: 20px; }
    .question-modal { padding: 24px; }
  }
</style>
</head>
<body>

<header>
  <div class="header-bg-text">OA</div>
  <div class="header-inner">
    <div class="header-rule"></div>
    <h1>Oral Argument <em>Tools</em></h1>
    <p class="subtitle">MyAppealCoach.com &mdash; AI-Powered Preparation for the Bench</p>
  </div>
</header>

<div class="tab-nav">
  <button class="active" onclick="switchTab('moot')">Moot Court Simulator</button>
  <button onclick="switchTab('hotbench')">Hot Bench Prep</button>
  <button onclick="switchTab('citecheck')">Record Cite Checker</button>
  <button onclick="switchTab('rebuttal')">Rebuttal Coach</button>
</div>

<main>

  <!-- ══════════════════════════════════════════════════════
       MOOT COURT SIMULATOR
  ══════════════════════════════════════════════════════ -->
  <div id="panel-moot" class="tool-panel active">

    <!-- Setup -->
    <div id="moot-setup">
      <div class="card">
        <h2>Moot Court Simulator</h2>
        <p class="desc">Upload your brief. A panel of three intense judges will interrupt your argument at a frequency you control. Your spoken answers are transcribed, critiqued, and improved in real time.</p>

        <label>Upload Brief (PDF)</label>
        <div class="upload-zone" id="moot-upload-zone">
          <input type="file" id="moot-brief-file" accept=".pdf" onchange="onMootBriefSelected()">
          <div class="upload-label">Drop your brief here or <strong>click to browse</strong></div>
          <div class="file-name" id="moot-file-name"></div>
        </div>

        <label>Panel Frequency</label>
        <div class="slider-row">
          <span style="font-family:'DM Mono',monospace;font-size:0.8rem;color:var(--muted);">Cold</span>
          <input type="range" id="freq-slider" min="1" max="10" value="5" oninput="updateFreqLabel()">
          <span style="font-family:'DM Mono',monospace;font-size:0.8rem;color:var(--muted);">Hot</span>
          <span class="slider-value" id="freq-value">5</span>
        </div>
        <div style="font-family:'DM Mono',monospace;font-size:0.78rem;color:var(--muted);margin-bottom:24px;" id="freq-desc">Active bench — questions every few minutes</div>

        <button class="btn btn-gold" id="moot-start-btn" disabled onclick="startMootCourt()">Begin Argument</button>
        <p style="font-size:0.82rem;color:var(--muted);font-style:italic;margin-top:12px;">Requires Chrome or Edge. Microphone access will be requested.</p>
      </div>
    </div>

    <!-- Session -->
    <div id="moot-session" class="hidden">
      <div class="card">
        <div class="panel-display" id="panel-display"></div>

        <div class="progress-label" id="progress-label">Question 0 of 0</div>
        <div class="progress-bar-outer">
          <div class="progress-bar-inner" id="progress-bar" style="width:0%"></div>
        </div>

        <div class="argument-area">
          <div class="argument-transcript" id="argument-transcript">
            <span class="transcript-placeholder">Your spoken argument will appear here…</span>
          </div>
          <div class="mic-status">
            <div class="mic-dot" id="mic-dot"></div>
            <span id="mic-status-text">Microphone inactive</span>
          </div>
        </div>

        <div class="gap12">
          <button class="btn btn-gold" id="start-speaking-btn" onclick="startSpeaking()">▶ Begin Argument</button>
          <button class="btn btn-outline btn-sm" id="pause-speaking-btn" onclick="pauseSpeaking()" style="display:none">⏸ Pause</button>
          <button class="btn btn-outline btn-sm" onclick="endSessionEarly()">End Session & Get Rebuttal Report</button>
        </div>
      </div>
    </div>

    <!-- Rebuttal Report -->
    <div id="moot-rebuttal" class="hidden">
      <div class="card">
        <h2>Rebuttal Coaching Report</h2>
        <div id="rebuttal-loading" class="loading-state">
          <div class="spinner"></div><br>Analyzing your session…
        </div>
        <div id="rebuttal-content" class="hidden">
          <div class="summary-banner" id="rebuttal-summary"></div>
          <div id="rebuttal-items"></div>
          <h3 style="font-size:1.1rem;margin-bottom:12px;font-weight:600;">Overall Recommendations</h3>
          <ul id="rebuttal-recs" style="padding-left:20px;line-height:2;font-size:1rem;"></ul>
          <div class="mt24">
            <button class="btn btn-outline" onclick="resetMoot()">New Argument Session</button>
          </div>
        </div>
      </div>
    </div>

  </div><!-- /moot panel -->


  <!-- ══════════════════════════════════════════════════════
       HOT BENCH PREP
  ══════════════════════════════════════════════════════ -->
  <div id="panel-hotbench" class="tool-panel">
    <div class="card" id="hb-setup">
      <h2>Hot Bench Prep</h2>
      <p class="desc">Upload your brief. Claude identifies the most dangerous questions this panel could ask — with recommended answers for each.</p>

      <label>Upload Brief (PDF)</label>
      <div class="upload-zone" id="hb-upload-zone">
        <input type="file" id="hb-brief-file" accept=".pdf" onchange="onHBBriefSelected()">
        <div class="upload-label">Drop your brief here or <strong>click to browse</strong></div>
        <div class="file-name" id="hb-file-name"></div>
      </div>

      <button class="btn" id="hb-start-btn" disabled onclick="startHotBench()">Identify Dangerous Questions</button>
    </div>

    <div id="hb-loading" class="loading-state hidden">
      <div class="spinner"></div><br>Analyzing brief for vulnerabilities…
    </div>

    <div id="hb-results" class="hidden">
      <div class="summary-banner" id="hb-summary"></div>
      <div class="vulnerability-banner" id="hb-vulnerability"></div>
      <ul class="question-list" id="hb-question-list"></ul>
      <div class="mt24">
        <button class="btn btn-outline" onclick="resetHotBench()">Analyze Another Brief</button>
      </div>
    </div>
  </div>


  <!-- ══════════════════════════════════════════════════════
       RECORD CITE CHECKER
  ══════════════════════════════════════════════════════ -->
  <div id="panel-citecheck" class="tool-panel">
    <div class="card" id="cc-setup">
      <h2>Record Cite Checker</h2>
      <p class="desc">Upload your brief and the appellate record. Every record citation in the brief is extracted and verified against the actual record.</p>

      <label>Upload Brief (PDF)</label>
      <div class="upload-zone" id="cc-brief-zone">
        <input type="file" id="cc-brief-file" accept=".pdf" onchange="onCCFileSelected('brief')">
        <div class="upload-label">Drop brief here or <strong>click to browse</strong></div>
        <div class="file-name" id="cc-brief-name"></div>
      </div>

      <label>Upload Record (PDF)</label>
      <div class="upload-zone" id="cc-record-zone">
        <input type="file" id="cc-record-file" accept=".pdf" onchange="onCCFileSelected('record')">
        <div class="upload-label">Drop record here or <strong>click to browse</strong></div>
        <div class="file-name" id="cc-record-name"></div>
      </div>

      <button class="btn" id="cc-start-btn" disabled onclick="startCiteCheck()">Verify Citations</button>
    </div>

    <div id="cc-loading" class="loading-state hidden">
      <div class="spinner"></div><br>Extracting and verifying citations…
    </div>

    <div id="cc-results" class="hidden">
      <div class="summary-banner" id="cc-summary"></div>
      <div id="cc-verified" class="cite-group"></div>
      <div id="cc-uncertain" class="cite-group"></div>
      <div id="cc-notfound" class="cite-group"></div>
      <div class="mt24">
        <button class="btn btn-outline" onclick="resetCiteCheck()">Check Another Brief</button>
      </div>
    </div>
  </div>


  <!-- ══════════════════════════════════════════════════════
       REBUTTAL COACH (standalone note)
  ══════════════════════════════════════════════════════ -->
  <div id="panel-rebuttal" class="tool-panel">
    <div class="card">
      <h2>Rebuttal Coach</h2>
      <p class="desc">The Rebuttal Coach is integrated with the Moot Court Simulator. After completing a moot court session, Claude automatically identifies your weakest answers and generates a targeted rebuttal strategy for each one.</p>
      <p style="font-size:1rem;line-height:1.7;color:var(--muted);">To access your rebuttal coaching report, run a Moot Court Simulator session and click <em>"End Session &amp; Get Rebuttal Report"</em> at any time.</p>
      <div class="mt24">
        <button class="btn btn-gold" onclick="switchTab('moot')">Go to Moot Court Simulator</button>
      </div>
    </div>
  </div>

</main>

<!-- Question interrupt overlay -->
<div class="question-overlay" id="question-overlay">
  <div class="question-modal">
    <div class="judge-label" id="overlay-judge-label">Judge Question</div>
    <div class="question-text" id="overlay-question-text"></div>
    <div style="font-family:'DM Mono',monospace;font-size:0.75rem;color:var(--muted);margin-bottom:8px;text-transform:uppercase;letter-spacing:0.07em;">Your Answer (transcribed)</div>
    <div class="answer-area" id="overlay-answer-area">Click "Answer" and speak your response…</div>
    <div id="overlay-critique" class="hidden"></div>
    <div class="modal-btn-row" id="overlay-btn-row">
      <button class="btn btn-gold" id="overlay-answer-btn" onclick="startAnswering()">🎙 Answer</button>
      <button class="btn btn-outline btn-sm" id="overlay-done-btn" onclick="submitAnswer()" style="display:none">Submit Answer</button>
    </div>
  </div>
</div>

<script>
// ─────────────────────────────────────────────────────
// TAB NAVIGATION
// ─────────────────────────────────────────────────────
function switchTab(id) {
  document.querySelectorAll('.tool-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-nav button').forEach(b => b.classList.remove('active'));
  document.getElementById('panel-' + id).classList.add('active');
  const tabs = { moot: 0, hotbench: 1, citecheck: 2, rebuttal: 3 };
  document.querySelectorAll('.tab-nav button')[tabs[id]].classList.add('active');
}

// ─────────────────────────────────────────────────────
// MOOT COURT SIMULATOR
// ─────────────────────────────────────────────────────
let mootState = {
  questions: [],
  panel: [],
  brief_excerpt: '',
  current_q: 0,
  session_log: [],
  frequency: 5,
  recognition: null,
  isRecording: false,
  fullTranscript: '',
  interruptTimer: null,
  answerRecognition: null,
  answerTranscript: '',
  speakingStartTime: null
};

function updateFreqLabel() {
  const v = document.getElementById('freq-slider').value;
  document.getElementById('freq-value').textContent = v;
  const descs = {
    1: 'Cold Panel — no interruptions during argument',
    2: 'Quiet bench — very rare questions',
    3: 'Quiet bench — occasional questions',
    4: 'Moderate bench — questions every few minutes',
    5: 'Active bench — regular interruptions',
    6: 'Active bench — frequent questions',
    7: 'Hot bench — questions every minute or so',
    8: 'Hot bench — questions every 30–45 seconds',
    9: 'Very hot bench — questions every 15–30 seconds',
    10: 'Scorching bench — question every ~15 seconds'
  };
  document.getElementById('freq-desc').textContent = descs[v] || '';
}

function onMootBriefSelected() {
  const f = document.getElementById('moot-brief-file').files[0];
  if (f) {
    document.getElementById('moot-file-name').textContent = '✓ ' + f.name;
    document.getElementById('moot-start-btn').disabled = false;
  }
}

async function startMootCourt() {
  const file = document.getElementById('moot-brief-file').files[0];
  const freq = parseInt(document.getElementById('freq-slider').value);
  mootState.frequency = freq;

  document.getElementById('moot-setup').innerHTML =
    '<div class="loading-state"><div class="spinner"></div><br>Convening your panel and preparing questions…</div>';

  const fd = new FormData();
  fd.append('brief', file);
  fd.append('frequency', freq);

  try {
    const res = await fetch('/api/oral/moot/init', { method: 'POST', body: fd });
    const data = await res.json();
    if (data.error) { alert('Error: ' + data.error); resetMoot(); return; }

    mootState.questions = data.questions || [];
    mootState.panel = data.panel || [];
    mootState.brief_excerpt = data.brief_excerpt || '';
    mootState.current_q = 0;
    mootState.session_log = [];

    // Hide setup, show session
    document.getElementById('moot-setup').classList.add('hidden');
    document.getElementById('moot-session').classList.remove('hidden');

    // Render panel
    const pd = document.getElementById('panel-display');
    pd.innerHTML = mootState.panel.map(j => `
      <div class="judge-card">
        <div class="judge-name">${j.name}</div>
        <div class="judge-focus">${j.focus}</div>
      </div>`).join('');

    updateProgress();
  } catch(e) {
    alert('Failed to initialize session: ' + e.message);
    resetMoot();
  }
}

function updateProgress() {
  const total = mootState.questions.length;
  const current = mootState.current_q;
  document.getElementById('progress-label').textContent = `Question ${current} of ${total}`;
  document.getElementById('progress-bar').style.width = total ? (current / total * 100) + '%' : '0%';
}

function startSpeaking() {
  if (!('webkitSpeechRecognition' in window) && !('SpeechRecognition' in window)) {
    alert('Speech recognition is not supported in this browser. Please use Chrome or Edge.');
    return;
  }

  document.getElementById('start-speaking-btn').style.display = 'none';
  document.getElementById('pause-speaking-btn').style.display = 'inline-block';

  mootState.speakingStartTime = Date.now();
  mootState.fullTranscript = '';
  startArgumentRecognition();
  scheduleNextQuestion();
}

function startArgumentRecognition() {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const recog = new SpeechRecognition();
  recog.continuous = true;
  recog.interimResults = true;
  recog.lang = 'en-US';

  let finalText = mootState.fullTranscript;

  recog.onresult = (e) => {
    let interim = '';
    for (let i = e.resultIndex; i < e.results.length; i++) {
      if (e.results[i].isFinal) {
        finalText += e.results[i][0].transcript + ' ';
      } else {
        interim = e.results[i][0].transcript;
      }
    }
    mootState.fullTranscript = finalText;
    const el = document.getElementById('argument-transcript');
    el.innerHTML = (finalText + '<em style="color:var(--muted)">' + interim + '</em>') ||
      '<span class="transcript-placeholder">Listening…</span>';
  };

  recog.onstart = () => {
    mootState.isRecording = true;
    document.getElementById('mic-dot').classList.add('recording');
    document.getElementById('mic-status-text').textContent = 'Recording argument…';
  };

  recog.onend = () => {
    if (mootState.isRecording) {
      // Auto-restart if still in session
      setTimeout(() => { if (mootState.isRecording) recog.start(); }, 200);
    }
  };

  recog.onerror = (e) => {
    if (e.error !== 'no-speech') console.error('Recognition error:', e.error);
  };

  mootState.recognition = recog;
  recog.start();
}

function pauseSpeaking() {
  mootState.isRecording = false;
  if (mootState.recognition) mootState.recognition.stop();
  if (mootState.interruptTimer) clearTimeout(mootState.interruptTimer);
  document.getElementById('mic-dot').classList.remove('recording');
  document.getElementById('mic-status-text').textContent = 'Paused';
  document.getElementById('pause-speaking-btn').style.display = 'none';
  document.getElementById('start-speaking-btn').style.display = 'inline-block';
  document.getElementById('start-speaking-btn').textContent = '▶ Resume Argument';
}

function scheduleNextQuestion() {
  if (mootState.frequency === 1) return; // Cold panel
  if (mootState.current_q >= mootState.questions.length) return;

  // Interval in ms based on frequency (1=never, 10=15s)
  const minSec = Math.max(15, 150 - (mootState.frequency - 1) * 15);
  const maxSec = minSec + 30;
  const delay = (minSec + Math.random() * (maxSec - minSec)) * 1000;

  mootState.interruptTimer = setTimeout(() => {
    if (mootState.isRecording && mootState.current_q < mootState.questions.length) {
      interruptWithQuestion();
    }
  }, delay);
}

function interruptWithQuestion() {
  // Pause recording
  mootState.isRecording = false;
  if (mootState.recognition) mootState.recognition.stop();
  if (mootState.interruptTimer) clearTimeout(mootState.interruptTimer);

  document.getElementById('mic-dot').classList.remove('recording');
  document.getElementById('mic-status-text').textContent = 'Question — microphone paused';

  const q = mootState.questions[mootState.current_q];
  document.getElementById('overlay-judge-label').textContent = q.judge + ' — Question ' + (mootState.current_q + 1);
  document.getElementById('overlay-question-text').textContent = q.question;
  document.getElementById('overlay-answer-area').textContent = 'Click "Answer" and speak your response…';
  document.getElementById('overlay-critique').classList.add('hidden');
  document.getElementById('overlay-critique').innerHTML = '';
  document.getElementById('overlay-answer-btn').style.display = 'inline-block';
  document.getElementById('overlay-done-btn').style.display = 'none';
  mootState.answerTranscript = '';

  // Text-to-speech
  if ('speechSynthesis' in window) {
    const utt = new SpeechSynthesisUtterance(q.judge + ' asks: ' + q.question);
    utt.rate = 0.95;
    utt.pitch = 1;
    window.speechSynthesis.speak(utt);
  }

  document.getElementById('question-overlay').classList.add('show');
}

function startAnswering() {
  if (!('webkitSpeechRecognition' in window) && !('SpeechRecognition' in window)) {
    alert('Speech recognition requires Chrome or Edge.');
    return;
  }

  document.getElementById('overlay-answer-btn').style.display = 'none';
  document.getElementById('overlay-done-btn').style.display = 'inline-block';
  document.getElementById('overlay-answer-area').textContent = 'Listening…';

  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const recog = new SpeechRecognition();
  recog.continuous = true;
  recog.interimResults = true;
  recog.lang = 'en-US';

  let finalText = '';

  recog.onresult = (e) => {
    let interim = '';
    for (let i = e.resultIndex; i < e.results.length; i++) {
      if (e.results[i].isFinal) finalText += e.results[i][0].transcript + ' ';
      else interim = e.results[i][0].transcript;
    }
    mootState.answerTranscript = finalText;
    document.getElementById('overlay-answer-area').textContent = finalText + interim;
  };

  recog.onerror = (e) => { console.error('Answer recognition error:', e.error); };
  mootState.answerRecognition = recog;
  recog.start();
}

async function submitAnswer() {
  if (mootState.answerRecognition) { mootState.answerRecognition.stop(); mootState.answerRecognition = null; }

  const answer = mootState.answerTranscript.trim() || '[No answer recorded]';
  const q = mootState.questions[mootState.current_q];

  document.getElementById('overlay-done-btn').disabled = true;
  document.getElementById('overlay-done-btn').textContent = 'Analyzing…';

  try {
    const res = await fetch('/api/oral/moot/critique', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        question: q.question,
        answer: answer,
        brief_excerpt: mootState.brief_excerpt,
        judge: q.judge
      })
    });
    const critique = await res.json();

    // Log the session entry
    mootState.session_log.push({
      judge: q.judge,
      question: q.question,
      answer: answer,
      score: critique.score,
      fatal_flaws: critique.fatal_flaws || [],
      critique: critique.critique,
      stronger_answer: critique.stronger_answer
    });

    // Render critique
    const scoreClass = critique.score >= 7 ? 'score-high' : critique.score >= 4 ? 'score-mid' : 'score-low';
    const critiqueHTML = `
      <div class="critique-box">
        <span class="score-badge ${scoreClass}">Score: ${critique.score}/10</span>
        <div class="critique-text">${critique.critique || ''}</div>
        ${(critique.fatal_flaws||[]).length ? '<div style="font-size:0.85rem;color:var(--red);margin-bottom:10px;">⚠ ' + critique.fatal_flaws.join(' &nbsp;|&nbsp; ') + '</div>' : ''}
        <div class="stronger-answer">
          <strong>Stronger Answer</strong>
          ${critique.stronger_answer || ''}
        </div>
      </div>`;

    document.getElementById('overlay-critique').innerHTML = critiqueHTML;
    document.getElementById('overlay-critique').classList.remove('hidden');

    // Speak the stronger answer
    if ('speechSynthesis' in window && critique.stronger_answer) {
      const utt = new SpeechSynthesisUtterance('A stronger answer would be: ' + critique.stronger_answer);
      utt.rate = 0.9;
      window.speechSynthesis.speak(utt);
    }

    mootState.current_q++;
    updateProgress();

    document.getElementById('overlay-done-btn').style.display = 'none';
    document.getElementById('overlay-done-btn').disabled = false;
    document.getElementById('overlay-done-btn').textContent = 'Submit Answer';

    // Add continue button
    const row = document.getElementById('overlay-btn-row');
    const continueBtn = document.createElement('button');
    continueBtn.className = 'btn btn-gold';
    continueBtn.textContent = mootState.current_q < mootState.questions.length ? 'Continue Argument' : 'End Session';
    continueBtn.onclick = continueArgument;
    row.appendChild(continueBtn);

  } catch(e) {
    alert('Critique failed: ' + e.message);
  }
}

function continueArgument() {
  window.speechSynthesis.cancel();
  document.getElementById('question-overlay').classList.remove('show');

  if (mootState.current_q >= mootState.questions.length) {
    endSession();
    return;
  }

  // Resume recording
  mootState.isRecording = true;
  startArgumentRecognition();
  scheduleNextQuestion();
  document.getElementById('mic-status-text').textContent = 'Recording argument…';
}

function endSessionEarly() {
  mootState.isRecording = false;
  if (mootState.recognition) mootState.recognition.stop();
  if (mootState.interruptTimer) clearTimeout(mootState.interruptTimer);
  window.speechSynthesis.cancel();
  document.getElementById('question-overlay').classList.remove('show');
  endSession();
}

async function endSession() {
  document.getElementById('moot-session').classList.add('hidden');
  document.getElementById('moot-rebuttal').classList.remove('hidden');
  document.getElementById('rebuttal-loading').classList.remove('hidden');
  document.getElementById('rebuttal-content').classList.add('hidden');

  if (mootState.session_log.length === 0) {
    document.getElementById('rebuttal-loading').classList.add('hidden');
    document.getElementById('rebuttal-content').classList.remove('hidden');
    document.getElementById('rebuttal-summary').textContent = 'No questions were answered during this session.';
    return;
  }

  try {
    const res = await fetch('/api/oral/moot/rebuttal', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_log: mootState.session_log,
        brief_excerpt: mootState.brief_excerpt
      })
    });
    const data = await res.json();
    renderRebuttonReport(data);
  } catch(e) {
    document.getElementById('rebuttal-loading').innerHTML = 'Failed to generate report: ' + e.message;
  }
}

function renderRebuttonReport(data) {
  document.getElementById('rebuttal-loading').classList.add('hidden');
  document.getElementById('rebuttal-content').classList.remove('hidden');
  document.getElementById('rebuttal-summary').textContent = data.summary || '';

  const items = data.rebuttal_points || [];
  document.getElementById('rebuttal-items').innerHTML = items.map(item => `
    <div class="rebuttal-item">
      <div class="rebuttal-trigger">📌 ${item.trigger}</div>
      <div class="rebuttal-strategy">${item.rebuttal_strategy}</div>
      <div class="rebuttal-key-phrase">"${item.key_phrase}"</div>
    </div>`).join('');

  const recs = data.overall_recommendations || [];
  document.getElementById('rebuttal-recs').innerHTML = recs.map(r => `<li>${r}</li>`).join('');
}

function resetMoot() {
  mootState = { questions:[], panel:[], brief_excerpt:'', current_q:0, session_log:[],
    frequency:5, recognition:null, isRecording:false, fullTranscript:'',
    interruptTimer:null, answerRecognition:null, answerTranscript:'', speakingStartTime:null };
  window.location.reload();
}

// ─────────────────────────────────────────────────────
// HOT BENCH PREP
// ─────────────────────────────────────────────────────
function onHBBriefSelected() {
  const f = document.getElementById('hb-brief-file').files[0];
  if (f) {
    document.getElementById('hb-file-name').textContent = '✓ ' + f.name;
    document.getElementById('hb-start-btn').disabled = false;
  }
}

async function startHotBench() {
  const file = document.getElementById('hb-brief-file').files[0];
  document.getElementById('hb-setup').classList.add('hidden');
  document.getElementById('hb-loading').classList.remove('hidden');

  const fd = new FormData();
  fd.append('brief', file);

  try {
    const res = await fetch('/api/oral/hot-bench', { method: 'POST', body: fd });
    const data = await res.json();
    if (data.error) { alert(data.error); resetHotBench(); return; }

    document.getElementById('hb-loading').classList.add('hidden');
    document.getElementById('hb-results').classList.remove('hidden');
    document.getElementById('hb-summary').textContent = data.case_summary || '';
    document.getElementById('hb-vulnerability').innerHTML = '⚠ Primary Vulnerability: ' + (data.primary_vulnerability || '');

    const qs = data.dangerous_questions || [];
    document.getElementById('hb-question-list').innerHTML = qs.map((q,i) => `
      <li class="question-item">
        <div class="question-item-header" onclick="toggleQ(this)">
          <div class="q-rank">${String(q.rank || i+1).padStart(2,'0')}</div>
          <div class="q-content">
            <div class="q-category">${q.category || ''}</div>
            <div class="q-text">${q.question}</div>
            <div class="q-danger">${q.why_dangerous || ''}</div>
          </div>
          <div style="color:var(--muted);font-size:1.2rem;align-self:center;">▾</div>
        </div>
        <div class="question-item-body">
          <div class="recommended-answer-label">Recommended Answer</div>
          <div class="recommended-answer-text">${q.recommended_answer || ''}</div>
        </div>
      </li>`).join('');
  } catch(e) {
    alert('Error: ' + e.message);
    resetHotBench();
  }
}

function toggleQ(header) {
  const body = header.nextElementSibling;
  body.classList.toggle('open');
  header.querySelector('div:last-child').textContent = body.classList.contains('open') ? '▴' : '▾';
}

function resetHotBench() {
  document.getElementById('hb-setup').classList.remove('hidden');
  document.getElementById('hb-loading').classList.add('hidden');
  document.getElementById('hb-results').classList.add('hidden');
  document.getElementById('hb-brief-file').value = '';
  document.getElementById('hb-file-name').textContent = '';
  document.getElementById('hb-start-btn').disabled = true;
}

// ─────────────────────────────────────────────────────
// RECORD CITE CHECKER
// ─────────────────────────────────────────────────────
let ccFiles = { brief: false, record: false };

function onCCFileSelected(type) {
  const f = document.getElementById('cc-' + type + '-file').files[0];
  if (f) {
    document.getElementById('cc-' + type + '-name').textContent = '✓ ' + f.name;
    ccFiles[type] = true;
    if (ccFiles.brief && ccFiles.record) document.getElementById('cc-start-btn').disabled = false;
  }
}

async function startCiteCheck() {
  document.getElementById('cc-setup').classList.add('hidden');
  document.getElementById('cc-loading').classList.remove('hidden');

  const fd = new FormData();
  fd.append('brief', document.getElementById('cc-brief-file').files[0]);
  fd.append('record', document.getElementById('cc-record-file').files[0]);

  try {
    const res = await fetch('/api/oral/cite-check', { method: 'POST', body: fd });
    const data = await res.json();
    if (data.error) { alert(data.error); resetCiteCheck(); return; }

    document.getElementById('cc-loading').classList.add('hidden');
    document.getElementById('cc-results').classList.remove('hidden');
    document.getElementById('cc-summary').textContent = data.summary || '';

    const renderGroup = (containerId, items, badgeClass, badgeLabel, icon) => {
      const el = document.getElementById(containerId);
      if (!items || !items.length) { el.innerHTML = ''; return; }
      el.innerHTML = `<h3>${icon} ${badgeLabel} <span class="cite-badge ${badgeClass}">${items.length}</span></h3>` +
        items.map(c => `
          <div class="cite-item">
            <div class="cite-ref">${c.citation}</div>
            <div class="cite-note">${c.note || ''}</div>
          </div>`).join('');
    };

    renderGroup('cc-verified',  data.verified,  'badge-ok',   'Verified',   '✓');
    renderGroup('cc-uncertain', data.uncertain, 'badge-warn', 'Uncertain',  '?');
    renderGroup('cc-notfound',  data.not_found, 'badge-err',  'Not Found',  '✗');
  } catch(e) {
    alert('Error: ' + e.message);
    resetCiteCheck();
  }
}

function resetCiteCheck() {
  document.getElementById('cc-setup').classList.remove('hidden');
  document.getElementById('cc-loading').classList.add('hidden');
  document.getElementById('cc-results').classList.add('hidden');
  document.getElementById('cc-brief-file').value = '';
  document.getElementById('cc-record-file').value = '';
  document.getElementById('cc-brief-name').textContent = '';
  document.getElementById('cc-record-name').textContent = '';
  ccFiles = { brief: false, record: false };
  document.getElementById('cc-start-btn').disabled = true;
}

// ─────────────────────────────────────────────────────
// DRAG & DROP ZONES
// ─────────────────────────────────────────────────────
document.querySelectorAll('.upload-zone').forEach(zone => {
  zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('dragover'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));
  zone.addEventListener('drop', e => {
    e.preventDefault();
    zone.classList.remove('dragover');
    const input = zone.querySelector('input[type=file]');
    if (input && e.dataTransfer.files.length) {
      input.files = e.dataTransfer.files;
      input.dispatchEvent(new Event('change'));
    }
  });
});

// Init freq label
updateFreqLabel();
</script>
</body>
</html>
"""
