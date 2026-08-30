"""
dem.py
------
Builds a regular-grid Digital Elevation Model (DEM) from scattered contour
vertices (lon, lat, elevation) extracted from a KML/KMZ file.

Steps:
 1. Project geographic coordinates (lon/lat, degrees) to a local
    equirectangular metric plane (x, y in metres) centred on the data's
    bounding box. This is accurate enough for the small (< tens of km)
    extents typical of a single-site contour survey, and keeps the whole
    pipeline dependency-light (no external projection database needed).
 2. Densify each contour polyline so long straight segments still
    contribute enough sample points to the interpolator.
 3. Interpolate a regular raster of elevations from the scattered points
    using linear (Delaunay-based) interpolation, falling back to
    nearest-neighbour to fill any cells outside the convex hull of the
    input points (e.g. near the map edges).
 4. Lightly smooth the raster to remove the "staircase" artefacts that
    linear interpolation between discrete contour bands otherwise produces
    (which would create false flat terraces and break flow routing).

The resulting grid is entirely derived from the input file: no coordinates
or elevations are hard-coded, so the module works for any contour map.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

import numpy as np
from scipy.interpolate import griddata
from scipy.ndimage import gaussian_filter

from .kml_parser import ContourLine

EARTH_RADIUS_M = 6_371_000.0


@dataclass
class DEM:
    Z: np.ndarray            # (rows, cols) elevation grid, metres
    lon0: float               # projection origin (deg)
    lat0: float
    x: np.ndarray             # 1D array of cell-centre x coords (m), len = cols
    y: np.ndarray             # 1D array of cell-centre y coords (m), len = rows
    cellsize_x_m: float
    cellsize_y_m: float
    nrows: int
    ncols: int

    # ---- coordinate helpers -------------------------------------------------
    def lonlat_to_xy(self, lon: float, lat: float) -> Tuple[float, float]:
        x = (lon - self.lon0) * np.cos(np.radians(self.lat0)) * np.pi / 180.0 * EARTH_RADIUS_M
        y = (lat - self.lat0) * np.pi / 180.0 * EARTH_RADIUS_M
        return x, y

    def rc_to_lonlat(self, row: int, col: int) -> Tuple[float, float]:
        x = self.x[col]
        y = self.y[row]
        lon = self.lon0 + (x / (np.cos(np.radians(self.lat0)) * np.pi / 180.0 * EARTH_RADIUS_M))
        lat = self.lat0 + (y / (np.pi / 180.0 * EARTH_RADIUS_M))
        return float(lon), float(lat)

    @property
    def cell_area_m2(self) -> float:
        return abs(self.cellsize_x_m * self.cellsize_y_m)


def _densify(points: List[Tuple[float, float]], max_seg_m: float, lat0: float):
    """Insert extra vertices so no segment (in metres) exceeds max_seg_m."""
    out = []
    coslat = np.cos(np.radians(lat0))
    for (lon1, lat1), (lon2, lat2) in zip(points[:-1], points[1:]):
        out.append((lon1, lat1))
        dx = (lon2 - lon1) * coslat * np.pi / 180.0 * EARTH_RADIUS_M
        dy = (lat2 - lat1) * np.pi / 180.0 * EARTH_RADIUS_M
        seg_len = float(np.hypot(dx, dy))
        n_extra = int(seg_len // max_seg_m)
        for i in range(1, n_extra + 1):
            t = i / (n_extra + 1)
            out.append((lon1 + t * (lon2 - lon1), lat1 + t * (lat2 - lat1)))
    out.append(points[-1])
    return out


def build_dem(
    contours: List[ContourLine],
    max_grid_dim: int = 160,
    smoothing_sigma: float = 1.0,
) -> DEM:
    """Build a DEM raster from parsed contour lines.

    Parameters
    ----------
    contours:        list of ContourLine (elevation + polyline vertices)
    max_grid_dim:     the longer raster dimension (rows or cols) is capped at
                       this many cells; the other dimension is scaled to
                       preserve the real-world aspect ratio. Controls the
                       resolution / performance trade-off.
    smoothing_sigma:  standard deviation (in cells) of the Gaussian filter
                       applied to remove contour-interpolation terracing.
    """
    all_lons = np.array([p[0] for c in contours for p in c.points])
    all_lats = np.array([p[1] for c in contours for p in c.points])
    lon0 = float((all_lons.min() + all_lons.max()) / 2.0)
    lat0 = float((all_lats.min() + all_lats.max()) / 2.0)

    # bounding box in metres, used to size the raster
    coslat = np.cos(np.radians(lat0))
    x_all = (all_lons - lon0) * coslat * np.pi / 180.0 * EARTH_RADIUS_M
    y_all = (all_lats - lat0) * np.pi / 180.0 * EARTH_RADIUS_M
    width_m = float(x_all.max() - x_all.min())
    height_m = float(y_all.max() - y_all.min())

    if width_m >= height_m:
        ncols = max_grid_dim
        nrows = max(8, int(round(max_grid_dim * height_m / width_m)))
    else:
        nrows = max_grid_dim
        ncols = max(8, int(round(max_grid_dim * width_m / height_m)))

    cellsize_x = width_m / ncols
    cellsize_y = height_m / nrows
    max_seg_m = 0.5 * min(cellsize_x, cellsize_y)

    # Gather (possibly densified) scatter points for interpolation
    xs, ys, zs = [], [], []
    for c in contours:
        pts = c.points if len(c.points) < 3 else _densify(c.points, max_seg_m, lat0)
        for lon, lat in pts:
            xs.append((lon - lon0) * coslat * np.pi / 180.0 * EARTH_RADIUS_M)
            ys.append((lat - lat0) * np.pi / 180.0 * EARTH_RADIUS_M)
            zs.append(c.elevation)
    xs = np.asarray(xs)
    ys = np.asarray(ys)
    zs = np.asarray(zs)

    x_edges = np.linspace(x_all.min(), x_all.max(), ncols)
    y_edges = np.linspace(y_all.min(), y_all.max(), nrows)
    grid_x, grid_y = np.meshgrid(x_edges, y_edges)  # shape (nrows, ncols)

    Z = griddata((xs, ys), zs, (grid_x, grid_y), method="linear")
    nan_mask = np.isnan(Z)
    if nan_mask.any():
        Z_nn = griddata((xs, ys), zs, (grid_x, grid_y), method="nearest")
        Z[nan_mask] = Z_nn[nan_mask]

    if smoothing_sigma > 0:
        Z = gaussian_filter(Z, sigma=smoothing_sigma)

    # row 0 should correspond to the northern-most (max lat) edge for
    # intuitive image-style indexing; flip if necessary.
    y_increasing_north = y_edges[-1] > y_edges[0]
    if y_increasing_north:
        Z = np.flipud(Z)
        y_edges = y_edges[::-1]

    return DEM(
        Z=Z,
        lon0=lon0,
        lat0=lat0,
        x=x_edges,
        y=y_edges,
        cellsize_x_m=cellsize_x,
        cellsize_y_m=abs(cellsize_y),
        nrows=nrows,
        ncols=ncols,
    )
