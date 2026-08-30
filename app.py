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
    POST /analyzeContour   (alias: POST /findCatchment)
    GET  /health
"""

from __future__ import annotations

import os
import tempfile
import traceback

from flask import Flask, jsonify, request

from pondcatchment import analyze

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 32 MB upload cap

ALLOWED_EXTENSIONS = {".kml", ".kmz"}


def _float_arg(form, key, default):
    val = form.get(key, None)
    return float(val) if val not in (None, "") else default


def _int_arg(form, key, default):
    val = form.get(key, None)
    return int(val) if val not in (None, "") else default


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
