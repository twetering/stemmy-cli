"""
10 JAAR POM — v2. Zeven zenders, abrupte zaps.

  open        100 begroetingen: los → versnellend → zwerm → harde knip
  sponsor     'Deze podcast werd u aangeboden door' — organisatie-entities
  natuur      het lachen van de podcaster (ElevenLabs [lacht]-tags), eindigt in lachkoor
  tijd        één toekomstvoorspelling per jaar
  bijwerking  farma-reclame op medical_condition-entities, hartmonitor
  symfonie    zes stopwoorden als salvo's op de beat, dan alles tegelijk
  slot

    PYTHONPATH=src python -m supercut.pieces.pom_10jaar2 [--stills]
"""

from __future__ import annotations

import json
import pickle
import random
import re
import sys
from collections import Counter, defaultdict

import numpy as np
from PIL import Image, ImageDraw

from stemmy_cli.analyzers.speaker import FILLER_PATTERNS

from ..audio import SR, level
from ..corpus import ROOT
from ..laughs import extract
from ..mix import Event, highpass, impulse_response, master, render
from .. import video as V
from .pom_10jaar import NEXT, _bg, pom_rows, wrap
from .pom_horror import OUT as POM, eleven, load_mp3, load_stereo

OUT = ROOT / "output" / "supercut" / "pom10"
VOICES = {"omroeper": "pNInz6obpgDQGcFmaJgB", "verteller": "JBFqnCBsd6RMkjVDRZzb", "lab": "21m00Tcm4TlvDq8ikWAM"}
BRANDS = ["Google", "Apple", "Facebook", "Amazon", "Spotify", "Uber", "Netflix", "NRC", "NOS", "New York Times"]
KWALEN = ["corona", "ziek", "depressief", "verslaafd", "dronken", "virus", "covid"]
FILLERS = ["ja", "dus", "gewoon", "nou", "eigenlijk", "toch", "weet je", "zeg maar", "maar goed", "hoe dan ook", "anyway"]
SHOTS = 4
BPM = 132
E8 = 60 / BPM / 2

TIJD = [("2016", ["Facebook gaat dus alles kapot maken."]),
        ("2017", ["Hebben die over vijf jaar nog een baan?"]),
        ("2018", ["Nu nog hopen dat de robot die me gaat vervangen ook van podcasts kan genieten."]),
        ("2019", ["Er komt nog een veel dreigendere toekomst aan, weet je wel."]),
        ("2020", ["Want het is over vijf jaar allemaal weg."]),
        ("2021", ["Allemaal signalen voor het einde van de westerse wereld."]),
        ("2022", ["Dit is niet iets wat je gaat zien aankomen."]),
        ("2023", ["Die mensen hebben straks nog een baan en wij niet."]),
        ("2024", ["We gaan heel erg gewend raken aan AI."]),
        ("2025", ["De toekomst is hier."])]

DANK = ["Waar ben jij dankbaar voor, Alexander?", "Jezus, waar ben ik dankbaar voor?", "Alleen maar dankbaar.",
        "Het is allemaal zo tering emotioneel, want dat is het nog steeds.", "Dank je wel."]

VO = {
    "sp1": ("omroeper", "Deze podcast werd u aangeboden door:"),
    "sp2": ("omroeper", "En nog negenduizend andere merken."),
    "la1": ("verteller", "Het lachen van de podcaster. Wetenschappers vermoeden een paringsritueel."),
    "la2": ("verteller", "En dan… raakt de groep in extase."),
    "nx1": ("lab", "De toekomst. Per jaar."),
    "bw1": ("lab", "Bijwerkingen kunnen zijn:"),
    "bw2": ("lab", "Lees voor gebruik de bijsluiter."),
    "st1": ("lab", "Tien jaar stopwoorden. Eén symfonie."),
}
MUSIC = {
    "sponsor": ("upbeat corporate advertising music, ukulele, claps and glockenspiel, overly happy, 132 bpm, no vocals", 16000),
    "farma": ("soft reassuring pharmaceutical commercial music, gentle piano and warm pads, calm, no vocals", 15000),
    "dankbaar": ("tender warm solo piano, nostalgic and sincere, slow, soft, no vocals", 20000),
    "symfonie": ("driving minimal techno beat, 132 bpm, punchy kick, clinical synth stabs, no vocals", 16000),
}


# ── materiaal ────────────────────────────────────────────────────────────

def mine2():
    cache = OUT / "mine2.pkl"
    if cache.exists():
        return pickle.load(open(cache, "rb"))
    rng = random.Random(11)
    greet, brands, kwalen, fill = [], defaultdict(list), defaultdict(list), defaultdict(list)
    counts, n_words, n_orgs = Counter(), 0, 0
    for url, pub, td in pom_rows():
        d = json.loads(td)
        ws = d.get("words") or []
        n_words += len(ws)
        y = pub[:4]
        # begroetingen: eerste minuut, 'Hé Ernst', 'Hallo', 'Welkom…'
        cur = []
        for w in ws:
            if w["start"] > 90_000:
                break
            cur.append(w)
            if w["text"].rstrip().endswith((".", "?", "!", ",")) or len(cur) > 7:
                txt = " ".join(x["text"] for x in cur)
                dur = (cur[-1]["end"] - cur[0]["start"]) / 1000
                if re.match(r"^(hé|hee|hey|hoi|hallo|goedemorgen|welkom)\b", txt.lower()) and 0.4 < dur < 2.8:
                    greet.append(dict(url=url, year=y, text=txt.rstrip(","), start=cur[0]["start"] / 1000, end=cur[-1]["end"] / 1000))
                cur = []
        for e in d.get("entities") or []:
            dur = (e["end"] - e["start"]) / 1000
            if e["entity_type"] == "organization":
                n_orgs += 1
                if e["text"] in BRANDS and 0.25 < dur < 1.0:
                    brands[e["text"]].append(dict(url=url, year=y, start=e["start"] / 1000, end=e["end"] / 1000))
            if e["entity_type"] == "medical_condition":
                k = e["text"].lower()
                if (k in KWALEN or k == "tyfus") and 0.2 < dur < 1.0:
                    kwalen[k].append(dict(url=url, year=y, start=e["start"] / 1000, end=e["end"] / 1000))
        low = [w["text"].strip(".,!?…").lower() for w in ws]
        for i in range(len(ws)):
            for f in set(FILLERS) | {p for p in FILLER_PATTERNS if " " not in p}:
                toks = f.split()
                if low[i:i + len(toks)] != toks:
                    continue
                counts[f] += 1
                if f not in FILLERS:
                    continue
                a, b = ws[i], ws[i + len(toks) - 1]
                dur = (b["end"] - a["start"]) / 1000
                if 0.15 < dur < 0.25 + 0.3 * len(toks) and min(w.get("confidence", 0) for w in ws[i:i + len(toks)]) > 0.6:
                    fill[f].append(dict(url=url, year=y, start=a["start"] / 1000, end=b["end"] / 1000))
    for dct in (brands, kwalen, fill):
        for k in dct:
            rng.shuffle(dct[k])
    rng.shuffle(greet)
    S = pickle.load(open(POM / "sents.pkl", "rb"))
    S += pickle.load(open(OUT / "stt_sents.pkl", "rb"))        # 2022–2023, via ElevenLabs STT
    by_text = {}
    for s in S:
        by_text.setdefault(s["text"], s)
    need = [q for _, qs in TIJD for q in qs] + ["Hallo, toekomst?", "Tot de volgende keer."] + DANK
    res = dict(greet=greet[:110], brands=dict(brands), kwalen=dict(kwalen), fill=dict(fill), counts=counts,
               n_words=n_words, n_orgs=n_orgs, sents={k: by_text[k] for k in need if k in by_text},
               org_counts=Counter(), )
    # tellingen per merk/kwaal voor in beeld
    oc, kc = Counter(), Counter()
    for url, pub, td in pom_rows():
        for e in json.loads(td).get("entities") or []:
            if e["entity_type"] == "organization":
                oc[e["text"]] += 1
            if e["entity_type"] == "medical_condition":
                kc[e["text"].lower()] += 1
    res["org_counts"], res["kwaal_counts"] = oc, kc
    pickle.dump(res, open(cache, "wb"))
    return res


def material(m):
    segs = []
    for k, g in enumerate(m["greet"]):
        segs.append(dict(id=f"gr{k}", audio_url=g["url"], start_time=max(0, g["start"] - 0.04), end_time=g["end"] + 0.12))
    for b in BRANDS:
        for k, o in enumerate(m["brands"].get(b, [])[:3]):
            segs.append(dict(id=f"br:{b}:{k}", audio_url=o["url"], start_time=o["start"] - 0.04, end_time=o["end"] + 0.1))
    for q in KWALEN + ["tyfus"]:
        for k, o in enumerate(m["kwalen"].get(q, [])[:3]):
            segs.append(dict(id=f"kw:{q}:{k}", audio_url=o["url"], start_time=o["start"] - 0.04, end_time=o["end"] + 0.1))
    for f in FILLERS:
        for k, o in enumerate(m["fill"].get(f, [])[:SHOTS + 2]):
            segs.append(dict(id=f"fi:{f}:{k}", audio_url=o["url"], start_time=o["start"] - 0.03, end_time=o["end"] + 0.07))
    for t, s in m["sents"].items():
        segs.append(dict(id="s:" + t, audio_url=s["url"], start_time=max(0, s["start"] - 0.05), end_time=s["end"] + 0.18))
    clips = {k: highpass(v, 70) for k, v in extract(segs).items()}
    return clips


def assets():
    from concurrent.futures import ThreadPoolExecutor
    jobs = [("music", "p10_" + k, dict(prompt=p, music_length_ms=ms)) for k, (p, ms) in MUSIC.items()]
    jobs += [("tts", "p10_" + k, dict(voice=VOICES[v], text=t, model_id="eleven_multilingual_v2",
                                     voice_settings=dict(stability=0.4, similarity_boost=0.8, style=0.6)))
             for k, (v, t) in VO.items()]
    with ThreadPoolExecutor(2) as ex:
        list(ex.map(lambda j: eleven(j[0], j[1], **j[2]), jobs))


def A(kind, name):
    return POM / "assets" / f"{kind}_p10_{name}.mp3"


def beep(f=1000, dur=0.09, amp=0.3):
    t = np.arange(int(dur * SR)) / SR
    return (np.sin(2 * np.pi * f * t) * amp * np.minimum(1, (dur - t) * 60)).astype(np.float32)


# ── compositie ───────────────────────────────────────────────────────────

def compose(m, clips, laughs):
    ev, sec = [], []
    t = 0.0
    rng = np.random.default_rng(4)

    def add(x, **kw):
        ev.append(Event(t=kw.pop("at", t), x=x, **{k: v for k, v in kw.items() if k in ("gain_db", "pan", "verb", "dry")},
                        meta=kw.get("meta", {})))

    def vo(key, gap=0.12, verb=0.08):
        nonlocal t
        x = level(load_mp3(A("tts", key)), -17)
        add(x, verb=verb, meta=dict(kind="vo", text=VO[key][1]))
        t += len(x) / SR + gap

    def music(name, t0, t1, gain, src=None):
        mm = load_stereo(src or A("music", name))
        n = int((t1 - t0) * SR)
        mm = np.concatenate([mm] * (1 + n // max(1, len(mm))))[:n].copy()
        f = int(0.02 * SR)
        mm[:f] *= np.linspace(0, 1, f)[:, None]
        mm[-f:] *= np.linspace(1, 0, f)[:, None]
        for ch, p in ((0, -1.0), (1, 1.0)):
            ev.append(Event(t=t0, x=mm[:, ch], pan=p, gain_db=gain, meta=dict(kind="music")))

    def zap():
        nonlocal t
        add(level(load_mp3(A("sfx", "zap")), -20), meta=dict(kind="zap"))
        t += 0.2

    # ── OPEN: 100 begroetingen
    s0 = t
    first = clips.get("s:Hallo, toekomst?")
    if first is not None:
        add(level(first, -18), meta=dict(kind="greet", text="Hallo, toekomst?", year="2016", tile=0))
        t += len(first) / SR - 0.1
    gr = [(k, g) for k, g in enumerate(m["greet"]) if f"gr{k}" in clips]
    n = len(gr)
    # tijdstippen: 4 los, dan versnellen, dan zwerm van ~1,6s
    times, tt, step = [], t, 0.75
    for i in range(n):
        if i < 4:
            times.append(tt); tt += step
        elif i < 22:
            step *= 0.84; times.append(tt); tt += max(step, 0.07)
        else:
            times.append(tt + rng.uniform(0, 1.6))
    for i, (k, g) in enumerate(gr):
        x = level(clips[f"gr{k}"], -19)
        gain = 0 if i < 22 else -9
        add(x, at=times[i], gain_db=gain, pan=float(rng.uniform(-0.9, 0.9)) if i >= 4 else 0.0, verb=0.05,
            meta=dict(kind="greet", text=g["text"], year=g["year"], tile=i + 1))
    swarm_end = max(times) + 0.5
    t = swarm_end
    sec.append(("open_cut", t, t))
    m_ = load_stereo(A("music", "open"))
    for ch, p in ((0, -1.0), (1, 1.0)):
        ev.append(Event(t=t + 0.05, x=m_[:, ch], pan=p, gain_db=-5, meta=dict(kind="music")))
    add(np.zeros(10, np.float32), at=t + 0.05, meta=dict(kind="title"))
    t += 2.2
    sec.insert(0, ("open", s0, t))
    zap()

    # ── SPONSOR
    s0 = t
    vo("sp1", gap=0.05)
    beat = 60 / BPM
    grid = np.ceil(t / beat) * beat
    t = grid
    for rnd in range(2):
        for b in BRANDS:
            k = f"br:{b}:{rnd}"
            if k not in clips:
                continue
            x = level(clips[k], -18)
            add(x, gain_db=1, pan=float(rng.uniform(-0.3, 0.3)), meta=dict(kind="brand", text=b, n=m["org_counts"][b]))
            t += beat if rnd == 0 else beat / 2
    t += beat * 0.5
    vo("sp2", gap=0.15)
    music("sponsor", s0, t, -13)
    sec.append(("sponsor", s0, t))
    zap()

    # ── LACHPAUZE: alle lachjes tegelijk, paar seconden
    s0 = t
    L = [l for l in laughs if l["laugh"]]
    t += 0.15
    for i, l in enumerate(L):
        x = level(l["x"], -22)
        add(x, at=t + rng.uniform(0, 0.5), gain_db=-2, pan=float(rng.uniform(-1, 1)), verb=0.15,
            meta=dict(kind="laugh", year=l["year"], n=i + 1, chorus=True))
    t += 2.6
    sec.append(("lachen", s0, t))
    zap()

    # ── TIJD: één (soms twee) toekomstuitspraak per jaar, geen aankondiging
    s0 = t
    t += 0.1
    for y, qs in TIJD:
        add(np.zeros(10, np.float32), meta=dict(kind="year", year=y))
        for q in qs:
            k = "s:" + q
            if k in clips:
                x = level(clips[k], -19)
                add(x, verb=0.06, meta=dict(kind="quote", text=q, year=y))
                t += len(x) / SR - 0.3
    t += 0.2
    music("tijd", s0, t, -13, src=A("music", "tijd"))
    sec.append(("tijd", s0, t))
    zap()

    # ── BIJWERKINGEN
    s0 = t
    vo("bw1", gap=0.15)
    for q in KWALEN:
        k = f"kw:{q}:0"
        if k not in clips:
            continue
        add(beep(1000), gain_db=-12, meta=dict(kind="beep"))
        x = level(clips[k], -18)
        add(x, at=t + 0.05, meta=dict(kind="kwaal", text=q, n=m["kwaal_counts"][q]))
        t += max(len(x) / SR, 0.42) + 0.08
    t += 0.15
    for i in range(3):
        k = f"kw:tyfus:{i}"
        if k in clips:
            add(beep(1400), gain_db=-10, meta=dict(kind="beep"))
            x = level(clips[k], -17)
            add(x, at=t + 0.03, gain_db=1, meta=dict(kind="kwaal", text="tyfus", n=m["kwaal_counts"]["tyfus"], alarm=True))
            t += max(len(x) / SR, 0.3) + 0.04
    add(beep(1000, dur=1.6, amp=0.25), gain_db=-12, meta=dict(kind="flatline"))
    t += 1.7
    music("farma", s0, t, -15)
    sec.append(("bijwerking", s0, t))
    zap()

    # ── SYMFONIE: geen aankondiging, gewoon beginnen
    s0 = t
    t = np.ceil(t / E8) * E8 + E8
    for f in FILLERS:
        for i in range(SHOTS):
            k = f"fi:{f}:{i}"
            if k in clips:
                add(level(clips[k], -18), pan=(-0.5, 0.5)[i % 2] * 0.6, meta=dict(kind="filler", word=f, i=i))
            t += E8
    for rep in range(3):
        for j, f in enumerate(FILLERS):
            k = f"fi:{f}:{SHOTS + rep % 2}"
            if k in clips:
                add(level(clips[k], -21), pan=-0.9 + j * 1.8 / len(FILLERS), meta=dict(kind="filler", word=f, i=99, all=True))
        t += E8 * 2
    t += E8 * 2
    music("symfonie", s0, t, -12)
    sec.append(("symfonie", s0, t))
    zap()

    # ── DANKBAAR: langzaam, warm, het enige oprechte moment
    s0 = t
    t += 0.5
    for q in DANK:
        k = "s:" + q
        if k in clips:
            x = level(clips[k], -19)
            add(x, verb=0.18, meta=dict(kind="dank", text=q, year=m["sents"][q]["year"]))
            t += len(x) / SR - 0.1 + 0.3
    music("dankbaar", s0, t + 0.5, -14)
    sec.append(("dankbaar", s0, t))

    # ── SLOT
    s0 = t
    t += 0.2
    k = "s:Tot de volgende keer."
    t -= 0.3
    if k in clips:
        x = level(clips[k], -18)
        add(x, verb=0.25, meta=dict(kind="quote", text="Tot de volgende keer.", year=""))
        t += len(x) / SR + 0.3
    if len(L) > 6:
        add(level(L[6]["x"], -23), verb=0.4, meta=dict(kind="laugh_end"))
    t += 4.8
    sec.append(("slot", s0, t))
    return ev, sec, t


# ── beeld ────────────────────────────────────────────────────────────────

W, H = V.W, V.H
PAL = [(255, 64, 64), (255, 196, 0), (30, 120, 255), (0, 200, 120), (255, 120, 200), (120, 60, 255),
       (255, 140, 0), (0, 180, 220), (230, 230, 230), (20, 20, 20)]


OLD_A, OLD_B = (244, 146, 84), (108, 82, 226)      # oranje → paars, de cover van 2016
NEW_A, NEW_B = (44, 92, 222), (236, 128, 92)       # blauw → oranje, de huidige huisstijl
NEW_FRAME = (236, 128, 92)


def diag(a, b):
    """Diagonaal verloop (linksboven → rechtsonder), zoals op beide covers."""
    yy, xx = np.mgrid[0:H, 0:W]
    f = ((xx / W) * 0.45 + (yy / H) * 0.55)[..., None]
    arr = np.array(a, np.float32) * (1 - f) + np.array(b, np.float32) * f
    return Image.fromarray(arr.astype(np.uint8))


def win98(d, box, title, active=True):
    """Windows-98-venster: grijze bevel, blauwe titelbalk, _ □ X."""
    x0, y0, x1, y1 = box
    d.rectangle(box, fill=(192, 192, 192))
    d.line([(x0, y0), (x1, y0)], fill=(255, 255, 255), width=3)
    d.line([(x0, y0), (x0, y1)], fill=(255, 255, 255), width=3)
    d.line([(x0, y1), (x1, y1)], fill=(64, 64, 64), width=3)
    d.line([(x1, y0), (x1, y1)], fill=(64, 64, 64), width=3)
    th = 54
    for i in range(x1 - x0 - 12):
        f = i / (x1 - x0)
        c = (int(0 + 16 * f), int(0 + 132 * f), int(128 + 80 * f)) if active else (128, 128, 128)
        d.line([(x0 + 6 + i, y0 + 6), (x0 + 6 + i, y0 + 6 + th)], fill=c)
    d.text((x0 + 22, y0 + 6 + th // 2), title, font=V.font("tahoma-bold", 30), fill=(255, 255, 255), anchor="lm")
    for k, sym in enumerate(("X", "□", "_")):
        bx = x1 - 52 - k * 50
        d.rectangle([bx, y0 + 13, bx + 42, y0 + th - 1], fill=(192, 192, 192), outline=(64, 64, 64))
        d.text((bx + 21, y0 + 6 + th // 2), sym, font=V.font("tahoma-bold", 26), fill=(0, 0, 0), anchor="mm")
    return y0 + 6 + th


def titlebar(d, title):
    """Smalle Win98-titelbalk bovenin elke rubriek."""
    d.rectangle([0, 0, W, 78], fill=(192, 192, 192))
    for i in range(W - 16):
        f = i / W
        d.line([(8 + i, 8), (8 + i, 70)], fill=(int(16 * f), int(132 * f), int(128 + 80 * f)))
    d.text((30, 39), title, font=V.font("tahoma-bold", 32), fill=(255, 255, 255), anchor="lm")
    for k, sym in enumerate(("X", "□", "_")):
        bx = W - 64 - k * 54
        d.rectangle([bx, 16, bx + 46, 62], fill=(192, 192, 192), outline=(64, 64, 64))
        d.text((bx + 23, 39), sym, font=V.font("tahoma-bold", 26), fill=(0, 0, 0), anchor="mm")


def paperclip(d, cx, cy, s=1.0, look=0.0):
    """Een paperclip met oogjes (parodie op de cover, geen logo)."""
    def P(x, y):
        return (cx + x * s, cy + y * s)
    wire = [P(30, 160), P(30, -120), P(0, -150), P(-30, -120), P(-30, 120), P(-5, 145), P(20, 120),
            P(20, -80), P(5, -95), P(-10, -80), P(-10, 90)]
    d.line(wire, fill=(150, 150, 165), width=int(12 * s), joint="curve")
    d.line(wire, fill=(215, 215, 228), width=int(5 * s), joint="curve")
    for ex in (-34, 34):
        d.ellipse([*P(ex - 26, -70), *P(ex + 26, -10)], fill=(255, 255, 255), outline=(0, 0, 0), width=3)
        px = ex + 8 * look
        d.ellipse([*P(px - 9, -46), *P(px + 9, -26)], fill=(0, 0, 0))
        d.arc([*P(ex - 26, -100), *P(ex + 26, -60)], 200, 340, fill=(0, 0, 0), width=int(5 * s))


def painter(ev, sec, m):
    S = {n: (a, b) for n, a, b in sec}
    bgs = {k: _bg(k) for k in ("natuur", "lab", "dark")}
    bgs["old"], bgs["new"] = diag(OLD_A, OLD_B), diag(NEW_A, NEW_B)
    TITLES = {"open": "POM.exe", "sponsor": "sponsor.exe", "lachen": "lachpauze.exe", "tijd": "tijdmachine.exe",
              "bijwerking": "bijsluiter.txt - Kladblok", "symfonie": "stopwoorden.xls", "dankbaar": "dankbaar.doc"}
    zaps = [e.t for e in ev if e.meta.get("kind") == "zap"]
    by = defaultdict(list)
    for e in ev:
        by[e.meta.get("kind")].append(e)
    title_t = by["title"][0].t
    static = [Image.fromarray(np.random.default_rng(s).integers(0, 255, (H // 6, W // 6), dtype=np.uint8), "L")
              .resize((W, H), Image.NEAREST).convert("RGB") for s in range(4)]
    n_tiles = len(by["greet"])
    cols = 10
    rows_ = int(np.ceil(n_tiles / cols))
    tw, th = (W - 80) // cols, int((H - 700) / max(1, rows_))
    fills = m["counts"]

    def last(kind, t, tail=0.0):
        cur = [e for e in by[kind] if e.t <= t < e.end + tail]
        return cur[-1] if cur else None

    def paint(t, img, d):
        for z in zaps:
            if 0 <= t - z < 0.1:
                img.paste(static[int(t * 30) % 4]); return
        name = next((n for n, (a, b) in S.items() if a <= t < b and not n.endswith("_cut")), "slot")
        cx = W // 2

        if name == "open":
            img.paste(bgs["dark"])
            if t < title_t:
                # mozaïek: elke tegel = één begroeting; licht op als hij klinkt, dooft na
                for e in by["greet"]:
                    i = e.meta["tile"]
                    r, c = divmod(i, cols)
                    x0, y0 = 40 + c * tw, 330 + r * th
                    if e.t <= t:
                        age = t - e.t
                        a = max(0.18, 1 - age / 0.8)
                        col = PAL[int(e.meta["year"]) % 8]
                        d.rectangle([x0 + 3, y0 + 3, x0 + tw - 3, y0 + th - 3], fill=tuple(int(cc * a) for cc in col))
                        if age < 0.8:
                            V.text_center(d, (x0 + tw // 2, y0 + th // 2), e.meta["year"][2:], V.font("mono", 22), (10, 10, 10))
                    else:
                        d.rectangle([x0 + 3, y0 + 3, x0 + tw - 3, y0 + th - 3], outline=(40, 40, 40))
                g = last("greet", t, 0.0)
                heard = sum(1 for e in by["greet"] if e.t <= t)
                V.text_center(d, (cx, 160), f"{heard:03d} BEGROETINGEN", V.font("mono", 44), (240, 240, 235), spacing=8)
                if g and heard < 24:
                    f = V.font("helv-black", 92)
                    for k, ln in enumerate(wrap(d, g.meta["text"].upper(), f, W - 120)):
                        V.text_center(d, (cx, 1500 + k * 100), ln, f, (255, 255, 255))
            else:
                # de cover van 2016, in al haar glorie
                img.paste(bgs["old"])
                age = t - title_t
                wob = lambda k: int(10 * np.sin(age * 9 + k))
                V.text_center(d, (cx, 520 + wob(0)), "een podcast over", V.font("comic", 92), (255, 214, 92))
                sz = V.fit_size(d, "10 JAAR", "comic-bold", W - 120, 250)
                V.text_center(d, (cx + 6, 806), "10 JAAR", V.font("comic-bold", sz), (20, 120, 60))
                V.text_center(d, (cx, 800 + wob(1)), "10 JAAR", V.font("comic-bold", sz), (52, 214, 110))
                V.text_center(d, (cx, 1060 + wob(2)), "MEDIA", V.font("comic-bold", 170), (52, 214, 110))
                V.text_center(d, (260, 1300), "met", V.font("comic", 80), (110, 220, 230))
                V.text_center(d, (cx, 1420), "Ernst & Alexander", V.font("comic", 84), (40, 70, 230))
                paperclip(d, W - 190, 1700, 0.9, look=np.sin(age * 3))
                flash = max(0, 1 - age / 0.2)
                if flash > 0:
                    d.rectangle([0, 0, W, H], fill=tuple(int(255 * flash) for _ in range(3)))

        elif name == "sponsor":
            b = last("brand", t, 0.05)
            v = last("vo", t, 0.1)
            if b:
                i = BRANDS.index(b.meta["text"])
                bgc = PAL[i % len(PAL)]
                fg = (10, 10, 10) if sum(bgc) > 400 else (255, 255, 255)
                d.rectangle([0, 0, W, H], fill=bgc)
                size = V.fit_size(d, b.meta["text"].upper(), "helv-black", W - 100, 260)
                V.text_center(d, (cx, H // 2 - 40), b.meta["text"].upper(), V.font("helv-black", size), fg)
                V.text_center(d, (cx, H // 2 + 160), f"{b.meta['n']}× genoemd", V.font("mono", 48), fg)
                V.text_center(d, (cx, 200), "AANGEBODEN DOOR", V.font("mono", 34), fg, spacing=10)
            else:
                d.rectangle([0, 0, W, H], fill=(255, 238, 200))
                if v:
                    f = V.font("helv-black", 86)
                    for k, ln in enumerate(wrap(d, v.meta["text"].upper(), f, W - 140)):
                        V.text_center(d, (cx, 860 + k * 100), ln, f, (30, 30, 30))
                    if "negenduizend" in v.meta["text"]:
                        V.text_center(d, (cx, 1260), f"{m['n_orgs']:,} organisaties genoemd".replace(",", "."),
                                      V.font("mono", 40), (200, 40, 40))

        elif name == "lachen":
            # geen tekst: elk lachje een kring die meedeint op zijn eigen volume
            img.paste(bgs["old"])
            lcols = [(255, 214, 92), (52, 214, 110), (110, 220, 230), (255, 120, 200), (255, 255, 255), (40, 70, 230)]
            for e in by["laugh"]:
                if e.t > t:
                    continue
                rr = np.random.default_rng(e.meta["n"] * 7)
                x0, y0 = rr.uniform(120, W - 120), rr.uniform(260, H - 160)
                age = t - e.t
                i = int(age * SR)
                env = float(np.abs(e.x[i:i + 2400]).max()) / (np.abs(e.x).max() + 1e-9) if i < len(e.x) else 0.0
                base = 40 + 260 * min(1, age / 0.5)
                col = lcols[e.meta["n"] % len(lcols)]
                for ring in range(4):
                    r = base * (1 - ring * 0.22) + 70 * env
                    w = max(2, int(10 * env) + 3 - ring)
                    d.ellipse([x0 - r, y0 - r, x0 + r, y0 + r], outline=col, width=w)
                if env > 0.05:
                    r = 30 + 90 * env
                    d.ellipse([x0 - r, y0 - r, x0 + r, y0 + r], fill=col)

        elif name == "tijd":
            img.paste(bgs["dark"])
            ye = [e for e in by["year"] if e.t <= t]
            ang = t * 4.0
            d.ellipse([cx - 300, 380, cx + 300, 980], outline=(80, 80, 80), width=3)
            d.line([(cx, 680), (cx + 260 * np.sin(ang), 680 - 260 * np.cos(ang))], fill=(255, 80, 60), width=6)
            if ye:
                y = ye[-1].meta["year"]
                V.text_center(d, (cx, 680), y, V.font("mono", 170), (245, 245, 240))
                q = last("quote", t, 0.1)
                if q and q.meta.get("year") == y:
                    f = V.font("helv-light", 66)
                    for k, ln in enumerate(wrap(d, q.meta["text"], f, W - 160)):
                        V.text_center(d, (cx, 1200 + k * 82), ln, f, (245, 245, 240))
                elif not any(e.meta.get("year") == y for e in by["quote"]):
                    V.text_center(d, (cx, 1200), "[ geen transcript ]", V.font("mono", 40), (110, 110, 110))
            v = last("vo", t, 0.1)
            if v:
                V.text_center(d, (cx, 1200), v.meta["text"], V.font("helv-light", 66), (245, 245, 240))
            V.text_center(d, (cx, 220), "DE TOEKOMST, PER JAAR", V.font("mono", 30), (255, 80, 60), spacing=8)

        elif name == "bijwerking":
            d.rectangle([0, 0, W, H], fill=(2, 12, 6))
            green = (60, 255, 120)
            k_ = last("kwaal", t, 0.1)
            alarm = bool(k_ and k_.meta.get("alarm"))
            flat = any(e.t <= t for e in by["flatline"])
            # ECG: scrollend spoor, piek bij elke beep
            beeps = [e.t for e in by["beep"]]
            pts = []
            for px in range(0, W, 6):
                tt = t - (W - px) / W * 2.5
                y = 700
                if not (flat and tt > by["flatline"][0].t):
                    for bt in beeps:
                        dt = tt - bt
                        if 0 <= dt < 0.12:
                            y -= 260 * np.sin(dt / 0.12 * np.pi) * (1 if int(dt * 100) % 2 else -0.4)
                    y += 6 * np.sin(tt * 9)
                pts.append((px, y))
            d.line(pts, fill=(255, 60, 60) if alarm or flat else green, width=4)
            bpm = 60 + 12 * sum(1 for b_ in beeps if b_ <= t)
            V.text_center(d, (W - 80, 200), f"{0 if flat else bpm}", V.font("mono", 90), (255, 60, 60) if alarm else green, anchor="rm")
            V.text_center(d, (W - 80, 280), "BPM", V.font("mono", 30), green, anchor="rm")
            V.text_center(d, (80, 200), "POM®  bijsluiter", V.font("mono", 34), green, anchor="lm")
            if k_:
                col = (255, 60, 60) if alarm else green
                V.text_center(d, (cx, 1150), k_.meta["text"].upper(), V.font("helv-black", 170), col)
                V.text_center(d, (cx, 1290), f"{k_.meta['n']}× in 10 jaar", V.font("mono", 40), col)
            v = last("vo", t, 0.1)
            if v:
                V.text_center(d, (cx, 1500), v.meta["text"], V.font("mono", 46), green)

        elif name == "symfonie":
            img.paste(bgs["lab"])
            ink = (25, 30, 40)
            V.text_center(d, (90, 170), "RAPPORT 10/POM · STOPWOORDEN", V.font("mono", 34), ink, anchor="lm")
            fired = [e for e in by["filler"] if e.t <= t]
            cur = fired[-1] if fired else None
            mx = max(fills[f] for f in FILLERS)
            for k, f in enumerate(FILLERS):
                y = 290 + k * 98
                hit = [e for e in fired if e.meta["word"] == f]
                pulse = 0
                if hit:
                    pulse = max(0, 1 - (t - hit[-1].t) / 0.2)
                bw = max(4, int((W - 500) * fills[f] / mx * (min(1, len(hit) / SHOTS) if not (cur and cur.meta.get("all")) else 1)))
                col = (220, 50, 40) if pulse > 0 else (60, 70, 90)
                d.rectangle([330, y - int(8 * pulse), 330 + bw, y + 64 + int(8 * pulse)], fill=col)
                V.text_center(d, (305, y + 32), f, V.font("mono", 34), ink, anchor="rm")
                if hit:
                    V.text_center(d, (340 + bw, y + 32), f"{fills[f]:,}".replace(",", "."), V.font("mono", 28), ink, anchor="lm")
            if cur:
                if cur.meta.get("all"):
                    a = max(0, 1 - (t - cur.t) / 0.25)
                    V.text_center(d, (cx, 1560), "ALLES", V.font("helv-black", 190 + int(40 * a)), (220, 50, 40))
                else:
                    a = max(0, 1 - (t - cur.t) / 0.2)
                    w_ = cur.meta["word"].upper()
                    sz = V.fit_size(d, w_, "helv-black", W - 100, 200 + int(60 * a))
                    V.text_center(d, (cx, 1560), w_, V.font("helv-black", sz), (220, 50, 40))
            else:
                v = last("vo", t, 0.1)
                if v:
                    V.text_center(d, (cx, 1500), v.meta["text"], V.font("mono", 42), ink)

        elif name == "dankbaar":
            # warm, traag: zachte gloed, zinnen in Didot, jaartal klein
            d.rectangle([0, 0, W, H], fill=(28, 18, 12))
            for k in range(7):
                r = 260 + k * 90 + 20 * np.sin(t * 0.6 + k)
                c = int(18 + 7 * (7 - k))
                d.ellipse([cx - r, 900 - r, cx + r, 900 + r], fill=(c + 22, c + 8, c - 4))
            q = last("dank", t, 0.4)
            if q:
                age = t - q.t
                a = min(1.0, age / 0.4) * (1 if t < q.end else max(0, 1 - (t - q.end) / 0.4))
                f = V.font("didot-italic", 70)
                for k, ln in enumerate(wrap(d, q.meta["text"], f, W - 200)):
                    V.text_center(d, (cx, 860 + k * 88), ln, f, tuple(int(c * a) for c in (250, 236, 214)))
                V.text_center(d, (cx, 1400), q.meta["year"], V.font("mono", 36), tuple(int(c * a) for c in (200, 160, 120)), spacing=10)

        else:
            q = last("quote", t, 0.1)
            slot_a = S["slot"][0]
            if q:
                img.paste(bgs["dark"])
                V.text_center(d, (cx, 900), q.meta["text"], V.font("didot-italic", 76), (240, 236, 228))
            elif t < S["slot"][1] - 1.6:
                # de paperclip schiet te hulp
                img.paste(bgs["old"])
                y = win98(d, (80, 560, W - 80, 1260), "POM-assistent")
                paperclip(d, 250, y + 260, 0.85, look=np.sin(t * 3))
                f = V.font("comic", 46)
                for k, ln in enumerate(["Het lijkt erop dat je", "10 jaar POM hebt", "geluisterd.", "", "Hulp nodig?"]):
                    d.text((420, y + 70 + k * 64), ln, font=f, fill=(0, 0, 0))
                for k, lab in enumerate(("Ja", "Nee", "Anyway")):
                    bx = 200 + k * 260
                    d.rectangle([bx, 1150, bx + 220, 1225], fill=(192, 192, 192), outline=(0, 0, 0), width=3)
                    d.text((bx + 110, 1188), lab, font=V.font("tahoma", 38), fill=(0, 0, 0), anchor="mm")
            else:
                # en dan: de huisstijl van nu
                img.paste(bgs["new"])
                d.rectangle([150, 560, W - 150, 1360], outline=NEW_FRAME, width=40)
                d.rectangle([150, 560, W - 150, 860], fill=NEW_FRAME)
                V.text_center(d, (cx, 712), "POM", V.font("helv-black", 300), (0, 0, 0))
                V.text_center(d, (cx, 1110), "2016 — 2025", V.font("helv-black", 96), (255, 255, 255))
                V.text_center(d, (cx, 1560), "teruggeluisterd door een AI", V.font("helv-medium", 40), (255, 255, 255))
        if name in TITLES and not (name == "open" and t >= title_t):
            titlebar(d, TITLES[name])

    return paint


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    print("· assets…")
    assets()
    print("· minen…")
    m = mine2()
    laughs = pickle.load(open(OUT / "laughs.pkl", "rb"))
    clips = material(m)
    print(f"  {len(clips)} clips, {len(m['greet'])} begroetingen, {sum(l['laugh'] for l in laughs)} lachjes")
    ev, sec, end = compose(m, clips, laughs)
    mix = render(ev, tail=0.5, verb_ir=impulse_response(2.2), verb_level=0.4)
    n = int(end * SR)
    mix = np.concatenate([mix, np.zeros((max(0, n - len(mix)), 2), np.float32)])[:n]
    master(mix, OUT / "10_jaar_pom_v2")
    print(f"· audio {end:.1f}s  " + ", ".join(f"{a}@{b:.1f}" for a, b, _ in sec))
    paint = painter(ev, sec, m)
    if "--stills" in sys.argv:
        for name, a, b in sec:
            if not name.endswith("_cut"):
                V.still(paint, a + (b - a) * 0.6, OUT / f"v2still_{a:05.1f}_{name}.png")
        return
    V.render(paint, end, OUT / "10_jaar_pom_v2.wav", OUT / "10_jaar_pom_v2.mp4", film_grain=False)
    print(f"· video {OUT/'10_jaar_pom_v2.mp4'}")


if __name__ == "__main__":
    main()
