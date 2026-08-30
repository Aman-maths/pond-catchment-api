"""
demo.py
-------
Standalone demonstration of the catchment-analysis pipeline on the sample
contour map (sample_data/contours_1m.kml). Produces:

    output/sample_output.json    - full structured API response
    output/demo_visualization.png - DEM + flow-accumulation + catchment plot

Run:  python demo.py
"""
import json
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from pondcatchment import kml_parser, analyze
from pondcatchment.dem import build_dem
from pondcatchment.hydrology import fill_sinks, flow_accumulation, flow_direction
from pondcatchment.catchment import _build_upstream_lists, delineate_catchment, find_candidate_sites

SAMPLE = os.path.join(os.path.dirname(__file__), "sample_data", "contours_1m.kml")
OUT_DIR = os.path.join(os.path.dirname(__file__), "output")
os.makedirs(OUT_DIR, exist_ok=True)


def main():
    print(f"Analysing {SAMPLE} ...")
    result = analyze(SAMPLE)

    out_json = os.path.join(OUT_DIR, "sample_output.json")
    with open(out_json, "w") as f:
        json.dump(result, f, indent=2)
    print(f"Wrote {out_json}")

    print("\nRecommended pond site:")
    site = result["recommended_pond_site"]
    print(f"  Location        : {site['pond_location']}")
    print(f"  Catchment area   : {site['catchment_area_hectares']:.2f} ha "
          f"({site['catchment_area_m2']:.0f} m^2)")
    print(f"  Pond surface area: {site['pond_sizing_estimate']['pond_surface_area_hectares']:.2f} ha")
    print(f"  Storage volume   : {site['pond_sizing_estimate']['estimated_storage_volume_m3']:.0f} m^3")

    # --- visualization -----------------------------------------------------
    contours = kml_parser.parse_contours(SAMPLE)
    dem = build_dem(contours, max_grid_dim=160)
    Zf = fill_sinks(dem.Z)
    fdir = flow_direction(Zf, dem.cellsize_x_m, dem.cellsize_y_m)
    acc = flow_accumulation(Zf, fdir)
    upstream = _build_upstream_lists(fdir)
    cands = find_candidate_sites(dem, acc, fdir, upstream, num_candidates=3)

    fig, axes = plt.subplots(1, 2, figsize=(15, 6.5))

    ax = axes[0]
    im = ax.imshow(dem.Z, cmap="terrain", origin="upper")
    ax.contour(dem.Z, levels=15, colors="k", linewidths=0.3, alpha=0.5)
    plt.colorbar(im, ax=ax, label="Elevation (m)", fraction=0.046)
    ax.set_title("Interpolated DEM from contours\n(with pond site candidates)")
    colors = ["red", "orange", "yellow"]
    for i, cand in enumerate(cands):
        ax.plot(cand.col, cand.row, marker="*", color=colors[i % 3], markersize=18,
                 markeredgecolor="black", label=f"Site {i+1} ({cand.catchment_area_m2/10000:.1f} ha)")
    ax.legend(loc="lower right", fontsize=8)
    ax.set_xlabel("Column"); ax.set_ylabel("Row")

    ax2 = axes[1]
    im2 = ax2.imshow(np.log1p(acc), cmap="Blues", origin="upper")
    plt.colorbar(im2, ax=ax2, label="log(1+flow accumulation)", fraction=0.046)
    mask1 = delineate_catchment(upstream, cands[0].row, cands[0].col, dem.nrows, dem.ncols)
    ax2.contour(mask1.astype(float), levels=[0.5], colors="red", linewidths=2)
    ax2.plot(cands[0].col, cands[0].row, marker="*", color="red", markersize=18, markeredgecolor="black")
    ax2.set_title("Flow accumulation & recommended catchment\n(red outline = catchment boundary of Site 1)")
    ax2.set_xlabel("Column"); ax2.set_ylabel("Row")

    plt.tight_layout()
    out_png = os.path.join(OUT_DIR, "demo_visualization.png")
    plt.savefig(out_png, dpi=150)
    print(f"Wrote {out_png}")


if __name__ == "__main__":
    main()
