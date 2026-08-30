"""
hydrology.py
------------
Classic raster hydrology primitives used to turn a DEM into flow routing
information:

  1. fill_sinks       - Priority-Flood depression filling (Barnes et al. 2014)
                         so every interior cell has a downhill path to the
                         raster edge.
  2. flow_direction    - D8 steepest-descent flow direction.
  3. flow_accumulation - number of upstream cells (contributing area, in
                         cell units) draining through every cell, computed
                         by processing cells from highest to lowest filled
                         elevation (a valid topological order once sinks are
                         filled).

These are generic raster algorithms - nothing here is specific to the
sample map; they operate on any (nrows, ncols) elevation array.
"""

from __future__ import annotations

import heapq
from typing import Tuple

import numpy as np

# 8-connected neighbourhood: (drow, dcol) and the D8 direction code (1..8,
# ArcGIS-style powers of two are not needed, we just need internal
# consistency between flow_direction and flow_accumulation / catchment).
NEIGHBORS = [
    (-1, -1), (-1, 0), (-1, 1),
    (0, -1),           (0, 1),
    (1, -1),  (1, 0),  (1, 1),
]


def fill_sinks(Z: np.ndarray) -> np.ndarray:
    """Priority-Flood depression filling.

    Returns a new array where every interior cell's elevation has been
    raised, if necessary, to the lowest level at which a monotonically
    non-increasing path to the raster boundary exists. Guarantees flow can
    always be routed to the edge (no unresolved internal pits).
    """
    nrows, ncols = Z.shape
    filled = Z.copy().astype(float)
    closed = np.zeros_like(Z, dtype=bool)

    heap = []
    counter = 0
    # seed the heap with every boundary cell
    for r in range(nrows):
        for c in (0, ncols - 1):
            heapq.heappush(heap, (filled[r, c], counter, r, c))
            counter += 1
            closed[r, c] = True
    for c in range(ncols):
        for r in (0, nrows - 1):
            if not closed[r, c]:
                heapq.heappush(heap, (filled[r, c], counter, r, c))
                counter += 1
                closed[r, c] = True

    while heap:
        elev, _, r, c = heapq.heappop(heap)
        for dr, dc in NEIGHBORS:
            nr, nc = r + dr, c + dc
            if 0 <= nr < nrows and 0 <= nc < ncols and not closed[nr, nc]:
                closed[nr, nc] = True
                if filled[nr, nc] < elev:
                    filled[nr, nc] = elev
                heapq.heappush(heap, (filled[nr, nc], counter, nr, nc))
                counter += 1
    return filled


def flow_direction(Z: np.ndarray, cellsize_x_m: float, cellsize_y_m: float) -> np.ndarray:
    """D8 steepest-descent flow direction.

    Returns an (nrows, ncols) int array where each cell holds the index
    (0..7) into NEIGHBORS pointing to the downhill neighbour it drains to,
    or -1 if the cell is a raster-edge outlet (no downhill neighbour, i.e.
    flow leaves the modelled area).
    """
    nrows, ncols = Z.shape
    fdir = np.full((nrows, ncols), -1, dtype=int)

    for r in range(nrows):
        for c in range(ncols):
            best_slope = 0.0
            best_k = -1
            for k, (dr, dc) in enumerate(NEIGHBORS):
                nr, nc = r + dr, c + dc
                if 0 <= nr < nrows and 0 <= nc < ncols:
                    dist = cellsize_x_m if dc != 0 and dr == 0 else (
                        cellsize_y_m if dr != 0 and dc == 0 else
                        (cellsize_x_m ** 2 + cellsize_y_m ** 2) ** 0.5
                    )
                    drop = Z[r, c] - Z[nr, nc]
                    slope = drop / dist
                    if slope > best_slope:
                        best_slope = slope
                        best_k = k
            fdir[r, c] = best_k
    return fdir


def downstream_cell(r: int, c: int, k: int) -> Tuple[int, int]:
    dr, dc = NEIGHBORS[k]
    return r + dr, c + dc


def flow_accumulation(Z_filled: np.ndarray, fdir: np.ndarray) -> np.ndarray:
    """Number of upstream cells (including itself) draining through each
    cell, computed by processing cells from highest to lowest elevation.
    Because Z_filled has no internal sinks, elevation-descending order is a
    valid topological order of the flow graph.
    """
    nrows, ncols = Z_filled.shape
    acc = np.ones((nrows, ncols), dtype=np.float64)

    order = np.dstack(np.unravel_index(
        np.argsort(-Z_filled.ravel(), kind="stable"), Z_filled.shape
    ))[0]

    for r, c in order:
        k = fdir[r, c]
        if k == -1:
            continue
        nr, nc = downstream_cell(r, c, k)
        if 0 <= nr < nrows and 0 <= nc < ncols:
            acc[nr, nc] += acc[r, c]
    return acc


def slope_grid(Z: np.ndarray, cellsize_x_m: float, cellsize_y_m: float) -> np.ndarray:
    """Simple percent-slope raster via central differences (for reporting /
    site-suitability, not used for routing)."""
    gy, gx = np.gradient(Z, cellsize_y_m, cellsize_x_m)
    return np.sqrt(gx ** 2 + gy ** 2) * 100.0
