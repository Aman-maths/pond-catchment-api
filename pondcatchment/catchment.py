"""
catchment.py
------------
Turns flow-routing rasters into pond-siting and catchment information:

  * find_candidate_sites   - locate promising pour points (high upstream
                              contributing area, away from the raster edge,
                              mutually well separated) along the derived
                              stream network.
  * delineate_catchment    - reverse-trace the D8 flow-direction graph from
                              a pour point to obtain the full contributing
                              (catchment) cell mask.
  * catchment_polygon      - vectorise a catchment cell mask into a
                              simplified (lon, lat) boundary polygon.
  * bathtub_reservoir       - simple "bathtub" flood-fill from the pour
                              point to estimate pond submergence area and
                              storage volume for a given dam/bund height.

None of the thresholds below reference any specific place; they are generic
raster-analysis parameters with sensible defaults, all overridable via the
API so the same code generalises to other contour maps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np
from skimage import measure

from .dem import DEM
from .hydrology import NEIGHBORS, downstream_cell


@dataclass
class Candidate:
    row: int
    col: int
    lon: float
    lat: float
    elevation: float
    accumulation_cells: float
    catchment_area_m2: float


def _build_upstream_lists(fdir: np.ndarray):
    """Invert the D8 flow-direction graph: for every cell, list the cells
    that flow directly into it."""
    nrows, ncols = fdir.shape
    upstream = [[[] for _ in range(ncols)] for _ in range(nrows)]
    for r in range(nrows):
        for c in range(ncols):
            k = fdir[r, c]
            if k == -1:
                continue
            nr, nc = downstream_cell(r, c, k)
            if 0 <= nr < nrows and 0 <= nc < ncols:
                upstream[nr][nc].append((r, c))
    return upstream


def delineate_catchment(upstream, pour_row: int, pour_col: int, nrows: int, ncols: int) -> np.ndarray:
    """Breadth-first reverse trace from the pour point to find every cell
    that drains into it. Returns a boolean mask, shape (nrows, ncols)."""
    mask = np.zeros((nrows, ncols), dtype=bool)
    stack = [(pour_row, pour_col)]
    mask[pour_row, pour_col] = True
    while stack:
        r, c = stack.pop()
        for ur, uc in upstream[r][c]:
            if not mask[ur, uc]:
                mask[ur, uc] = True
                stack.append((ur, uc))
    return mask


def find_candidate_sites(
    dem: DEM,
    acc: np.ndarray,
    fdir: np.ndarray,
    upstream,
    num_candidates: int = 3,
    border_margin_frac: float = 0.04,
    stream_percentile: float = 95.0,
    min_separation_frac: float = 0.12,
    min_catchment_cells: int = 25,
) -> List[Candidate]:
    """Identify the most promising pond pour-point locations.

    Approach: build the derived stream network (cells whose upstream
    contributing area exceeds `stream_percentile`), discard cells too close
    to the raster border (their catchment would be artificially truncated
    by the extent of the survey, not by real topography), then greedily
    pick the highest-accumulation remaining cells enforcing a minimum
    separation so nearby duplicate points on the same stream are not all
    reported.
    """
    nrows, ncols = acc.shape
    margin_r = max(1, int(round(nrows * border_margin_frac)))
    margin_c = max(1, int(round(ncols * border_margin_frac)))

    valid = np.zeros_like(acc, dtype=bool)
    valid[margin_r: nrows - margin_r, margin_c: ncols - margin_c] = True

    threshold = np.percentile(acc, stream_percentile)
    stream_mask = (acc >= threshold) & valid & (acc >= min_catchment_cells)

    candidates_idx = np.argwhere(stream_mask)
    if candidates_idx.size == 0:
        # relax: just take the highest-accumulation interior cells
        candidates_idx = np.argwhere(valid)

    scores = [acc[r, c] for r, c in candidates_idx]
    order = np.argsort(-np.array(scores))

    min_sep = min_separation_frac * max(nrows, ncols)
    chosen: List[Tuple[int, int]] = []
    for idx in order:
        r, c = candidates_idx[idx]
        if all((r - cr) ** 2 + (c - cc) ** 2 >= min_sep ** 2 for cr, cc in chosen):
            chosen.append((r, c))
        if len(chosen) >= num_candidates:
            break

    results: List[Candidate] = []
    for r, c in chosen:
        mask = delineate_catchment(upstream, r, c, nrows, ncols)
        area_m2 = float(mask.sum()) * dem.cell_area_m2
        lon, lat = dem.rc_to_lonlat(int(r), int(c))
        results.append(
            Candidate(
                row=int(r), col=int(c), lon=lon, lat=lat,
                elevation=float(dem.Z[r, c]),
                accumulation_cells=float(acc[r, c]),
                catchment_area_m2=area_m2,
            )
        )
    results.sort(key=lambda cand: cand.catchment_area_m2, reverse=True)
    return results


def catchment_polygon(mask: np.ndarray, dem: DEM, tolerance_cells: float = 1.0):
    """Vectorise a boolean catchment mask into a simplified list of
    (lon, lat) boundary vertices using marching squares."""
    if mask.sum() == 0:
        return []
    padded = np.pad(mask.astype(float), 1, mode="constant", constant_values=0)
    contours = measure.find_contours(padded, level=0.5)
    if not contours:
        return []
    largest = max(contours, key=len)

    try:
        from shapely.geometry import Polygon

        poly = Polygon([(pt[1] - 1, pt[0] - 1) for pt in largest])
        if not poly.is_valid:
            poly = poly.buffer(0)
        simplified = poly.simplify(tolerance_cells, preserve_topology=True)
        coords = list(simplified.exterior.coords)
    except Exception:
        coords = [(pt[1] - 1, pt[0] - 1) for pt in largest[::max(1, len(largest) // 200)]]

    boundary = []
    for col, row in coords:
        row_c = min(max(row, 0), dem.nrows - 1)
        col_c = min(max(col, 0), dem.ncols - 1)
        lon, lat = dem.rc_to_lonlat(int(round(row_c)), int(round(col_c)))
        boundary.append([lon, lat])
    return boundary


def bathtub_reservoir(dem: DEM, pour_row: int, pour_col: int, pond_depth_m: float,
                       catchment_mask: Optional[np.ndarray] = None):
    """Estimate pond submergence area and storage volume by flood-filling
    outward from the pour point up to (pour_elevation + pond_depth_m),
    constrained to stay within the delineated catchment (if provided) so
    the flood does not leak over an unrelated ridge on a coarse grid.
    """
    nrows, ncols = dem.Z.shape
    water_level = dem.Z[pour_row, pour_col] + pond_depth_m

    visited = np.zeros((nrows, ncols), dtype=bool)
    stack = [(pour_row, pour_col)]
    visited[pour_row, pour_col] = True
    cells = []

    while stack:
        r, c = stack.pop()
        cells.append((r, c))
        for dr, dc in NEIGHBORS:
            nr, nc = r + dr, c + dc
            if 0 <= nr < nrows and 0 <= nc < ncols and not visited[nr, nc]:
                if catchment_mask is not None and not catchment_mask[nr, nc]:
                    continue
                if dem.Z[nr, nc] <= water_level:
                    visited[nr, nc] = True
                    stack.append((nr, nc))

    area_m2 = len(cells) * dem.cell_area_m2
    volume_m3 = sum(max(0.0, water_level - dem.Z[r, c]) for r, c in cells) * dem.cell_area_m2
    mean_depth = volume_m3 / area_m2 if area_m2 > 0 else 0.0

    boundary = catchment_polygon(visited, dem, tolerance_cells=0.75)
    return {
        "pond_surface_area_m2": area_m2,
        "pond_surface_area_hectares": area_m2 / 10_000.0,
        "estimated_storage_volume_m3": volume_m3,
        "mean_water_depth_m": mean_depth,
        "assumed_bund_water_level_m": float(water_level),
        "pond_boundary_lonlat": boundary,
    }
