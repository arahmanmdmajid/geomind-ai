# =====================================================================
# suggest.py — SUGGESTED QUESTION BUTTONS
# ---------------------------------------------------------------------
# Suggestions are built from the loaded area's own data: the distances in them
# come from default_distance() (the median school-to-facility distance), and the
# hospital question only appears if hospitals are actually mapped there.
# After each answer we offer follow-ups that make sense for the block that ran.
# =====================================================================

from __future__ import annotations

from .analysis import KIND_LABEL, Projected, default_distance, fmt
from .data import Area

STARTERS = ["Which schools and clinics are near me?",
            "Which areas have poor access to healthcare?",
            "Which school has the worst access to healthcare?"]


def build_suggestions(area: Area | None) -> list[str]:
    """Starter questions for a freshly loaded area (or generic ones when nothing is loaded)."""
    if area is None:
        return STARTERS
    P = Projected(area)
    d = fmt(default_distance(P))
    place = area.short_name
    return [
        f"Which schools are more than {d} from a medical facility?",
        "Which school has the worst access to healthcare?",
        f"Where are the healthcare coverage gaps at {d}?",
        (f"Which hospitals are nearest the centre of {place}?" if len(P.hospitals)
         else f"Which medical facilities are within {d} of schools?"),
        f"Are there enough schools and clinics in {place}?",
    ]


def follow_ups(area: Area, result: dict) -> list[str]:
    """Three next questions that fit the answer the user just got."""
    d = fmt(result["d"]) if result.get("d") else fmt(default_distance(Projected(area)))
    place = area.short_name
    op, rel = result["op"], result.get("relation")
    label = KIND_LABEL.get(result.get("target"), "schools")
    if op == "find" and rel == "within":
        return [f"Which {label} are more than {d} away?", f"Show coverage gaps at {d}", f"Summarise access in {place}"]
    if op == "find" and rel == "beyond":
        return [f"Which {label} are within {d}?", "Show a distance map to the nearest medical facility",
                "Which school has the worst access to healthcare?"]
    if op == "find":
        return ["Which areas have poor access to healthcare?", "Show a distance map to the nearest school",
                f"Summarise access in {place}"]
    if op == "coverage":
        return [f"Which schools are more than {d} from a medical facility?",
                "Show a distance map to the nearest medical facility", "Which school has the worst access to healthcare?"]
    if op == "distance_grid":
        return ["Which areas have poor access to healthcare?", "Which schools are closest to medical facilities?",
                f"Summarise access in {place}"]
    return ["Which schools are closest to medical facilities?", "Which areas have poor access to healthcare?",
            "How far is the nearest school from each neighbourhood?"]
