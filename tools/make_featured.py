# Rebuilds data/featured.json (the prebuilt featured districts).
# Needs the Overture Maps CLI:  pip install overturemaps   (then run: python tools/make_featured.py)
# For each district: Nominatim gives the boundary polygon, Overture gives the places inside its
# bounding box, and we keep confident schools / medical facilities inside the polygon.
import json, os, sys, math, subprocess, time, urllib.parse, urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
UA = {"User-Agent": "GeoMindAI-hackathon/1.0"}
HERE = Path(__file__).parent
OVT = os.environ.get("OVERTURE_CLI", "overturemaps")   # path to the overturemaps executable

PLACES = [
    ("ghirnatah", "Ghirnatah, Riyadh, Saudi Arabia",  "Ghirnatah District, Riyadh"),
    ("gulberg",   "Gulberg, Lahore, Pakistan",        "Gulberg, Lahore"),
    ("clifton",   "Clifton, Karachi, Pakistan",       "Clifton, Karachi"),
]

SCHOOL = ("school", "elementary_school", "middle_school", "high_school", "primary_school",
          "secondary_school", "private_school", "preschool", "kindergarten")
MED = ("hospital", "clinic", "medical_center", "doctor", "physician", "dentist",
       "urgent_care", "emergency_room", "health_and_medical", "medical_service")

from shapely.geometry import shape, Point, box


def as_dict(v):
    if isinstance(v, str):
        try: return json.loads(v)
        except ValueError: return {}
    return v or {}

def cat(p):  return (as_dict(p.get("categories")).get("primary") or "").lower()
def nm(p):   return as_dict(p.get("names")).get("primary") or ""

def haversine(a, b):
    (lo1, la1), (lo2, la2) = a, b
    R = 6371000
    p1, p2 = math.radians(la1), math.radians(la2)
    dp = math.radians(la2 - la1); dl = math.radians(lo2 - lo1)
    h = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * R * math.asin(math.sqrt(h))


def build(key, query, label):
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": query, "format": "jsonv2", "limit": 1, "polygon_geojson": 1})
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
        hit = json.load(r)[0]
    bb = hit["boundingbox"]
    s, n, w, e = float(bb[0]), float(bb[1]), float(bb[2]), float(bb[3])
    geom = hit["geojson"]
    osm_ref = hit["osm_type"][0].upper() + str(hit["osm_id"])   # e.g. "R13254432"

    # If the geocoder gave a point (no boundary), use the bbox rectangle instead
    if geom["type"] == "Point":
        poly = box(w, s, e, n)
        geom = json.loads(json.dumps(poly.__geo_interface__))
        print(f"  ({key}: no polygon from geocoder — using bbox rectangle)")
    else:
        poly = shape(geom)

    out = HERE / f"_places_{key}.geojson"
    subprocess.run([str(OVT), "download", f"--bbox={w},{s},{e},{n}",
                    "-f", "geojson", "--type=place", "-o", str(out)], check=True)
    feats = json.loads(out.read_text(encoding="utf-8"))["features"]

    def collect(pats, exact):
        rows = []
        for f in feats:
            p = f["properties"]; c = cat(p)
            ok = (c in pats) if exact else any(k in c for k in pats)
            if ok and (p.get("confidence") or 0) > 0.5:
                lon, lat = f["geometry"]["coordinates"]
                if poly.contains(Point(lon, lat)):
                    rows.append({"lon": lon, "lat": lat, "name": nm(p), "category": c,
                                 "confidence": round(p.get("confidence", 0), 3)})
        return rows

    schools = collect(SCHOOL, True)
    medical = collect(MED, False)
    medical.sort(key=lambda r: -r["confidence"])
    kept = []
    for r in medical:
        if all(haversine((r["lon"], r["lat"]), (k["lon"], k["lat"])) > 75 for k in kept):
            kept.append(r)

    def fc(rows):
        return {"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
             "properties": {"name": r["name"], "category": r["category"]}} for r in rows]}

    c0 = poly.centroid
    print(f"  {label}: {len(schools)} schools, {len(kept)} facilities  (osm {osm_ref})")
    return {
        "name": label, "osm": osm_ref,
        "center": [round(c0.y, 5), round(c0.x, 5)],
        "boundary": {"type": "Feature", "geometry": geom, "properties": {"name": label}},
        "schools": fc(schools), "facilities": fc(kept),
    }


featured = {}
for key, query, label in PLACES:
    print(f"Building {key} …")
    featured[key] = build(key, query, label)
    time.sleep(2)

path = HERE.parent / "data" / "featured.json"
path.write_text(json.dumps(featured, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
print(f"\nWrote {path}  ({path.stat().st_size/1024:.1f} KB)")
