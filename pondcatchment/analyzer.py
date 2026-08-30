"""
analyzer.py
-----------
Top-level orchestration: contour file -> DEM -> hydrology -> pond siting ->
catchment delineation -> structured result dictionary (JSON-serialisable).

This is the single entry point the Flask route (and any future batch /
CLI / notebook usage) should call, so the pipeline stays reusable and
generalises cleanly to other contour maps in later phases.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

import numpy as np

from . import kml_parser
from .catchment import (
    Candidate,
    _build_upstream_lists,
    bathtub_reservoir,
    catchment_polygon,
    delineate_catchment,
    find_candidate_sites,
)
from .dem import build_dem
from .hydrology import fill_sinks, flow_accumulation, flow_direction, slope_grid


def _candidate_to_dict(cand: Candidate, dem, rank: int) -> Dict[str, Any]:
    return {
        "rank": rank,
        "pond_location": {"lon": cand.lon, "lat": cand.lat, "elevation_m": cand.elevation},
        "catchment_area_m2": cand.catchment_area_m2,
        "catchment_area_hectares": cand.catchment_area_m2 / 10_000.0,
        "catchment_area_acres": cand.catchment_area_m2 / 4046.8564224,
        "contributing_cells": cand.accumulation_cells,
    }


def analyze(
    file_path: str,
    max_grid_dim: int = 160,
    num_candidates: int = 3,
    pond_depth_m: float = 2.0,
    runoff_coefficient: float = 0.35,
    annual_rainfall_mm: float = 1200.0,
    border_margin_frac: float = 0.04,
    stream_percentile: float = 95.0,
) -> Dict[str, Any]:
    t0 = time.time()

    # 1. Parse contours -----------------------------------------------------
    contours = kml_parser.parse_contours(file_path)
    contour_info = kml_parser.contour_summary(contours)

    # 2. Build DEM ------------------------------------------------------------
    dem = build_dem(contours, max_grid_dim=max_grid_dim)

    # 3. Hydrology --------------------------------------------------------
    Z_filled = fill_sinks(dem.Z)
    fdir = flow_direction(Z_filled, dem.cellsize_x_m, dem.cellsize_y_m)
    acc = flow_accumulation(Z_filled, fdir)
    slope = slope_grid(dem.Z, dem.cellsize_x_m, dem.cellsize_y_m)
    upstream = _build_upstream_lists(fdir)

    # 4. Candidate pond sites -----------------------------------------------
    candidates = find_candidate_sites(
        dem, acc, fdir, upstream,
        num_candidates=num_candidates,
        border_margin_frac=border_margin_frac,
        stream_percentile=stream_percentile,
    )

    if not candidates:
        raise ValueError("Could not identify a viable pond/catchment site on this terrain.")

    result_candidates = []
    primary_full: Optional[Dict[str, Any]] = None

    for i, cand in enumerate(candidates):
        mask = delineate_catchment(upstream, cand.row, cand.col, dem.nrows, dem.ncols)
        boundary = catchment_polygon(mask, dem)

        elevs_in_catchment = dem.Z[mask]
        slopes_in_catchment = slope[mask]

        annual_runoff_volume_m3 = (
            runoff_coefficient * (annual_rainfall_mm / 1000.0) * cand.catchment_area_m2
        )

        entry = _candidate_to_dict(cand, dem, rank=i + 1)
        entry.update({
            "catchment_boundary_lonlat": boundary,
            "terrain_stats": {
                "min_elevation_m": float(elevs_in_catchment.min()),
                "max_elevation_m": float(elevs_in_catchment.max()),
                "mean_elevation_m": float(elevs_in_catchment.mean()),
                "relief_m": float(elevs_in_catchment.max() - elevs_in_catchment.min()),
                "mean_slope_percent": float(slopes_in_catchment.mean()),
            },
            "hydrology_estimate": {
                "runoff_coefficient_used": runoff_coefficient,
                "annual_rainfall_mm_used": annual_rainfall_mm,
                "estimated_annual_runoff_volume_m3": annual_runoff_volume_m3,
                "note": (
                    "Runoff volume uses the simplified formula "
                    "V = C * P * A with a generic default runoff "
                    "coefficient and rainfall depth; pass "
                    "runoff_coefficient / annual_rainfall_mm from local "
                    "hydrological data for a site-accurate figure."
                ),
            },
        })

        if i == 0:
            reservoir = bathtub_reservoir(dem, cand.row, cand.col, pond_depth_m, catchment_mask=mask)
            entry["pond_sizing_estimate"] = reservoir
            primary_full = entry

        result_candidates.append(entry)

    elapsed = time.time() - t0

    return {
        "status": "ok",
        "input_summary": contour_info,
        "dem_grid": {
            "rows": dem.nrows,
            "cols": dem.ncols,
            "cellsize_x_m": dem.cellsize_x_m,
            "cellsize_y_m": dem.cellsize_y_m,
        },
        "parameters_used": {
            "max_grid_dim": max_grid_dim,
            "num_candidates_requested": num_candidates,
            "pond_depth_m": pond_depth_m,
            "runoff_coefficient": runoff_coefficient,
            "annual_rainfall_mm": annual_rainfall_mm,
            "border_margin_frac": border_margin_frac,
            "stream_percentile": stream_percentile,
        },
        "recommended_pond_site": primary_full,
        "candidate_sites": result_candidates,
        "processing_time_seconds": round(elapsed, 2),
    }
