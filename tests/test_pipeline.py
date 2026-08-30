import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pondcatchment import analyze  # noqa: E402

SAMPLE = os.path.join(os.path.dirname(__file__), "..", "sample_data", "contours_1m.kml")


def test_analyze_returns_expected_structure():
    result = analyze(SAMPLE, max_grid_dim=80, num_candidates=2)
    assert result["status"] == "ok"
    assert result["input_summary"]["num_contour_lines"] > 0
    assert "recommended_pond_site" in result
    site = result["recommended_pond_site"]
    assert site["catchment_area_m2"] > 0
    assert -90 <= site["pond_location"]["lat"] <= 90
    assert -180 <= site["pond_location"]["lon"] <= 180
    assert len(result["candidate_sites"]) <= 2


def test_kml_parser_no_hardcoded_values():
    from pondcatchment import kml_parser
    contours = kml_parser.parse_contours(SAMPLE)
    summary = kml_parser.contour_summary(contours)
    # sanity: values come from file content, not literals in code
    assert summary["num_contour_lines"] == len(contours)
    assert summary["elevation_min"] < summary["elevation_max"]


if __name__ == "__main__":
    test_analyze_returns_expected_structure()
    test_kml_parser_no_hardcoded_values()
    print("All tests passed.")
