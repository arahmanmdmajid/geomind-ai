# =====================================================================
# mapview.py — DRAWING THE MAP (Folium = Leaflet maps from Python)
# ---------------------------------------------------------------------
# The map is drawn in two parts, because of how streamlit-folium works:
#
#   base_map()     the basemap tiles, area boundary and every school/facility dot.
#                  This only changes when the area or the light/dark theme changes.
#   answer_layer() the current answer (buffers, highlighted hits, lines, shaded gaps,
#                  grid squares) plus the user's pin. It is passed to st_folium as a
#                  "feature group to add", which updates the map WITHOUT rebuilding it,
#                  so the user's zoom and position are kept when they click the map.
# =====================================================================

from __future__ import annotations

import math

import folium
from shapely.geometry import mapping

from .analysis import SING, fmt
from .data import Area

# Esri "Canvas" basemaps: free, no key, one for each theme.
# max_native_zoom=16 stops blank tiles when zooming in past the tiles that exist.
TILES = {
    "light": "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}",
    "dark": "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}",
}

# The same colour tokens as the JavaScript app, for both themes.
COLORS = {
    "light": {"school": "#2563eb", "facility": "#e11d48", "near": "#059669", "isolate": "#d97706",
              "accent": "#1d4ed8", "ring": "#ffffff"},
    "dark": {"school": "#60a5fa", "facility": "#fb7185", "near": "#34d399", "isolate": "#fbbf24",
             "accent": "#3b82f6", "ring": "#0b1220"},
}


def colors(theme: str) -> dict:
    return COLORS["dark" if theme == "dark" else "light"]


def _latlon(lonlat):
    """Our data stores (lon, lat) like GeoJSON; Folium wants [lat, lon]."""
    return [lonlat[1], lonlat[0]]


def dot(lonlat, fill, C, popup=None, radius=6, **extra):
    # bubbling_mouse_events=False: clicking a dot opens its popup without also dropping a pin.
    return folium.CircleMarker(_latlon(lonlat), radius=radius, color=C["ring"], weight=1.5, fill=True,
                               fill_color=fill, fill_opacity=1, popup=popup, bubbling_mouse_events=False, **extra)


# ---------------------------------------------------------------------
# Base map
# ---------------------------------------------------------------------
def base_map(area: Area | None, theme: str, center, zoom) -> folium.Map:
    C = colors(theme)
    m = folium.Map(location=center, zoom_start=zoom, tiles=None, control_scale=True)
    folium.TileLayer(TILES["dark" if theme == "dark" else "light"], attr="Tiles © Esri",
                     max_native_zoom=16, max_zoom=19, name="Basemap").add_to(m)
    if area is None:
        return m
    folium.GeoJson(mapping(area.boundary), name="Boundary", interactive=False,
                   style_function=lambda _: {"color": C["accent"], "weight": 2, "fillOpacity": 0.05}).add_to(m)
    for _, r in area.facilities.iterrows():
        dot((r.geometry.x, r.geometry.y), C["facility"], C, popup=r["name"] or "Medical facility").add_to(m)
    for _, r in area.schools.iterrows():
        dot((r.geometry.x, r.geometry.y), C["school"], C, popup=r["name"] or "School").add_to(m)
    return m


# ---------------------------------------------------------------------
# Answer layer (redrawn on every rerun; cheap)
# ---------------------------------------------------------------------
def answer_layer(result: dict | None, pin, theme: str) -> tuple[folium.FeatureGroup, list[dict]]:
    """Returns (feature_group, extra_legend_rows)."""
    C = colors(theme)
    fg = folium.FeatureGroup(name="Answer")
    legend: list[dict] = []

    if pin:
        dot(pin, C["accent"], C, radius=8, tooltip="Your pin").add_to(fg)
        legend.append({"color": C["accent"], "label": "Your pin", "round": True})

    if not result:
        return fg, legend
    op = result["op"]

    if op == "find" and result.get("target") != "all" and result.get("ref"):
        ref, d = result["ref"], result.get("d")
        col = C["near"] if result["relation"] == "within" else C["isolate"] if result["relation"] == "beyond" else C["accent"]
        if ref["kind"] == "point":
            dot(ref["coords"], C["accent"], C, radius=7, tooltip=ref["label"]).add_to(fg)
            if ref["label"] != "your map pin":          # the pin already has its own legend row
                legend.append({"color": C["accent"], "label": ref["label"][0].upper() + ref["label"][1:],
                               "round": True})
            if d:
                folium.Circle(_latlon(ref["coords"]), radius=d, color=C["accent"], weight=1.5, dash_array="6 5",
                              fill=True, fill_opacity=0.04, interactive=False).add_to(fg)
        elif d and len(ref.get("coords_list", [])) <= 150:
            # a buffer ring around every reference feature
            for c in ref["coords_list"]:
                folium.Circle(_latlon(c), radius=d, color=C["near"], weight=1, fill=True, fill_opacity=0.05,
                              interactive=False).add_to(fg)
        for i in result["items"]:
            folium.CircleMarker(_latlon(i["coords"]), radius=9, color=col, weight=3, fill=True, fill_color=col,
                                fill_opacity=0.85, bubbling_mouse_events=False,
                                popup=f"{i['name']} — {fmt(i['dist'])}").add_to(fg)
        for i in result["shown"]:
            if i.get("near"):
                folium.PolyLine([_latlon(i["coords"]), _latlon(i["near"])], color=col, weight=2, dash_array="5 5",
                                interactive=False).add_to(fg)
        if result["relation"] != "all":
            legend.append({"color": col, "label": f"{'Within' if result['relation'] == 'within' else 'More than'} {fmt(d)}",
                           "round": True})

    elif op == "coverage":
        if result.get("gap"):
            folium.GeoJson(result["gap"], interactive=False,
                           style_function=lambda _: {"stroke": False, "fillColor": C["isolate"], "fillOpacity": 0.35}).add_to(fg)
        if len(result["target_coords"]) <= 150:
            for c in result["target_coords"]:
                folium.Circle(_latlon(c), radius=result["d"], color=C["near"], weight=1, fill=False,
                              interactive=False).add_to(fg)
        for s in result["in_gap"]:
            folium.CircleMarker(_latlon(s["coords"]), radius=9, color=C["isolate"], weight=3, fill=True,
                                fill_color=C["school"], fill_opacity=1, bubbling_mouse_events=False,
                                popup=f"{s['name']} — in a coverage gap").add_to(fg)
        legend.append({"color": C["isolate"], "label": f"More than {fmt(result['d'])} from a {SING[result['target']]}"})

    elif op == "distance_grid":
        ramp = [row["color"] for row in result["legend"]]
        features = [{"type": "Feature", "geometry": c["geom"], "properties": {"color": ramp[min(c["cls"], len(ramp) - 1)]}}
                    for c in result["cells"]]
        folium.GeoJson({"type": "FeatureCollection", "features": features}, interactive=False,
                       style_function=lambda f: {"stroke": False, "fillColor": f["properties"]["color"],
                                                 "fillOpacity": 0.5}).add_to(fg)
        legend.append({"color": "transparent", "label": f"Distance to nearest {SING[result['target']]}"})
        legend.extend(result["legend"])

    elif op == "summary" and result.get("worst"):
        w = result["worst"]
        folium.PolyLine([_latlon(w["school"]), _latlon(w["facility"])], color=C["isolate"], weight=3,
                        dash_array="6 6").add_to(fg)
        folium.CircleMarker(_latlon(w["school"]), radius=10, color=C["isolate"], weight=3, fill=True,
                            fill_color=C["isolate"], fill_opacity=0.9,
                            popup=f"{w['name']} — farthest from healthcare ({fmt(w['dist'])})").add_to(fg)
    return fg, legend


# ---------------------------------------------------------------------
# Choosing a view (centre + zoom) that fits some points
# ---------------------------------------------------------------------
def view_for_bounds(west, south, east, north, width_px=900, height_px=620, pad=0.15, max_zoom=17):
    """Centre (lat, lon) and zoom so that the box fits in the map.

    Web maps use the Web Mercator projection: at zoom z the whole world is 256 * 2^z pixels
    wide, so we solve for the largest z where the box still fits (with some padding)."""
    def merc_y(lat):
        lat = max(min(lat, 85), -85)
        return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))

    lon_span = max(east - west, 1e-6) * (1 + 2 * pad)
    y_span = max(merc_y(north) - merc_y(south), 1e-6) * (1 + 2 * pad)
    zx = math.log2(width_px * 360 / (256 * lon_span))
    zy = math.log2(height_px * 2 * math.pi / (256 * y_span))
    zoom = int(max(2, min(max_zoom, math.floor(min(zx, zy)))))
    return [(south + north) / 2, (west + east) / 2], zoom


def view_for_area(area: Area):
    return view_for_bounds(*area.boundary.bounds)


def view_for_result(area: Area, result: dict):
    """Zoom to the answer: the listed hits for 'find', otherwise the whole area."""
    if result["op"] == "find" and result.get("shown"):
        pts = [i["coords"] for i in result["shown"]]
        ref = result["ref"]
        if ref["kind"] == "point":
            pts.append(ref["coords"])
            if result.get("d"):
                # include the whole search circle: d metres north/south/east/west of the reference point
                lon, lat = ref["coords"]
                dlat = result["d"] / 111320
                dlon = result["d"] / (111320 * max(math.cos(math.radians(lat)), 0.1))
                pts += [(lon - dlon, lat - dlat), (lon + dlon, lat + dlat)]
        if len(pts) == 1:
            return [pts[0][1], pts[0][0]], 16
        lons, lats = [p[0] for p in pts], [p[1] for p in pts]
        return view_for_bounds(min(lons), min(lats), max(lons), max(lats), pad=0.1)
    return view_for_area(area)
