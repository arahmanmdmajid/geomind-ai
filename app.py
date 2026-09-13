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

# A little CSS: a wider sidebar so the chat is comfortable to read.
st.markdown("<style>section[data-testid='stSidebar']{min-width:400px}</style>", unsafe_allow_html=True)


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
            say("Choose a location first — search in the sidebar or tap a featured district, "
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
                say("I need your location to answer that. Tap the 📍 button under “Your location” in the "
                    "sidebar and allow access — or search for an area instead.")
                return
        elif S.area is None:
            say("I couldn't load map data around your location. Search for an area instead.")
            return

    if handle_place(p.get("place")) == "unknown":
        say(f"I don't have {p['place']} loaded. Search for it in the sidebar, then ask again.")
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
# 5. SIDEBAR — pick an area, your location, and the chat
# =====================================================================
theme = "dark" if (st.context.theme.type == "dark") else "light"

with st.sidebar:
    st.title("🗺️ GeoMind AI")
    st.caption("Plain English → map answer. Every number is computed by GeoPandas, not invented by the AI.")

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

    # --- featured districts ---
    st.caption("Featured:")
    with st.container(horizontal=True):          # buttons side by side, wrapping when narrow
        for key, d in data.featured_raw().items():
            st.button(d["name"].split(",")[0].replace(" District", ""), key=f"feat_{key}",
                      on_click=queue_featured, args=(key,))

    # --- status badge ---
    if S.area:
        ns, nf = len(S.area.schools), len(S.area.facilities)
        dot_ = "🟢" if S.area.source == data.OVERTURE else "🟠"
        if ns == 0 or nf == 0:
            dot_ = "🔴"
        st.caption(f"{dot_} **{S.area.name}** · {S.area.source} · {ns} schools · {nf} medical facilities")
        if S.area.source == data.LIVE_OSM:
            st.caption("Live OpenStreetMap coverage varies by city; sparse mapping means lower counts.")

    # --- browser geolocation ---
    st.caption("Your location (for “near me” questions):")
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
    if S.me:
        st.caption(f"📍 Using your location ({S.me[1]:.4f}, {S.me[0]:.4f})")

    st.divider()
    AI_LABEL = "Groq · " + ai.MODEL if client else "keyword router (no GROQ_API_KEY)"
    st.caption(f"Ask GeoMind · AI: {AI_LABEL}")

    # --- chat history ---
    chat = st.container(height=380, autoscroll=True)   # autoscroll keeps the newest message in view
    for msg in S.messages:
        with chat.chat_message(msg["role"]):
            st.write(msg["content"])
            if msg.get("tag"):
                st.caption(f"`{msg['tag']}`")

    # --- suggestion buttons (run immediately) ---
    for i, text in enumerate(S.chips):
        st.button(text, key=f"chip_{i}_{text}", on_click=queue_question, args=(text,), width="stretch")

    st.chat_input("Ask about schools & healthcare…", key="chat_box", on_submit=on_chat_submit)


# =====================================================================
# 6. MAIN AREA — the map
# =====================================================================
if S.area:
    st.subheader(S.area.name)
else:
    st.subheader("Pick an area to begin")

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
st.markdown(chips_html, unsafe_allow_html=True)

center, zoom = S.view
# The base map is only rebuilt when the area or theme changes; answers + pin arrive through
# `feature_group_to_add`, and centre/zoom are updated in place, so clicks keep your view.
m = mapview.base_map(S.area, theme, center, zoom)
out = st_folium(m, key="map", height=620, use_container_width=True, center=center, zoom=zoom,
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
    st.caption(f"Latest answer (computed): {S.answer['text']}")
st.caption("Data: Overture Maps (featured districts) and OpenStreetMap contributors (live areas). "
           "Basemap tiles © Esri. Search by Photon, boundaries by Nominatim.")
