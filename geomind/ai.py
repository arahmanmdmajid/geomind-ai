# =====================================================================
# ai.py — THE AI PART (and what happens when the AI is unavailable)
# ---------------------------------------------------------------------
# The AI is used for exactly two small jobs:
#
#   1. ROUTE   : read the English question and fill in ONE analysis block as JSON,
#                e.g. {"operation": "coverage", "params": {"distance_m": 500}}
#   2. EXPLAIN : re-phrase the answer WE computed in 1-3 friendly sentences,
#                using only the facts we give it (this is called "grounding").
#
# The model runs on Groq (model openai/gpt-oss-120b). The API key comes from
# Streamlit secrets (see README) — it is never written in the code.
#
# If there is no key, Groq is down, or we hit the free-tier rate limit (HTTP 429),
# we fall back to `local_route()` — a simple keyword matcher that produces the
# SAME blocks — and show our computed text as the answer. The app keeps working.
# =====================================================================

from __future__ import annotations

import json
import re

from .data import featured_in_text, featured_raw

MODEL = "openai/gpt-oss-120b"
OPERATIONS = ["find", "coverage", "distance_grid", "summary", "unsupported"]

# Does the question talk about the user's OWN position? ("near me", "my location" ...)
ME_RE = re.compile(r"\b(near|closest to|nearest to|around|from) me\b"
                   r"|\bmy (location|position|area|neighbou?rhood)\b|where i am")


def make_client(api_key: str | None):
    """Create a Groq client, or None when there is no key (keyword router only)."""
    if not api_key:
        return None
    from groq import Groq
    # max_retries=0: on a 429 we want to fall back immediately, not wait and retry.
    return Groq(api_key=api_key, max_retries=0, timeout=30)


# ---------------------------------------------------------------------
# 1. ROUTER PROMPT — the "form" the model fills in, with worked examples
# ---------------------------------------------------------------------
def router_prompt(place: str) -> str:
    return f"""You turn a question about schools and healthcare in {place} into ONE analysis block.
Return ONLY JSON: {{"operation": "...", "params": {{...}}}}.

Blocks:
- find: list or count features. params:
    target: "schools" | "facilities" | "hospitals" | "all"   ("facilities" = any medical facility, clinic, doctor, healthcare)
    relation: "all" | "within" | "beyond"
    from: "facilities" | "schools" | "hospitals" | "pin" | "me" | "centre"
          ("pin" = "this location"/"here"; "me" = "near me"/"my location"; "centre" = "this area"/"the area")
    distance_m: number in METRES (convert km to m) — needed for within/beyond
    sort: "nearest" | "farthest"
    output: "list" | "count"
    limit: number
- coverage: parts of the area farther than a distance from the target. params: target ("facilities" or "hospitals"), distance_m
- distance_grid: colour map of distance to the nearest target across the area. params: target
- summary: overview of the area (size, counts, densities, average distance, farthest school). params: {{}}
- unsupported: ONLY for topics unrelated to schools, healthcare, access, distances, buffers or maps.

Use from "me" ONLY when the user asks about their OWN position ("near me", "closest to me", "my location").
"Show me ..." does NOT mean from "me". "Closest to medical facilities" means from "facilities".
Use from "pin" for "this location", "here" or a radius with no other reference.
Add params.place ONLY when the question names a specific place (e.g. "Gulberg, Lahore").
Anything about schools, hospitals, clinics, healthcare access, buffers, distances or maps IS in scope.

Examples:
"How many schools are there?" -> {{"operation":"find","params":{{"target":"schools","relation":"all","output":"count"}}}}
"How many medical facilities are in Gulberg, Lahore?" -> {{"operation":"find","params":{{"target":"facilities","relation":"all","output":"count","place":"Gulberg, Lahore"}}}}
"Which schools are within 2 km of medical facilities?" -> {{"operation":"find","params":{{"target":"schools","relation":"within","from":"facilities","distance_m":2000}}}}
"Create a 2 km buffer around medical facilities" -> {{"operation":"find","params":{{"target":"schools","relation":"within","from":"facilities","distance_m":2000}}}}
"Which medical facilities are within 2 km of schools?" -> {{"operation":"find","params":{{"target":"facilities","relation":"within","from":"schools","distance_m":2000}}}}
"Which schools are closest to medical facilities?" -> {{"operation":"find","params":{{"target":"schools","relation":"all","from":"facilities","sort":"nearest","limit":5}}}}
"Which schools are more than 3 km from a medical facility?" -> {{"operation":"find","params":{{"target":"schools","relation":"beyond","from":"facilities","distance_m":3000}}}}
"Which school has the worst access to healthcare?" -> {{"operation":"find","params":{{"target":"schools","relation":"all","from":"facilities","sort":"farthest","limit":1}}}}
"Show me schools and medical facilities on a map" -> {{"operation":"find","params":{{"target":"all"}}}}
"What is the nearest hospital to this area?" -> {{"operation":"find","params":{{"target":"hospitals","relation":"all","from":"centre","sort":"nearest","limit":1}}}}
"Which healthcare facilities are closest to me?" -> {{"operation":"find","params":{{"target":"facilities","relation":"all","from":"me","sort":"nearest","limit":5}}}}
"Show me all schools within 3 km of this location" -> {{"operation":"find","params":{{"target":"schools","relation":"within","from":"pin","distance_m":3000}}}}
"Show me hospitals within a 5 km radius" -> {{"operation":"find","params":{{"target":"hospitals","relation":"within","from":"pin","distance_m":5000}}}}
"Which areas have schools but no medical facility within 2 km?" -> {{"operation":"coverage","params":{{"target":"facilities","distance_m":2000}}}}
"Which areas have poor access to healthcare?" -> {{"operation":"coverage","params":{{"target":"facilities"}}}}
"How far is the nearest school from each neighbourhood?" -> {{"operation":"distance_grid","params":{{"target":"schools"}}}}
"Are there enough schools in this neighbourhood?" -> {{"operation":"summary","params":{{}}}}
"What's the weather today?" -> {{"operation":"unsupported","params":{{}}}}"""


# ---------------------------------------------------------------------
# 2. KEYWORD FALLBACK — used when the AI is unreachable. Same blocks.
# ---------------------------------------------------------------------
def local_route(q: str) -> dict:
    t = q.lower()
    km = re.search(r"(\d+(?:\.\d+)?)\s*(km|kilomet)", t)
    m = re.search(r"(\d+)\s*(m\b|meter|metre)", t)
    dm = float(km.group(1)) * 1000 if km else int(m.group(1)) if m else None
    key = featured_in_text(t)
    place = featured_raw()[key]["name"] if key else None
    mentions_health = re.search(r"(hospital|clinic|medical|health|facilit|doctor|dentist)", t)

    def block(op, **params):
        return {"operation": op, "params": params}

    if not re.search(r"(school|hospital|clinic|medical|health|facilit|doctor|buffer|access|coverage|area|neighbo"
                     r"|map|distance|near|far|closest)", t):
        return block("unsupported")
    target = ("hospitals" if "hospital" in t
              else "schools" if "school" in t and not re.search(r"(facilit|clinic).*within.*school", t)
              else "facilities")
    frm = ("me" if ME_RE.search(t) else "pin" if re.search(r"this location|\bhere\b|\bpin\b|radius", t)
           else "centre" if re.search(r"this area|centre|center", t) else None)

    if re.search(r"enough|summar|overview|density", t):
        return block("summary", place=place)
    if re.search(r"each neighbo|grid|heat ?map|distance map", t):
        return block("distance_grid", target="schools" if "school" in t else "facilities", place=place)
    if re.search(r"gap|poor access|no (medical|health|hospital|clinic)|areas", t):
        return block("coverage", target="facilities", distance_m=dm, place=place)
    if frm:
        return block("find", target=target, relation="within" if dm else "all", **{"from": frm},
                     distance_m=dm, sort="nearest", limit=1 if re.search(r"\bnearest\b", t) else 5, place=place)
    if re.search(r"worst|isolated|underserved|farthest|furthest", t):
        return block("find", target="schools", relation="all", **{"from": "facilities"}, sort="farthest",
                     limit=1, place=place)
    if re.search(r"(more than|beyond|farther|further|over)", t) and dm:
        return block("find", target="schools", relation="beyond", **{"from": "facilities"}, distance_m=dm, place=place)
    if re.search(r"within|buffer|inside|fall", t):
        fac_first = re.search(r"(facilit|clinic|hospital)\w*\s+(are\s+)?within", t)
        return block("find", target=("hospitals" if target == "hospitals" else "facilities") if fac_first else "schools",
                     relation="within", **{"from": "schools" if fac_first else "facilities"}, distance_m=dm, place=place)
    if re.search(r"closest|nearest", t):
        return block("find", target="schools", relation="all", **{"from": "facilities"}, sort="nearest",
                     limit=5, place=place)
    if re.search(r"how many|number of|count", t):
        if "school" in t and mentions_health:
            return block("summary", place=place)
        return block("find", target=target, relation="all", output="count", place=place)
    if re.search(r"show|map", t):
        return block("find", target="all", place=place)
    return block("summary", place=place)


# ---------------------------------------------------------------------
# 3. NORMALIZE — clean up whatever the model (or fallback) returned
# ---------------------------------------------------------------------
def normalize(intent: dict | None, q: str) -> dict:
    """Map loose values onto our known ones and fix common model slips.

    Examples of slips we correct using the wording of the question itself:
      * "Show me schools ..." -> the model sometimes picks from="me"; "show me" is not about location.
      * "closest to medical facilities" -> measure from facilities, not from the user.
    """
    intent = intent if isinstance(intent, dict) else {}
    op = intent.get("operation") if intent.get("operation") in OPERATIONS else "summary"
    p = intent.get("params") if isinstance(intent.get("params"), dict) else {}

    def norm(v):
        return "" if v is None else str(v).lower()

    def kind(v):
        v = norm(v)
        return ("hospitals" if "hosp" in v else "schools" if "school" in v
                else "all" if re.search(r"all|both", v) else "facilities")

    out = {"_q": q.lower()}
    if p.get("target"):
        out["target"] = kind(p["target"])
    if p.get("from"):
        f = norm(p["from"])
        out["from"] = ("me" if re.search(r"(^me$|user|my)", f) else "pin" if re.search(r"pin|location|point|here", f)
                       else "centre" if re.search(r"cent|area", f)
                       else "facilities" if kind(f) == "all" else kind(f))
    if p.get("relation"):
        r = norm(p["relation"])
        out["relation"] = ("within" if re.search(r"within|inside|near", r)
                           else "beyond" if re.search(r"beyond|more|far|outside", r) else "all")
    if p.get("sort"):
        out["sort"] = "farthest" if "far" in norm(p["sort"]) else "nearest"
    if p.get("output"):
        out["output"] = "count" if "count" in norm(p["output"]) else "list"
    try:
        dm = float(p.get("distance_m"))
        if dm > 0:
            out["distance_m"] = dm * 1000 if dm < 50 else dm    # guard against km sent as metres
    except (TypeError, ValueError):
        pass
    try:
        lim = int(float(p.get("limit")))
        if lim > 0:
            out["limit"] = lim
    except (TypeError, ValueError):
        pass
    if isinstance(p.get("place"), str) and p["place"]:
        out["place"] = p["place"]
    if op == "find" and not out.get("target"):
        out["target"] = "schools"

    # --- guard rails ---
    t = out["_q"]
    if out.get("from") == "me" and not ME_RE.search(t):
        if re.search(r"this location|\bhere\b|radius|this point|\bpin\b", t):
            out["from"] = "pin"
        elif out.get("target") == "schools" and re.search(r"(facilit|clinic|hospital|medical|health)", t):
            out["from"] = "hospitals" if "hospital" in t else "facilities"
        elif out.get("target") != "schools" and "school" in t:
            out["from"] = "schools"
        else:
            out["from"] = "centre"
    if (op == "find" and re.search(r"\b(show|display|map)\b", t) and "school" in t
            and re.search(r"(facilit|clinic|hospital|medical)", t) and not re.search(r"\d", t)
            and not re.search(r"(within|beyond|more than|closest|nearest)", t)):
        out["target"] = "all"
    if op == "find" and out.get("relation") not in (None, "all") and not out.get("from"):
        out["from"] = "facilities" if out.get("target") == "schools" else "schools"
    return {"operation": op, "params": out}


def describe(intent: dict) -> str:
    """The block tag shown under each answer, e.g. 'find(target=schools, relation=within, distance_m=500)'."""
    parts = []
    for k, v in intent["params"].items():
        if k.startswith("_") or k == "place" or v in (None, ""):
            continue
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        parts.append(f"{k}={v}")
    return f"{intent['operation']}({', '.join(parts)})"


# ---------------------------------------------------------------------
# 4. CALLING GROQ (with fallbacks)
# ---------------------------------------------------------------------
def _chat(client, messages, as_json: bool) -> str:
    kwargs = {"model": MODEL, "temperature": 0 if as_json else 0.3, "messages": messages}
    if as_json:
        kwargs["response_format"] = {"type": "json_object"}   # forces valid JSON we can parse
    r = client.chat.completions.create(**kwargs)
    return r.choices[0].message.content or ""


def error_reason(err: Exception) -> str:
    """Short, user-friendly reason: 'rate limited (429)' or the error class name."""
    status = getattr(err, "status_code", None)
    return "rate limited (429)" if status == 429 else f"{type(err).__name__}"


def route(q: str, place: str, client) -> tuple[dict, str | None]:
    """Question -> raw block. Returns (intent, fallback_reason); reason is None when the AI answered."""
    if client is None:
        return local_route(q), "no Groq key"
    try:
        content = _chat(client, [{"role": "system", "content": router_prompt(place)},
                                 {"role": "user", "content": q}], as_json=True)
        return json.loads(content), None
    except Exception as err:            # rate limit, network, bad JSON ... -> keyword router
        return local_route(q), error_reason(err)


def explain(q: str, result: dict, place: str, source: str, client) -> str:
    """Friendly re-phrasing of OUR computed text. Falls back to the computed text itself."""
    if client is None:
        return result["text"]
    system = (f"You are a GIS analyst for {place}. Explain the analysis result in 1-3 friendly sentences. "
              "Use PLAIN TEXT ONLY: no markdown, no asterisks, no bold, no bullet points. "
              "Use ONLY the facts given: never add names, numbers, benchmarks or standards that are not in them. "
              f"If the facts list names, mention up to 5 of them. The data source is {source}; "
              '"medical facilities" means hospitals, clinics, medical centres and dentists.')
    try:
        text = _chat(client, [{"role": "system", "content": system},
                              {"role": "user", "content": f"Question: {q}\nFacts: {result['text']}"}], as_json=False)
        return text.strip() or result["text"]
    except Exception:
        return result["text"]
