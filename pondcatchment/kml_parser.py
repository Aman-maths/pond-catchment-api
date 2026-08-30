"""
kml_parser.py
-------------
Generic KML / KMZ contour parser.

Extracts every LineString placemark that represents a terrain contour and
returns its elevation (parsed from the placemark <name>, an ExtendedData
field, or the <coordinates> Z value) together with its (lon, lat) vertices.

This module makes NO assumption about a specific map: it works on the
placemark structure only (folder names, styling colours, feature counts are
all ignored), so it generalises to any KML/KMZ contour export from tools
such as QGIS, Google Earth, Global Mapper, Contour Map Generator, etc.
"""

from __future__ import annotations

import io
import os
import re
import zipfile
from dataclasses import dataclass
from typing import List, Optional, Tuple

from lxml import etree

Point = Tuple[float, float]  # (lon, lat)


@dataclass
class ContourLine:
    elevation: float
    points: List[Point]


_NUM_RE = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def _local_findall(elem, tag: str):
    """Namespace-agnostic recursive find of all descendants with local tag."""
    return [e for e in elem.iter() if _strip_ns(e.tag) == tag]


def _local_find(elem, tag: str):
    for e in elem.iter():
        if e is elem:
            continue
        if _strip_ns(e.tag) == tag:
            return e
    return None


def _parse_coordinates(text: str) -> List[Point]:
    pts: List[Point] = []
    if not text:
        return pts
    for tok in text.split():
        parts = tok.split(",")
        if len(parts) >= 2:
            try:
                lon, lat = float(parts[0]), float(parts[1])
                pts.append((lon, lat))
            except ValueError:
                continue
    return pts


def _extract_elevation(placemark) -> Optional[float]:
    """Try several strategies to obtain the numeric elevation of a contour
    placemark, in order of reliability."""

    # 1. <name> text, e.g. "277.0"
    name_el = _local_find(placemark, "name")
    if name_el is not None and name_el.text:
        m = _NUM_RE.search(name_el.text.strip())
        if m:
            try:
                return float(m.group())
            except ValueError:
                pass

    # 2. ExtendedData / SimpleData fields that look like an elevation
    for sd in _local_findall(placemark, "SimpleData"):
        key = sd.get("name", "").lower()
        if key in ("elev", "elevation", "height", "z", "contour", "value"):
            try:
                return float(sd.text)
            except (TypeError, ValueError):
                continue

    # 3. <description> text
    desc_el = _local_find(placemark, "description")
    if desc_el is not None and desc_el.text:
        m = _NUM_RE.search(desc_el.text)
        if m:
            try:
                return float(m.group())
            except ValueError:
                pass

    return None


def _load_kml_bytes(path: str) -> bytes:
    """Return raw KML bytes, transparently unzipping KMZ archives."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".kmz":
        with zipfile.ZipFile(path) as zf:
            # KMZ spec: the main doc is usually doc.kml, but fall back to the
            # first .kml entry found.
            names = zf.namelist()
            kml_name = next((n for n in names if n.lower() == "doc.kml"), None)
            if kml_name is None:
                kml_name = next((n for n in names if n.lower().endswith(".kml")), None)
            if kml_name is None:
                raise ValueError("No .kml file found inside the KMZ archive")
            return zf.read(kml_name)
    with open(path, "rb") as f:
        return f.read()


def parse_contours(path: str) -> List[ContourLine]:
    """Parse a KML/KMZ file and return every contour LineString found.

    Placemarks without a LineString geometry (e.g. label Points) are
    skipped.  Placemarks whose elevation cannot be determined are skipped
    with the assumption they are non-contour annotation features.
    """
    raw = _load_kml_bytes(path)
    parser = etree.XMLParser(recover=True, huge_tree=True)
    root = etree.fromstring(raw, parser=parser)

    lines: List[ContourLine] = []
    for placemark in _local_findall(root, "Placemark"):
        linestring = _local_find(placemark, "LineString")
        if linestring is None:
            continue  # skip Points / Polygons (e.g. label placemarks)

        coord_el = _local_find(linestring, "coordinates")
        if coord_el is None or not coord_el.text:
            continue

        pts = _parse_coordinates(coord_el.text)
        if len(pts) < 2:
            continue

        elev = _extract_elevation(placemark)
        if elev is None:
            continue

        lines.append(ContourLine(elevation=elev, points=pts))

    if not lines:
        raise ValueError(
            "No contour LineStrings with a parseable elevation were found "
            "in the supplied file."
        )
    return lines


def contour_summary(lines: List[ContourLine]) -> dict:
    elevs = [l.elevation for l in lines]
    lons = [p[0] for l in lines for p in l.points]
    lats = [p[1] for l in lines for p in l.points]
    return {
        "num_contour_lines": len(lines),
        "elevation_min": min(elevs),
        "elevation_max": max(elevs),
        "contour_interval_estimate": _estimate_interval(elevs),
        "bbox": {
            "min_lon": min(lons),
            "max_lon": max(lons),
            "min_lat": min(lats),
            "max_lat": max(lats),
        },
    }


def _estimate_interval(elevs: List[float]) -> float:
    uniq = sorted(set(round(e, 3) for e in elevs))
    if len(uniq) < 2:
        return 0.0
    diffs = [round(b - a, 3) for a, b in zip(uniq[:-1], uniq[1:])]
    diffs = [d for d in diffs if d > 0]
    return min(diffs) if diffs else 0.0
