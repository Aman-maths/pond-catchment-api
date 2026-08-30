# Pond Catchment Analysis API

A backend API that takes a **contour map (KML/KMZ)**, reconstructs the
terrain, identifies a suitable **pond location**, and returns the
**catchment (watershed) information** needed for pond planning — catchment
boundary & area, terrain statistics, and a simple reservoir sizing
estimate.

Built for a mid-semester assignment; designed so **Phase 2** can drop in a
different contour map with no code changes.

---

## 1. Quick start

```bash
git clone <this-repo-url>
cd pond-catchment-api
pip install -r requirements.txt

python app.py
# Server starts on http://localhost:5000
```

Test it with the bundled sample map:

```bash
curl -X POST http://localhost:5000/analyzeContour \
  -F "file=@sample_data/contours_1m.kml"
```

Or run the pipeline directly (no server) and generate the demo artifacts:

```bash
python demo.py
# writes output/sample_output.json and output/demo_visualization.png
```

Run the tests:

```bash
python tests/test_pipeline.py
```

---

## 2. API documentation

### `POST /analyzeContour`  (alias: `POST /findCatchment`)

Accepts a contour map and returns catchment information for pond planning.

**Request** — `multipart/form-data`

| Field                 | Type   | Required | Default | Description |
|-----------------------|--------|----------|---------|-------------|
| `file`                 | file   | yes      | —       | `.kml` or `.kmz` contour map |
| `grid_resolution`      | int    | no       | 160     | Longer dimension (cells) of the interpolated DEM raster. Higher = more detail, slower. |
| `num_candidates`       | int    | no       | 3       | Number of alternative pond sites to return |
| `pond_depth_m`         | float  | no       | 2.0     | Assumed bund/dam water depth used for the reservoir sizing estimate |
| `runoff_coefficient`   | float  | no       | 0.35    | Rational-method runoff coefficient (land-cover dependent) |
| `annual_rainfall_mm`   | float  | no       | 1200.0  | Annual rainfall depth used for the runoff-volume estimate |
| `border_margin_frac`   | float  | no       | 0.04    | Fraction of the raster excluded near the edges when searching for pond sites (avoids picking the trivial map-edge outlet) |
| `stream_percentile`    | float  | no       | 95.0    | Percentile of flow accumulation used to define the derived stream network |

**Response** — `200 OK`, `application/json`

```json
{
  "status": "ok",
  "input_summary": {
    "num_contour_lines": 1355,
    "elevation_min": 267.0,
    "elevation_max": 298.0,
    "contour_interval_estimate": 1.0,
    "bbox": { "min_lon": 81.28, "max_lon": 81.31, "min_lat": 21.24, "max_lat": 21.26 }
  },
  "dem_grid": { "rows": 131, "cols": 160, "cellsize_x_m": 20.2, "cellsize_y_m": 20.2 },
  "parameters_used": { "...": "..." },
  "recommended_pond_site": {
    "rank": 1,
    "pond_location": { "lon": 81.29, "lat": 21.25, "elevation_m": 276.2 },
    "catchment_area_m2": 66926.0,
    "catchment_area_hectares": 6.69,
    "catchment_area_acres": 16.54,
    "contributing_cells": 164.0,
    "catchment_boundary_lonlat": [[81.29, 21.25], "..."],
    "terrain_stats": {
      "min_elevation_m": 276.2, "max_elevation_m": 288.0,
      "mean_elevation_m": 281.9, "relief_m": 11.8, "mean_slope_percent": 5.0
    },
    "hydrology_estimate": {
      "runoff_coefficient_used": 0.35, "annual_rainfall_mm_used": 1200.0,
      "estimated_annual_runoff_volume_m3": 28109.0, "note": "..."
    },
    "pond_sizing_estimate": {
      "pond_surface_area_m2": 10202.0, "pond_surface_area_hectares": 1.02,
      "estimated_storage_volume_m3": 11346.0, "mean_water_depth_m": 1.11,
      "assumed_bund_water_level_m": 278.2,
      "pond_boundary_lonlat": [[81.29, 21.25], "..."]
    }
  },
  "candidate_sites": [ "... up to num_candidates entries, same shape as above ..." ],
  "processing_time_seconds": 3.7
}
```

| Error                        | Status | Cause |
|-------------------------------|--------|-------|
| No file uploaded              | 400    | `file` field missing |
| Unsupported file type         | 400    | Extension is not `.kml`/`.kmz` |
| No contour lines parseable    | 422    | File has no LineString placemarks with a usable elevation |
| Internal analysis error       | 500    | Unexpected failure (see server logs) |

### `GET /health`
Liveness check, returns `{"status": "ok"}`.

---

## 3. Catchment estimation approach

```
KML/KMZ  ──▶  parse contours   ──▶  interpolate DEM  ──▶  fill sinks
(lon,lat,elev)     (lines)           (regular grid)      (Priority-Flood)
                                                                │
                                                                ▼
                                                        D8 flow direction
                                                                │
                                                                ▼
                                                        flow accumulation
                                                                │
                                                                ▼
                                             candidate pour points (pond sites)
                                                                │
                                                                ▼
                                        reverse-trace flow graph → catchment mask
                                                                │
                                                                ▼
                                polygon extraction + terrain stats + reservoir sizing
```

1. **Parsing** (`pondcatchment/kml_parser.py`) — every `Placemark` with a
   `LineString` is treated as one contour; its elevation is read from the
   placemark name / ExtendedData / description (whichever is present). This
   makes the parser tolerant of different KML export styles, not just the
   sample file's structure.

2. **DEM construction** (`dem.py`) — contour vertices are projected to a
   local metric (equirectangular) plane, densified along long segments, and
   interpolated onto a regular raster with `scipy.interpolate.griddata`
   (linear, with nearest-neighbour fallback at the edges). A light Gaussian
   filter removes the "terracing" artefacts that linear interpolation
   between discrete elevation bands otherwise creates — important because
   raw terracing would create thousands of false flat cells and break flow
   routing.

3. **Hydrological conditioning & routing** (`hydrology.py`)
   - **Sink filling** — Priority-Flood algorithm (Barnes et al., 2014) so
     every cell has a monotonic downhill path to the raster boundary.
   - **Flow direction** — standard D8 steepest-descent to one of 8
     neighbours.
   - **Flow accumulation** — cells are processed from highest to lowest
     (filled) elevation, passing their accumulated upstream cell-count to
     their downstream neighbour. This is the standard raster proxy for
     upstream contributing (catchment) area.

4. **Pond site selection** (`catchment.py: find_candidate_sites`) — the
   derived stream network is the set of cells whose accumulation exceeds a
   high percentile (default 95th). Cells within a border margin of the
   raster are excluded (their catchment would otherwise be artificially
   truncated by the extent of the survey rather than by real topography).
   The remaining stream cells are ranked by contributing area and picked
   greedily with a minimum-separation constraint, giving several
   independent, well-spread candidate sites rather than many near-duplicate
   points on the same stream.

5. **Catchment delineation** — for a chosen pour point, the D8 flow-direction
   graph is inverted and reverse breadth-first-searched from the pour point,
   collecting every cell that (directly or indirectly) drains into it. This
   cell mask **is** the watershed / catchment. `catchment_area = num_cells ×
   cell_area`. The mask is vectorised into a simplified boundary polygon
   with marching squares (`skimage.measure.find_contours`) and converted
   back to (lon, lat).

6. **Pond sizing (reservoir estimate)** — a "bathtub" flood-fill from the
   pour point up to `pour_elevation + pond_depth_m` (constrained to the
   catchment) estimates the submerged surface area and storage volume —
   `volume = Σ (water_level − cell_elevation) × cell_area` over the flooded
   cells. This is a first-order estimate suitable for planning, not a
   substitute for detailed cross-section survey / reservoir capacity curve.

7. **Runoff estimate** — a simple rational-method figure,
   `V = C × P × A`, is reported alongside the catchment (runoff coefficient
   `C` and annual rainfall `P` are request parameters with generic
   defaults) so the catchment area is put in usable hydrological context
   without hard-coding any location-specific rainfall data.

### Why this generalises to Phase 2
No coordinate, elevation, filename, or result value from the sample map
appears anywhere in the code. Every number used (bounding box, elevation
range, contour interval, raster size, projection origin, thresholds) is
**derived at request time** from whatever file is uploaded. Swapping in a
different KML/KMZ contour map — different location, extent, elevation
range, or contour interval — requires no code changes; only the tunable
parameters in the table above may need adjusting via the API request
(e.g. a much larger area may warrant a higher `grid_resolution`).

### Known limitations (by design, for this phase)
- The DEM is interpolated purely from contour lines; a source LiDAR/photogrammetry
  DEM (if available in a later phase) would remove interpolation artefacts entirely — the
  pipeline's `build_dem()` step could be swapped for a raster loader with the
  rest of the pipeline unchanged.
- Pond-site suitability currently uses flow accumulation + a border margin.
  A future phase could add slope, land-use/soil, and proximity-to-access
  constraints, plugged in as extra filters inside `find_candidate_sites()`.
- The projection is a local equirectangular approximation, adequate for
  single-site surveys of a few kilometres; very large-extent maps would
  benefit from a proper UTM projection (`pyproj`), which the `DEM` class's
  `lonlat_to_xy` / `rc_to_lonlat` methods are structured to make a
  drop-in swap.

---

## 4. Demonstration on the sample map (`sample_data/contours_1m.kml`)

Sample file: 1355 one-metre contour lines, elevation range 267 m – 298 m,
covering roughly a 3.1 km × 2.6 km survey area.

Running `python demo.py` produces:

- `output/sample_output.json` — full API response
- `output/demo_visualization.png` — DEM with candidate pond sites (left)
  and flow-accumulation with the recommended catchment boundary (right)

Recommended pond site for the sample map (from an actual run — regenerate
with `python demo.py`, values are computed, not hard-coded):

| Metric | Value |
|---|---|
| Location | ≈ 81.2908° E, 21.2543° N |
| Elevation | ≈ 276.2 m |
| Catchment area | ≈ 6.7 ha (16.5 acres) |
| Mean catchment slope | ≈ 5.0 % |
| Pond surface area (2 m depth) | ≈ 1.02 ha |
| Estimated storage volume | ≈ 11,300 m³ |

Two alternative candidate sites (with smaller catchments) are also returned
in `candidate_sites` for comparison.

---

## 5. Project structure

```
pond-catchment-api/
├── app.py                    # Flask routes (/analyzeContour, /findCatchment, /health)
├── pondcatchment/
│   ├── kml_parser.py          # KML/KMZ → contour lines
│   ├── dem.py                 # contour points → DEM raster
│   ├── hydrology.py           # sink fill, D8 flow direction, flow accumulation
│   ├── catchment.py           # pond site selection, watershed delineation, reservoir sizing
│   └── analyzer.py            # orchestrates the full pipeline
├── sample_data/contours_1m.kml
├── demo.py                    # runs the pipeline on the sample map, produces output/
├── tests/test_pipeline.py
├── requirements.txt
├── Procfile                   # for Render/Railway/Heroku-style deployment
└── README.md
```

## 6. Deployment

The app is a standard Flask app (WSGI entry point `app:app`) and deploys
as-is to Render, Railway, Fly.io, or any container/PaaS host using the
included `Procfile`. For local testing over the internet without a full
deployment, `ngrok http 5000` after `python app.py` gives a temporary
public URL.
