"""
IK DENK — 822 keer begon iemand een zin met 'ik denk'.

Hele zinnen (geen losse woorden), geknipt met de bestaande ParallelExtractor op
de woordtimings uit de DB. Gecomponeerd op een beat-grid (96 BPM):
  I   stotter: alleen 'ik denk' van acht mensen op achtsten
  II  bekentenis: korte zinnen, één voor één, op de maat
  III canon: voorspellingen over elkaar heen, links-rechts
  IV  wijsheid: ruimte, alleen pad
  coda: een AI-stem begint 'Ik denk…' en maakt de zin niet af.

    python -m supercut.pieces.ik_denk
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from stemmy_cli.audio.parallel_extractor import ParallelExtractor

from ..audio import SR, level
from ..corpus import ROOT, Corpus
from ..mix import Event, drone, highpass, impulse_response, kick, master, render, tick
from .. import video as V

OUT = ROOT / "output" / "supercut" / "ik_denk"
BPM = 96
BEAT = 60 / BPM
AI_VOICE = "PIpnKISoU0pJEeZu0rOq"     # 'Theo' uit de voices-tabel

# Recordindex (eerste woord, laatste woord) — handmatig gecureerd uit 822 kandidaten.
STOTTER = [1383602, 1621232, 763839, 822706, 410789, 1203075, 1050235, 1568962]
BEKENTENIS = [(698667, 698674), (119128, 119141), (392976, 392983),
              (202443, 202453), (936986, 936993)]
CANON = [(1597144, 1597154), (260168, 260176), (1491327, 1491333), (1305697, 1305706),
         (1624702, 1624709), (1177500, 1177508)]
WIJSHEID = [(787347, 787355), (1141606, 1141612), (1641255, 1641266), (798735, 798745)]
INTRO_TEXT = "Achthonderdtweeëntwintig keer begon iemand een zin met: ik denk."


# ── materiaal ────────────────────────────────────────────────────────────

def sentence(c: Corpus, i: int, j: int) -> dict:
    r = c.rec
    iid = c.item_list[r[i]["item"]]
    url, fmt, _ = c.items[iid]
    t0 = r[i]["start"] / 1000
    words = [(c.texts[k], r[k]["start"] / 1000 - t0, r[k]["end"] / 1000 - t0) for k in range(i, j + 1)]
    return dict(id=f"s{i}", audio_url=url, start_time=max(0, t0 - 0.06), end_time=r[j]["end"] / 1000 + 0.18,
                words=words, fmt=fmt, text=" ".join(w[0] for w in words), lead=0.06)


def load_mp3(path: str) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                         capture_output=True).stdout
    return np.frombuffer(raw, np.float32).copy()


def gather(c: Corpus) -> dict:
    """Alle zinnen in één keer via de bestaande extractor (byte-range, parallel)."""
    specs = {}
    for i in STOTTER:
        s = sentence(c, i, i + 1)                      # alleen 'Ik denk'
        s["end_time"] = s["start_time"] + 0.06 + s["words"][1][2] + 0.05
        specs[s["id"] + "_st"] = {**s, "id": s["id"] + "_st"}
    for grp in (BEKENTENIS, CANON, WIJSHEID):
        for i, j in grp:
            s = sentence(c, i, j)
            specs[s["id"]] = s
    tmp = Path(tempfile.mkdtemp())
    ok, bad = ParallelExtractor(max_workers=16, output_dir=tmp).extract_segments(list(specs.values()))
    for r in ok:
        x = highpass(load_mp3(r.output_path), 80)
        specs[r.segment_id]["x"] = level(x, -20)
    for r in bad:
        print("  ! mislukt:", r.segment_id, r.error)
    return {k: v for k, v in specs.items() if "x" in v}


def ai_voice(text: str) -> np.ndarray:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=True)
    from stemmy_cli.tts.elevenlabs import generate_tts
    p = OUT / f"ai_{abs(hash(text)) % 10**6}.mp3"
    cache = OUT / ("ai_" + "".join(ch for ch in text if ch.isalnum())[:30] + ".mp3")
    if not cache.exists():
        generate_tts(text, AI_VOICE, output_path=cache)
    return level(highpass(load_mp3(str(cache)), 80), -19)


# ── compositie ───────────────────────────────────────────────────────────

def compose(mat: dict):
    ev, marks = [], {}
    rng = np.random.default_rng(3)
    t = 0.8
    intro = ai_voice(INTRO_TEXT)
    ev.append(Event(t=t, x=intro, verb=0.12, meta=dict(kind="ai", text=INTRO_TEXT)))
    t = round((t + len(intro) / SR + 0.6) / BEAT) * BEAT
    marks["music"] = t

    # I — stotter op achtsten, twee keer de reeks, tweede keer dichter
    st = [v for k, v in mat.items() if k.endswith("_st")]
    for rep in range(2):
        for n, s in enumerate(st):
            step = BEAT / 2 if rep == 0 else BEAT / 4
            tt = t + n * step
            ev.append(Event(t=tt - s["lead"], x=s["x"], pan=(-0.6, 0.6)[n % 2] * (0.4 + 0.6 * rep),
                            gain_db=-1, verb=0.1, meta=dict(kind="stutter", s=s, n=n)))
        t += len(st) * (BEAT / 2 if rep == 0 else BEAT / 4) + BEAT
    marks["bekentenis"] = t

    # II — bekentenis: elke zin op een eerste tel
    for k in ("s" + str(i) for i, _ in BEKENTENIS):
        if k not in mat:
            continue
        s = mat[k]
        ev.append(Event(t=t - s["lead"], x=s["x"], verb=0.14, pan=float(rng.uniform(-0.15, 0.15)),
                        meta=dict(kind="sentence", s=s, lane=0)))
        dur = len(s["x"]) / SR
        t += np.ceil((dur + 0.25) / (BEAT * 2)) * BEAT * 2
    marks["canon"] = t

    # III — canon: om de twee tellen een nieuwe stem, alles blijft doorlopen
    lanes = 0
    for n, k in enumerate("s" + str(i) for i, _ in CANON):
        if k not in mat:
            continue
        s = mat[k]
        ev.append(Event(t=t + n * BEAT * 2 - s["lead"], x=s["x"], verb=0.2, gain_db=-2,
                        pan=(-0.7, 0.7, -0.35, 0.35, -0.9, 0.9)[n % 6],
                        meta=dict(kind="sentence", s=s, lane=n)))
        lanes = n
    t = max(e.end for e in ev) + BEAT
    t = np.ceil(t / BEAT) * BEAT
    marks["wijsheid"] = t

    # IV — wijsheid: veel lucht
    for k in ("s" + str(i) for i, _ in WIJSHEID):
        if k not in mat:
            continue
        s = mat[k]
        ev.append(Event(t=t - s["lead"], x=s["x"], verb=0.22, meta=dict(kind="sentence", s=s, lane=0)))
        t += len(s["x"]) / SR + BEAT * 1.5
    marks["coda"] = t + 0.8

    # coda — de AI begint, en maakt het niet af
    coda = ai_voice("Ik denk…")
    ev.append(Event(t=marks["coda"], x=coda, verb=0.35, meta=dict(kind="ai_coda", text="Ik denk…")))
    marks["cut"] = marks["coda"] + len(coda) / SR - 0.05
    marks["end"] = marks["cut"] + 3.5

    # muziek: kick vanaf canon, ticks in stotter+canon, pad overal
    k_ = kick()
    tk = tick()
    b = marks["music"]
    while b < marks["wijsheid"]:
        in_canon = marks["canon"] <= b
        if in_canon or b < marks["bekentenis"]:
            ev.append(Event(t=b, x=k_, gain_db=-6 if in_canon else -11, meta=dict(kind="kick")))
        if b < marks["bekentenis"] or in_canon:
            ev.append(Event(t=b + BEAT / 2, x=tk, gain_db=-22, pan=0.3, meta=dict(kind="tick")))
        b += BEAT
    chords = [[110.0, 130.81, 164.81], [87.31, 110.0, 130.81], [65.41, 98.0, 130.81], [98.0, 123.47, 146.83]]
    bar = BEAT * 4
    b, n = marks["music"], 0
    while b < marks["cut"]:
        pad = drone(bar * 2 + 1.0, chords[n % 4], amp=0.045, fade=0.9)
        ev.append(Event(t=b, x=pad, verb=0.3, gain_db=-2 if b >= marks["wijsheid"] else -5, meta=dict(kind="pad")))
        b += bar * 2
        n += 1
    return ev, marks


def mixdown(ev, marks):
    mix = render(ev, tail=0.5, verb_ir=impulse_response(2.6), verb_level=0.45)
    i = int(marks["cut"] * SR)
    mix[i:] = 0                                      # de zin wordt niet afgemaakt
    n = int(marks["end"] * SR)
    return np.concatenate([mix, np.zeros((max(0, n - len(mix)), 2), np.float32)])[:n]


# ── beeld ────────────────────────────────────────────────────────────────

PALETTE = [(255, 120, 70), (255, 196, 92), (120, 200, 255), (200, 140, 255), (110, 240, 190), (255, 110, 160)]
INK = (244, 240, 232)
ACC = (255, 84, 54)


def painter(ev, marks):
    sents = [e for e in ev if e.meta.get("kind") == "sentence"]
    stut = [e for e in ev if e.meta.get("kind") == "stutter"]
    W, H = V.W, V.H
    hw, hh = W // 2, H // 2

    def ribbon(d, e, y, color, t, amp=170, alpha=1.0):
        """Lichtend lint: de echte amplitude-envelope, uitgerold tot 'nu'."""
        env = V.waveform_strip(e.x, 160)
        prog = np.clip((t - e.t) / (len(e.x) / SR), 0, 1)
        n = max(2, int(prog * len(env)))
        xs = np.linspace(40, W - 40, len(env))
        for layer in range(3):
            pts_top, pts_bot = [], []
            for k in range(n):
                wob = np.sin(k * 0.21 + t * 2 + layer) * 6
                a = env[k] * amp * (1 - layer * 0.28)
                pts_top.append((xs[k], y - a + wob))
                pts_bot.append((xs[k], y + a + wob))
            col = tuple(int(c_ * alpha * (0.9 - layer * 0.25)) for c_ in color)
            d.line(pts_top, fill=col, width=3 - layer)
            d.line(pts_bot, fill=col, width=3 - layer)
        # 'speelkop'
        if prog < 1:
            x = xs[n - 1]
            d.ellipse([x - 6, y - 6, x + 6, y + 6], fill=tuple(int(255 * alpha) for _ in range(3)))

    def words(d, e, y, t, size=74, alpha=1.0, max_w=W - 140):
        """Woorden verschijnen op hun echte tijdstip; 'Ik denk' in accentkleur."""
        s = e.meta["s"]
        f = V.font("helv-light", size)
        fb = V.font("helv-bold", size)
        x0, x, line = 70, 70, 0
        for k, (w, ws, we) in enumerate(s["words"]):
            on = t >= e.t + s["lead"] + ws - 0.02
            if not on:
                break
            fnt = fb if k < 2 else f
            tw = d.textlength(w + " ", font=fnt)
            if x + tw > x0 + max_w:
                x, line = x0, line + 1
            age = t - (e.t + s["lead"] + ws)
            a = V.ease(age / 0.18) * alpha
            col = ACC if k < 2 else INK
            col = tuple(int(c_ * a) for c_ in col)
            d.text((x, y + line * size * 1.18 - (1 - V.ease(age / 0.18)) * 10), w, font=fnt, fill=col)
            x += tw

    def paint(t, img, d):
        glow = Image.new("RGB", (W, H), (0, 0, 0))
        g = ImageDraw.Draw(glow)

        if t < marks["music"]:
            # intro: AI-tekst, typemachine
            e = ev[0]
            txt = e.meta["text"]
            prog = np.clip((t - e.t) / (len(e.x) / SR), 0, 1)
            shown = txt[: int(len(txt) * prog)]
            f = V.font("mono", 40)
            lines, cur = [], ""
            for w in shown.split(" "):
                if d.textlength(cur + w, font=f) > W - 200:
                    lines.append(cur)
                    cur = ""
                cur += w + " "
            lines.append(cur)
            for k, ln in enumerate(lines):
                d.text((100, 820 + k * 60), ln, font=f, fill=(200, 196, 188))
            if int(t * 2) % 2 == 0:
                last = lines[-1]
                cx = 100 + d.textlength(last, font=f)
                d.rectangle([cx, 820 + (len(lines) - 1) * 60, cx + 22, 820 + (len(lines) - 1) * 60 + 44],
                            fill=(200, 196, 188))
            V.text_center(d, (hw, 1500), "STEMMY-ARCHIEF  ·  822 ZINNEN", V.font("mono", 22), (90, 88, 84), spacing=6)

        elif t < marks["bekentenis"]:
            # stotter: gigantische IK DENK, elke inzet een flits, kleur per stem
            hits = [e for e in stut if e.t <= t]
            if hits:
                e = hits[-1]
                age = t - e.t
                n = e.meta["n"]
                col = PALETTE[n % len(PALETTE)]
                a = max(0.0, 1 - age / 0.35)
                size = 330 + int(60 * a)
                for k in range(len(hits[-6:])):
                    hk = hits[-6:][k]
                    aa = max(0.0, 1 - (t - hk.t) / 1.2) * 0.35
                    V.text_center(g, (hw, 300 + k * 260), "IK DENK", V.font("helv-black", 240),
                                  tuple(int(c_ * aa) for c_ in PALETTE[hk.meta["n"] % 6]))
                V.text_center(g, (hw, hh), "IK", V.font("helv-black", size), tuple(int(c_ * (0.4 + 0.6 * a)) for c_ in col))
                V.text_center(g, (hw, hh + size * 0.85), "DENK", V.font("helv-black", size),
                              tuple(int(c_ * (0.4 + 0.6 * a)) for c_ in col))
                V.text_center(d, (hw, 1760), e.meta["s"]["fmt"].upper()[:40], V.font("mono", 22), (110, 106, 100), spacing=4)

        else:
            active = [e for e in sents if e.t <= t < e.end + 1.2]
            canon = marks["canon"] <= t < marks["wijsheid"]
            for e in active:
                lane = e.meta["lane"]
                fade = 1.0 if t < e.end else max(0.0, 1 - (t - e.end) / 1.2)
                col = PALETTE[(lane + (0 if canon else 2)) % len(PALETTE)]
                if canon:
                    y_r = 260 + lane * 270
                    ribbon(g, e, y_r, col, t, amp=70, alpha=fade)
                    words(d, e, y_r + 80, t, size=40, alpha=fade)
                else:
                    ribbon(g, e, 640, col, t, amp=190, alpha=fade)
                    words(d, e, 1000, t, size=78, alpha=fade)
                    V.text_center(d, (hw, 1760), e.meta["s"]["fmt"].upper()[:40], V.font("mono", 22),
                                  tuple(int(110 * fade) for _ in range(3)), spacing=4)
            if marks["coda"] - 0.3 <= t:
                pass

        if t >= marks["coda"]:
            # coda: dezelfde typemachine als het begin — en dan zwart
            if t < marks["cut"]:
                e = [x for x in ev if x.meta.get("kind") == "ai_coda"][0]
                prog = np.clip((t - e.t) / (len(e.x) / SR), 0, 1)
                txt = "Ik denk"[: int(7 * min(1, prog * 1.6))]
                f = V.font("mono", 72)
                d.text((100, 900), txt, font=f, fill=INK)
                cx = 100 + d.textlength(txt, font=f)
                d.rectangle([cx + 8, 900, cx + 46, 980], fill=INK)
            elif t > marks["cut"] + 1.0:
                a = V.ease((t - marks["cut"] - 1.0) / 1.0)
                V.text_center(d, (hw, hh), "IK DENK", V.font("helv-black", 120), tuple(int(c_ * a) for c_ in INK))
                V.text_center(d, (hw, hh + 110), "822 MENSEN  ·  1 AI", V.font("mono", 26),
                              tuple(int(150 * a) for _ in range(3)), spacing=6)

        # bloom: glow-laag vervagen en optellen
        small = glow.resize((W // 4, H // 4), Image.BILINEAR).filter(ImageFilter.GaussianBlur(10))
        bloom = small.resize((W, H), Image.BILINEAR)
        base = np.asarray(img, np.int16) + np.asarray(glow, np.int16) + (np.asarray(bloom, np.int16) * 1.6).astype(np.int16)
        img.paste(Image.fromarray(np.clip(base, 0, 255).astype(np.uint8)))

    return paint


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    c = Corpus.load()
    print("· zinnen ophalen via ParallelExtractor…")
    mat = gather(c)
    print(f"  {len(mat)} clips")
    ev, marks = compose(mat)
    mix = mixdown(ev, marks)
    m = master(mix, OUT / "ik_denk")
    print(f"· audio {marks['end']:.1f}s, {m['input_i']} LUFS in")
    paint = painter(ev, marks)
    if "--stills" in sys.argv:
        for tt in (2, marks["music"] + 1.5, marks["bekentenis"] + 2, marks["canon"] + 7, marks["wijsheid"] + 2,
                   marks["coda"] + 0.4, marks["cut"] + 2.5):
            V.still(paint, tt, OUT / f"still_{tt:05.1f}.png")
        return
    V.render(paint, marks["end"], OUT / "ik_denk.wav", OUT / "ik_denk.mp4", film_grain=False)
    print(f"· video {OUT/'ik_denk.mp4'}")


if __name__ == "__main__":
    main()
