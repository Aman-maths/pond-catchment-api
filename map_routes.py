"""
map_routes.py
-------------
Adds the "draw an area on a map" feature to the existing pond-catchment app.

Put this file next to app.py. In app.py add exactly two lines:

    from map_routes import bp
    app.register_blueprint(bp)

It adds:
    GET  /          the map page (map.html)
    GET  /region    the bounds of the mapped area
    POST /analyze   takes the drawn polygon, returns pond + catchment + volume

Settings (optional environment variables):
    REGION_FILE       contour KML to use   (default: sample_data/contours_1m.kml)
    MAX_SELECTION_KM2 biggest area allowed  (default 10)
    MAX_JOBS          analyses at once      (default 2)
    QUEUE_WAIT_S      seconds to wait for a free slot before answering 503 (default 30)
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
import threading

from flask import Blueprint, jsonify, request, send_from_directory
from shapely.geometry import LineString, Polygon
from shapely.strtree import STRtree

from pondcatchment import analyze, kml_parser

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REGION_FILE = os.environ.get(
    "REGION_FILE", os.path.join(BASE_DIR, "sample_data", "contours_1m.kml")
)
MAX_SELECTION_KM2 = float(os.environ.get("MAX_SELECTION_KM2", "10"))
MAX_JOBS = int(os.environ.get("MAX_JOBS", "2"))
QUEUE_WAIT_S = float(os.environ.get("QUEUE_WAIT_S", "30"))
CACHE_MAX = int(os.environ.get("CACHE_MAX", "50"))
DEFAULT_GRID = 120

bp = Blueprint("map_routes", __name__)

_region_lock = threading.Lock()
_region = None  # (geometries, elevations, spatial index, bbox)
_slots = threading.BoundedSemaphore(MAX_JOBS)
_cache = {}
_cache_lock = threading.Lock()


def _load_region():
    """Read the big contour file once, then keep it in memory."""
    global _region
    with _region_lock:
        if _region is None:
            contours = kml_parser.parse_contours(REGION_FILE)
            geoms = [LineString(c.points) for c in contours]
            elevs = [c.elevation for c in contours]
            bbox = kml_parser.contour_summary(contours)["bbox"]
            _region = (geoms, elevs, STRtree(geoms), bbox)
    return _region


def _lines_of(geom):
    if geom.is_empty:
        return []
    if geom.geom_type == "LineString":
        return [geom]
    if hasattr(geom, "geoms"):
        out = []
        for g in geom.geoms:
            out += _lines_of(g)
        return out
    return []


def _clip(poly):
    """Cut every contour line to the drawn polygon, keeping its elevation."""
    geoms, elevs, tree, _ = _load_region()
    pieces = []
    for i in tree.query(poly, predicate="intersects"):
        part = geoms[int(i)].intersection(poly)
        for line in _lines_of(part):
            coords = list(line.coords)
            if len(coords) >= 2:
                pieces.append((elevs[int(i)], coords))
    return pieces


def _write_kml(pieces, path):
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>']
    for elev, coords in pieces:
        txt = " ".join(f"{c[0]:.7f},{c[1]:.7f},0" for c in coords)
        out.append(
            f"<Placemark><name>{elev}</name><LineString>"
            f"<coordinates>{txt}</coordinates></LineString></Placemark>"
        )
    out.append("</Document></kml>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))


def _area_km2(poly):
    minx, miny, maxx, maxy = poly.bounds
    lat0 = (miny + maxy) / 2.0
    return poly.area * 111.32 * math.cos(math.radians(lat0)) * 110.57


def _num(body, key, default):
    v = body.get(key)
    try:
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


@bp.route("/", methods=["GET"])
def map_page():
    return send_from_directory(BASE_DIR, "map.html")


@bp.route("/region", methods=["GET"])
def region():
    try:
        bbox = _load_region()[3]
    except Exception as exc:
        return jsonify({"status": "error", "message": f"Cannot read region file: {exc}"}), 500
    return jsonify({"status": "ok", "bbox": bbox, "max_selection_km2": MAX_SELECTION_KM2})


@bp.route("/analyze", methods=["POST"])
def analyze_polygon():
    body = request.get_json(silent=True) or {}
    try:
        poly = Polygon([(float(p[0]), float(p[1])) for p in body.get("polygon", [])])
    except Exception:
        return jsonify({"status": "error", "message": "Send a polygon as [[lon, lat], ...]."}), 400
    if not poly.is_valid:
        poly = poly.buffer(0)
    if poly.geom_type != "Polygon":
        poly = max(poly.geoms, key=lambda g: g.area)
    if poly.is_empty or poly.area == 0:
        return jsonify({"status": "error", "message": "The drawn area is empty."}), 400

    km2 = _area_km2(poly)
    if km2 > MAX_SELECTION_KM2:
        return jsonify({
            "status": "error",
            "message": f"Selected area is {km2:.1f} km2. The limit is {MAX_SELECTION_KM2:g} km2. Please draw a smaller area.",
        }), 422

    grid = int(_num(body, "grid_resolution", DEFAULT_GRID))
    rainfall = _num(body, "annual_rainfall_mm", 1200.0)
    runoff = _num(body, "runoff_coefficient", 0.35)
    depth = _num(body, "pond_depth_m", 2.0)

    key = hashlib.sha1(json.dumps(
        [[round(x, 6), round(y, 6)] for x, y in poly.exterior.coords]
        + [grid, rainfall, runoff, depth]).encode()).hexdigest()
    with _cache_lock:
        hit = _cache.get(key)
    if hit is not None:
        return jsonify({**hit, "cached": True}), 200

    if not _slots.acquire(timeout=QUEUE_WAIT_S):
        return jsonify({"status": "error", "message": "Server is busy. Please try again in a moment."}), 503

    tmp_dir = tempfile.mkdtemp(prefix="pond_sel_")
    try:
        pieces = _clip(poly)
        if len(pieces) < 3 or len({e for e, _ in pieces}) < 2:
            return jsonify({
                "status": "error",
                "message": "Too few contour lines inside your selection. Draw a bigger area inside the red box.",
            }), 422
        kml_path = os.path.join(tmp_dir, "selection.kml")
        _write_kml(pieces, kml_path)

        result = analyze(
            kml_path,
            max_grid_dim=grid,
            num_candidates=3,
            pond_depth_m=depth,
            runoff_coefficient=runoff,
            annual_rainfall_mm=rainfall,
        )

        site = result["recommended_pond_site"]
        inflow = site["hydrology_estimate"]["estimated_annual_runoff_volume_m3"]
        storage = site["pond_sizing_estimate"]["estimated_storage_volume_m3"]
        result["water_volume_summary"] = {
            "annual_runoff_m3": inflow,
            "pond_storage_m3": storage,
            "collectable_m3": min(inflow, storage),
        }
        result["selection"] = {"area_km2": round(km2, 3), "contour_pieces": len(pieces)}

        with _cache_lock:
            if len(_cache) >= CACHE_MAX:
                _cache.pop(next(iter(_cache)))
            _cache[key] = result
        return jsonify({**result, "cached": False}), 200
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 422
    except Exception as exc:
        return jsonify({"status": "error", "message": f"Internal analysis error: {exc}"}), 500
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        _slots.release()
