# =====================================================================
# analysis.py — THE FIVE BUILDING BLOCKS (where every answer is computed)
# ---------------------------------------------------------------------
# The AI never calculates anything. It only fills in a small form such as
#     {"operation": "find", "params": {"target": "schools", "relation": "within",
#                                       "from": "facilities", "distance_m": 500}}
# and the functions in this file do the real GIS work with GeoPandas/Shapely:
#
#   find           list or count schools / facilities / hospitals, optionally
#                  within or beyond a distance from something
#   coverage       which parts of the area are farther than X metres from care
#                  (exact polygons: buffers -> union -> difference with boundary)
#   distance_grid  a colour map of distance to the nearest school / facility
#   summary        overview: size, counts, densities, average distance, worst school
#   unsupported    handled in app.py (polite "I can only answer ..." message)
#
# WHY UTM? Longitude/latitude are angles, not metres, so you cannot measure distances
# with them directly. estimate_utm_crs() picks the local UTM zone (a flat, metre-based
# map) for wherever the area is on Earth. We project, measure, then convert results
# back to lon/lat for drawing on the map.
#
# Every block returns a plain dict ("result") with:
#   "op"   : which block ran
#   "text" : the computed answer in English (the AI may re-phrase it, never change it)
#   ...    : map data in lon/lat, used by mapview.py to draw the answer
# =====================================================================

from __future__ import annotations

import math

import geopandas as gpd
import numpy as np
from shapely.geometry import Point, box, mapping
from shapely.ops import unary_union

from .data import WGS84, Area

KIND_LABEL = {"schools": "schools", "facilities": "medical facilities", "hospitals": "hospitals"}
SING = {"schools": "school", "facilities": "medical facility", "hospitals": "hospital"}

# "Nice" round distances used for default radii and legend breaks.
NICE = [100, 200, 250, 300, 500, 750, 1000, 1500, 2000, 3000, 5000, 7500, 10000, 20000]

# Colour ramp for distance_grid: green (close) -> red (far).
RAMP = ["#1a9850", "#91cf60", "#fee08b", "#fc8d59", "#d73027"]


# ---------------------------------------------------------------------
# Small formatting helpers
# ---------------------------------------------------------------------
def fmt(m: float) -> str:
    """850 -> '850 m', 1234 -> '1.2 km', 12345 -> '12 km'."""
    if m < 1000:
        return f"{int(m + 0.5)} m"
    return f"{m / 1000:.1f} km" if m < 10000 else f"{m / 1000:.0f} km"


def nice_up(v: float) -> int:
    """Round a distance UP to the next 'nice' value (e.g. 430 -> 500)."""
    for n in NICE:
        if n >= v:
            return n
    return int(math.ceil(v / 10000) * 10000)


def name_of(row, kind: str) -> str:
    """A feature's name, or 'an unnamed school' when the map data has no name."""
    nm = row.get("name") if hasattr(row, "get") else None
    return nm if isinstance(nm, str) and nm.strip() else f"an unnamed {SING.get(kind, 'place')}"


# ---------------------------------------------------------------------
# Projected view of an Area (all distances in metres)
# ---------------------------------------------------------------------
class Projected:
    """The Area re-projected to its local UTM zone, plus handy coordinate arrays.

    Building this once per question keeps the blocks below short."""

    def __init__(self, area: Area):
        self.area = area
        boundary = gpd.GeoSeries([area.boundary], crs=WGS84)
        self.utm = boundary.estimate_utm_crs()                 # e.g. EPSG:32638 for Riyadh
        self.boundary = boundary.to_crs(self.utm).iloc[0]      # polygon in metres
        self.schools = area.schools.to_crs(self.utm)
        self.facilities = area.facilities.to_crs(self.utm)
        # A hospital is a facility whose category mentions "hospital".
        is_hosp = self.facilities["category"].fillna("").str.contains("hospital", case=False)
        self.hospitals = self.facilities[is_hosp]

    def feats(self, kind: str) -> gpd.GeoDataFrame:
        return {"schools": self.schools, "hospitals": self.hospitals}.get(kind, self.facilities)

    def to_lonlat(self, geom):
        """Convert one metre-based geometry back to lon/lat for the map."""
        return gpd.GeoSeries([geom], crs=self.utm).to_crs(WGS84).iloc[0]

    def lonlat_of(self, gdf: gpd.GeoDataFrame) -> list[tuple[float, float]]:
        """Lon/lat of every row (we keep the original lon/lat points for drawing)."""
        src = self.area.schools if gdf is self.schools else self.area.facilities
        pts = src.loc[gdf.index].geometry
        return [(p.x, p.y) for p in pts]

    def project_point(self, lonlat: tuple[float, float]):
        return gpd.GeoSeries([Point(lonlat)], crs=WGS84).to_crs(self.utm).iloc[0]


def xy(gdf: gpd.GeoDataFrame) -> np.ndarray:
    """N x 2 array of (x, y) metres."""
    return np.column_stack([gdf.geometry.x.to_numpy(), gdf.geometry.y.to_numpy()]) if len(gdf) else np.empty((0, 2))


def nearest(src: np.ndarray, ref: np.ndarray, src_ids=None, ref_ids=None) -> tuple[np.ndarray, np.ndarray]:
    """For every point in `src`, the distance (m) to and index of its nearest point in `ref`.

    This is the same idea as geopandas.sjoin_nearest, written with numpy so we can skip
    a feature measuring the distance to ITSELF (e.g. "schools near other schools").
    Points are processed in chunks so thousands of features don't use too much memory."""
    n = len(src)
    dist = np.full(n, np.inf)
    idx = np.full(n, -1)
    if n == 0 or len(ref) == 0:
        return dist, idx
    for start in range(0, n, 500):
        chunk = src[start:start + 500]
        d = np.hypot(chunk[:, None, 0] - ref[None, :, 0], chunk[:, None, 1] - ref[None, :, 1])
        if src_ids is not None and ref_ids is not None:
            d[np.asarray(src_ids[start:start + 500])[:, None] == np.asarray(ref_ids)[None, :]] = np.inf
        best = d.argmin(axis=1)
        dist[start:start + 500] = d[np.arange(len(chunk)), best]
        idx[start:start + 500] = np.where(np.isfinite(dist[start:start + 500]), best, -1)
    return dist, idx


def school_distances(P: Projected) -> np.ndarray:
    """Distance from every school to its nearest medical facility, sorted, in metres."""
    d, _ = nearest(xy(P.schools), xy(P.facilities))
    return np.sort(d[np.isfinite(d)])


def default_distance(P: Projected) -> int:
    """A sensible radius for THIS area: the median school-to-facility distance, rounded up.

    That way suggestions and defaults adapt to each place instead of being hard-coded."""
    d = school_distances(P)
    return nice_up(max(float(d[len(d) // 2]), 150)) if len(d) else 1000


# ---------------------------------------------------------------------
# "from": what are we measuring distance FROM?
# ---------------------------------------------------------------------
def resolve_from(P: Projected, frm: str, notes: list, pin=None, me=None) -> dict:
    """Turn the 'from' parameter into either a SET of features or a single POINT.

    pin / me are (lon, lat) tuples or None. Honest notes are added whenever we had to
    fall back (e.g. no hospitals mapped, no pin set, location unavailable)."""
    area = P.area
    if frm in ("schools", "facilities", "hospitals"):
        feats = P.feats(frm)
        if frm == "hospitals" and feats.empty:
            notes.append(f"No hospitals are mapped in {area.name}, so I measured from all medical facilities.")
            frm, feats = "facilities", P.facilities
        label = "a medical facility" if frm == "facilities" else f"a {SING[frm]}"
        return {"kind": "set", "from": frm, "feats": feats, "label": label}

    centre = area.boundary.centroid
    centre_ll = (centre.x, centre.y)
    fallback = "your map pin" if pin else "the centre of the area"
    if frm == "me":
        if me and area.boundary.contains(Point(me)):
            return {"kind": "point", "coords": me, "label": "your location"}
        if me:
            away = fmt(P.project_point(me).distance(P.boundary.centroid))
            notes.append(f"Your location is about {away} from {area.short_name} and I couldn't load data "
                         f"around you, so I used {fallback} instead.")
        else:
            notes.append(f"I don't have your location yet (tap the 📍 location button and allow access), "
                         f"so I used {fallback} instead.")
    if pin and frm != "centre":
        return {"kind": "point", "coords": pin, "label": "your map pin"}
    if frm == "pin":
        notes.append("No pin is set (click the map to drop one), so I used the centre of the area.")
    return {"kind": "point", "coords": centre_ll, "label": f"the centre of {area.short_name}"}


# =====================================================================
# BLOCK 1: find
# =====================================================================
def op_find(area: Area, p: dict, pin=None, me=None) -> dict:
    P = Projected(area)
    notes: list[str] = []
    name = area.name

    if p.get("target") == "all":
        ns, nf, nh = len(P.schools), len(P.facilities), len(P.hospitals)
        return {"op": "find", "target": "all",
                "text": f"{name} has {ns} schools and {nf} medical facilities ({nh} of them hospitals) "
                        f"mapped in {area.source} data. All of them are shown on the map."}

    target = p.get("target") or "schools"
    T = P.feats(target)
    if target == "hospitals" and T.empty:
        notes.append(f"No hospitals are mapped in {name}, so I used all medical facilities.")
        target, T = "facilities", P.facilities
    if T.empty:
        return {"op": "find", "target": target, "items": [], "shown": [],
                "text": f"No {KIND_LABEL[target]} are mapped in {name}."}

    ref = resolve_from(P, p.get("from") or ("facilities" if target == "schools" else "schools"), notes, pin, me)
    relation = p.get("relation") or "all"
    d = None if relation == "all" else (p.get("distance_m") or default_distance(P))

    # --- measure a distance for every target feature ---
    t_ll = P.lonlat_of(T)
    if ref["kind"] == "point":
        pt = P.project_point(ref["coords"])
        dists = T.geometry.distance(pt).to_numpy()
        near_ll = [ref["coords"]] * len(T)
        near_names = [None] * len(T)
    else:
        R = ref["feats"]
        # Tag each row with "<layer>:<row id>" so a feature never counts itself as its own nearest.
        t_ids = [("s:" if target == "schools" else "f:") + str(i) for i in T.index]
        r_ids = [("s:" if ref["from"] == "schools" else "f:") + str(i) for i in R.index]
        dists, idx = nearest(xy(T), xy(R), t_ids, r_ids)
        r_ll = P.lonlat_of(R)
        near_ll = [r_ll[j] if j >= 0 else None for j in idx]
        near_names = [name_of(R.iloc[j], ref["from"]) if j >= 0 else None for j in idx]

    items = [{"name": name_of(T.iloc[i], target), "coords": t_ll[i], "dist": float(dists[i]),
              "near": near_ll[i], "near_name": near_names[i]}
             for i in range(len(T)) if np.isfinite(dists[i])]

    if relation == "within":
        items = [i for i in items if i["dist"] <= d]
    elif relation == "beyond":
        items = [i for i in items if i["dist"] > d]
    sort = p.get("sort") or ("farthest" if relation == "beyond" else "nearest")
    items.sort(key=lambda i: i["dist"], reverse=(sort == "farthest"))
    limit = max(1, min(int(p.get("limit") or 10), 25))
    shown = items[:limit]

    # --- write the answer ---
    rel_text = (f"within {fmt(d)} of {ref['label']}" if relation == "within"
                else f"more than {fmt(d)} from {ref['label']}" if relation == "beyond" else "")
    if p.get("output") == "count":
        text = (f"{name} has {len(items)} {KIND_LABEL[target]}." if relation == "all"
                else f"{len(items)} of {len(T)} {KIND_LABEL[target]} in {name} are {rel_text}.")
    elif relation == "all" and limit == 1 and shown:
        i = shown[0]
        text = (f"The {sort} {SING[target]} to {ref['label']} is {i['name']}, {fmt(i['dist'])} away"
                + (f" (nearest: {i['near_name']})" if i["near_name"] else "") + ".")
    else:
        head = (f"{len(items)} {KIND_LABEL[target]} in {name}, ranked by distance to {ref['label']} ({sort} first)"
                if relation == "all" else f"{len(items)} of {len(T)} {KIND_LABEL[target]} are {rel_text}")
        lst = "; ".join(f"{i['name']} ({fmt(i['dist'])}" + (f", nearest: {i['near_name']}" if i["near_name"] else "") + ")"
                        for i in shown)
        more = f"; and {len(items) - len(shown)} more" if len(items) > len(shown) else ""
        text = f"{head}{': ' + lst if shown else ''}{more}."
    if notes:
        text += " " + " ".join(notes)

    # For the map: the reference features (drawn as buffers) when it is a set.
    ref_out = {k: v for k, v in ref.items() if k != "feats"}
    if ref["kind"] == "set":
        ref_out["coords_list"] = P.lonlat_of(ref["feats"])
    return {"op": "find", "target": target, "relation": relation, "d": d, "sort": sort,
            "ref": ref_out, "items": items, "shown": shown, "text": text}


# =====================================================================
# BLOCK 2: coverage (exact polygons)
# =====================================================================
def op_coverage(area: Area, p: dict) -> dict:
    """Parts of the area farther than `distance_m` from the target, as exact polygons.

    Steps (classic GIS "buffer analysis"):
      1. buffer : draw a circle of radius d around every facility
      2. union  : merge all circles into one shape (the covered zone)
      3. difference : boundary minus covered zone = the coverage GAP
    Then % of area = gap area / boundary area, and schools in the gap are those whose
    nearest facility is farther than d."""
    P = Projected(area)
    notes = []
    target = p.get("target") or "facilities"
    if target == "schools" and "school" not in p.get("_q", ""):
        target = "facilities"          # "coverage" is about access to care unless schools are named
    T = P.feats(target)
    if target == "hospitals" and T.empty:
        notes.append("No hospitals are mapped, so I used all medical facilities.")
        target, T = "facilities", P.facilities
    d = p.get("distance_m") or default_distance(P)

    covered = unary_union(list(T.geometry.buffer(d, resolution=32))) if len(T) else None
    gap = P.boundary.difference(covered) if covered is not None else P.boundary
    gap_km2 = gap.area / 1e6
    pct = int(round(gap.area / P.boundary.area * 100)) if P.boundary.area else 0

    dists, _ = nearest(xy(P.schools), xy(T))
    in_gap_mask = dists > d
    s_ll = P.lonlat_of(P.schools)
    in_gap = [{"name": name_of(P.schools.iloc[i], "schools"), "coords": s_ll[i]}
              for i in range(len(P.schools)) if in_gap_mask[i]]

    if gap.is_empty or (pct == 0 and not in_gap):
        text = (f"Every part of {area.name} is within {fmt(d)} of a {SING[target]}, "
                f"so there are no coverage gaps at that distance.")
    else:
        names = "; ".join(s["name"] for s in in_gap[:8]) + (f"; and {len(in_gap) - 8} more" if len(in_gap) > 8 else "")
        text = (f"About {pct}% of {area.name} ({gap_km2:.2f} km²) is more than {fmt(d)} from the nearest "
                f"{SING[target]} (shaded on the map). {len(in_gap)} of {len(P.schools)} schools are in those gaps"
                f"{': ' + names if in_gap else ''}.")
    if notes:
        text += " " + " ".join(notes)
    return {"op": "coverage", "target": target, "d": d, "pct": pct, "gap_km2": gap_km2,
            "gap": mapping(P.to_lonlat(gap)) if not gap.is_empty else None,
            "target_coords": P.lonlat_of(T), "in_gap": in_gap, "text": text}


# =====================================================================
# BLOCK 3: distance_grid
# =====================================================================
def make_cells(P: Projected) -> tuple[list, float]:
    """Square grid covering the area: about 600 cells, each at least 50 m wide.

    Returns (cells, side_m). Only cells whose centre is inside the boundary are kept."""
    area_km2 = P.boundary.area / 1e6
    side = max(math.sqrt(area_km2 / 600), 0.05) * 1000
    minx, miny, maxx, maxy = P.boundary.bounds
    all_cells = [box(x, y, x + side, y + side)
                 for x in np.arange(minx, maxx, side) for y in np.arange(miny, maxy, side)]
    inside = [c for c in all_cells if P.boundary.contains(c.centroid)]
    return (inside or all_cells), side


def op_grid(area: Area, p: dict) -> dict:
    P = Projected(area)
    target = p.get("target") or "schools"
    T = P.feats(target)
    if T.empty:
        target, T = "facilities", P.facilities
    cells, side = make_cells(P)
    centres = np.array([[c.centroid.x, c.centroid.y] for c in cells])
    ds, _ = nearest(centres, xy(T))
    s = np.sort(ds)

    def q(x):                       # the x-th quantile (0..1) of the sorted distances
        return float(s[int(math.floor(x * (len(s) - 1)))])

    breaks = sorted({nice_up(q(x)) for x in (0.2, 0.4, 0.6, 0.8)})
    cls = [next((i for i, b in enumerate(breaks) if v <= b), len(breaks)) for v in ds]
    legend = [{"color": RAMP[i], "label": f"≤ {fmt(b)}"} for i, b in enumerate(breaks)]
    legend.append({"color": RAMP[min(len(breaks), 4)], "label": f"> {fmt(breaks[-1])}"})

    # One lon/lat polygon per cell, converted in a single fast GeoSeries call.
    cells_ll = gpd.GeoSeries(cells, crs=P.utm).to_crs(WGS84)
    text = (f"I split {area.name} into {len(cells)} squares of about {int(round(side))} m and measured the distance "
            f"from each to the nearest {SING[target]}. Half of the area is within {fmt(q(0.5))}, and the farthest "
            f"square is {fmt(float(s[-1]))} away. Greener squares are closer, redder squares farther.")
    return {"op": "distance_grid", "target": target,
            "cells": [{"geom": mapping(g), "cls": c} for g, c in zip(cells_ll, cls)],
            "legend": legend, "text": text}


# =====================================================================
# BLOCK 4: summary
# =====================================================================
def op_summary(area: Area, p: dict | None = None) -> dict:
    P = Projected(area)
    a = P.boundary.area / 1e6
    ns, nf, nh = len(P.schools), len(P.facilities), len(P.hospitals)
    d, idx = nearest(xy(P.schools), xy(P.facilities))
    finite = d[np.isfinite(d)]
    avg = float(finite.mean()) if len(finite) else 0.0
    worst = None
    if len(finite):
        i = int(np.argmax(np.where(np.isfinite(d), d, -1)))
        worst = {"name": name_of(P.schools.iloc[i], "schools"), "dist": float(d[i]),
                 "school": P.lonlat_of(P.schools)[i], "facility": P.lonlat_of(P.facilities)[idx[i]]}
    text = (f"{area.name} covers about {a:.2f} km² with {ns} schools ({ns / a:.1f} per km²) and {nf} medical "
            f"facilities, {nh} of them hospitals. That is {nf / ns if ns else 0:.1f} medical facilities per school; "
            f"a school is on average {fmt(avg)} from the nearest one"
            + (f", and the farthest is {worst['name']} at {fmt(worst['dist'])}" if worst else "") + ". "
            "This data contains no official standard for how many schools or clinics an area should have.")
    return {"op": "summary", "worst": worst, "text": text}


def run_op(area: Area, operation: str, params: dict, pin=None, me=None) -> dict:
    """Dispatch table: operation name (from the AI) -> the Python function that computes it."""
    if operation == "find":
        return op_find(area, params, pin, me)
    if operation == "coverage":
        return op_coverage(area, params)
    if operation == "distance_grid":
        return op_grid(area, params)
    return op_summary(area, params)
