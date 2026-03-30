import os
import json
import math
import base64
import traceback
import anthropic
import fitz  # PyMuPDF
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
 
app = Flask(__name__)
CORS(app)
 
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MAX_PAGES_PER_CHUNK = 80
MAX_FILE_BYTES = 900 * 1024 * 1024
app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_BYTES
 
# HTML is embedded directly to avoid file-path issues on Render
INDEX_HTML = '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="UTF-8">\n<meta name="viewport" content="width=device-width, initial-scale=1.0">\n<title>Appeal Record Summary</title>\n<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,400;0,600;0,700;1,400&family=Crimson+Pro:ital,wght@0,300;0,400;0,500;1,300;1,400&family=DM+Mono:wght@300;400&display=swap" rel="stylesheet">\n<style>\n  :root {\n    --navy: #0d1b2a; --navy-mid: #162032;\n    --gold: #c9a84c; --gold-light: #e2c47a; --cream: #faf6ef;\n    --text-light: #7a6a50; --border-gold: rgba(201,168,76,0.3);\n  }\n  * { box-sizing: border-box; margin: 0; padding: 0; }\n  body { font-family: \'Crimson Pro\', Georgia, serif; background: var(--navy); color: var(--cream); min-height: 100vh; }\n  body::before { content: \'\'; position: fixed; inset: 0; background-image: radial-gradient(ellipse at 20% 0%, rgba(201,168,76,0.08) 0%, transparent 50%), radial-gradient(ellipse at 80% 100%, rgba(201,168,76,0.06) 0%, transparent 50%); pointer-events: none; z-index: 0; }\n  .wrapper { position: relative; z-index: 1; max-width: 1100px; margin: 0 auto; padding: 0 24px 80px; }\n\n  header { text-align: center; padding: 56px 0 40px; border-bottom: 1px solid var(--border-gold); margin-bottom: 48px; }\n  .seal { width: 64px; height: 64px; margin: 0 auto 20px; border: 2px solid var(--gold); border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 28px; color: var(--gold); position: relative; }\n  .seal::before { content: \'\'; position: absolute; inset: 4px; border: 1px solid var(--border-gold); border-radius: 50%; }\n  h1 { font-family: \'Playfair Display\', serif; font-size: clamp(2rem,4vw,3rem); font-weight: 700; color: var(--gold-light); letter-spacing: 0.02em; line-height: 1.1; }\n  .subtitle { margin-top: 10px; font-size: 1.05rem; color: rgba(201,168,76,0.6); font-style: italic; letter-spacing: 0.08em; }\n\n  .upload-section { background: var(--navy-mid); border: 1px solid var(--border-gold); border-radius: 4px; padding: 48px 40px; text-align: center; margin-bottom: 32px; }\n  .pick-area { border: 2px dashed rgba(201,168,76,0.4); border-radius: 4px; padding: 52px 32px; background: rgba(201,168,76,0.03); transition: all 0.25s; }\n  .pick-area.has-file { border-color: var(--gold); background: rgba(201,168,76,0.07); }\n  .drop-icon { font-size: 2.8rem; margin-bottom: 16px; display: block; opacity: 0.7; }\n  .pick-area h3 { font-family: \'Playfair Display\', serif; font-size: 1.3rem; color: var(--gold-light); margin-bottom: 8px; }\n  .pick-area p { color: rgba(250,246,239,0.5); font-size: 0.95rem; margin-bottom: 20px; }\n  #fileInput { display: none; }\n  .btn-browse { display: inline-block; border: 1px solid var(--gold); color: var(--gold); background: transparent; padding: 10px 28px; font-family: \'Crimson Pro\', serif; font-size: 1rem; border-radius: 3px; cursor: pointer; transition: all 0.2s; }\n  .btn-browse:hover { background: rgba(201,168,76,0.12); }\n  .file-info { margin-top: 18px; display: none; align-items: center; justify-content: center; gap: 10px; font-size: 0.95rem; }\n  .file-info.visible { display: flex; }\n  .fname { color: var(--gold-light); font-weight: 500; word-break: break-all; }\n  .fsize { color: var(--text-light); font-size: 0.85rem; flex-shrink: 0; }\n  .btn-clear { background: none; border: none; color: var(--text-light); cursor: pointer; font-size: 1.1rem; padding: 0 4px; flex-shrink: 0; }\n  .btn-clear:hover { color: #f4a0a0; }\n  .btn-analyze { margin-top: 28px; background: linear-gradient(135deg, var(--gold) 0%, #a07c30 100%); color: var(--navy); border: none; padding: 16px 52px; font-family: \'Playfair Display\', serif; font-size: 1.05rem; font-weight: 600; letter-spacing: 0.05em; border-radius: 3px; cursor: pointer; transition: all 0.2s; box-shadow: 0 4px 20px rgba(201,168,76,0.3); display: block; margin-left: auto; margin-right: auto; }\n  .btn-analyze:hover:not(:disabled) { transform: translateY(-2px); box-shadow: 0 8px 28px rgba(201,168,76,0.4); }\n  .btn-analyze:disabled { opacity: 0.45; cursor: not-allowed; }\n\n  .progress-section { display: none; background: var(--navy-mid); border: 1px solid var(--border-gold); border-radius: 4px; padding: 40px; text-align: center; margin-bottom: 32px; }\n  .progress-section.visible { display: block; }\n  .spinner { width: 52px; height: 52px; border: 3px solid rgba(201,168,76,0.2); border-top-color: var(--gold); border-radius: 50%; animation: spin 0.8s linear infinite; margin: 0 auto 24px; }\n  @keyframes spin { to { transform: rotate(360deg); } }\n  .progress-label { font-family: \'Playfair Display\', serif; font-size: 1.15rem; color: var(--gold-light); margin-bottom: 8px; }\n  .progress-sub { font-size: 0.9rem; color: rgba(250,246,239,0.45); font-style: italic; }\n  .progress-bar-wrap { margin-top: 20px; background: rgba(201,168,76,0.1); border: 1px solid var(--border-gold); border-radius: 3px; height: 8px; overflow: hidden; }\n  .progress-bar-fill { height: 100%; background: linear-gradient(90deg, var(--gold), var(--gold-light)); width: 0%; transition: width 0.5s ease; border-radius: 3px; }\n\n  .error-box { display: none; background: rgba(139,32,32,0.15); border: 1px solid rgba(139,32,32,0.5); border-radius: 4px; padding: 20px 24px; margin-bottom: 32px; color: #f4a0a0; font-size: 0.95rem; line-height: 1.6; }\n  .error-box.visible { display: block; }\n\n  #results { display: none; }\n  #results.visible { display: block; }\n  .results-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 40px; padding-bottom: 20px; border-bottom: 1px solid var(--border-gold); flex-wrap: wrap; gap: 16px; }\n  .results-header h2 { font-family: \'Playfair Display\', serif; font-size: 1.6rem; color: var(--gold-light); }\n  .btn-reset { background: transparent; border: 1px solid var(--border-gold); color: var(--gold); padding: 10px 24px; font-family: \'Crimson Pro\', serif; font-size: 0.95rem; border-radius: 3px; cursor: pointer; transition: all 0.2s; }\n  .btn-reset:hover { background: rgba(201,168,76,0.1); border-color: var(--gold); }\n\n  .section-card { background: var(--navy-mid); border: 1px solid var(--border-gold); border-radius: 4px; margin-bottom: 24px; overflow: hidden; }\n  .section-header { background: linear-gradient(90deg, rgba(201,168,76,0.12) 0%, transparent 100%); padding: 20px 28px; display: flex; align-items: center; gap: 16px; cursor: pointer; border-bottom: 1px solid var(--border-gold); user-select: none; transition: background 0.2s; }\n  .section-header:hover { background: linear-gradient(90deg, rgba(201,168,76,0.18) 0%, transparent 100%); }\n  .section-num { width: 36px; height: 36px; border: 1px solid var(--gold); border-radius: 50%; display: flex; align-items: center; justify-content: center; font-family: \'DM Mono\', monospace; font-size: 0.8rem; color: var(--gold); flex-shrink: 0; }\n  .section-title { font-family: \'Playfair Display\', serif; font-size: 1.1rem; color: var(--gold-light); flex: 1; }\n  .section-toggle { color: var(--gold); font-size: 1.1rem; transition: transform 0.25s; }\n  .section-card.collapsed .section-toggle { transform: rotate(-90deg); }\n  .section-body { padding: 28px; font-size: 1rem; line-height: 1.75; color: rgba(250,246,239,0.85); }\n  .section-card.collapsed .section-body { max-height: 0; padding-top: 0; padding-bottom: 0; overflow: hidden; }\n  .section-body h3 { font-family: \'Playfair Display\', serif; font-size: 1.05rem; color: var(--gold); margin: 22px 0 8px; padding-bottom: 4px; border-bottom: 1px solid rgba(201,168,76,0.2); }\n  .section-body h3:first-child { margin-top: 0; }\n  .section-body h4 { font-family: \'Crimson Pro\', serif; font-size: 0.95rem; font-weight: 500; color: var(--gold-light); margin: 14px 0 4px; text-transform: uppercase; letter-spacing: 0.06em; }\n  .section-body p { margin-bottom: 10px; }\n  .section-body ul, .section-body ol { padding-left: 22px; margin-bottom: 10px; }\n  .section-body li { margin-bottom: 5px; }\n  .date-tag { display: inline-block; background: rgba(201,168,76,0.12); border: 1px solid rgba(201,168,76,0.25); color: var(--gold); font-family: \'DM Mono\', monospace; font-size: 0.75rem; padding: 2px 8px; border-radius: 2px; margin-right: 6px; vertical-align: middle; }\n  .doc-entry { padding: 14px 0; border-bottom: 1px solid rgba(201,168,76,0.1); }\n  .doc-entry:last-child { border-bottom: none; }\n  .tag-admitted { display: inline-block; background: rgba(26,77,46,0.3); border: 1px solid rgba(26,77,46,0.6); color: #6fcf97; font-size: 0.72rem; padding: 2px 8px; border-radius: 2px; font-family: \'DM Mono\', monospace; }\n  .tag-denied { display: inline-block; background: rgba(139,32,32,0.3); border: 1px solid rgba(139,32,32,0.6); color: #f4a0a0; font-size: 0.72rem; padding: 2px 8px; border-radius: 2px; font-family: \'DM Mono\', monospace; }\n\n  footer { text-align: center; padding: 32px 0 0; border-top: 1px solid var(--border-gold); margin-top: 48px; font-size: 0.85rem; color: rgba(201,168,76,0.35); font-style: italic; }\n  @media (max-width: 600px) { .upload-section, .section-body { padding: 24px 20px; } .pick-area { padding: 36px 20px; } .btn-analyze { width: 100%; } }\n</style>\n</head>\n<body>\n<div class="wrapper">\n  <header>\n    <div class="seal">⚖</div>\n    <h1>Appeal Record Summary</h1>\n    <p class="subtitle">Appellate Document Analysis &amp; Digest</p>\n  </header>\n\n  <div class="upload-section" id="uploadSection">\n    <div class="pick-area" id="pickArea">\n      <span class="drop-icon">📄</span>\n      <h3>Upload Court of Appeals Record</h3>\n      <p>Select a PDF up to 900 MB — large records are processed automatically</p>\n      <input type="file" id="fileInput" accept=".pdf" />\n      <button class="btn-browse" onclick="document.getElementById(\'fileInput\').click()">Browse for PDF…</button>\n      <div class="file-info" id="fileInfo">\n        <span>📎</span>\n        <span class="fname" id="fileName"></span>\n        <span class="fsize" id="fileSize"></span>\n        <button class="btn-clear" onclick="clearFile()" title="Remove">✕</button>\n      </div>\n    </div>\n    <button class="btn-analyze" id="analyzeBtn" disabled onclick="analyzeDocument()">Analyze Record</button>\n  </div>\n\n  <div class="progress-section" id="progressSection">\n    <div class="spinner"></div>\n    <p class="progress-label" id="progressLabel">Uploading document…</p>\n    <p class="progress-sub" id="progressSub">Please wait — large records may take several minutes</p>\n    <div class="progress-bar-wrap"><div class="progress-bar-fill" id="progressBarFill"></div></div>\n  </div>\n\n  <div class="error-box" id="errorBox"></div>\n\n  <div id="results">\n    <div class="results-header">\n      <h2>Record Analysis</h2>\n      <button class="btn-reset" onclick="resetApp()">↩ Analyze Another</button>\n    </div>\n    <div id="sectionsContainer"></div>\n  </div>\n\n  <footer>Appeal Record Summary · AI-Assisted Legal Document Analysis · Not a Substitute for Legal Advice</footer>\n</div>\n\n<script>\nconst MAX_MB = 900;\nconst MAX_BYTES = MAX_MB * 1024 * 1024;\nlet selectedFile = null;\n\ndocument.getElementById(\'fileInput\').addEventListener(\'change\', function(e) {\n  if (e.target.files && e.target.files[0]) setFile(e.target.files[0]);\n});\n\nfunction setFile(file) {\n  if (!file.name.toLowerCase().endsWith(\'.pdf\')) { showError(\'Please select a PDF file.\'); return; }\n  if (file.size > MAX_BYTES) {\n    showError(\'This file is \' + (file.size/1048576).toFixed(0) + \' MB, which exceeds the 900 MB limit.\');\n    return;\n  }\n  selectedFile = file;\n  document.getElementById(\'fileName\').textContent = file.name;\n  document.getElementById(\'fileSize\').textContent = formatBytes(file.size);\n  document.getElementById(\'fileInfo\').classList.add(\'visible\');\n  document.getElementById(\'pickArea\').classList.add(\'has-file\');\n  document.getElementById(\'analyzeBtn\').disabled = false;\n  hideError();\n}\n\nfunction clearFile() {\n  selectedFile = null;\n  document.getElementById(\'fileInput\').value = \'\';\n  document.getElementById(\'fileInfo\').classList.remove(\'visible\');\n  document.getElementById(\'pickArea\').classList.remove(\'has-file\');\n  document.getElementById(\'analyzeBtn\').disabled = true;\n}\n\nfunction formatBytes(b) {\n  if (b < 1024) return b + \' B\';\n  if (b < 1048576) return (b/1024).toFixed(1) + \' KB\';\n  return (b/1048576).toFixed(1) + \' MB\';\n}\n\nasync function analyzeDocument() {\n  if (!selectedFile) return;\n\n  document.getElementById(\'uploadSection\').style.display = \'none\';\n  document.getElementById(\'results\').classList.remove(\'visible\');\n  hideError();\n  showProgress();\n  setProgress(\'Uploading document…\', \'Sending to server…\');\n  animateBar(0, 15, 3000);\n\n  const formData = new FormData();\n  formData.append(\'file\', selectedFile);\n\n  let response;\n  try {\n    response = await fetch(\'/analyze\', { method: \'POST\', body: formData });\n  } catch (e) {\n    return fail(\'Could not reach the server: \' + e.message);\n  }\n\n  if (response.ok) {\n    animateBar(15, 90, 2000);\n    setProgress(\'Analyzing the appellate record with AI…\', \'This may take several minutes for large records\');\n  }\n\n  let data;\n  try {\n    data = await response.json();\n  } catch (e) {\n    return fail(\'Server returned an unexpected response.\');\n  }\n\n  if (!response.ok || data.error) {\n    return fail(data.error || \'Server error \' + response.status);\n  }\n\n  animateBar(90, 100, 500);\n  setTimeout(() => { hideProgress(); renderResults(data); }, 600);\n}\n\n// Smoothly animate the progress bar between two values over a duration\nfunction animateBar(from, to, ms) {\n  const fill = document.getElementById(\'progressBarFill\');\n  const start = performance.now();\n  function step(now) {\n    const t = Math.min((now - start) / ms, 1);\n    fill.style.width = (from + (to - from) * t) + \'%\';\n    if (t < 1) requestAnimationFrame(step);\n  }\n  requestAnimationFrame(step);\n}\n\nfunction setProgress(label, sub) {\n  document.getElementById(\'progressLabel\').textContent = label;\n  document.getElementById(\'progressSub\').textContent = sub;\n}\nfunction showProgress() { document.getElementById(\'progressSection\').classList.add(\'visible\'); }\nfunction hideProgress() { document.getElementById(\'progressSection\').classList.remove(\'visible\'); }\nfunction showError(msg) { const b = document.getElementById(\'errorBox\'); b.textContent = \'⚠ \' + msg; b.classList.add(\'visible\'); }\nfunction hideError() { document.getElementById(\'errorBox\').classList.remove(\'visible\'); }\nfunction fail(msg) { showError(msg); hideProgress(); document.getElementById(\'uploadSection\').style.display = \'block\'; }\n\nfunction resetApp() {\n  clearFile();\n  document.getElementById(\'uploadSection\').style.display = \'block\';\n  document.getElementById(\'results\').classList.remove(\'visible\');\n  document.getElementById(\'sectionsContainer\').innerHTML = \'\';\n  hideError(); hideProgress();\n}\n\n// ── Render results ─────────────────────────────────────────────────────────\nfunction renderResults(data) {\n  const c = document.getElementById(\'sectionsContainer\');\n  c.innerHTML = \'\';\n  if (data.caseTitle) {\n    const el = document.createElement(\'p\');\n    el.style.cssText = \'font-family:"Playfair Display",serif;font-size:1.1rem;color:var(--gold);margin-bottom:24px;font-style:italic;\';\n    el.textContent = data.caseTitle; c.appendChild(el);\n  }\n  addSection(c,\'1\',\'Pleadings Filed\',                                                   renderPleadings(data.pleadings||[]));\n  addSection(c,\'2\',\'Witnesses & Testimony (Affidavit, Declaration, Deposition, Trial)\', renderWitnesses(data.witnesses||[]));\n  addSection(c,\'3\',\'Dispositive Motions\',                                               renderDispositive(data.dispositiveMotions||[]));\n  addSection(c,\'4\',\'Orders & Judgments\',                                                renderOrders(data.ordersAndJudgments||[]));\n  addSection(c,\'5\',\'Trial\',                                                             renderTrial(data.trial||{occurred:false}));\n  addSection(c,\'6\',\'Pretrial Orders, Motions in Limine & Pretrial Rulings\',             renderPretrial(data.pretrialMatters||[]));\n  addSection(c,\'7\',\'Trial Objections\',                                                  renderObjections(data.trialObjections||[]));\n  document.getElementById(\'results\').classList.add(\'visible\');\n}\n\nfunction addSection(container, num, title, bodyHTML) {\n  const card = document.createElement(\'div\');\n  card.className = \'section-card\';\n  card.innerHTML = `<div class="section-header" onclick="toggleSection(this.parentElement)"><div class="section-num">${num}</div><div class="section-title">${title}</div><div class="section-toggle">▾</div></div><div class="section-body">${bodyHTML}</div>`;\n  container.appendChild(card);\n}\nfunction toggleSection(card) { card.classList.toggle(\'collapsed\'); }\nfunction dt(d) { if (!d||d===\'unknown\') return \'\'; return `<span class="date-tag">${escHtml(d)}</span>`; }\nfunction escHtml(s) { if (!s) return \'\'; return String(s).replace(/&/g,\'&amp;\').replace(/</g,\'&lt;\').replace(/>/g,\'&gt;\'); }\n\nfunction renderPleadings(items) {\n  if (!items.length) return \'<p><em>No pleadings identified in the record.</em></p>\';\n  return items.map(p=>`<div class="doc-entry">${dt(p.date)}<strong>${escHtml(p.title)}</strong>${p.type?` <span style="color:var(--text-light);font-size:0.85rem;">[${escHtml(p.type)}]</span>`:\'\'}${p.summary?`<p style="margin-top:6px;">${escHtml(p.summary)}</p>`:\'\'}</div>`).join(\'\');\n}\nfunction renderWitnesses(items) {\n  if (!items.length) return \'<p><em>No witness testimony identified in the record.</em></p>\';\n  return items.map(w=>`<div class="doc-entry"><strong style="color:var(--gold-light);">${escHtml(w.name)}</strong>${w.type?` <span style="color:var(--text-light);font-size:0.85rem;">[${escHtml(w.type)}]</span>`:\'\'}${dt(w.date)}${w.keyTestimony&&w.keyTestimony.length?`<ul style="margin-top:8px;">${w.keyTestimony.map(t=>`<li>${escHtml(t)}</li>`).join(\'\')}</ul>`:\'\'}</div>`).join(\'\');\n}\nfunction renderDispositive(items) {\n  if (!items.length) return \'<p><em>No dispositive motions identified in the record.</em></p>\';\n  return items.map(m=>`<div class="doc-entry">${dt(m.date)}<strong>${escHtml(m.title)}</strong>${m.type?` <span style="color:var(--text-light);font-size:0.85rem;">[${escHtml(m.type)}]</span>`:\'\'}${m.filedBy?`<div style="font-size:0.88rem;color:var(--text-light);margin-top:4px;">Filed by: ${escHtml(m.filedBy)}</div>`:\'\'}${m.mainArguments&&m.mainArguments.length?`<h4>Main Arguments</h4><ul>${m.mainArguments.map(a=>`<li>${escHtml(a)}</li>`).join(\'\')}</ul>`:\'\'}${m.courtRuling?`<div style="margin-top:8px;"><strong style="color:var(--gold);">Court Ruling</strong> ${dt(m.rulingDate)}<br>${escHtml(m.courtRuling)}</div>`:\'\'}</div>`).join(\'\');\n}\nfunction renderOrders(items) {\n  if (!items.length) return \'<p><em>No orders or judgments identified in the record.</em></p>\';\n  return items.map(o=>`<div class="doc-entry">${dt(o.date)}<strong>${escHtml(o.title)}</strong>${o.type?` <span style="color:var(--text-light);font-size:0.85rem;">[${escHtml(o.type)}]</span>`:\'\'}${o.summary?`<p style="margin-top:6px;">${escHtml(o.summary)}</p>`:\'\'}</div>`).join(\'\');\n}\nfunction renderTrial(trial) {\n  if (!trial.occurred) return \'<p><em>No trial is reflected in this record.</em></p>\';\n  let html=\'\';\n  if (trial.startDate||trial.endDate) html+=`<p><strong>Trial Dates:</strong> ${escHtml(trial.startDate||\'\')}${trial.endDate&&trial.endDate!==trial.startDate?\' – \'+escHtml(trial.endDate):\'\'}</p>`;\n  if (trial.summary) html+=`<p>${escHtml(trial.summary)}</p>`;\n  (trial.days||[]).forEach(d=>{\n    html+=`<h3>Day ${d.dayNumber}${d.date?\' — \'+escHtml(d.date):\'\'}</h3>`;\n    if(d.description)html+=`<p>${escHtml(d.description)}</p>`;\n    if(d.witnesses&&d.witnesses.length)html+=`<p><strong>Witnesses:</strong> ${d.witnesses.map(escHtml).join(\', \')}</p>`;\n    if(d.significantEvents&&d.significantEvents.length)html+=`<ul>${d.significantEvents.map(e=>`<li>${escHtml(e)}</li>`).join(\'\')}</ul>`;\n  });\n  if(trial.exhibits&&trial.exhibits.length){\n    html+=`<h3>Exhibits</h3>`;\n    html+=trial.exhibits.map(ex=>`<div class="doc-entry"><strong>Exhibit ${escHtml(ex.id)}</strong> ${dt(ex.date)}${ex.admitted!==undefined?(ex.admitted?\'<span class="tag-admitted">ADMITTED</span>\':\'<span class="tag-denied">NOT ADMITTED</span>\'):\'\'}${ex.offeredBy?` <span style="font-size:0.85rem;color:var(--text-light);">Offered by: ${escHtml(ex.offeredBy)}</span>`:\'\'}<div style="margin-top:4px;">${escHtml(ex.description)}</div>${ex.reason?`<div style="font-size:0.88rem;color:var(--text-light);">${escHtml(ex.reason)}</div>`:\'\'}</div>`).join(\'\');\n  }\n  return html||\'<p><em>Trial occurred but details were not extracted.</em></p>\';\n}\nfunction renderPretrial(items) {\n  if (!items.length) return \'<p><em>No pretrial motions or orders identified in the record.</em></p>\';\n  return items.map(m=>`<div class="doc-entry">${dt(m.date)}<strong>${escHtml(m.title)}</strong>${m.type?` <span style="color:var(--text-light);font-size:0.85rem;">[${escHtml(m.type)}]</span>`:\'\'}${m.summary?`<p style="margin-top:6px;">${escHtml(m.summary)}</p>`:\'\'}${m.ruling?`<p><strong style="color:var(--gold);">Ruling:</strong> ${escHtml(m.ruling)}</p>`:\'\'}</div>`).join(\'\');\n}\nfunction renderObjections(items) {\n  if (!items.length) return \'<p><em>No trial objections identified in the record.</em></p>\';\n  return items.map(o=>`<div class="doc-entry">${dt(o.date)}${o.sustained!==undefined?(o.sustained?\'<span class="tag-admitted">SUSTAINED</span>\':\'<span class="tag-denied">OVERRULED</span>\'):\'\'}${o.objectingParty?` <strong>${escHtml(o.objectingParty)}</strong>`:\'\'}${o.basis?` <span style="color:var(--text-light);font-size:0.88rem;">Basis: ${escHtml(o.basis)}</span>`:\'\'}${o.evidenceOrTestimony?`<p style="margin-top:6px;">${escHtml(o.evidenceOrTestimony)}</p>`:\'\'}${o.courtRuling?`<p><strong style="color:var(--gold);">Ruling:</strong> ${escHtml(o.courtRuling)}</p>`:\'\'}</div>`).join(\'\');\n}\n</script>\n</body>\n</html>\n'
 
SYSTEM_PROMPT = """You are an expert appellate attorney and legal analyst specializing in reviewing and summarizing court of appeals records. Your task is to thoroughly analyze the uploaded court record and produce a structured, comprehensive summary.
 
Return ONLY valid JSON — no markdown fences, no preamble, nothing else.
 
The JSON must have exactly these keys:
{{
  "caseTitle": "string — case name/number if identifiable",
  "pleadings": [...],
  "witnesses": [...],
  "dispositiveMotions": [...],
  "ordersAndJudgments": [...],
  "trial": {{ ... }},
  "pretrialMatters": [...],
  "trialObjections": [...]
}}
 
For each array item, include a "date" field (string, e.g. "March 5, 2021" or "unknown") and a "title" field.
 
pleadings: each item: {{ date, title, type (e.g. "Petition","Answer","Affirmative Defense","Third-Party Petition", etc.), summary }}
 
witnesses: each item: {{ name, date, type ("Affidavit"|"Declaration"|"Deposition"|"Trial Testimony"), keyTestimony (array of strings) }}
 
dispositiveMotions: each item: {{ date, title, type ("Motion to Dismiss"|"Motion for Summary Judgment"|"Plea to the Jurisdiction"|"Motion for Directed Verdict"|other), filedBy, mainArguments (array of strings), courtRuling, rulingDate }}
 
ordersAndJudgments: each item: {{ date, title, type ("Interlocutory Order"|"Final Judgment"|"Partial Summary Judgment"|"Order on Motion"|other), summary }}
 
trial: {{
  occurred: boolean,
  startDate: string,
  endDate: string,
  days: [ {{ dayNumber, date, description, witnesses: [string], significantEvents: [string] }} ],
  exhibits: [ {{ id, description, offeredBy, date, admitted: boolean, reason }} ],
  summary: string
}}
 
pretrialMatters: each item: {{ date, title, type ("Motion in Limine"|"Pretrial Order"|"Scheduling Order"|"Pretrial Conference"|other), summary, ruling }}
 
trialObjections: each item: {{ date, objectingParty, basis, evidenceOrTestimony, courtRuling, sustained: boolean }}
 
Be exhaustive. If a section has no relevant documents return an empty array (or for trial, occurred: false). For dates, use the format found in the document."""
 
MERGE_PROMPT = """You are an expert appellate attorney. Merge all the partial summaries below into one comprehensive, deduplicated summary.
 
Return ONLY valid JSON with exactly these keys: caseTitle, pleadings, witnesses, dispositiveMotions, ordersAndJudgments, trial, pretrialMatters, trialObjections.
 
For the trial object: if any section shows a trial occurred, set occurred: true and merge the days, exhibits, and summary from all sections.
 
Partial summaries to merge:
"""
 
@app.route("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")
 
@app.route("/health")
def health():
    return jsonify({{"status": "ok", "html_chars": len(INDEX_HTML), "api_key_set": bool(ANTHROPIC_API_KEY)}})
 
@app.route("/analyze", methods=["POST"])
def analyze():
    try:
        return _analyze()
    except Exception as e:
        print("UNHANDLED ERROR:\n", traceback.format_exc())
        return jsonify({{"error": f"Unexpected server error: {{str(e)}}"}}), 500
 
def _analyze():
    if not ANTHROPIC_API_KEY:
        return jsonify({{"error": "Server is missing ANTHROPIC_API_KEY. Add it in Render dashboard → Environment."}}), 500
 
    if "file" not in request.files:
        return jsonify({{"error": "No file received."}}), 400
 
    f = request.files["file"]
    fname = f.filename or ""
    if not fname.lower().endswith(".pdf"):
        return jsonify({{"error": "Please upload a PDF file."}}), 400
 
    try:
        pdf_bytes = f.read()
    except Exception as e:
        return jsonify({{"error": f"Failed to read file: {{str(e)}}"}}), 400
 
    if not pdf_bytes:
        return jsonify({{"error": "The uploaded file is empty."}}), 400
 
    mb = len(pdf_bytes) / 1048576
    print(f"File: {{fname}}  {{mb:.1f}} MB")
 
    if len(pdf_bytes) > MAX_FILE_BYTES:
        return jsonify({{"error": f"File is {{mb:.0f}} MB. Maximum is 900 MB."}}), 400
 
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        return jsonify({{"error": f"Could not open PDF (corrupted or password-protected): {{str(e)}}"}}), 400
 
    total_pages = len(doc)
    print(f"Pages: {{total_pages}}")
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
 
    # Small PDF: send as base64
    if total_pages <= MAX_PAGES_PER_CHUNK and len(pdf_bytes) <= 32 * 1024 * 1024:
        doc.close()
        print("Strategy: direct base64")
        try:
            return jsonify(analyze_pdf_bytes(client, pdf_bytes))
        except Exception as e:
            print("Direct error:\n", traceback.format_exc())
            return jsonify({{"error": f"Analysis failed: {{str(e)}}"}}), 500
 
    # Large PDF: extract text, chunk, analyze, merge
    n = math.ceil(total_pages / MAX_PAGES_PER_CHUNK)
    print(f"Strategy: chunked text ({{n}} chunks)")
    try:
        partials = analyze_in_chunks(client, doc, total_pages)
        doc.close()
    except Exception as e:
        doc.close()
        print("Chunk error:\n", traceback.format_exc())
        return jsonify({{"error": f"Analysis failed during text extraction: {{str(e)}}"}}), 500
 
    if not partials:
        return jsonify({{"error": "No readable text found. The PDF may be a scanned image without OCR text."}}), 400
 
    try:
        result = partials[0] if len(partials) == 1 else merge_results(client, partials)
        return jsonify(result)
    except Exception as e:
        print("Merge error:\n", traceback.format_exc())
        return jsonify({{"error": f"Analysis failed during merge: {{str(e)}}"}}), 500
 
 
def analyze_pdf_bytes(client, pdf_bytes):
    b64 = base64.b64encode(pdf_bytes).decode("utf-8")
    r = client.messages.create(
        model="claude-sonnet-4-20250514", max_tokens=8000, system=SYSTEM_PROMPT,
        messages=[{{"role": "user", "content": [
            {{"type": "document", "source": {{"type": "base64", "media_type": "application/pdf", "data": b64}}}},
            {{"type": "text", "text": "Analyze this appellate court record and return the structured JSON summary."}}
        ]}}]
    )
    return parse_json(r.content[0].text)
 
 
def analyze_in_chunks(client, doc, total_pages):
    results = []
    n = math.ceil(total_pages / MAX_PAGES_PER_CHUNK)
    for i in range(n):
        start = i * MAX_PAGES_PER_CHUNK
        end = min(start + MAX_PAGES_PER_CHUNK, total_pages)
        label = f"pages {{start+1}}-{{end}} of {{total_pages}}"
        print(f"  Chunk {{i+1}}/{{n}}: {{label}}")
        parts = []
        for p in range(start, end):
            t = doc[p].get_text("text")
            if t.strip():
                parts.append(f"[Page {{p+1}}]\n{{t}}")
        text = "\n\n".join(parts)
        if not text.strip():
            print(f"    No text, skipping")
            continue
        try:
            r = client.messages.create(
                model="claude-sonnet-4-20250514", max_tokens=8000, system=SYSTEM_PROMPT,
                messages=[{{"role": "user", "content": f"Analyze this section ({{label}}) and return the structured JSON summary.\n\n{{text}}"}}]
            )
            results.append(parse_json(r.content[0].text))
            print(f"    OK")
        except Exception as e:
            print(f"    Error: {{e}}")
    return results
 
 
def merge_results(client, partials):
    combined = json.dumps(partials, indent=2)
    print(f"Merging {{len(partials)}} partials")
    r = client.messages.create(
        model="claude-sonnet-4-20250514", max_tokens=8000,
        system=MERGE_PROMPT + combined,
        messages=[{{"role": "user", "content": "Merge these partial summaries into one comprehensive JSON summary."}}]
    )
    return parse_json(r.content[0].text)
 
 
def parse_json(raw):
    clean = raw.strip()
    if "```" in clean:
        for part in clean.split("```"):
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
