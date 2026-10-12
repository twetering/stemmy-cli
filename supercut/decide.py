"""
Zinnen scoren met de OpenAI Decisions API (gpt-6-luna).

Eén call per zin, negen vragen tegelijk; antwoorden zijn kansen/scores,
dus je kunt sorteren ('de 30 grappigste zinnen die zonder context werken').
"""
from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from .corpus import ROOT

URL = "https://api.openai.com/v1/decisions"

LEAD = ("Je beoordeelt één DOELZIN uit een Nederlandstalige podcast (soms Engels). De zin wordt als losse "
        "audioclip gebruikt in een compilatie van de interessantste, grappigste, raarste en kwetsbaarste "
        "uitspraken uit duizenden uren podcast. De context staat er alleen ter info; beoordeel de DOELZIN. "
        "Let op: de tekst is automatisch getranscribeerd en kan kleine fouten bevatten.")

LV = lambda *xs: [dict(label=a, description=b) for a, b in xs]

QUESTIONS = [
    dict(type="predicate", name="zelfstandig",
         instructions="Werkt de doelzin als losse clip, zonder de context: is hij af, begrijpelijk en niet "
                      "afhankelijk van eerder gezegde dingen (zoals 'dat', 'hij', 'die' zonder duidelijke betekenis)?"),
    dict(type="predicate", name="grappig", instructions="Is de doelzin op zichzelf grappig of geestig voor een luisteraar?"),
    dict(type="predicate", name="raar", instructions="Is de doelzin raar, absurd, bizar of verrassend als je hem los hoort?"),
    dict(type="predicate", name="kwetsbaar",
         instructions="Is de doelzin persoonlijk en kwetsbaar: een bekentenis, twijfel, angst, verdriet, dankbaarheid of emotie over zichzelf?"),
    dict(type="predicate", name="ongemakkelijk", instructions="Is de doelzin pijnlijk, ongemakkelijk of cringe om terug te horen?"),
    dict(type="predicate", name="voorspelling", instructions="Doet de doelzin een uitspraak of voorspelling over de toekomst?"),
    dict(type="score", name="stellig", instructions="Hoe stellig of overtuigd is de spreker in de doelzin?",
         levels=LV(("twijfelend", "aarzelend, vragend, voorzichtig"), ("neutraal", "gewone mededeling"),
                   ("stellig", "duidelijke overtuiging"), ("absoluut", "groot, onvoorwaardelijk statement"))),
    dict(type="score", name="citeerbaar",
         instructions="Hoe goed is de doelzin als losse quote in een compilatie die mensen willen delen?",
         levels=LV(("waardeloos", "vulsel, afgebroken of betekenisloos"), ("zwak", "begrijpelijk maar oninteressant"),
                   ("aardig", "heeft iets"), ("goed", "sterk, je onthoudt hem"), ("top", "zou je meteen delen"))),
    dict(type="choice", name="toon", instructions="Wat is de toon van de doelzin?",
         choices=[dict(value=v, description=d) for v, d in [
             ("enthousiast", "opgetogen, verkopend"), ("sarcastisch", "ironisch, spottend"),
             ("twijfelend", "onzeker, zoekend"), ("boos", "geïrriteerd, fel"), ("ontroerd", "geraakt, teder"),
             ("zakelijk", "informatief, neutraal"), ("anders", "past niet hierboven")]]),
]


def _key():
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=True)
    return os.environ["OPENAI_API_KEY"]


def make_input(before: list[str], target: str, after: list[str]) -> str:
    ctx_b = " ".join(before).strip()
    ctx_a = " ".join(after).strip()
    return f"{LEAD}\n\nCONTEXT ERVOOR: {ctx_b or '-'}\n\nDOELZIN: {target}\n\nCONTEXT ERNA: {ctx_a or '-'}"


def decide(inp: str, session: requests.Session | None = None, retries: int = 4) -> dict:
    s = session or requests
    for k in range(retries):
        t0 = time.time()
        r = s.post(URL, headers={"Authorization": "Bearer " + _key()},
                   json=dict(model="gpt-6-luna", input=inp, questions=QUESTIONS), timeout=60)
        if r.status_code == 200:
            d = r.json()
            out = {"_latency": time.time() - t0, "_tokens": d.get("usage", {}).get("input_tokens", 0)}
            for a in d["answers"]:
                if a["type"] == "predicate":
                    out[a["name"]] = a["probability"]
                elif a["type"] == "score":
                    out[a["name"]] = a["score"]
                elif a["type"] == "choice":
                    out[a["name"]] = a["choice"]
                else:
                    out[a["name"]] = None          # refusal
            return out
        if r.status_code in (429, 500, 502, 503):
            time.sleep(1.5 * (k + 1))
            continue
        raise RuntimeError(f"{r.status_code}: {r.text[:300]}")
    raise RuntimeError("te veel retries")


def decide_many(inputs: list[str], workers: int = 16) -> list[dict]:
    import threading
    local = threading.local()

    def one(inp):
        if not hasattr(local, "s"):
            local.s = requests.Session()
        try:
            return decide(inp, local.s)
        except Exception as e:
            return {"_error": str(e)[:200]}
    with ThreadPoolExecutor(workers) as ex:
        return list(ex.map(one, inputs))
