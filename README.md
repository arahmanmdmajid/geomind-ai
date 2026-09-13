# 🗺️ GeoMind AI — Streamlit edition

Ask in plain English how close schools are to medical facilities, and get an answer
**computed from open map data** and drawn on a map.

This is the all-Python version of GeoMind AI, built so the team (who mostly read Python)
can compare it side by side with the original JavaScript app:

| | JavaScript app | This app |
|---|---|---|
| Where | Hugging Face Space `GeoMind-AI/geomind-ai` (static page) | Streamlit Community Cloud |
| Map | Leaflet + Turf.js | Folium (Leaflet) via `streamlit-folium` + GeoPandas/Shapely |
| AI calls | Browser → Cloudflare Worker → Groq | Python server → Groq (key in Streamlit Secrets) |
| URL | https://geomind-ai-geomind-ai.static.hf.space | _add the Streamlit Cloud URL here after deploying_ |

## How it works

1. **Pick an area.** Tap a featured district (Ghirnatah, Gulberg, Clifton — prebuilt Overture Maps
   extracts in `data/featured.json`) or search any place (suggestions from Photon; schools and
   clinics downloaded live from OpenStreetMap via Overpass).
2. **Ask a question.** The AI (Groq, `openai/gpt-oss-120b`) only fills in **one of five analysis blocks**
   as JSON. It never computes a number.
3. **GeoPandas computes the answer** in the local UTM projection (metres), and the map shows it.
4. The AI re-phrases our computed facts in 1–3 sentences (it may not add facts). The **block tag**
   (e.g. `find(target=schools, relation=within, distance_m=250)`) is shown under every answer.

### The five blocks

| Block | What it does | Example question |
|---|---|---|
| `find` | list/count schools, facilities or hospitals, all / within / beyond a distance from facilities, schools, hospitals, your pin, your location or the centre | “Which schools are more than 3 km from a medical facility?” |
| `coverage` | exact gap polygons: buffer every facility → union → boundary minus union; % of area and schools in the gaps | “Which areas have poor access to healthcare?” |
| `distance_grid` | ~600 squares coloured by distance to the nearest school/facility | “How far is the nearest school from each neighbourhood?” |
| `summary` | size, counts, densities, average distance, farthest school | “Are there enough schools in this neighbourhood?” |
| `unsupported` | polite refusal for off-topic questions | “What's the weather today?” |

If the AI is unavailable (no key, error, or free-tier **rate limit 429**) a **keyword router**
picks the same blocks and the computed text is shown; the tag then says `keyword router (reason)`.

### Map features
- Click the map to drop a **pin** (“schools within 1 km of this location”).
- **Near me:** tap the 📍 button in the sidebar. If you are outside the loaded area, the app finds
  your neighbourhood (Nominatim reverse geocoding), loads it and answers from your position. Without
  a location it answers from your pin or the area centre and says so.
- Light/dark basemaps (Esri) follow the Streamlit theme (⋮ menu → Settings). Chat history is kept.
  Streamlit does not rerun the script on a theme change, so the basemap switches on your next click.

## Project layout

```
app.py                 the Streamlit UI (sidebar chat + map)
geomind/data.py        featured extracts, Photon search, Nominatim, live Overpass (raced mirrors)
geomind/analysis.py    find / coverage / distance_grid / summary with GeoPandas in UTM
geomind/ai.py          Groq client, router prompt, normalize() guard rails, keyword fallback, explain
geomind/suggest.py     data-driven suggestion buttons and follow-ups
geomind/mapview.py     Folium map: boundary, markers, answer layers, basemaps, view fitting
data/featured.json     prebuilt Overture extracts (see tools/make_featured.py)
tests/                 pytest checks (no internet or key needed)
```

## Run locally

```bash
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

Optional AI key for local runs — create `.streamlit/secrets.toml` (it is git-ignored):

```toml
GROQ_API_KEY = "your-groq-key"
```

Without a key the app still works using the keyword router.

**Testing “near me” without moving:** open `http://localhost:8501/?me=31.5120,74.3430` (latitude,longitude).

### Tests

```bash
pip install pytest
pytest
```

Checks: Ghirnatah 8 schools / 16 facilities; Gulberg 79 / 59 with 43 schools within 250 m; Clifton 54 / 35;
Ghirnatah coverage at 500 m; `normalize()` guard rails; the 18 tester questions all route to a real block with
the keyword router; weather → `unsupported`; 429 fallback; Overpass parsing and 75 m de-duplication.

`tools/check_groq_router.py` runs the tester questions against the real model (paced for the free tier)
and compares the chosen blocks with `tests/router_raw.json`.

## Deploy on Streamlit Community Cloud

1. Go to https://share.streamlit.io and sign in with GitHub.
2. **Create app** → *Deploy a public app from GitHub* → repository `arahmanmdmajid/geomind-ai`,
   branch `main`, main file `app.py`. Under **Advanced settings** pick Python 3.12.
3. In the same Advanced settings (or later: app ⋮ → **Settings → Secrets**) paste:
   ```toml
   GROQ_API_KEY = "your-groq-key"
   ```
4. **Deploy**. The first build installs GeoPandas and takes a few minutes; later cold starts are faster.
5. Put the app URL in the table at the top of this README.

## Differences from the JavaScript app (deliberate)

- **Coverage uses exact polygons** (buffer → union → difference) instead of a grid of squares, so the
  % can differ by a few points (Ghirnatah at 500 m: JS grid ≈ 39%).
- **Live OSM features are clipped to the area boundary** (the JS app keeps everything in the bounding box).
- Streamlit cannot ask for the browser location from code, so “near me” uses a 📍 button; a question
  asked before sharing the location is answered again automatically once it arrives.

Data: Overture Maps Foundation, © OpenStreetMap contributors. Tiles © Esri.
