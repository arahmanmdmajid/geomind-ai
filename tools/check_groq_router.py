# Runs the tester questions through the REAL Groq model and compares the chosen block
# with the choices recorded from the JavaScript app (tests/router_raw.json).
#
# Usage (key from .streamlit/secrets.toml or the GROQ_API_KEY environment variable):
#     python tools/check_groq_router.py
# The free tier returns HTTP 429 after a handful of quick calls, so we pause between
# questions and wait + retry when rate limited.
import json
import os
import sys
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from geomind import ai                                     # noqa: E402
from tests.test_geomind import TESTER_QUESTIONS            # noqa: E402

key = os.environ.get("GROQ_API_KEY")
secrets = ROOT / ".streamlit" / "secrets.toml"
if not key and secrets.exists():
    key = tomllib.loads(secrets.read_text(encoding="utf-8")).get("GROQ_API_KEY")
if not key:
    sys.exit("No GROQ_API_KEY found (set the env var or .streamlit/secrets.toml).")

client = ai.make_client(key)
recorded = {r["q"]: r["raw"]["operation"] for r in json.loads((ROOT / "tests" / "router_raw.json").read_text("utf-8"))}
questions = TESTER_QUESTIONS + ["What's the weather today?"]

rows, mismatches = [], 0
for q in questions:
    for attempt in range(6):
        try:
            content = ai._chat(client, [{"role": "system", "content": ai.router_prompt("Gulberg, Lahore")},
                                        {"role": "user", "content": q}], as_json=True)
            intent = ai.normalize(json.loads(content), q)
            break
        except Exception as err:
            if getattr(err, "status_code", None) == 429 and attempt < 5:
                print("  429 rate limited — waiting 15 s")
                time.sleep(15)
                continue
            intent = {"operation": f"ERROR {ai.error_reason(err)}", "params": {}}
            break
    want = recorded.get(q)
    ok = want is None or want == intent["operation"]
    mismatches += not ok
    tag = ai.describe(intent) if not intent["operation"].startswith("ERROR") else intent["operation"]
    print(f"{'OK ' if ok else 'DIFF'} {tag:<85} <= {q}" + ("" if ok else f"   (recorded: {want})"))
    time.sleep(6)

print(f"\n{len(questions)} questions, {mismatches} differ from router_raw.json")
