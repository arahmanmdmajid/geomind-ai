"""GeoMind AI (Streamlit edition) — helper package.

The app is split into small, single-purpose modules so each one is easy to read:

    data.py      where the schools / medical facilities come from (featured files or live OpenStreetMap)
    analysis.py  the five analysis "building blocks" that compute every answer (GeoPandas)
    ai.py        the AI router (question -> block), keyword fallback, and the explanation step
    suggest.py   the suggested-question buttons
    mapview.py   drawing everything on a Folium (Leaflet) map

None of these modules import Streamlit, so they can be tested with plain pytest.
Only app.py (the user interface) talks to Streamlit.
"""
