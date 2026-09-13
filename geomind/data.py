# =====================================================================
# data.py — WHERE THE MAP DATA COMES FROM
# ---------------------------------------------------------------------
# GeoMind uses a "hybrid" data strategy:
#
#   * FEATURED districts (Ghirnatah, Gulberg, Clifton) load a small file we
#     prepared in advance from Overture Maps (data/featured.json). Fast and
#     reliable — perfect for a demo.
#   * ANY OTHER place the user searches for is downloaded LIVE from
#     OpenStreetMap through the free Overpass API.
#
# Every loader returns the same thing: an `Area` object holding the boundary
# polygon, a GeoDataFrame of schools and a GeoDataFrame of medical facilities.
# The rest of the app never needs to know where the data came from.
#
# Free web services used (none need an API key):
#   Photon     https://photon.komoot.io        search-as-you-type place suggestions
#   Nominatim  https://nominatim.openstreetmap.org   place boundaries + "what area am I in?"
#   Overpass   https://overpass-api.de (and mirrors)  live OpenStreetMap features
# =====================================================================

from __future__ import annotations

import json
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import requests
from shapely.geometry import Point, box, shape

# Public services ask every app to identify itself with a User-Agent header.
HEADERS = {"User-Agent": "GeoMindAI-streamlit/1.0 (hackathon demo)"}

FEATURED_PATH = Path(__file__).resolve().parent.parent / "data" / "featured.json"

# Three public Overpass servers. We ask all of them at once and use whichever
# answers first ("racing"), because any single public server can be busy.
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

# Words that identify a featured district inside a question or a search result.
# "cc" is the country code, so a "Clifton" in England does not match Karachi's Clifton.
FEATURED_MATCH = {
    "ghirnatah": {"cc": "sa", "aliases": ["ghirnatah", "غرناطة"]},
    "gulberg": {"cc": "pk", "aliases": ["gulberg", "گلبرگ"]},
    "clifton": {"cc": "pk", "aliases": ["clifton", "کلفٹن"]},
}

OVERTURE = "Overture extract"
LIVE_OSM = "Live OpenStreetMap"

# GPS coordinates (longitude/latitude in degrees) use this code everywhere.
WGS84 = "EPSG:4326"


# ---------------------------------------------------------------------
# The Area object: everything we know about the currently loaded place
# ---------------------------------------------------------------------
@dataclass
class Area:
    name: str                      # e.g. "Gulberg, Lahore"
    source: str                    # OVERTURE or LIVE_OSM (shown to the user)
    boundary: object               # a shapely Polygon / MultiPolygon in longitude/latitude
    schools: gpd.GeoDataFrame      # one row per school: name, category, geometry (Point)
    facilities: gpd.GeoDataFrame   # one row per medical facility
    key: str | None = None         # featured key ("gulberg") or None for live areas

    @property
    def short_name(self) -> str:
        """'Gulberg, Lahore' -> 'Gulberg' (reads better inside sentences)."""
        return self.name.split(",")[0]

    @property
    def center(self) -> tuple[float, float]:
        """(lat, lon) of the middle of the boundary — Folium wants latitude first."""
        c = self.boundary.centroid
        return (c.y, c.x)


def points_gdf(rows: list[dict]) -> gpd.GeoDataFrame:
    """Turn [{'lon':.., 'lat':.., 'name':.., 'category':..}, ...] into a GeoDataFrame of points."""
    return gpd.GeoDataFrame(
        {"name": [r.get("name") or "" for r in rows],
         "category": [r.get("category") or "" for r in rows]},
        geometry=[Point(r["lon"], r["lat"]) for r in rows],
        crs=WGS84,
    )


def area_km2(geom) -> float:
    """Area of a lon/lat shape in square kilometres (measured in a local metric projection)."""
    s = gpd.GeoSeries([geom], crs=WGS84)
    return float(s.to_crs(s.estimate_utm_crs()).area.iloc[0] / 1e6)


# =====================================================================
# 1. FEATURED DISTRICTS (prebuilt Overture Maps extracts)
# =====================================================================
_FEATURED_CACHE: dict | None = None


def featured_raw() -> dict:
    """The raw featured.json content: {key: {name, osm, center, boundary, schools, facilities}}."""
    global _FEATURED_CACHE
    if _FEATURED_CACHE is None:
        _FEATURED_CACHE = json.loads(FEATURED_PATH.read_text(encoding="utf-8"))
    return _FEATURED_CACHE


def load_featured(key: str) -> Area:
    """Build an Area from one entry of featured.json (no internet needed)."""
    d = featured_raw()[key]
    return Area(
        name=d["name"],
        source=OVERTURE,
        boundary=shape(d["boundary"]["geometry"]),
        # GeoDataFrame.from_features reads GeoJSON features straight into a table.
        schools=gpd.GeoDataFrame.from_features(d["schools"]["features"], crs=WGS84),
        facilities=gpd.GeoDataFrame.from_features(d["facilities"]["features"], crs=WGS84),
        key=key,
    )


def featured_in_text(text: str) -> str | None:
    """If a featured district is mentioned in `text` (e.g. 'schools in Gulberg'), return its key."""
    t = (text or "").lower()
    for key, m in FEATURED_MATCH.items():
        if key in featured_raw() and any(a.lower() in t for a in m["aliases"]):
            return key
    return None


def match_featured(props: dict) -> str | None:
    """Does a search / reverse-geocode result point at one of our featured districts?

    First we compare OpenStreetMap ids (exact), then fall back to name + country code."""
    osm = (props.get("osm_type") or "").upper()[:1] + str(props.get("osm_id") or "")
    for key, d in featured_raw().items():
        if d.get("osm") == osm:
            return key
    hay = " ".join(str(props.get(k) or "") for k in ("name", "city", "state")).lower()
    cc = (props.get("countrycode") or "").lower()
    for key, m in FEATURED_MATCH.items():
        if key in featured_raw() and cc == m["cc"] and any(a.lower() in hay for a in m["aliases"]):
            return key
    return None


# =====================================================================
# 2. SEARCH SUGGESTIONS (Photon)
# =====================================================================
def photon_search(query: str, limit: int = 6) -> list[dict]:
    """Search-as-you-type suggestions for a place name.

    Returns a list of Photon "properties" dicts, each with an extra "_lonlat" key.
    Photon often returns near-duplicates, so we keep one per name+city+state+country,
    preferring results that have an extent (a real area rather than a single point)."""
    if len((query or "").strip()) < 3:
        return []
    r = requests.get("https://photon.komoot.io/api/", params={"q": query, "limit": 12},
                     headers=HEADERS, timeout=8)
    r.raise_for_status()
    feats = r.json().get("features", [])
    feats.sort(key=lambda f: 0 if f["properties"].get("extent") else 1)   # areas first
    seen, out = set(), []
    for f in feats:
        p = dict(f["properties"])
        k = "|".join(str(p.get(x) or "") for x in ("name", "city", "state", "country")).lower()
        if k in seen:
            continue
        seen.add(k)
        p["_lonlat"] = f["geometry"]["coordinates"]
        out.append(p)
    return out[:limit]


def describe_place(p: dict) -> str:
    """'Gulberg — Lahore, Punjab, Pakistan' label for a suggestion."""
    rest = ", ".join(str(p[k]) for k in ("city", "state", "country") if p.get(k))
    return f"{p.get('name') or '(unnamed)'} — {rest}" if rest else (p.get("name") or "(unnamed)")


# =====================================================================
# 3. LIVE OPENSTREETMAP AREAS (Nominatim boundary + Overpass features)
# =====================================================================
def nominatim_polygon(osm_type: str, osm_id) -> object | None:
    """Ask Nominatim for the real boundary polygon of an OSM object, or None if it has none."""
    ref = (osm_type or "").upper()[:1] + str(osm_id)
    try:
        r = requests.get("https://nominatim.openstreetmap.org/lookup",
                         params={"osm_ids": ref, "format": "jsonv2", "polygon_geojson": 1},
                         headers=HEADERS, timeout=8)
        d = r.json()
        if d and "Polygon" in (d[0].get("geojson") or {}).get("type", ""):
            return shape(d[0]["geojson"])
    except Exception:
        pass              # no polygon -> the caller uses a bounding box instead
    return None


def overpass_query(s: float, w: float, n: float, e: float) -> str:
    """Overpass QL: schools/kindergartens + hospitals/clinics/doctors/dentists + anything tagged healthcare."""
    bb = f"({s},{w},{n},{e})"
    return ("[out:json][timeout:45];("
            f'nwr["amenity"~"^(school|kindergarten)$"]{bb};'
            f'nwr["amenity"~"^(hospital|clinic|doctors|dentist)$"]{bb};'
            f'nwr["healthcare"]{bb};'
            ");out center tags;")


def fetch_overpass(query: str, timeout: int = 25) -> list[dict]:
    """Send the query to every mirror at once and return the first successful answer.

    Busy public mirrors often need ~20 s, so each attempt gets up to `timeout` seconds."""
    def attempt(url):
        r = requests.post(url, data={"data": query}, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        return r.json().get("elements", [])

    errors = []
    pool = ThreadPoolExecutor(max_workers=len(OVERPASS_MIRRORS))
    try:
        futures = [pool.submit(attempt, u) for u in OVERPASS_MIRRORS]
        for fut in as_completed(futures):
            try:
                return fut.result()           # first mirror to answer wins
            except Exception as err:          # this mirror failed; wait for the others
                errors.append(str(err))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)   # don't wait for the slow losers
    raise RuntimeError("All Overpass mirrors failed: " + "; ".join(errors))


def parse_overpass(elements: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split raw Overpass elements into school rows and medical rows."""
    schools, medical = [], []
    for el in elements:
        tags = el.get("tags") or {}
        # Nodes have lat/lon; ways and relations (buildings, campuses) have a "center".
        c = el.get("center") or el
        lon, lat = c.get("lon"), c.get("lat")
        if not lon or not lat:
            continue
        row = {"lon": lon, "lat": lat, "name": tags.get("name") or tags.get("name:en") or ""}
        amenity = tags.get("amenity")
        if amenity in ("school", "kindergarten"):
            schools.append({**row, "category": amenity})
        elif amenity in ("hospital", "clinic", "doctors", "dentist") or tags.get("healthcare"):
            medical.append({**row, "category": amenity or tags.get("healthcare")})
    return schools, medical


def dedupe(gdf: gpd.GeoDataFrame, min_gap_m: float = 75) -> gpd.GeoDataFrame:
    """Drop points closer than `min_gap_m` metres to one we already kept.

    The same clinic is often mapped twice (a point AND a building outline). Distances
    must be measured in metres, so we project to the local UTM zone first."""
    if gdf.empty:
        return gdf
    utm = gdf.to_crs(gdf.estimate_utm_crs())
    kept, kept_geoms = [], []
    for idx, g in utm.geometry.items():
        if all(g.distance(k) > min_gap_m for k in kept_geoms):
            kept.append(idx)
            kept_geoms.append(g)
    return gdf.loc[kept].reset_index(drop=True)


def load_live(props: dict, lonlat: tuple[float, float] | None = None, boundary=None) -> Area:
    """Download schools and medical facilities from OpenStreetMap for a searched place.

    props   : a Photon (or reverse-geocode) result — name, city, country, osm_type, osm_id, extent
    lonlat  : the result's point, used when there is no extent
    boundary: an already-known polygon (from a reverse lookup), if any
    Raises an exception if Overpass is unavailable, so the caller can keep the previous area."""
    place = ", ".join(str(props[k]) for k in ("name", "city", "country") if props.get(k)) or "Selected area"

    if boundary is None and props.get("osm_type") and props.get("osm_id"):
        boundary = nominatim_polygon(props["osm_type"], props["osm_id"])

    # Photon's extent is [west, north, east, south].
    if props.get("extent"):
        w, n, e, s = props["extent"]
    else:
        lon, lat = lonlat or props["_lonlat"]
        s, n, w, e = lat - 0.02, lat + 0.02, lon - 0.02, lon + 0.02
    if boundary is None:
        boundary = box(w, s, e, n)
    else:
        # Query the polygon's own box: it can be larger than Photon's extent.
        w, s, e, n = boundary.bounds

    elements = fetch_overpass(overpass_query(s, w, n, e))
    school_rows, medical_rows = parse_overpass(elements)

    # Overpass searches a rectangle; keep only features inside the real boundary.
    schools = points_gdf(school_rows)
    facilities = points_gdf(medical_rows)
    schools = schools[schools.within(boundary)].reset_index(drop=True)
    facilities = dedupe(facilities[facilities.within(boundary)].reset_index(drop=True))
    return Area(name=place, source=LIVE_OSM, boundary=boundary, schools=schools, facilities=facilities)


# =====================================================================
# 4. "NEAR ME": find and load the neighbourhood around a GPS position
# =====================================================================
def reverse_area(lon: float, lat: float) -> tuple[dict, object | None]:
    """Which neighbourhood contains this point? Returns (props, boundary_or_None).

    Falls back to a 1.5 km box around the point if Nominatim fails or returns something
    city-sized (bigger than 60 km² would make the live download slow)."""
    try:
        r = requests.get("https://nominatim.openstreetmap.org/reverse",
                         params={"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 14,
                                 "polygon_geojson": 1, "addressdetails": 1},
                         headers=HEADERS, timeout=8)
        d = r.json()
        if d and d.get("boundingbox"):
            s, n, w, e = map(float, d["boundingbox"])
            if area_km2(box(w, s, e, n)) <= 60:
                a = d.get("address") or {}
                props = {
                    "name": a.get("suburb") or a.get("neighbourhood") or a.get("quarter")
                            or a.get("city_district") or d.get("name") or "Your area",
                    "city": a.get("city") or a.get("town") or a.get("village") or a.get("county"),
                    "country": a.get("country"), "countrycode": a.get("country_code"),
                    "osm_type": d.get("osm_type"), "osm_id": d.get("osm_id"),
                    "extent": [w, n, e, s],
                }
                boundary = None
                geo = d.get("geojson") or {}
                if "Polygon" in geo.get("type", ""):
                    poly = shape(geo)
                    if poly.contains(Point(lon, lat)):
                        boundary = poly
                return props, boundary
    except Exception:
        pass
    # Fallback: a box of +/- 1.5 km. 1 degree of latitude is ~111 km.
    dlat = 1.5 / 111.32
    dlon = 1.5 / (111.32 * max(math.cos(math.radians(lat)), 0.1))
    props = {"name": "Area around your location", "extent": [lon - dlon, lat + dlat, lon + dlon, lat - dlat]}
    return props, None


def load_around_point(lon: float, lat: float) -> Area:
    """Load the area the user is standing in: our featured extract if it is one, else live OSM."""
    props, boundary = reverse_area(lon, lat)
    key = match_featured(props)
    if key:
        return load_featured(key)
    return load_live(props, (lon, lat), boundary)
