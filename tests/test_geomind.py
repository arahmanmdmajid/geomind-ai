# =====================================================================
# Automated checks. Run with:   pytest
# They use the bundled featured.json only — no internet and no API key needed.
# =====================================================================

import json
from pathlib import Path

import pytest

from geomind import ai, analysis, data, suggest

HERE = Path(__file__).parent

# The 18 questions our testers asked (plus one off-topic question at the end).
TESTER_QUESTIONS = [
    "How many schools are in Gulberg, Lahore?", "How many medical facilities are in Gulberg, Lahore?",
    "Which schools are within 2 km of medical facilities?", "Which medical facilities are within 2 km of schools?",
    "Which schools are closest to medical facilities?", "Which schools are more than 3 km from a medical facility?",
    "Show me schools and medical facilities on a map.", "Create a 2 km buffer around medical facilities.",
    "Which schools fall within the medical-facility buffer?",
    "Which areas have schools but no medical facility within 2 km?", "What is the nearest hospital to this area?",
    "Show me all schools within 3 km of this location.", "Which healthcare facilities are closest to me?",
    "Are there enough schools in this neighborhood?", "Show me hospitals within a 5 km radius.",
    "Which areas have poor access to healthcare?", "Find schools near this location.",
    "How far is the nearest school from each neighborhood",
]


@pytest.fixture(scope="module")
def areas():
    return {k: data.load_featured(k) for k in ("ghirnatah", "gulberg", "clifton")}


# ---------------------------------------------------------------------
# Data: the featured extracts have the known counts
# ---------------------------------------------------------------------
@pytest.mark.parametrize("key, schools, facilities", [
    ("ghirnatah", 8, 16), ("gulberg", 79, 59), ("clifton", 54, 35)])
def test_featured_counts(areas, key, schools, facilities):
    a = areas[key]
    assert len(a.schools) == schools
    assert len(a.facilities) == facilities
    res = analysis.op_find(a, {"target": "schools", "relation": "all", "output": "count"})
    assert res["text"].startswith(f"{a.name} has {schools} schools")


def test_gulberg_43_schools_within_250m(areas):
    res = analysis.op_find(areas["gulberg"], {"target": "schools", "relation": "within",
                                              "from": "facilities", "distance_m": 250})
    assert len(res["items"]) == 43
    assert res["text"].startswith("43 of 79 schools are within 250 m of a medical facility")


def test_coverage_ghirnatah_500m_near_js_result(areas):
    # The JS app estimates 39% with a ~600-cell grid; we use exact buffer polygons,
    # so the number can differ by a few points (grid cells are all-or-nothing).
    res = analysis.op_coverage(areas["ghirnatah"], {"target": "facilities", "distance_m": 500, "_q": ""})
    print("Ghirnatah coverage gap at 500 m (polygons):", res["pct"], "%", round(res["gap_km2"], 3), "km²")
    assert abs(res["pct"] - 39) <= 6
    assert res["gap"] is not None


@pytest.mark.parametrize("key", ["ghirnatah", "gulberg", "clifton"])
def test_every_block_runs(areas, key):
    a = areas[key]
    for op, p in [("find", {"target": "schools", "relation": "beyond", "from": "facilities", "distance_m": 300}),
                  ("find", {"target": "hospitals", "relation": "all", "from": "centre", "sort": "nearest", "limit": 1}),
                  ("find", {"target": "facilities", "relation": "within", "from": "pin", "distance_m": 1000}),
                  ("find", {"target": "all"}),
                  ("coverage", {"target": "facilities", "_q": ""}),
                  ("distance_grid", {"target": "schools"}),
                  ("summary", {})]:
        res = analysis.run_op(a, op, p, pin=(a.boundary.centroid.x, a.boundary.centroid.y))
        assert res["op"] == op and res["text"]
    assert len(suggest.build_suggestions(a)) == 5


def test_distance_grid_cells_and_legend(areas):
    res = analysis.op_grid(areas["gulberg"], {"target": "facilities"})
    assert 400 <= len(res["cells"]) <= 800
    assert res["legend"][-1]["label"].startswith(">")


def test_find_me_without_location_falls_back_honestly(areas):
    res = analysis.op_find(areas["clifton"], {"target": "facilities", "relation": "all", "from": "me", "limit": 5})
    assert res["ref"]["label"].startswith("the centre of")
    assert "don't have your location" in res["text"]


def test_find_me_inside_area_uses_position(areas):
    a = areas["gulberg"]
    me = (a.boundary.centroid.x, a.boundary.centroid.y)
    res = analysis.op_find(a, {"target": "facilities", "relation": "all", "from": "me", "limit": 5}, me=me)
    assert res["ref"]["label"] == "your location"


# ---------------------------------------------------------------------
# normalize(): guard rails against common model slips
# ---------------------------------------------------------------------
def test_normalize_show_me_is_not_from_me():
    q = "Show me schools and medical facilities on a map."
    out = ai.normalize({"operation": "find", "params": {"target": "schools", "from": "me"}}, q)
    assert out["params"]["target"] == "all"
    assert out["params"]["from"] != "me"


def test_normalize_closest_to_medical_facilities():
    q = "Which schools are closest to medical facilities?"
    out = ai.normalize({"operation": "find", "params": {"target": "schools", "from": "user"}}, q)
    assert out["params"]["from"] == "facilities"


def test_normalize_this_location_is_pin_and_km_guard():
    q = "Show me all schools within 3 km of this location."
    out = ai.normalize({"operation": "find", "params": {"target": "School", "relation": "within",
                                                         "from": "me", "distance_m": 3}}, q)
    assert out["params"]["from"] == "pin"
    assert out["params"]["distance_m"] == 3000


def test_normalize_bad_operation_becomes_summary():
    assert ai.normalize({"operation": "dance"}, "hi")["operation"] == "summary"
    assert ai.normalize(None, "hi")["operation"] == "summary"


# ---------------------------------------------------------------------
# Keyword fallback router: the 18 tester questions all get a real block
# ---------------------------------------------------------------------
@pytest.mark.parametrize("q", TESTER_QUESTIONS)
def test_keyword_router_supports_tester_questions(q):
    intent = ai.normalize(ai.local_route(q), q)
    assert intent["operation"] != "unsupported", q


def test_keyword_router_weather_is_unsupported():
    assert ai.local_route("What's the weather today?")["operation"] == "unsupported"


def test_keyword_router_matches_model_on_key_questions():
    # Where the real model's choice is recorded, the fallback should pick the same block.
    raw = json.loads((HERE / "router_raw.json").read_text(encoding="utf-8"))
    same = sum(ai.normalize(ai.local_route(r["q"]), r["q"])["operation"] == r["raw"]["operation"] for r in raw)
    assert same >= len(raw) - 1


def test_route_falls_back_on_rate_limit():
    class Boom(Exception):
        status_code = 429

    class FakeClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    raise Boom()

    intent, reason = ai.route("Which areas have poor access to healthcare?", "Gulberg", FakeClient)
    assert intent["operation"] == "coverage"
    assert reason == "rate limited (429)"
    res = {"text": "computed"}
    assert ai.explain("q", res, "Gulberg", "x", FakeClient) == "computed"


def test_describe_tag():
    intent = ai.normalize({"operation": "find", "params": {"target": "schools", "relation": "within",
                                                            "from": "facilities", "distance_m": 250}}, "x")
    assert ai.describe(intent) == "find(target=schools, from=facilities, relation=within, distance_m=250)"


# ---------------------------------------------------------------------
# Live OSM parsing (fake Overpass response, no network)
# ---------------------------------------------------------------------
def test_parse_overpass_and_dedupe():
    els = [
        {"type": "node", "lat": 31.5, "lon": 74.34, "tags": {"amenity": "school", "name": "A"}},
        {"type": "way", "center": {"lat": 31.501, "lon": 74.341}, "tags": {"amenity": "kindergarten"}},
        {"type": "node", "lat": 31.51, "lon": 74.35, "tags": {"amenity": "clinic", "name": "C1"}},
        {"type": "way", "center": {"lat": 31.5102, "lon": 74.3501}, "tags": {"healthcare": "clinic", "name": "C1 bldg"}},
        {"type": "node", "lat": 31.52, "lon": 74.36, "tags": {"amenity": "hospital"}},
        {"type": "node", "lat": 31.52, "lon": 74.36, "tags": {"shop": "bakery"}},
    ]
    schools, medical = data.parse_overpass(els)
    assert len(schools) == 2 and len(medical) == 3
    kept = data.dedupe(data.points_gdf(medical))
    assert len(kept) == 2          # the clinic point and its building ~25 m apart count once


def test_featured_matching():
    assert data.featured_in_text("schools in Gulberg please") == "gulberg"
    assert data.match_featured({"name": "Clifton", "city": "Karachi", "countrycode": "PK"}) == "clifton"
    assert data.match_featured({"name": "Clifton", "city": "Bristol", "countrycode": "GB"}) is None
