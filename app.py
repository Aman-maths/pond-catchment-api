"""
Pond Catchment Analysis API
============================
Flask backend that accepts a contour map (KML/KMZ) and returns catchment
information for pond planning: a recommended pond location, its
contributing catchment boundary/area, basic terrain statistics, and a
simple reservoir sizing estimate.

Run locally:
    pip install -r requirements.txt
    python app.py
    # Server listens on http://0.0.0.0:5000

Endpoints:
    GET  /                 simple browser upload form (pick a file, get results)
    POST /analyzeContour   (alias: POST /findCatchment)
    GET  /health
"""

from __future__ import annotations

import os
import tempfile
import traceback

from flask import Flask, jsonify, request, Response

from pondcatchment import analyze

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 32 MB upload cap

ALLOWED_EXTENSIONS = {".kml", ".kmz"}

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pond Catchment Analysis</title>
<style>
  body { font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif; max-width: 720px; margin: 40px auto; padding: 0 20px; color: #1b1b1b; }
  h1 { font-size: 22px; margin-bottom: 4px; }
  p.sub { color: #555; margin-top: 0; }
  .card { border: 1px solid #ddd; border-radius: 8px; padding: 20px; margin-top: 20px; }
  input[type=file] { display: block; margin: 12px 0; }
  button { background: #1b1b1b; color: #fff; border: none; padding: 10px 18px; border-radius: 6px; font-size: 15px; cursor: pointer; }
  button:disabled { background: #999; cursor: default; }
  #status { margin-top: 14px; font-size: 14px; color: #444; }
  table { border-collapse: collapse; width: 100%; margin-top: 16px; }
  td, th { border: 1px solid #ddd; padding: 8px 10px; text-align: left; font-size: 14px; }
  th { background: #fafafa; width: 45%; }
  #raw { margin-top: 18px; }
  #raw summary { cursor: pointer; font-size: 13px; color: #555; }
  pre { white-space: pre-wrap; word-break: break-word; background: #f7f7f7; border: 1px solid #eee; padding: 12px; border-radius: 6px; font-size: 12px; max-height: 400px; overflow: auto; }
</style>
</head>
<body>
  <h1>Pond Catchment Analysis</h1>
  <p class="sub">Upload a contour map (.kml or .kmz) to get a recommended pond location and catchment information.</p>

  <div class="card">
    <form id="form">
      <input type="file" id="file" name="file" accept=".kml,.kmz" required>
      <button type="submit" id="submitBtn">Analyze</button>
    </form>
    <div id="status"></div>
    <div id="result"></div>
  </div>

<script>
const form = document.getElementById('form');
const statusEl = document.getElementById('status');
const resultEl = document.getElementById('result');
const submitBtn = document.getElementById('submitBtn');

async function waitForServerAwake() {
  // The free-tier server may be asleep; ping /health repeatedly until it
  // responds, so the heavy analysis request below always hits an
  // already-awake server instead of timing out mid-wake-up.
  const maxAttempts = 20;
  for (let i = 0; i < maxAttempts; i++) {
    try {
      const res = await fetch('/health', { cache: 'no-store' });
      if (res.ok) return true;
    } catch (e) { /* server still asleep / network hiccup, keep trying */ }
    statusEl.textContent = 'Waking up the server... (' + (i + 1) + '/' + maxAttempts + ')';
    await new Promise(r => setTimeout(r, 3000));
  }
  return false;
}

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const fileInput = document.getElementById('file');
  if (!fileInput.files.length) return;

  submitBtn.disabled = true;
  resultEl.innerHTML = '';
  statusEl.textContent = 'Checking server status...';

  const awake = await waitForServerAwake();
  if (!awake) {
    statusEl.textContent = 'The server did not wake up in time. Please wait a moment and try again.';
    submitBtn.disabled = false;
    return;
  }

  statusEl.textContent = 'Analyzing terrain... this can take 20\u201360 seconds, please wait.';

  const fd = new FormData();
  fd.append('file', fileInput.files[0]);
  // Use a lighter grid for the browser demo so the analysis reliably
  // finishes within the hosting platform's request time limits, even on
  // a slow/cold free-tier instance. The API itself still defaults to a
  // higher-resolution grid (160) when called directly (e.g. via curl).
  fd.append('grid_resolution', '100');

  try {
    const res = await fetch('/analyzeContour', { method: 'POST', body: fd });
    const rawText = await res.text();

    let data;
    try {
      data = JSON.parse(rawText);
    } catch (parseErr) {
      statusEl.textContent = 'The server returned an unexpected response (status ' + res.status + '). This can happen if the request took too long. Please try again.';
      resultEl.innerHTML = '<details id="raw"><summary>Raw server response</summary><pre>' + rawText.replace(/</g, '&lt;').slice(0, 2000) + '</pre></details>';
      submitBtn.disabled = false;
      return;
    }

    if (!res.ok || data.status === 'error') {
      statusEl.textContent = 'Error: ' + (data.message || res.statusText);
      submitBtn.disabled = false;
      return;
    }

    statusEl.textContent = 'Done.';
    const site = data.recommended_pond_site;
    const loc = site.pond_location;
    let html = '<table>';
    html += `<tr><th>Recommended pond location</th><td>${loc.lat.toFixed(5)}, ${loc.lon.toFixed(5)} (elev ${loc.elevation_m.toFixed(1)} m)</td></tr>`;
    html += `<tr><th>Catchment area</th><td>${site.catchment_area_hectares.toFixed(2)} ha (${site.catchment_area_m2.toFixed(0)} m&sup2;)</td></tr>`;
    html += `<tr><th>Mean catchment slope</th><td>${site.terrain_stats.mean_slope_percent.toFixed(1)} %</td></tr>`;
    html += `<tr><th>Catchment relief</th><td>${site.terrain_stats.relief_m.toFixed(1)} m</td></tr>`;
    if (site.pond_sizing_estimate) {
      html += `<tr><th>Pond surface area (2 m depth)</th><td>${site.pond_sizing_estimate.pond_surface_area_hectares.toFixed(2)} ha</td></tr>`;
      html += `<tr><th>Estimated storage volume</th><td>${site.pond_sizing_estimate.estimated_storage_volume_m3.toFixed(0)} m&sup3;</td></tr>`;
    }
    html += `<tr><th>Estimated annual inflow</th><td>${site.hydrology_estimate.estimated_annual_runoff_volume_m3.toFixed(0)} m&sup3;/yr</td></tr>`;
    html += `<tr><th>Alternative sites returned</th><td>${data.candidate_sites.length}</td></tr>`;
    html += '</table>';
    html += `<details id="raw"><summary>View full JSON response</summary><pre>${JSON.stringify(data, null, 2)}</pre></details>`;
    resultEl.innerHTML = html;
  } catch (err) {
    statusEl.textContent = 'Request failed: ' + err + '. This is often a network/timeout issue on slower connections \u2014 please try again.';
  }
  submitBtn.disabled = false;
});
</script>
</body>
</html>
"""


def _float_arg(form, key, default):
    val = form.get(key, None)
    return float(val) if val not in (None, "") else default


def _int_arg(form, key, default):
    val = form.get(key, None)
    return int(val) if val not in (None, "") else default


@app.route("/", methods=["GET"])
def index():
    return Response(INDEX_HTML, mimetype="text/html")


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


def _handle_analyze():
    if "file" not in request.files:
        return jsonify({
            "status": "error",
            "message": "No file uploaded. Send the contour map as multipart/form-data under the field name 'file'.",
        }), 400

    upload = request.files["file"]
    if upload.filename == "":
        return jsonify({"status": "error", "message": "Empty filename."}), 400

    ext = os.path.splitext(upload.filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify({
            "status": "error",
            "message": f"Unsupported file type '{ext}'. Expected one of {sorted(ALLOWED_EXTENSIONS)}.",
        }), 400

    tmp_dir = tempfile.mkdtemp(prefix="pondcatchment_")
    tmp_path = os.path.join(tmp_dir, upload.filename)
    upload.save(tmp_path)

    try:
        result = analyze(
            tmp_path,
            max_grid_dim=_int_arg(request.form, "grid_resolution", 160),
            num_candidates=_int_arg(request.form, "num_candidates", 3),
            pond_depth_m=_float_arg(request.form, "pond_depth_m", 2.0),
            runoff_coefficient=_float_arg(request.form, "runoff_coefficient", 0.35),
            annual_rainfall_mm=_float_arg(request.form, "annual_rainfall_mm", 1200.0),
            border_margin_frac=_float_arg(request.form, "border_margin_frac", 0.04),
            stream_percentile=_float_arg(request.form, "stream_percentile", 95.0),
        )
        return jsonify(result), 200
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 422
    except Exception as exc:  # pragma: no cover - defensive
        app.logger.error("Analysis failed: %s\n%s", exc, traceback.format_exc())
        return jsonify({"status": "error", "message": f"Internal analysis error: {exc}"}), 500
    finally:
        try:
            os.remove(tmp_path)
            os.rmdir(tmp_dir)
        except OSError:
            pass


@app.route("/analyzeContour", methods=["POST"])
def analyze_contour():
    return _handle_analyze()


@app.route("/findCatchment", methods=["POST"])
def find_catchment():
    return _handle_analyze()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
