"""
10 JAAR POM — parodie/analyse in vijf zenders.

Snelle montage met abrupte zap-overgangen; elk blok een eigen stijl, eigen
ElevenLabs-muziek en een eigen AI-stem:
  open    — openingszinnen uit alle jaren
  RECLAME — 70's commercial: tien jaar superlatieven
  HMMM    — natuurdocumentaire over de 'inbetweens' (gaps 600–3000ms, echo-methode)
  NEXT    — tijdmachine: één toekomstvoorspelling per jaar
  STOP    — lab-rapport: stopwoordjes geteld (FILLER_PATTERNS uit analyzers/speaker.py)

    PYTHONPATH=src python -m supercut.pieces.pom_10jaar [--stills]
"""

from __future__ import annotations

import json
import pickle
import random
import re
import sqlite3
import sys
import tempfile
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from stemmy_cli.analyzers.speaker import FILLER_PATTERNS
from stemmy_cli.audio.parallel_extractor import ParallelExtractor

from ..audio import SR, level, loudness
from ..corpus import DB_PATH, ROOT
from ..mix import Event, highpass, impulse_response, master, render
from .. import video as V
from .pom_horror import OUT as POM, eleven, load_mp3, load_stereo

OUT = ROOT / "output" / "supercut" / "pom10"
VOICES = {"omroeper": "pNInz6obpgDQGcFmaJgB", "verteller": "JBFqnCBsd6RMkjVDRZzb", "lab": "21m00Tcm4TlvDq8ikWAM"}

OPEN = ["Hallo, toekomst?", "Welkom bij een podcast over media.", "Hé Ernst.", "Oké, zullen we beginnen?",
        "Hé, wat zie je er ontzettend fit uit, Alexander?"]
RECLAME = ["Fantastisch.", "Ongelooflijk.", "Het is echt een briljant stuk.", "Nou, wat fantastische ontwikkelingen.",
           "Ja, je moet betrouwbaar zijn en magisch.", "Dus dat is echt revolutionair."]
NEXT = [("2016", "Het is hopelijk ook de toekomst."), ("2017", "Hebben die over vijf jaar nog een baan?"),
        ("2018", "En technologie is altijd al zoiets van, ja, hallo, dit gaat de wereld veranderen."),
        ("2019", "En de next big thing was reality."), ("2020", "Want het is over vijf jaar allemaal weg."),
        ("2021", "Het is de toekomst."), ("2022", None), ("2023", None), ("2024", None),
        ("2025", "De toekomst is hier.")]
SLOT = "Tot de volgende keer."

VO = {
    "rec1": ("omroeper", "Nieuw! Van POM!"),
    "rec2": ("omroeper", "Tien jaar superlatieven. Nu verkrijgbaar."),
    "hm1": ("verteller", "En hier, in zijn natuurlijke habitat: de podcaster. Hij denkt na."),
    "hm2": ("verteller", "Fascinerend."),
    "nx1": ("lab", "Tien jaar lang, de toekomst."),
    "st1": ("lab", "Analyse. Tien jaar POM. Meest gebruikte stopwoord:"),
}
MUSIC = {
    "reclame": ("cheesy upbeat 1970s TV commercial jingle, brass, hammond organ, bright and fast, no vocals", 15000),
    "natuur": ("gentle nature documentary orchestral underscore, sense of wonder, strings and woodwinds, no vocals", 18000),
    "tijd": ("ticking clock and pulsing synth arpeggio, time travel, building, 120 bpm, no vocals", 18000),
    "lab": ("minimal cold clinical electronic music, lab beeps, sterile pulse, no vocals", 16000),
    "open": ("short bright retro TV station ident sting with whoosh, upbeat, no vocals", 5000),
}
SFX = {"zap": ("old CRT television channel switch, static burst and click", 0.8),
       "ding": ("game show correct answer ding", 1.0)}


# ── materiaal ────────────────────────────────────────────────────────────

def pom_rows():
    con = sqlite3.connect(DB_PATH)
    return con.execute("SELECT i.audio_url, i.published_at, i.transcript_data FROM items i JOIN formats f "
                       "ON f.id=i.format_id WHERE f.title LIKE 'POM%' AND length(transcript_data)>100").fetchall()


def mine():
    """Zinnen + inbetweens + stopwoord-tellingen (gecachet)."""
    cache = OUT / "mine.pkl"
    if cache.exists():
        return pickle.load(open(cache, "rb"))
    S = pickle.load(open(POM / "sents.pkl", "rb"))
    by_text = {}
    for s in S:
        by_text.setdefault(s["text"], s)
    rng = random.Random(7)
    gaps, hums, counts = [], [], Counter()
    singles = {p for p in FILLER_PATTERNS if " " not in p}
    for url, pub, td in pom_rows():
        ws = json.loads(td).get("words") or []
        for i, w in enumerate(ws):
            t = w["text"].strip(".,!?…").lower()
            if t in singles:
                counts[t] += 1
            if t in ("hmm", "hm", "mmm", "uhm", "eh", "euh") and w["end"] - w["start"] > 250:
                hums.append(dict(url=url, year=pub[:4], start=w["start"] / 1000 - 0.05, end=w["end"] / 1000 + 0.12, kind=t))
            # tussenruimte zoals in echo/server.py: gap van 600–3000ms = hmm/lach/adem
            if i + 1 < len(ws):
                g = ws[i + 1]["start"] - w["end"]
                if 600 <= g <= 3000:
                    gaps.append(dict(url=url, year=pub[:4], start=(w["end"] + 50) / 1000,
                                     end=(ws[i + 1]["start"] - 50) / 1000, kind="gap"))
        for p in ("weet je", "zeg maar", "snap je"):
            counts[p] += sum(1 for k in range(len(ws) - 1)
                             if f'{ws[k]["text"].lower().strip(".,!?")} {ws[k+1]["text"].lower().strip(".,!?")}' == p)
    rng.shuffle(gaps)
    rng.shuffle(hums)
    res = dict(by_text={k: by_text[k] for k in set(OPEN + RECLAME + [t for _, t in NEXT if t] + [SLOT]) if k in by_text},
               gaps=gaps[:90], hums=hums[:20], counts=counts)
    OUT.mkdir(parents=True, exist_ok=True)
    pickle.dump(res, open(cache, "wb"))
    return res


def extract(segs) -> dict:
    ok, bad = ParallelExtractor(max_workers=16, output_dir=Path(tempfile.mkdtemp())).extract_segments(segs)
    out = {}
    for r in ok:
        x = load_mp3(r.output_path)
        if len(x) > SR * 0.15:
            out[r.segment_id] = highpass(x, 70)
    return out


def material(m):
    segs = []
    for k, s in m["by_text"].items():
        segs.append(dict(id="s:" + k, audio_url=s["url"], start_time=max(0, s["start"] - 0.05), end_time=s["end"] + 0.18))
    for n, g in enumerate(m["gaps"] + m["hums"]):
        segs.append(dict(id=f"g{n}", audio_url=g["url"], start_time=g["start"], end_time=g["end"]))
    top = m["counts"].most_common(1)[0][0]
    # mitrailleur: het top-stopwoord, los, 20 keer
    pool = [s for s in pickle.load(open(POM / "sents.pkl", "rb")) if s["words"]]
    rng = random.Random(3)
    rng.shuffle(pool)
    shots = []
    for s in pool:
        for w, ws, we in s["words"]:
            if w.strip(".,!?").lower() == top and 0.15 < we - ws < 0.6:
                shots.append(dict(url=s["url"], start=ws - 0.03, end=we + 0.06, year=s["year"]))
                break
        if len(shots) >= 24:
            break
    for n, sh in enumerate(shots):
        segs.append(dict(id=f"w{n}", audio_url=sh["url"], start_time=sh["start"], end_time=sh["end"]))
    clips = extract(segs)
    # inbetweens: alleen gaps waar écht iets klinkt (lach/hmm/adem), geen stilte
    allg = m["gaps"] + m["hums"]
    scored = []
    for n, g in enumerate(allg):
        x = clips.get(f"g{n}")
        if x is None:
            continue
        l = loudness(x)
        if -40 < l:
            scored.append((l, n, g))
    scored.sort(reverse=True)
    inb = [(clips[f"g{n}"], g) for _, n, g in scored[:12]]
    words = [(clips[f"w{n}"], sh) for n, sh in enumerate(shots) if f"w{n}" in clips]
    sent = {k[2:]: v for k, v in clips.items() if k.startswith("s:")}
    return sent, inb, words, top


def assets():
    from concurrent.futures import ThreadPoolExecutor
    jobs = [("music", "p10_" + k, dict(prompt=p, music_length_ms=ms)) for k, (p, ms) in MUSIC.items()]
    jobs += [("sfx", "p10_" + k, dict(text=t, duration_seconds=d)) for k, (t, d) in SFX.items()]
    jobs += [("tts", "p10_" + k, dict(voice=VOICES[v], text=t, model_id="eleven_multilingual_v2",
                                     voice_settings=dict(stability=0.4, similarity_boost=0.8, style=0.6)))
             for k, (v, t) in VO.items()]
    with ThreadPoolExecutor(2) as ex:
        list(ex.map(lambda j: eleven(j[0], j[1], **j[2]), jobs))


def A(kind, name):
    return POM / "assets" / f"{kind}_p10_{name}.mp3"


# ── compositie ───────────────────────────────────────────────────────────

def compose(sent, inb, words, top, counts):
    ev, sec = [], []          # sec: (naam, t0, t1)
    t = 0.0

    def say(text, gain=0, verb=0.05, gap=0.12, pan=0.0, **meta):
        nonlocal t
        x = level(sent[text], -19)
        ev.append(Event(t=t, x=x, gain_db=gain, verb=verb, pan=pan, meta=dict(kind="quote", text=text, **meta)))
        t += len(x) / SR - 0.15 + gap

    def vo(key, gap=0.15, gain=0, verb=0.1):
        nonlocal t
        x = level(load_mp3(A("tts", key)), -17)
        ev.append(Event(t=t, x=x, gain_db=gain, verb=verb, meta=dict(kind="vo", text=VO[key][1], who=VO[key][0])))
        t += len(x) / SR + gap

    def music(name, t0, t1, gain):
        m = load_stereo(A("music", name))
        n = int((t1 - t0) * SR)
        m = np.concatenate([m] * (1 + n // max(1, len(m))))[:n].copy()
        f = int(0.03 * SR)
        m[:f] *= np.linspace(0, 1, f)[:, None]
        m[-f:] *= np.linspace(1, 0, f)[:, None]
        for ch, p in ((0, -1.0), (1, 1.0)):
            ev.append(Event(t=t0, x=m[:, ch], pan=p, gain_db=gain, meta=dict(kind="music")))

    def zap():
        nonlocal t
        ev.append(Event(t=t, x=level(load_mp3(A("sfx", "zap")), -20), meta=dict(kind="zap")))
        t += 0.22

    # ── OPEN
    s0 = t
    t += 0.3
    for k, q in enumerate(OPEN):
        say(q, gap=0.02, pan=(-0.3, 0.3)[k % 2])
    m = load_stereo(A("music", "open"))
    for ch, p in ((0, -1.0), (1, 1.0)):
        ev.append(Event(t=t, x=m[:, ch], pan=p, gain_db=-6, meta=dict(kind="music")))
    ev.append(Event(t=t, x=np.zeros(10, np.float32), meta=dict(kind="title")))
    t += 2.4
    sec.append(("open", s0, t))
    zap()

    # ── RECLAME
    s0 = t
    vo("rec1", gap=0.1)
    for q in RECLAME:
        say(q, gap=0.05, gain=1, verb=0.08)
    ev.append(Event(t=t - 0.1, x=level(load_mp3(A("sfx", "ding")), -18), meta=dict(kind="ding")))
    vo("rec2", gap=0.2)
    music("reclame", s0, t, -13)
    sec.append(("reclame", s0, t))
    zap()

    # ── HMMM
    s0 = t
    vo("hm1", gap=0.3, verb=0.15)
    for k, (x, g) in enumerate(inb[:9]):
        x = level(x, -21)
        ev.append(Event(t=t, x=x, verb=0.12, pan=float(np.sin(k * 1.7) * 0.5), meta=dict(kind="inb", year=g["year"], what=g["kind"])))
        t += min(len(x) / SR, 1.3) + 0.06
    t += 0.2
    vo("hm2", gap=0.3, verb=0.2)
    music("natuur", s0, t, -15)
    sec.append(("natuur", s0, t))
    zap()

    # ── NEXT BIG THING
    s0 = t
    vo("nx1", gap=0.15)
    beat = 0.5
    for y, q in NEXT:
        ev.append(Event(t=t, x=np.zeros(10, np.float32), meta=dict(kind="year", year=y)))
        if q and q in sent:
            say(q, gap=0.15, verb=0.08, year=y)
        else:
            t += beat * 1.2
    music("tijd", s0, t, -13)
    sec.append(("tijd", s0, t))
    zap()

    # ── STOPWOORDJES
    s0 = t
    vo("st1", gap=0.05)
    ev.append(Event(t=t, x=np.zeros(10, np.float32), meta=dict(kind="reveal", word=top)))
    t += 0.5
    step = 0.32
    for k, (x, sh) in enumerate(words):
        x = level(x, -19)
        ev.append(Event(t=t, x=x, pan=float(((k * 0.37) % 1) * 1.4 - 0.7), meta=dict(kind="shot", n=k, year=sh["year"])))
        t += step
        step = max(0.085, step * 0.86)
    t += 0.5
    music("lab", s0, t, -15)
    sec.append(("lab", s0, t))
    zap()

    # ── SLOT
    s0 = t
    t += 0.2
    say(SLOT, gap=0.4, verb=0.25)
    if len(inb) > 9:
        x = level(inb[9][0], -22)
        ev.append(Event(t=t, x=x, verb=0.4, meta=dict(kind="inb_end")))
        t += len(x) / SR
    t += 1.8
    sec.append(("slot", s0, t))
    return ev, sec, t


# ── beeld ────────────────────────────────────────────────────────────────

W, H = V.W, V.H


def _bg(kind):
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    if kind == "reclame":
        for y in range(H):
            f = y / H
            d.line([(0, y), (W, y)], fill=(int(255 - 30 * f), int(196 - 90 * f), int(30 + 10 * f)))
    elif kind == "natuur":
        for y in range(H):
            f = y / H
            d.line([(0, y), (W, y)], fill=(int(14 + 10 * f), int(38 + 22 * (1 - f)), int(26 + 6 * f)))
        d.rectangle([0, 0, W, 220], fill=(0, 0, 0))
        d.rectangle([0, H - 220, W, H], fill=(0, 0, 0))
    elif kind == "lab":
        d.rectangle([0, 0, W, H], fill=(244, 243, 238))
        for x in range(0, W, 40):
            d.line([(x, 0), (x, H)], fill=(222, 228, 232) if x % 200 else (200, 212, 220))
        for y in range(0, H, 40):
            d.line([(0, y), (W, y)], fill=(222, 228, 232) if y % 200 else (200, 212, 220))
    else:
        d.rectangle([0, 0, W, H], fill=(8, 8, 9))
    return img


def wrap(d, text, f, max_w):
    lines, cur = [], ""
    for w in text.split():
        if cur and d.textlength(cur + " " + w, font=f) > max_w:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    return lines + [cur]


def painter(ev, sec, counts, top, total_words):
    bgs = {k: _bg(k) for k in ("reclame", "natuur", "lab", "dark")}
    zaps = [e.t for e in ev if e.meta.get("kind") == "zap"]
    quotes = [e for e in ev if e.meta.get("kind") == "quote"]
    vos = [e for e in ev if e.meta.get("kind") == "vo"]
    inbs = [e for e in ev if e.meta.get("kind") in ("inb", "inb_end")]
    years = [e for e in ev if e.meta.get("kind") == "year"]
    shots = [e for e in ev if e.meta.get("kind") == "shot"]
    title_t = next(e.t for e in ev if e.meta.get("kind") == "title")
    reveal_t = next(e.t for e in ev if e.meta.get("kind") == "reveal")
    top5 = counts.most_common(6)
    static = [Image.fromarray(np.random.default_rng(s).integers(0, 255, (H // 6, W // 6), dtype=np.uint8), "L")
              .resize((W, H), Image.NEAREST).convert("RGB") for s in range(4)]

    def active(lst, t, tail=0.1):
        cur = [e for e in lst if e.t <= t < e.end + tail]
        return cur[-1] if cur else None

    def paint(t, img, d):
        # zap: 3 frames ruis
        for z in zaps:
            if 0 <= t - z < 0.1:
                img.paste(static[int(t * 30) % 4])
                return
        name = next((n for n, a, b in sec if a <= t < b), "slot")
        cx = W // 2
        q = active(quotes, t)
        v = active(vos, t, 0.05)

        if name == "open":
            img.paste(bgs["dark"])
            if t < title_t:
                if q:
                    s_ = q.meta["text"]
                    f = V.font("helv-bold", 76)
                    for k, ln in enumerate(wrap(d, s_, f, W - 160)):
                        V.text_center(d, (cx, 900 + k * 90), ln, f, (245, 245, 240))
            else:
                a = V.ease((t - title_t) / 0.15)
                V.text_center(d, (cx, 820), "10", V.font("helv-black", 420), tuple(int(c * a) for c in (255, 210, 40)))
                V.text_center(d, (cx, 1100), "JAAR POM", V.font("helv-black", 150), tuple(int(c * a) for c in (245, 245, 240)))
                V.text_center(d, (cx, 1230), "PARODIE  ·  ANALYSE  ·  2016—2025", V.font("mono", 26), (140, 140, 140), spacing=6)

        elif name == "reclame":
            img.paste(bgs["reclame"])
            # roterende starburst
            ang = t * 0.9
            pts = []
            for k in range(32):
                r = 520 if k % 2 == 0 else 360
                a_ = ang + k * np.pi / 16
                pts.append((cx + 260 + r * np.cos(a_) * 0.55, 520 + r * np.sin(a_) * 0.55))
            d.polygon(pts, fill=(230, 40, 40))
            V.text_center(d, (cx + 260, 500), "NIEUW!", V.font("helv-black", 96), (255, 245, 210))
            V.text_center(d, (cx + 260, 590), "sinds 2016", V.font("helv-bold", 38), (255, 230, 180))
            txt = (q.meta["text"] if q else (v.meta["text"] if v else "")).upper()
            f = V.font("helv-black", 100 if len(txt) < 30 else 76)
            for k, ln in enumerate(wrap(d, txt, f, W - 140)):
                # schaduw + tekst, licht schuin 'gestapeld'
                V.text_center(d, (cx + 8, 1080 + k * 112 + 8), ln, f, (120, 20, 20))
                V.text_center(d, (cx, 1080 + k * 112), ln, f, (255, 255, 250))
            if q:
                V.text_center(d, (cx, 1640), f"* echt gezegd in POM, {q.meta.get('year','')}".strip(", "),
                              V.font("helv", 28), (120, 40, 20))
            d.rounded_rectangle([90, 1700, 430, 1820], 18, fill=(255, 255, 255))
            V.text_center(d, (260, 1760), "€ 0,-", V.font("helv-black", 70), (230, 40, 40))

        elif name == "natuur":
            img.paste(bgs["natuur"])
            # bokeh
            for k in range(14):
                x = (k * 173 + t * (18 + k * 3)) % (W + 200) - 100
                y = 300 + (k * 311) % 1300
                r = 30 + (k * 37) % 70
                c = (40, 80 + k * 4, 50)
                d.ellipse([x - r, y - r, x + r, y + r], outline=c, width=2)
            if v:
                f = V.font("didot-italic", 52)
                for k, ln in enumerate(wrap(d, v.meta["text"], f, W - 200)):
                    V.text_center(d, (cx, 1500 + k * 66), ln, f, (232, 226, 205))
            e = active(inbs, t)
            if e:
                age = t - e.t
                rr = 80 + 160 * V.waveform_strip(e.x[int(age * SR):int(age * SR) + 4800] if age * SR < len(e.x) else e.x[-10:], 1)[0]
                d.ellipse([cx - rr, 900 - rr, cx + rr, 900 + rr], outline=(232, 226, 205), width=3)
                lab = "hmm" if e.meta.get("what", "gap") != "gap" else "…"
                V.text_center(d, (cx, 900), lab, V.font("didot-italic", 90), (232, 226, 205))
                yr = e.meta.get("year", "")
                V.text_center(d, (cx, 1180), f"Homo podcastus  ·  waargenomen {yr}", V.font("didot-italic", 38), (170, 190, 160))
            V.text_center(d, (100, 160), "NATUUR", V.font("helv-black", 34), (220, 200, 120), anchor="lm")

        elif name == "tijd":
            img.paste(bgs["dark"])
            y_e = None
            for e in years:
                if e.t <= t:
                    y_e = e
            # klok
            ang = t * 4.0
            d.ellipse([cx - 300, 380, cx + 300, 980], outline=(80, 80, 80), width=3)
            d.line([(cx, 680), (cx + 260 * np.sin(ang), 680 - 260 * np.cos(ang))], fill=(255, 80, 60), width=6)
            d.line([(cx, 680), (cx + 160 * np.sin(ang / 12), 680 - 160 * np.cos(ang / 12))], fill=(220, 220, 220), width=10)
            if y_e:
                V.text_center(d, (cx, 680), y_e.meta["year"], V.font("mono", 170), (245, 245, 240))
                nodata = not any(qq.meta.get("year") == y_e.meta["year"] for qq in quotes)
                if nodata and y_e.meta["year"] != "2025":
                    V.text_center(d, (cx, 1200), "[ geen transcript ]", V.font("mono", 40), (110, 110, 110))
            txt = q.meta["text"] if q and q.meta.get("year") else (v.meta["text"] if v else "")
            f = V.font("helv-light", 66)
            for k, ln in enumerate(wrap(d, txt, f, W - 160)):
                V.text_center(d, (cx, 1200 + k * 82), ln, f, (245, 245, 240))
            V.text_center(d, (cx, 220), "DE TOEKOMST, PER JAAR", V.font("mono", 30), (255, 80, 60), spacing=8)

        elif name == "lab":
            img.paste(bgs["lab"])
            ink = (25, 30, 40)
            V.text_center(d, (90, 170), "RAPPORT 10/POM", V.font("mono", 34), ink, anchor="lm")
            V.text_center(d, (90, 215), f"n = {total_words:,} woorden  ·  {len(set())} 2016—2025".replace("  0 ", " "),
                          V.font("mono", 26), (90, 96, 110), anchor="lm")
            if t >= reveal_t - 2.5:
                # staafdiagram groeit
                g = V.ease((t - (reveal_t - 2.5)) / 2.0)
                mx = top5[0][1]
                for k, (w, n) in enumerate(top5):
                    y = 420 + k * 150
                    bw = int((W - 420) * n / mx * g)
                    d.rectangle([300, y, 300 + bw, y + 90], fill=(220, 50, 40) if k == 0 else (60, 70, 90))
                    V.text_center(d, (270, y + 45), w, V.font("mono", 40), ink, anchor="rm")
                    V.text_center(d, (310 + bw, y + 45), f"{int(n * g):,}", V.font("mono", 34), ink, anchor="lm")
            fired = [e for e in shots if e.t <= t]
            if fired:
                n = len(fired)
                V.text_center(d, (cx, 1420), top.upper(), V.font("helv-black", 200 + min(80, n * 4)), (220, 50, 40))
                V.text_center(d, (cx, 1600), f"× {n}", V.font("mono", 70), ink)
                last = fired[-1]
                V.text_center(d, (cx, 1700), f"bron: POM {last.meta['year']}", V.font("mono", 28), (90, 96, 110))
            elif v:
                f = V.font("mono", 44)
                for k, ln in enumerate(wrap(d, v.meta["text"], f, W - 180)):
                    V.text_center(d, (cx, 1420 + k * 60), ln, f, ink)

        else:  # slot
            img.paste(bgs["dark"])
            if q:
                V.text_center(d, (cx, 900), q.meta["text"], V.font("didot-italic", 76), (240, 236, 228))
            else:
                V.text_center(d, (cx, 860), "10 JAAR POM", V.font("helv-black", 120), (245, 245, 240))
                V.text_center(d, (cx, 960), "gemaakt door een AI die alles heeft teruggeluisterd", V.font("didot-italic", 40),
                              (150, 146, 140))

    return paint


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    print("· ElevenLabs assets…")
    assets()
    print("· materiaal minen…")
    m = mine()
    sent, inb, words, top = material(m)
    print(f"  {len(sent)} zinnen, {len(inb)} inbetweens, {len(words)}× '{top}'  | top: {m['counts'].most_common(6)}")
    ev, sec, end = compose(sent, inb, words, top, m["counts"])
    mix = render(ev, tail=0.5, verb_ir=impulse_response(2.2), verb_level=0.4)
    n = int(end * SR)
    mix = np.concatenate([mix, np.zeros((max(0, n - len(mix)), 2), np.float32)])[:n]
    master(mix, OUT / "10_jaar_pom")
    print(f"· audio {end:.1f}s  secties: " + ", ".join(f"{a}@{b:.1f}" for a, b, _ in sec))
    total = sum(len(json.loads(td).get("words") or []) for _, _, td in pom_rows()) if "--stills" not in sys.argv else 1_100_000
    paint = painter(ev, sec, m["counts"], top, total)
    if "--stills" in sys.argv:
        for name, a, b in sec:
            V.still(paint, a + (b - a) * 0.55, OUT / f"still_{a:05.1f}_{name}.png")
        return
    V.render(paint, end, OUT / "10_jaar_pom.wav", OUT / "10_jaar_pom.mp4", film_grain=False)
    print(f"· video {OUT/'10_jaar_pom.mp4'}")


if __name__ == "__main__":
    main()
