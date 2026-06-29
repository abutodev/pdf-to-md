#!/usr/bin/env python3
"""
PDF → Markdown Converter — Local Web UI
Powered by marker-pdf  https://github.com/datalab-to/marker

Setup:
    pip install flask marker-pdf

Run:
    python pdf_to_md.py
    Then open http://localhost:5000
"""

import os
import sys
import uuid
import threading
import traceback
from pathlib import Path

try:
    from flask import Flask, request, jsonify, send_file, render_template_string
except ImportError:
    print("Flask is required.  Run: pip install flask")
    sys.exit(1)

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE   = Path(__file__).parent
UPLOAD = BASE / "uploads"
OUTPUT = BASE / "outputs"
UPLOAD.mkdir(exist_ok=True)
OUTPUT.mkdir(exist_ok=True)

# ── Flask ──────────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB per file

# ── Global state ───────────────────────────────────────────────────────────────
_models      = None
_model_lock  = threading.Lock()
_status      = {"state": "loading", "message": "Initializing…"}

_jobs: dict  = {}
_jobs_lock   = threading.Lock()
_queue: list = []
_queue_lock  = threading.Lock()
_wake        = threading.Event()


# ── Background: model loading ──────────────────────────────────────────────────

def _load_models():
    global _models, _status
    try:
        _status["message"] = "Importing marker-pdf…"
        try:
            from marker.models import create_model_dict
        except ImportError:
            _status = {
                "state":   "error",
                "message": "marker-pdf not installed.  Run: pip install marker-pdf",
            }
            return

        _status["message"] = (
            "Loading AI models — first run may download ~2 GB from HuggingFace. "
            "This can take 1–5 minutes…"
        )
        models = create_model_dict()

        with _model_lock:
            _models = models

        _status = {"state": "ready", "message": "Ready — drop a PDF to convert!"}
        threading.Thread(target=_worker, daemon=True).start()

    except Exception as exc:
        _status = {"state": "error", "message": f"Model load failed: {exc}"}
        print(traceback.format_exc())


# ── Job worker ─────────────────────────────────────────────────────────────────

def _worker():
    """Process one job at a time to avoid OOM on large models."""
    while True:
        _wake.wait()
        _wake.clear()
        while True:
            with _queue_lock:
                if not _queue:
                    break
                job = _queue.pop(0)
            _process(**job)


def _log(job_id: str, msg: str):
    with _jobs_lock:
        _jobs[job_id]["log"].append(msg)
    print(f"  [{job_id[:6]}] {msg}")


def _process(job_id: str, pdf_path: Path, options: dict):
    _log(job_id, f"Starting: {pdf_path.name}")
    with _jobs_lock:
        _jobs[job_id]["status"] = "converting"

    out_dir = OUTPUT / job_id
    out_dir.mkdir(exist_ok=True)

    try:
        from marker.converters.pdf import PdfConverter

        fmt = options.get("format", "markdown")
        ext = {"markdown": "md", "html": "html", "json": "json"}.get(fmt, "md")
        cfg = {"output_format": fmt}
        if options.get("force_ocr"):
            cfg["force_ocr"] = True

        _log(job_id, "Initializing converter…")
        try:
            from marker.config.parser import ConfigParser
            cp = ConfigParser(cfg)
            converter = PdfConverter(
                config=cp.generate_config_dict(),
                artifact_dict=_models,
                renderer=cp.get_renderer(),
            )
        except Exception:
            # Fallback for older marker API
            converter = PdfConverter(artifact_dict=_models)

        _log(job_id, "Converting — large PDFs may take several minutes…")
        rendered = converter(str(pdf_path))

        _log(job_id, "Saving output…")
        text = _extract_text(rendered, fmt)

        # Strip the UUID prefix we prepended on upload  (uuid4 = 36 chars + "_")
        stem = pdf_path.stem
        if len(stem) > 37 and stem[36] == "_":
            stem = stem[37:]
        if not stem:
            stem = "output"

        out_file = out_dir / f"{stem}.{ext}"
        out_file.write_text(text, encoding="utf-8")

        # Save embedded images if marker returns them
        for name, img in (getattr(rendered, "images", None) or {}).items():
            ip = out_dir / name
            ip.parent.mkdir(parents=True, exist_ok=True)
            if hasattr(img, "save"):
                img.save(str(ip))

        with _jobs_lock:
            _jobs[job_id].update(
                status="done",
                output_file=str(out_file),
                output_filename=out_file.name,
            )
        _log(job_id, f"Done ✓  →  {out_file.name}")

    except Exception as exc:
        with _jobs_lock:
            _jobs[job_id].update(status="error", error=str(exc))
        _log(job_id, f"Error: {exc}")
        print(traceback.format_exc())


def _extract_text(rendered, fmt: str) -> str:
    """Pull a string from whatever marker returns, trying multiple attributes."""
    for attr in ("markdown", "html", "text"):
        val = getattr(rendered, attr, None)
        if isinstance(val, str) and val:
            return val
    try:
        from marker.output import text_from_rendered
        text, _, _ = text_from_rendered(rendered)
        return text
    except Exception:
        pass
    try:
        import json
        return json.dumps(vars(rendered), default=str, indent=2)
    except Exception:
        return str(rendered)


# ── HTTP API ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/model-status")
def api_model_status():
    return jsonify(_status)


@app.route("/api/convert", methods=["POST"])
def api_convert():
    if _status["state"] != "ready":
        return jsonify({"error": f"Not ready: {_status['message']}"}), 503
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file uploaded"}), 400
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Only PDF files are accepted"}), 400

    options = {
        "format":    request.form.get("format", "markdown"),
        "force_ocr": request.form.get("force_ocr") == "true",
    }
    job_id   = str(uuid.uuid4())
    safe     = "".join(c for c in f.filename if c.isalnum() or c in " ._-")
    pdf_path = UPLOAD / f"{job_id}_{safe}"
    f.save(str(pdf_path))

    with _jobs_lock:
        _jobs[job_id] = {
            "status":   "queued",
            "filename": f.filename,
            "log":      ["Queued"],
        }
    with _queue_lock:
        _queue.append({"job_id": job_id, "pdf_path": pdf_path, "options": options})
    _wake.set()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def api_status(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    return (jsonify(job), 200) if job else (jsonify({"error": "Not found"}), 404)


@app.route("/api/download/<job_id>")
def api_download(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "Not ready"}), 404
    return send_file(
        job["output_file"],
        as_attachment=True,
        download_name=job["output_filename"],
    )


@app.route("/api/remove/<job_id>", methods=["DELETE"])
def api_remove(job_id):
    with _jobs_lock:
        _jobs.pop(job_id, None)
    return jsonify({"ok": True})


# ── HTML / CSS / JS ────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PDF → Markdown</title>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --bg:      #f5f5f7;
  --surface: #ffffff;
  --border:  #e2e2e7;
  --accent:  #6c63ff;
  --accent2: #534ccf;
  --text:    #1c1c1e;
  --muted:   #6e6e73;
  --green:   #30d158;
  --red:     #ff453a;
  --orange:  #ff9f0a;
  --r:       14px;
}

body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
}

/* ── Header ── */
header {
  background: rgba(255,255,255,0.85);
  backdrop-filter: blur(20px);
  -webkit-backdrop-filter: blur(20px);
  border-bottom: 1px solid var(--border);
  padding: 14px 24px;
  display: flex;
  align-items: center;
  gap: 14px;
  position: sticky;
  top: 0;
  z-index: 10;
}
.logo {
  width: 32px; height: 32px;
  background: linear-gradient(135deg, #6c63ff, #9c67d4);
  border-radius: 8px;
  display: flex; align-items: center; justify-content: center;
  color: white; font-size: 15px; font-weight: 800;
  flex-shrink: 0;
}
header h1 { font-size: 17px; font-weight: 600; flex: 1; }

.pill {
  padding: 4px 12px;
  border-radius: 99px;
  font-size: 12px; font-weight: 500;
  display: flex; align-items: center; gap: 6px;
  white-space: nowrap;
}
.pill .dot {
  width: 7px; height: 7px;
  border-radius: 50%; flex-shrink: 0;
}
.pill-loading { background: #fff3cd; color: #856404; }
.pill-loading .dot { background: var(--orange); animation: blink 1.2s infinite; }
.pill-ready   { background: #d4edda; color: #155724; }
.pill-ready .dot { background: var(--green); }
.pill-error   { background: #f8d7da; color: #721c24; }
.pill-error .dot { background: var(--red); }

@keyframes blink { 0%,100%{opacity:1} 50%{opacity:.3} }

/* ── Layout ── */
main { max-width: 780px; margin: 0 auto; padding: 28px 20px 80px; }

/* ── Card ── */
.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--r);
  padding: 22px;
  margin-bottom: 16px;
  box-shadow: 0 1px 4px rgba(0,0,0,0.04);
}
.card-title {
  font-size: 11px; font-weight: 600;
  text-transform: uppercase; letter-spacing: .07em;
  color: var(--muted);
  margin-bottom: 16px;
}

/* ── Drop zone ── */
.drop-zone {
  position: relative;
  border: 2px dashed var(--border);
  border-radius: 10px;
  padding: 44px 20px;
  text-align: center;
  cursor: pointer;
  transition: border-color .18s, background .18s;
  user-select: none;
}
.drop-zone:hover, .drop-zone.over {
  border-color: var(--accent);
  background: rgba(108,99,255,.04);
}
.drop-zone.disabled { opacity: .45; pointer-events: none; }
.drop-zone input {
  position: absolute; inset: 0;
  opacity: 0; cursor: pointer; font-size: 0;
}
.dz-icon { font-size: 40px; margin-bottom: 12px; }
.dz-text { font-size: 14px; color: var(--muted); line-height: 1.6; }
.dz-text strong { color: var(--accent); }
.dz-sub { font-size: 12px; color: var(--muted); margin-top: 4px; }
#model-msg {
  font-size: 12px; color: var(--muted);
  margin-top: 10px; text-align: center;
  min-height: 18px;
}

/* ── Options ── */
.opts-row { display: flex; gap: 14px; flex-wrap: wrap; }
.opt-group { flex: 1; min-width: 160px; }
.opt-group > label:first-child {
  display: block; font-size: 12px; font-weight: 500;
  color: var(--muted); margin-bottom: 6px;
}
select {
  width: 100%; padding: 8px 10px;
  border: 1px solid var(--border); border-radius: 8px;
  font-size: 13px; background: var(--bg); color: var(--text);
  outline: none; cursor: pointer;
}
select:focus { border-color: var(--accent); }
.checkbox-row {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 10px;
  border: 1px solid var(--border); border-radius: 8px;
  background: var(--bg); font-size: 13px; cursor: pointer;
  transition: border-color .15s;
}
.checkbox-row:hover { border-color: var(--accent); }
.checkbox-row input { accent-color: var(--accent); width: 15px; height: 15px; flex-shrink: 0; }

/* ── Job list ── */
#job-list .empty {
  text-align: center; color: var(--muted);
  font-size: 13px; padding: 28px 0;
}
.job {
  border: 1px solid var(--border);
  border-radius: 10px;
  overflow: hidden;
  margin-bottom: 10px;
  transition: border-color .2s;
}
.job.done       { border-color: #a8d5b5; }
.job.error      { border-color: #f5b8b8; }
.job.converting { border-color: #ffd166; }

.job-top {
  padding: 12px 14px;
  display: flex; align-items: center; gap: 10px;
}
.job-icon { font-size: 20px; flex-shrink: 0; }
.job-info { flex: 1; overflow: hidden; }
.job-name {
  font-size: 14px; font-weight: 500;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.job-badge {
  padding: 3px 10px; border-radius: 99px;
  font-size: 11px; font-weight: 600; white-space: nowrap;
}
.badge-queued     { background: #f0f0f5; color: #555; }
.badge-converting { background: #fff3cd; color: #856404; }
.badge-done       { background: #d4edda; color: #155724; }
.badge-error      { background: #f8d7da; color: #721c24; }
.job-remove {
  background: none; border: none; cursor: pointer;
  color: var(--muted); font-size: 16px; padding: 4px;
  line-height: 1; flex-shrink: 0;
  transition: color .15s;
}
.job-remove:hover { color: var(--red); }

.job-bottom {
  padding: 0 14px 12px;
  display: flex; gap: 8px; align-items: flex-start;
}
.job-log {
  flex: 1;
  background: #f5f5f7; border-radius: 6px;
  padding: 8px 10px;
  font-family: "SF Mono", "Fira Code", ui-monospace, monospace;
  font-size: 11px; color: var(--muted);
  max-height: 90px; overflow-y: auto;
  line-height: 1.65;
}
.job-log p { white-space: pre-wrap; }

.btn-dl {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 7px 14px;
  background: var(--accent); color: white;
  border: none; border-radius: 7px;
  font-size: 12px; font-weight: 500;
  cursor: pointer; white-space: nowrap;
  text-decoration: none;
  transition: background .15s;
  flex-shrink: 0;
}
.btn-dl:hover { background: var(--accent2); }
</style>
</head>
<body>

<header>
  <div class="logo">P</div>
  <h1>PDF → Markdown</h1>
  <div id="model-pill" class="pill pill-loading">
    <span class="dot"></span>
    <span id="pill-text">Loading models…</span>
  </div>
</header>

<main>

  <div class="card">
    <div class="card-title">Upload</div>
    <div class="drop-zone disabled" id="drop-zone">
      <input type="file" id="file-input" accept=".pdf" multiple>
      <div class="dz-icon">📄</div>
      <div class="dz-text">
        <strong>Click to browse</strong> or drag &amp; drop PDFs here
      </div>
      <div class="dz-sub">Multiple files supported &nbsp;·&nbsp; Max 200 MB per file</div>
    </div>
    <p id="model-msg">Initializing — please wait…</p>
  </div>

  <div class="card">
    <div class="card-title">Options</div>
    <div class="opts-row">
      <div class="opt-group">
        <label for="opt-format">Output format</label>
        <select id="opt-format">
          <option value="markdown">Markdown (.md)</option>
          <option value="html">HTML (.html)</option>
          <option value="json">JSON (.json)</option>
        </select>
      </div>
      <div class="opt-group">
        <label>OCR</label>
        <label class="checkbox-row">
          <input type="checkbox" id="opt-ocr">
          Force OCR (use for scanned PDFs)
        </label>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card-title">Conversions</div>
    <div id="job-list">
      <div class="empty">No conversions yet — upload a PDF above to get started.</div>
    </div>
  </div>

</main>

<script>
const dropZone  = document.getElementById('drop-zone');
const fileInput = document.getElementById('file-input');
const jobList   = document.getElementById('job-list');
const modelPill = document.getElementById('model-pill');
const pillText  = document.getElementById('pill-text');
const modelMsg  = document.getElementById('model-msg');

let modelsReady = false;
const activePolls = new Map();   // jobId -> intervalId
const jobEls = {};               // jobId -> DOM element

// ── Model-status polling ───────────────────────────────────────────────────
async function pollModelStatus() {
  try {
    const r = await fetch('/api/model-status');
    const d = await r.json();
    modelMsg.textContent = d.message;
    pillText.textContent = d.state === 'loading' ? 'Loading models…'
                         : d.state === 'ready'   ? 'Ready'
                         :                         'Error';
    modelPill.className = `pill pill-${d.state}`;
    if (d.state === 'ready') {
      modelsReady = true;
      dropZone.classList.remove('disabled');
      fileInput.disabled = false;
      return;
    }
    if (d.state !== 'error') setTimeout(pollModelStatus, 2500);
  } catch { setTimeout(pollModelStatus, 3000); }
}
pollModelStatus();

// ── Drag & drop ────────────────────────────────────────────────────────────
dropZone.addEventListener('dragover',  e => { e.preventDefault(); dropZone.classList.add('over'); });
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('over'));
dropZone.addEventListener('drop', e => {
  e.preventDefault();
  dropZone.classList.remove('over');
  handleFiles([...e.dataTransfer.files]);
});
fileInput.addEventListener('change', () => {
  handleFiles([...fileInput.files]);
  fileInput.value = '';
});

function handleFiles(files) {
  const pdfs = files.filter(f => f.name.toLowerCase().endsWith('.pdf'));
  if (!pdfs.length) { alert('Please drop PDF files only.'); return; }
  if (!modelsReady)  { alert('Models are still loading. Please wait.'); return; }
  pdfs.forEach(uploadFile);
}

// ── Upload & queue ─────────────────────────────────────────────────────────
async function uploadFile(file) {
  const opts = {
    format:    document.getElementById('opt-format').value,
    force_ocr: document.getElementById('opt-ocr').checked,
  };
  const tempId = 'tmp-' + Date.now() + '-' + Math.random().toString(36).slice(2);
  addJobCard(tempId, file.name, 'queued');

  const fd = new FormData();
  fd.append('file', file);
  fd.append('format', opts.format);
  fd.append('force_ocr', String(opts.force_ocr));

  try {
    const r = await fetch('/api/convert', { method: 'POST', body: fd });
    const d = await r.json();
    if (d.error) { updateJob(tempId, 'error', [d.error]); return; }

    // Remap temp id -> real job id
    const el = jobEls[tempId];
    if (el) {
      el.dataset.job = d.job_id;
      delete jobEls[tempId];
      jobEls[d.job_id] = el;
      // Update log and download divs' ids
      const logEl = el.querySelector('.job-log');
      if (logEl) logEl.id = `log-${d.job_id}`;
      const dlEl = el.querySelector('.job-dl');
      if (dlEl) dlEl.id = `dl-${d.job_id}`;
      const removeBtn = el.querySelector('.job-remove');
      if (removeBtn) removeBtn.dataset.jid = d.job_id;
    }
    startPolling(d.job_id);
  } catch (e) {
    updateJob(tempId, 'error', [`Upload failed: ${e.message}`]);
  }
}

// ── Job polling ────────────────────────────────────────────────────────────
function startPolling(jobId) {
  if (activePolls.has(jobId)) return;
  const id = setInterval(() => pollJob(jobId), 2000);
  activePolls.set(jobId, id);
}
async function pollJob(jobId) {
  try {
    const r = await fetch(`/api/status/${jobId}`);
    const d = await r.json();
    if (d.error) return;
    updateJob(jobId, d.status, d.log || [], d.output_filename);
    if (d.status === 'done' || d.status === 'error') {
      clearInterval(activePolls.get(jobId));
      activePolls.delete(jobId);
    }
  } catch { /* ignore transient errors */ }
}

// ── DOM helpers ────────────────────────────────────────────────────────────
function addJobCard(jobId, filename, status) {
  const empty = jobList.querySelector('.empty');
  if (empty) empty.remove();

  const el = document.createElement('div');
  el.className = `job ${status}`;
  el.dataset.job = jobId;
  el.innerHTML = `
    <div class="job-top">
      <div class="job-icon">${statusIcon(status)}</div>
      <div class="job-info">
        <div class="job-name" title="${esc(filename)}">${esc(filename)}</div>
      </div>
      <span class="job-badge badge-${status}" id="badge-${jobId}">${badgeText(status)}</span>
      <button class="job-remove" data-jid="${jobId}" onclick="removeJob(this.dataset.jid)">✕</button>
    </div>
    <div class="job-bottom">
      <div class="job-log" id="log-${jobId}"><p>Queued</p></div>
      <div class="job-dl" id="dl-${jobId}"></div>
    </div>`;
  jobList.prepend(el);
  jobEls[jobId] = el;
}

function updateJob(jobId, status, log = [], outputFilename = null) {
  const el = jobEls[jobId];
  if (!el) return;

  el.className = `job ${status}`;

  const icon = el.querySelector('.job-icon');
  if (icon) icon.textContent = statusIcon(status);

  const badge = document.getElementById(`badge-${jobId}`);
  if (badge) {
    badge.className = `job-badge badge-${status}`;
    badge.textContent = badgeText(status);
  }

  const logEl = document.getElementById(`log-${jobId}`);
  if (logEl && log.length) {
    logEl.innerHTML = log.map(l => `<p>${esc(l)}</p>`).join('');
    logEl.scrollTop = logEl.scrollHeight;
  }

  const dlEl = document.getElementById(`dl-${jobId}`);
  if (dlEl && status === 'done' && outputFilename) {
    dlEl.innerHTML =
      `<a class="btn-dl" href="/api/download/${jobId}" download="${esc(outputFilename)}">` +
      `⬇ ${esc(outputFilename)}</a>`;
  }
}

async function removeJob(jobId) {
  const el = jobEls[jobId];
  if (el) { el.remove(); delete jobEls[jobId]; }
  clearInterval(activePolls.get(jobId));
  activePolls.delete(jobId);
  await fetch(`/api/remove/${jobId}`, { method: 'DELETE' });
  if (!Object.keys(jobEls).length) {
    jobList.innerHTML = '<div class="empty">No conversions yet — upload a PDF above to get started.</div>';
  }
}

function statusIcon(s) {
  return { queued: '🕐', converting: '⚙️', done: '✅', error: '❌' }[s] || '📄';
}
function badgeText(s) {
  return { queued: 'Queued', converting: 'Converting…', done: 'Done', error: 'Error' }[s] || s;
}
function esc(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
</script>
</body>
</html>"""


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import webbrowser

    print("╔══════════════════════════════════════╗")
    print("║   PDF → Markdown Converter           ║")
    print("╚══════════════════════════════════════╝")
    print()
    print("Loading models in background…")
    print("URL → http://localhost:5000")
    print("Stop → Ctrl+C")
    print()

    threading.Thread(target=_load_models, daemon=True).start()
    threading.Timer(1.5, lambda: webbrowser.open("http://localhost:5000")).start()

    app.run(port=5000, debug=False, use_reloader=False)
