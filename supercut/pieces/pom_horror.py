"""
HET KOMT ERAAN — een horrortrailer uit negen jaar POM (2016–2025).

Twee mannen praten enthousiast over technologie. Chronologisch gemonteerd en in
trailer-grammatica gezet (drones, braams, stilte, trailerstem) wordt het een
langzaam naderende dreiging. Score en effecten via ElevenLabs Music/SFX, stem
via ElevenLabs TTS, zinnen via ParallelExtractor uit items.transcript_data.

    PYTHONPATH=src python -m supercut.pieces.pom_horror [--stills]
"""

from __future__ import annotations

import os
import pickle
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import requests
from PIL import Image

from stemmy_cli.audio.parallel_extractor import ParallelExtractor

from ..audio import SR, level
from ..corpus import ROOT
from ..mix import Event, highpass, impulse_response, lowpass, master, pitch, render
from .. import video as V

OUT = ROOT / "output" / "supercut" / "pom"
TRAILER_VOICE = "pNInz6obpgDQGcFmaJgB"     # ElevenLabs 'Adam' — diepe trailerstem

# (tekst, gain_db, galm, behandeling)
SCRIPT = [
    ("act", "A"),
    ("q", "Oké, zullen we beginnen?"),
    ("card", "MEI 2016"),
    ("q", "Omdat ik het toch een beetje eng vind dat ik een connected smart home krijg."),
    ("laugh", 0),
    ("q", "Het algoritme zegt ook van je moet die en die toevoegen als vriend."),
    ("q", "Dus je gaat ook nooit meer twijfelen waar ga je iets kopen."),
    ("sfx", "tapestop"),
    ("act", "B"),
    ("vo", "Ze praatten over de toekomst."),
    ("q", "Je hebt er geen controle meer over, zoals je daar vroeger controle over had."),
    ("q", "Ja, ik vind het wel eng om het erover te hebben."),
    ("q", "Dus je hebt die controle niet."),
    ("q", "Oh, ik vind het eng hoeveel er tegenwoordig op mij hangt."),
    ("q", "Dat je nooit meer tegengeluiden hebt."),
    ("sfx", "braam"),
    ("vo", "Negen jaar lang."),
    ("act", "C"),
    ("q", "Ik heb daar geen controle over."),
    ("q", "Kijk, niemand snapt honderd algoritmes die tegelijk werken."),
    ("q", "Ik ben er zelf behoorlijk verslaafd aan."),
    ("chant", "Ja, nu gaan we die overnemen, gaan we die overnemen, gaan we die overnemen."),
    ("sfx", "riser"),
    ("act", "D"),
    ("q!", "Als de robots komen."),
    ("q", "Wij gaan met z'n allen vechten tegen de robots."),
    ("q", "Echt het einde van de wereld in 2025."),
    ("cut", None),
    ("q_dry", "Ik vind het heel eng."),
    ("title", "HET KOMT ERAAN"),
    ("laugh", 1),
    ("q_last", "Oké, zullen we beginnen?"),
]

MUSIC = {
    "A": ("light, slightly naive tech podcast intro music, plucky synth and soft glockenspiel, "
          "cheerful but a little too sweet, 2016 startup vibe, no vocals", 16000),
    "B": ("dark cinematic horror underscore, low sustained drone, distant dissonant strings, "
          "uneasy, sparse, no percussion, no vocals", 30000),
    "C": ("tense horror trailer build, pulsing low synth ostinato, heartbeat, rising dissonant strings, "
          "accelerating, no vocals", 20000),
    "D": ("massive horror trailer climax, huge distorted brass braams, pounding drums, screeching strings, "
          "no vocals", 10000),
}
SFX = {
    "braam": ("cinematic horror trailer braam, deep distorted brass hit with long dark tail", 4),
    "riser": ("horror trailer riser, reversed strings and noise rising to a peak, ending abruptly", 5),
    "tapestop": ("tape stop effect, audio slowing down and dying", 1.5),
    "hit": ("deep cinematic impact boom with sub bass and metallic reverb tail", 3),
}


# ── ElevenLabs ───────────────────────────────────────────────────────────

def _key():
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=True)
    return os.environ["ELEVENLABS_API_KEY"]


def load_mp3(path) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                         capture_output=True).stdout
    return np.frombuffer(raw, np.float32).copy()


def load_stereo(path) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "2", "-ar", str(SR), "-f", "f32le", "-"],
                         capture_output=True).stdout
    return np.frombuffer(raw, np.float32).reshape(-1, 2).copy()


def eleven(kind: str, name: str, **body) -> Path:
    p = OUT / "assets" / f"{kind}_{name}.mp3"
    if p.exists():
        return p
    p.parent.mkdir(parents=True, exist_ok=True)
    url = {"music": "https://api.elevenlabs.io/v1/music",
           "sfx": "https://api.elevenlabs.io/v1/sound-generation",
           "tts": f"https://api.elevenlabs.io/v1/text-to-speech/{body.pop('voice', TRAILER_VOICE)}"}[kind]
    import time
    for attempt in range(6):
        r = requests.post(url, headers={"xi-api-key": _key()}, json=body, timeout=300)
        if r.status_code != 429:
            break
        time.sleep(3 * (attempt + 1))
    r.raise_for_status()
    p.write_bytes(r.content)
    return p


def assets():
    from concurrent.futures import ThreadPoolExecutor
    jobs = [("music", k, dict(prompt=p, music_length_ms=ms)) for k, (p, ms) in MUSIC.items()]
    jobs += [("sfx", k, dict(text=t, duration_seconds=d)) for k, (t, d) in SFX.items()]
    jobs += [("tts", f"vo{n}", dict(text=t, model_id="eleven_multilingual_v2",
                                    voice_settings=dict(stability=0.55, similarity_boost=0.8, style=0.35)))
             for n, (k, t) in enumerate(SCRIPT) if k == "vo"]
    with ThreadPoolExecutor(2) as ex:
        list(ex.map(lambda j: eleven(j[0], j[1], **j[2]), jobs))


# ── materiaal ────────────────────────────────────────────────────────────

def material():
    S = pickle.load(open(OUT / "sents.pkl", "rb"))
    by_text = {}
    for s in S:
        by_text.setdefault(s["text"], s)
    segs, meta = [], {}
    for n, (k, t) in enumerate(SCRIPT):
        if k.startswith("q") or k == "chant":
            s = by_text[t]
            sid = f"q{n}"
            segs.append(dict(id=sid, audio_url=s["url"], start_time=max(0, s["start"] - 0.06), end_time=s["end"] + 0.2))
            meta[sid] = s
    # 'inbetweens': gelach uit de transcripten (AssemblyAI schrijft 'Haha' soms uit)
    import json, re, sqlite3
    from ..corpus import DB_PATH
    con = sqlite3.connect(DB_PATH)
    laughs = []
    for url, pub, td in con.execute("SELECT i.audio_url, i.published_at, i.transcript_data FROM items i JOIN formats f "
                                    "ON f.id=i.format_id WHERE f.title LIKE 'POM%' AND length(transcript_data)>100"):
        for w in json.loads(td).get("words") or []:
            if re.match(r"^(ha){2,}[.,!?]*$", w["text"].lower()) and w["end"] - w["start"] > 350:
                laughs.append(dict(url=url, year=pub[:4], start=w["start"] / 1000, end=w["end"] / 1000))
    laughs.sort(key=lambda l: -(l["end"] - l["start"]))
    for n, l in enumerate(laughs[:2]):
        sid = f"laugh{n}"
        segs.append(dict(id=sid, audio_url=l["url"], start_time=l["start"] - 0.05, end_time=min(l["end"] + 0.15, l["start"] + 1.1)))
        meta[sid] = dict(text="(lacht)", year=l["year"], words=[], start=l["start"], title="")
    tmp = Path(tempfile.mkdtemp())
    ok, bad = ParallelExtractor(max_workers=16, output_dir=tmp).extract_segments(segs)
    clips = {}
    for r in ok:
        x = load_mp3(r.output_path)
        if len(x) < SR * 0.2:
            print("  ! leeg:", r.segment_id, meta[r.segment_id]["text"][:50], meta[r.segment_id].get("url", "")[:60])
            continue
        clips[r.segment_id] = level(highpass(x, 80), -19)
    for r in bad:
        print("  ! mislukt:", r.segment_id, r.error)
    return clips, meta


# ── compositie ───────────────────────────────────────────────────────────

def compose(clips, meta):
    A = OUT / "assets"
    ev, marks, acts = [], {}, {}
    t = 0.6
    vo_n = 0
    for n, (k, val) in enumerate(SCRIPT):
        sid = f"q{n}"
        if k == "act":
            acts[val] = t
            marks["act_" + val] = t
        elif k in ("q", "q!", "q_dry", "q_last"):
            x = clips[sid]
            s = meta[sid]
            verb = {"q": 0.12, "q!": 0.3, "q_dry": 0.0, "q_last": 0.35}[k]
            if k == "q!":
                ev.append(Event(t=t - 0.05, x=load_mp3(A / "sfx_hit.mp3"), gain_db=-4, meta=dict(kind="hit")))
            ev.append(Event(t=t, x=x, verb=verb, gain_db=1 if k == "q_dry" else 0,
                            meta=dict(kind="quote", s=s, act=_act(acts, t), loud=(k == "q!"))))
            gap = {"A": 0.35, "B": 0.55, "C": 0.12, "D": 0.1}.get(_act(acts, t), 0.4)
            if k == "q_dry":
                gap = 1.2
            t += len(x) / SR - 0.2 + gap
        elif k == "chant":
            x = clips[sid]
            s = meta[sid]
            ev.append(Event(t=t, x=x, verb=0.25, meta=dict(kind="quote", s=s, act="C", chant=True)))
            low = pitch(x, -5)
            ev.append(Event(t=t + 0.04, x=low, gain_db=-5, verb=0.5, pan=-0.6, meta=dict(kind="fx")))
            ev.append(Event(t=t + 0.09, x=pitch(x, -12), gain_db=-8, verb=0.6, pan=0.6, meta=dict(kind="fx")))
            t += len(x) / SR + 0.05
        elif k == "laugh":
            x = clips.get(f"laugh{val}")
            if x is None:
                continue
            if val == 1:
                x = pitch(x, -2)
            ev.append(Event(t=t, x=x, gain_db=-3, verb=0.15 if val == 0 else 0.55,
                            meta=dict(kind="laugh", s=meta[f"laugh{val}"])))
            t += len(x) / SR + (0.2 if val == 0 else 0.6)
        elif k == "card":
            ev.append(Event(t=t, x=np.zeros(10, np.float32), meta=dict(kind="card", text=val, dur=1.6)))
            t += 1.4
        elif k == "vo":
            x = level(load_mp3(A / f"tts_vo{n}.mp3"), -17)
            x = lowpass(x, 9000)
            t += 0.3
            ev.append(Event(t=t, x=x, verb=0.3, meta=dict(kind="vo", text=val)))
            t += len(x) / SR + 0.6
        elif k == "sfx":
            x = load_mp3(A / f"sfx_{val}.mp3")
            if val == "riser":
                x = level(x, -16)
                st = t - len(x) / SR + 0.3
                ev.append(Event(t=max(0, st), x=x, meta=dict(kind="sfx", name=val)))
                t += 0.25
            else:
                x = level(x, -14 if val == "braam" else -18)
                ev.append(Event(t=t - (0 if val == "braam" else 0.3), x=x, verb=0.2,
                                meta=dict(kind="braam" if val == "braam" else "sfx", name=val)))
                t += 0.9 if val == "braam" else 0.2
        elif k == "cut":
            marks["cut"] = t
            t += 1.1
        elif k == "title":
            x = level(load_mp3(A / "sfx_braam.mp3"), -12)
            ev.append(Event(t=t, x=x, verb=0.35, meta=dict(kind="title", text=val)))
            marks["title"] = t
            t += 3.4
    marks["end"] = t + 2.2

    # score: elke akte zijn eigen cue, met korte fades, tot de volgende akte
    bounds = [("A", acts["A"], acts["B"]), ("B", acts["B"] - 0.4, acts["C"]),
              ("C", acts["C"] - 0.2, acts["D"]), ("D", acts["D"] - 0.1, marks["cut"])]
    for name, a, b in bounds:
        m = load_stereo(A / f"music_{name}.mp3")
        n = int((b - a) * SR)
        if len(m) < n:
            m = np.concatenate([m, m])[:n] if len(m) else m
        m = m[:n].copy()
        f = int(0.25 * SR)
        m[:f] *= np.linspace(0, 1, f)[:, None]
        m[-int(0.04 * SR):] *= np.linspace(1, 0, int(0.04 * SR))[:, None]
        g = {"A": -15, "B": -13, "C": -11, "D": -8}[name]
        for ch, pan in ((0, -1.0), (1, 1.0)):
            ev.append(Event(t=a, x=m[:, ch], pan=pan, gain_db=g, meta=dict(kind="music", act=name)))
    return ev, marks


def _act(acts, t):
    cur = "A"
    for k, v in sorted(acts.items(), key=lambda kv: kv[1]):
        if t >= v - 1e-6:
            cur = k
    return cur


def mixdown(ev, marks):
    mix = render(ev, tail=1.0, verb_ir=impulse_response(3.0), verb_level=0.5)
    # harde stilte na de climax (ook de galm), tot 'Ik vind het heel eng'
    i0, i1 = int(marks["cut"] * SR), int((marks["cut"] + 1.05) * SR)
    mix[i0:i1] = 0
    n = int(marks["end"] * SR)
    return np.concatenate([mix, np.zeros((max(0, n - len(mix)), 2), np.float32)])[:n]


# ── beeld: found footage / trailer ───────────────────────────────────────

W, H = V.W, V.H
_yy, _xx = np.mgrid[0:H, 0:W]
VIGNETTE = np.clip(1.25 - 0.9 * (((_xx - W / 2) / (W / 1.6)) ** 2 + ((_yy - H / 2) / (H / 1.5)) ** 2), 0.15, 1)[..., None]
SCAN = np.where((_yy % 4) < 2, 1.0, 0.82)[..., None]
RED = (225, 30, 30)
MONTHS = "JAN FEB MRT APR MEI JUN JUL AUG SEP OKT NOV DEC".split()


def painter(ev, marks):
    quotes = [e for e in ev if e.meta.get("kind") in ("quote", "laugh")]
    vos = [e for e in ev if e.meta.get("kind") == "vo"]
    cards = [e for e in ev if e.meta.get("kind") == "card"]
    hits = [e.t for e in ev if e.meta.get("kind") in ("braam", "hit", "title")]
    title = next(e for e in ev if e.meta.get("kind") == "title")

    def paint(t, img, d):
        cx = W // 2
        shake = 0.0
        for h in hits:
            if 0 <= t - h < 0.5:
                shake = max(shake, 1 - (t - h) / 0.5)
        in_cut = marks["cut"] <= t < marks["cut"] + 1.05

        cur = [e for e in quotes if e.t <= t < e.end + 0.15]
        if cur and not in_cut:
            e = cur[-1]
            s = e.meta["s"]
            act = e.meta.get("act", "A")
            # camcorder-stempel met afleveringsdatum
            pub = s.get("pub") or ""
            V.text_center(d, (70, 120), "● REC", V.font("mono", 34), RED if int(t * 2) % 2 == 0 else (60, 10, 10), anchor="lm")
            V.text_center(d, (W - 70, 120), f"POM  ·  {s.get('year', '')}", V.font("mono", 34), (220, 220, 210), anchor="rm")
            # oscilloscoop van de stem
            seg = e.x[int(max(0, t - e.t) * SR): int(max(0, t - e.t) * SR) + 2400]
            if len(seg) > 10:
                pts = [(60 + k * (W - 120) / len(seg[::8]), 760 + float(v) * 900) for k, v in enumerate(seg[::8])]
                d.line(pts, fill=(170, 255, 190) if act in "AB" else (255, 120, 110), width=2)
            # ondertitel, woord voor woord op de echte timing
            words = s.get("words") or []
            base = s.get("start", 0) - 0.06
            f = V.font("didot", 64 if not e.meta.get("loud") else 92)
            shown = [w for w, ws, we in words if t >= e.t + (ws - base) - 0.02]
            if e.meta.get("kind") == "laugh":
                shown = ["(lacht)"]
                f = V.font("didot-italic", 58)
            lines, curl = [], ""
            for w in shown:
                if d.textlength(curl + w, font=f) > W - 160:
                    lines.append(curl.strip())
                    curl = ""
                curl += w + " "
            lines.append(curl.strip())
            y0 = 1240 - (len(lines) - 1) * 40
            for k, ln in enumerate(lines):
                V.text_center(d, (cx, y0 + k * (f.size + 16)), ln, f, (240, 236, 228))

        # trailerstem: tekst groot, flikkerend in
        for e in vos:
            if e.t - 0.1 <= t < e.end + 0.5:
                a = V.ease((t - e.t + 0.1) / 0.3) * (1 if t < e.end else max(0, 1 - (t - e.end) / 0.5))
                a *= 0.75 + 0.25 * np.random.default_rng(int(t * 30)).random()
                txt = e.meta["text"].rstrip(".").upper()
                size = V.fit_size(d, txt, "helv-black", W - 120, 120)
                V.text_center(d, (cx, H // 2), txt, V.font("helv-black", size), tuple(int(c * a) for c in (235, 230, 220)))
        for e in cards:
            if e.t <= t < e.t + e.meta["dur"]:
                a = min(1, (t - e.t) / 0.15) * min(1, (e.t + e.meta["dur"] - t) / 0.3)
                V.text_center(d, (cx, H // 2), e.meta["text"], V.font("mono", 90), tuple(int(c * a) for c in (240, 240, 230)), spacing=18)

        # titel
        if title.t <= t < title.t + 3.2:
            a = V.ease((t - title.t) / 0.08)
            for k, word in enumerate(["HET", "KOMT", "ERAAN"]):
                V.text_center(d, (cx, 740 + k * 230), word, V.font("helv-black", 250), tuple(int(c * a) for c in (RED if k == 2 else (240, 236, 228))))
            V.text_center(d, (cx, 1480), "POM  ·  2016 — 2025", V.font("mono", 32), tuple(int(150 * a) for _ in range(3)), spacing=10)
        if t >= marks["end"] - 1.6:
            V.text_center(d, (cx, H // 2), "binnenkort in je oren", V.font("didot-italic", 54), (150, 146, 140))

        # nabewerking: flits, chromatische aberratie, slice-glitch, scanlines, vignet, flikker
        a = np.asarray(img).astype(np.float32)
        rng = np.random.default_rng(int(t * 1000))
        if shake > 0.6:
            a = a * 0.4 + 255 * 0.6 * (shake - 0.6) / 0.4 + a * 0.6
        act_now = _act({k[4:]: v for k, v in marks.items() if k.startswith("act_")}, t)
        glitch = shake * 1.0 + (0.25 if act_now in "CD" and t < marks["cut"] else 0)
        if glitch > 0.05:
            k = int(4 + 22 * glitch)
            a[..., 0] = np.roll(a[..., 0], k, axis=1)
            a[..., 2] = np.roll(a[..., 2], -k, axis=1)
            for _ in range(int(glitch * 8)):
                y = rng.integers(0, H - 80)
                h = rng.integers(8, 80)
                a[y:y + h] = np.roll(a[y:y + h], rng.integers(-60, 60), axis=1)
            a = np.roll(a, (int(rng.normal(0, 8 * shake)), int(rng.normal(0, 8 * shake))), axis=(0, 1))
        flick = 0.92 + 0.08 * rng.random()
        a = a * SCAN * VIGNETTE * flick
        a += rng.normal(0, 9, (H // 4, W // 4, 1)).repeat(4, 0).repeat(4, 1)
        img.paste(Image.fromarray(np.clip(a, 0, 255).astype(np.uint8)))

    return paint


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    print("· ElevenLabs score/sfx/stem…")
    assets()
    print("· zinnen ophalen…")
    clips, meta = material()
    ev, marks = compose(clips, meta)
    mix = mixdown(ev, marks)
    m = master(mix, OUT / "het_komt_eraan")
    print(f"· audio {marks['end']:.1f}s")
    paint = painter(ev, marks)
    if "--stills" in sys.argv:
        for tt in (marks["act_A"] + 4, marks["act_B"] + 1.2, marks["act_B"] + 8, marks["act_C"] + 3,
                   marks["act_D"] + 0.1, marks["cut"] + 1.6, marks["title"] + 0.5):
            V.still(paint, tt, OUT / f"still_{tt:05.1f}.png", bg=(6, 6, 7))
        return
    V.render(paint, marks["end"], OUT / "het_komt_eraan.wav", OUT / "het_komt_eraan.mp4", bg=(6, 6, 7), film_grain=False)
    print(f"· video {OUT/'het_komt_eraan.mp4'}")


if __name__ == "__main__":
    main()
