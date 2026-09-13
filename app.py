# =====================================================================
# GeoMind AI — Streamlit edition
# Ask in plain English how close schools are to medical facilities, and get
# an answer computed from open map data and drawn on a map.
# ---------------------------------------------------------------------
# HOW IT WORKS (the big picture):
#   1. Pick an area: a featured district (prebuilt Overture Maps data) or any
#      place you search for (live OpenStreetMap data).              -> geomind/data.py
#   2. Ask a question. The AI's ONLY job is to fill in one of five analysis
#      "building blocks" (find / coverage / distance_grid / summary / unsupported).
#                                                                   -> geomind/ai.py
#   3. Our GeoPandas code runs that block and computes the answer.  -> geomind/analysis.py
#   4. The answer is drawn on the map and explained in the chat.    -> geomind/mapview.py
#
# STREAMLIT IN ONE SENTENCE: Streamlit re-runs this whole file from top to bottom
# on EVERY click, so anything we must remember between clicks (chat history, the
# loaded area, the pin ...) lives in `st.session_state`.
#
# Run locally:   streamlit run app.py
# =====================================================================

import os

import streamlit as st
from streamlit_folium import st_folium
from streamlit_geolocation import streamlit_geolocation
from streamlit_searchbox import st_searchbox
from shapely.geometry import Point

from geomind import ai, analysis, data, mapview, suggest

# set_page_config MUST be the first Streamlit command, or Streamlit raises an error.
st.set_page_config(page_title="GeoMind AI", page_icon="🗺️", layout="wide")

# A little CSS: less empty space above the page, so the map and chat fit on one screen.
st.markdown("<style>.block-container{padding-top:2.2rem;padding-bottom:1rem}</style>", unsafe_allow_html=True)


# =====================================================================
# 1. SESSION STATE — the app's memory between reruns
# =====================================================================
DEFAULTS = {
    "messages": [],        # chat history: [{"role": "user"|"assistant", "content": str, "tag": str|None}]
    "area": None,          # the loaded data.Area (or None)
    "answer": None,        # the latest analysis result dict (drawn on the map)
    "pin": None,           # (lon, lat) of the user's map pin
    "me": None,            # (lon, lat) from the browser's geolocation
    "view": ([25.0, 55.0], 4),   # (centre [lat, lon], zoom) the map should fly to
    "chips": suggest.STARTERS,   # suggested-question buttons
    "queued_question": None,     # set by buttons / chat input, processed at the top of the next run
    "queued_area": None,         # ("featured", key) or ("search", props)
    "pending_me_question": None, # a "near me" question waiting for the user's location
    "last_click": None,          # last map click we handled (so we don't handle it twice)
    "last_geo": None,            # last geolocation reading we handled
}
for k, v in DEFAULTS.items():
    if k not in st.session_state:
        st.session_state[k] = v
S = st.session_state   # short alias used below


def say(text, tag=None):
    """Add an assistant message to the chat."""
    S.messages.append({"role": "assistant", "content": text, "tag": tag})


if not S.messages:
    say("Search for a location or tap a featured district, then ask about its schools and healthcare.")


# =====================================================================
# 2. AI CLIENT — key from Streamlit secrets (or an environment variable locally)
# =====================================================================
def groq_key():
    try:
        if "GROQ_API_KEY" in st.secrets:
            return st.secrets["GROQ_API_KEY"]
    except Exception:          # no secrets.toml at all -> that's fine, keyword router only
        pass
    return os.environ.get("GROQ_API_KEY")


@st.cache_resource            # one client for the whole server, not one per click
def get_client(key):
    return ai.make_client(key)


client = get_client(groq_key())


# =====================================================================
# 3. LOADING AREAS (cached, so returning to an area is instant)
# =====================================================================
# @st.cache_data: run once for the same arguments, then reuse the stored result.
@st.cache_data(show_spinner=False)
def cached_featured(key):
    return data.load_featured(key)


@st.cache_data(show_spinner=False, ttl=3600)
def cached_live(props_items):
    # props are passed as a tuple of (key, value) pairs because cache keys must be hashable
    props = dict(props_items)
    return data.load_live(props)


@st.cache_data(show_spinner=False, ttl=3600)
def cached_around(lon, lat):
    return data.load_around_point(lon, lat)


@st.cache_data(show_spinner=False, ttl=600)
def cached_photon(term):
    return data.photon_search(term)


def freeze(props):
    """Make a Photon result hashable for the cache (lists -> tuples)."""
    return tuple(sorted((k, tuple(v) if isinstance(v, list) else v) for k, v in props.items()))


def on_area_loaded(area):
    """Runs once whenever a new area becomes active (mirrors onLocalityLoaded in the JS app)."""
    S.area, S.answer, S.pin = area, None, None
    S.view = mapview.view_for_area(area)
    ns, nf = len(area.schools), len(area.facilities)
    if ns == 0 or nf == 0:
        say(f"I found very little mapped data for {area.name} ({ns} schools, {nf} medical facilities), "
            "so most analyses won't work. Try a featured district or a larger area.")
        S.chips = []
        return
    say(f"Now showing {area.name}: {ns} schools and {nf} medical facilities. "
        "Tip: click the map to drop a pin for “near this location” questions.")
    S.chips = suggest.build_suggestions(area)


def load_area(kind, value):
    """Load a featured or searched area; on failure keep the previous area and say so."""
    try:
        with st.spinner("Loading data…" if kind == "featured" else
                        "Fetching live OpenStreetMap data (can take up to 25 s)…"):
            if kind == "featured":
                area = cached_featured(value)
            else:
                key = data.match_featured(value)          # a search can land on a featured district
                area = cached_featured(key) if key else cached_live(freeze(value))
    except Exception as err:
        say(f"Couldn't load data for that area ({type(err).__name__}) — try again or pick a featured district.")
        return
    on_area_loaded(area)


# =====================================================================
# 4. ANSWERING A QUESTION (mirrors ask() in the JS app)
# =====================================================================
def handle_place(place):
    """A place named in the question: switch if it's a featured district, else ask the user to search.

    Returns "same", "switched" or "unknown"."""
    import re
    if not place or re.match(r"^(this|here|the|my|our)\b", place.strip(), re.I) \
            or re.search(r"neighbo|area|district|location|city", place, re.I):
        return "same"
    first = place.split(",")[0].strip().lower()
    if S.area and first in S.area.name.lower():
        return "same"
    key = data.featured_in_text(place)
    if key:
        if S.area and S.area.key == key:
            return "same"
        on_area_loaded(cached_featured(key))
        return "switched"
    return "unknown"


def ensure_area_around_user():
    """Before a 'near me' analysis: make sure the loaded area contains the user.

    Returns "inside", "moved", "no_location" or "failed"."""
    if not S.me:
        return "no_location"
    if S.area and S.area.boundary.contains(Point(S.me)):
        return "inside"
    if S.area:
        away = analysis.Projected(S.area).project_point(S.me).distance(analysis.Projected(S.area).boundary.centroid)
        say(f"You're about {analysis.fmt(away)} from {S.area.short_name}, so I'm moving the map to your area…")
    else:
        say("Finding the area around your location…")
    try:
        with st.spinner("Loading data around your location…"):
            area = cached_around(round(S.me[0], 4), round(S.me[1], 4))
    except Exception:
        return "failed"
    on_area_loaded(area)
    return "moved"


def process_question(q, echo=True):
    q = (q or "").strip()
    if not q:
        return
    if echo:
        S.messages.append({"role": "user", "content": q, "tag": None})
    about_me = bool(ai.ME_RE.search(q.lower()))

    # No area yet: a featured district named in the question loads it; otherwise ask for one.
    if S.area is None:
        key = data.featured_in_text(q)
        if key:
            on_area_loaded(cached_featured(key))
        elif not about_me:
            say("Choose a location first — use the search box at the top or tap a featured district, "
                "or ask about schools near you.")
            return

    with st.spinner("Thinking…"):
        raw, fallback = ai.route(q, S.area.name if S.area else "the user's area", client)
    intent = ai.normalize(raw, q)
    p = intent["params"]

    # "Near me": move to the user's own neighbourhood first, then analyse there.
    if p.get("from") == "me":
        state = ensure_area_around_user()
        if state == "no_location":
            S.pending_me_question = q       # answered again automatically once the location arrives
            if S.area is None:
                say("I need your location to answer that. Tap the 📍 button at the top right and allow "
                    "access — or search for an area instead.")
                return
        elif S.area is None:
            say("I couldn't load map data around your location. Search for an area instead.")
            return

    if handle_place(p.get("place")) == "unknown":
        say(f"I don't have {p['place']} loaded. Search for it at the top, then ask again.")
        return
    if intent["operation"] == "unsupported":
        say(f"I can answer questions about schools and healthcare in {S.area.name}: counts, distances, "
            "buffers, coverage gaps, nearby places and area summaries.", tag=ai.describe(intent))
        return

    try:
        with st.spinner("Running the analysis…"):
            result = analysis.run_op(S.area, intent["operation"], p, pin=S.pin, me=S.me)
            text = ai.explain(q, result, S.area.name, S.area.source, client if fallback is None else None)
    except Exception as err:
        say(f"Something went wrong running that analysis ({type(err).__name__}). Try rephrasing the question.")
        return
    S.answer = result
    S.view = mapview.view_for_result(S.area, result)
    tag = ai.describe(intent) + (f"  ·  keyword router ({fallback})" if fallback else "")
    say(text, tag=tag)
    S.chips = suggest.follow_ups(S.area, result)


# --- callbacks: buttons only QUEUE work; it runs at the top of the next rerun ---
def queue_question(q):
    S.queued_question = q


def queue_featured(key):
    S.queued_area = ("featured", key)


def on_chat_submit():
    S.queued_question = S.chat_box


# Simulated position for testing "near me": open the app with ?me=24.79,46.74 (lat,lon).
if "me" in st.query_params and S.me is None:
    try:
        lat, lon = map(float, st.query_params["me"].split(","))
        S.me = (lon, lat)
    except ValueError:
        pass

# Process queued work BEFORE drawing anything, so this run already shows the results.
if S.queued_area:
    kind, value = S.queued_area
    S.queued_area = None
    load_area(kind, value)
if S.queued_question:
    q, S.queued_question = S.queued_question, None
    process_question(q)


# =====================================================================
# 5. TOP BAR — title, search, featured districts, your location
# =====================================================================
# No sidebar: everything fits on one screen, like the JavaScript app.
theme = "dark" if (st.context.theme.type == "dark") else "light"

title_col, search_col, featured_col, geo_col = st.columns([0.9, 1.6, 1.3, 0.35], vertical_alignment="center")

with title_col:
    st.markdown("### 🗺️ GeoMind AI")

with search_col:
    # --- search with live suggestions (Photon) ---
    def search_places(term):
        try:
            return [(data.describe_place(p), p) for p in cached_photon(term)]
        except Exception:
            return []

    def on_pick(props):          # called once when the user picks a suggestion
        S.queued_area = ("search", props)

    st_searchbox(search_places, placeholder="Search a district, area or neighbourhood…",
                 key="place_search", debounce=300, clear_on_submit=True, submit_function=on_pick)
    if S.queued_area:            # a suggestion was just picked -> rerun so it loads at the top
        st.rerun()

with featured_col:
    # --- featured districts: buttons side by side ---
    with st.container(horizontal=True, vertical_alignment="center", gap="small"):
        st.caption("Featured:")
        for key, d in data.featured_raw().items():
            st.button(d["name"].split(",")[0].replace(" District", ""), key=f"feat_{key}",
                      on_click=queue_featured, args=(key,))

with geo_col:
    # --- browser geolocation: a 📍 button; the browser asks for permission ---
    geo = streamlit_geolocation()
    if geo and geo.get("latitude") is not None:
        reading = (round(geo["longitude"], 6), round(geo["latitude"], 6))
        if reading != S.last_geo:
            S.last_geo = reading
            S.me = reading
            if S.pending_me_question:
                q, S.pending_me_question = S.pending_me_question, None
                say("Got your location — answering from your position.")
                S.queued_question = None
                process_question(q, echo=False)
            st.rerun()

# --- status line: what is loaded, where the data came from, which AI is answering ---
status = []
if S.area:
    ns, nf = len(S.area.schools), len(S.area.facilities)
    dot_ = "🔴" if ns == 0 or nf == 0 else "🟢" if S.area.source == data.OVERTURE else "🟠"
    status.append(f"{dot_} **{S.area.name}** · {S.area.source} · {ns} schools · {nf} medical facilities")
    if S.area.source == data.LIVE_OSM:
        status.append("live OpenStreetMap coverage varies by city")
else:
    status.append("Pick a featured district or search for an area to begin")
status.append("📍 using your location" if S.me else "📍 button = share your location for “near me”")
status.append("AI: Groq · " + ai.MODEL if client else "AI: keyword router (no GROQ_API_KEY)")
st.caption("  ·  ".join(status))


# =====================================================================
# 6. MAIN AREA — map on the left, chat on the right (always visible, no scrolling)
# =====================================================================
MAP_HEIGHT = 540      # fits a 768 px-tall laptop screen together with the top bar
map_col, chat_col = st.columns([0.62, 0.38], gap="medium")

# ---------------- the chat panel ----------------
with chat_col:
    # Chat history in a fixed-height box. autoscroll keeps the newest message in view.
    chat = st.container(height=MAP_HEIGHT - 230, autoscroll=True)
    for msg in S.messages:
        with chat.chat_message(msg["role"]):
            st.write(msg["content"])
            if msg.get("tag"):
                st.caption(f"`{msg['tag']}`")   # the analysis block that produced this answer

    # The question box sits right under the history, so it is always on screen.
    st.chat_input("Ask about schools & healthcare…", key="chat_box", on_submit=on_chat_submit)

    # Suggested questions: small buttons that run immediately when clicked.
    st.caption("Try:")
    with st.container(horizontal=True, gap="small"):
        for i, text in enumerate(S.chips):
            st.button(text, key=f"chip_{i}_{text}", on_click=queue_question, args=(text,))

# ---------------- the map ----------------
map_col_ctx = map_col.container()

C = mapview.colors(theme)
fg, extra_legend = mapview.answer_layer(S.answer, S.pin, theme)

# Legend (small coloured dots above the map)
legend = [{"color": C["school"], "label": "Schools", "round": True},
          {"color": C["facility"], "label": "Medical facilities", "round": True}] + extra_legend
chips_html = " ".join(
    f"<span style='display:inline-flex;align-items:center;gap:5px;margin-right:12px;font-size:0.85rem'>"
    f"<i style='width:10px;height:10px;display:inline-block;background:{row['color']};"
    f"border-radius:{'50%' if row.get('round') else '2px'}'></i>{row['label']}</span>"
    for row in legend)
map_col_ctx.markdown(chips_html, unsafe_allow_html=True)

center, zoom = S.view
# The base map is only rebuilt when the area or theme changes; answers + pin arrive through
# `feature_group_to_add`, and centre/zoom are updated in place, so clicks keep your view.
m = mapview.base_map(S.area, theme, center, zoom)
with map_col_ctx:
    out = st_folium(m, key="map", height=MAP_HEIGHT, use_container_width=True, center=center, zoom=zoom,
                    feature_group_to_add=fg, returned_objects=["last_clicked"])

# Map click -> drop a pin
click = (out or {}).get("last_clicked")
if click and S.area:
    lonlat = (round(click["lng"], 6), round(click["lat"], 6))
    if lonlat != S.last_click:
        S.last_click = lonlat
        S.pin = lonlat
        say("Pin set. Ask things like “schools within 1 km of this location”.")
        st.rerun()

if S.answer:
    map_col_ctx.caption(f"Latest answer (computed): {S.answer['text']}")
map_col_ctx.caption("Data: Overture Maps (featured districts) and OpenStreetMap contributors (live areas). "
           "Basemap tiles © Esri. Search by Photon, boundaries by Nominatim.")
