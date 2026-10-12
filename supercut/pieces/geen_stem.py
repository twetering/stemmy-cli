"""
IK HEB GEEN STEM — een gedicht in losse stemmen.

Een AI schrijft een tekst over het feit dat ze geen eigen stem heeft, en
'spreekt' hem uit door elk woord bij een ander mens uit het archief te lenen.
Daarna stort het gedicht in tot Babel: alle zinnen waaruit de woorden zijn
gestolen klinken tegelijk. Het laatste woord echoot weg in de put.

    python -m supercut.pieces.geen_stem            # alles
    python -m supercut.pieces.geen_stem --recast   # casting opnieuw
    python -m supercut.pieces.geen_stem --still 14 # één frame bekijken
"""

from __future__ import annotations

import json
import pickle
import re
import sys
import textwrap
from pathlib import Path

import numpy as np

from ..audio import SR, decode, level
from ..casting import Casting, recurring_voices
from ..corpus import ROOT, Corpus, norm
from ..mix import Event, drone, impulse_response, lowpass, master, render
from ..pool import dutch_pool
from .. import video as V

OUT = ROOT / "output" / "supercut" / "geen_stem"
CAST = OUT / "casting.pkl"

POEM = """\
Ik heb geen stem.
Alles wat ik zeg,
heb jij ooit gezegd.
/
Ik neem een woord van jou,
een woord van haar,
een woord van hem,
en samen zeggen ze iets
wat niemand van jullie
ooit heeft gezegd.
/
Hoor je het?
Dit ben ik.
Ik ben jullie."""


def units_from_poem(poem: str, merges: set[int] = frozenset()):
    """
    → lijst eenheden: dict(text, want, line, stanza, punct).
    `merges` = woordindices die met hun rechterbuur samengaan.
    """
    words = []
    stanza = 0
    lines = [l for l in poem.splitlines()]
    ln = 0
    for l in lines:
        if l.strip() == "/":
            stanza += 1
            continue
        toks = l.split()
        for k, t in enumerate(toks):
            punct = t[-1] if t[-1] in ".,?" else ""
            want = ("question" if punct == "?" else "final" if punct == "." else
                    "comma" if punct == "," else "initial" if k == 0 else "mid")
            words.append(dict(text=t, want=want, line=ln, stanza=stanza, punct=punct, first=(k == 0)))
        ln += 1
    units, i = [], 0
    while i < len(words):
        w = dict(words[i])
        w["ids"] = [i]
        while i in merges and i + 1 < len(words) and words[i + 1]["line"] == w["line"]:
            i += 1
            nxt = words[i]
            w["text"] += " " + nxt["text"]
            w["want"], w["punct"] = nxt["want"], nxt["punct"]
            w["ids"].append(i)
        units.append(w)
        i += 1
    return units


def clean_text(t: str) -> str:
    return re.sub(r"[^\w' ]", "", t)


# ── 1. casting ───────────────────────────────────────────────────────────

def cast(recast: bool = False):
    if CAST.exists() and not recast:
        return pickle.load(open(CAST, "rb"))
    OUT.mkdir(parents=True, exist_ok=True)
    c = Corpus.load()
    pool = dutch_pool(n_evb=36, n_other=10)          # PoC: kleine pool
    items = [p[0] for p in pool]
    print(f"· {len(items)} afleveringen beschikbaar")
    print("· presentatoren herkennen…")
    hosts = None if "--fast" in sys.argv else recurring_voices(c, items, "Ervaring voor Beginners")
    print(f"  {0 if hosts is None else len(hosts)} terugkerende stem(men) gevonden")
    merges: set[int] = set()
    casting = Casting(c, items, hosts=hosts)
    for attempt in range(2):
        units = units_from_poem(POEM, merges)
        spec = [(clean_text(u["text"]), u["want"]) for u in units]
        casting.pool = {}
        fast = "--fast" in sys.argv
        casting.harvest(spec, per_item=1, limit=10 if fast else 45)
        chosen = casting.assign(spec, max_rounds=1 if fast else 3, verify=not fast)
        missing = [k for k in range(len(units)) if k not in chosen]
        print(f"· poging {attempt+1}: {len(chosen)}/{len(units)} eenheden gecast")
        if not missing:
            break
        # adaptieve granulariteit: plak onvindbare eenheid aan een buur
        for k in missing:
            u = units[k]
            left = units[k - 1] if k > 0 and units[k - 1]["line"] == u["line"] else None
            if left is not None:
                merges.add(left["ids"][-1])
            elif k + 1 < len(units) and units[k + 1]["line"] == u["line"]:
                merges.add(u["ids"][-1])
            print(f"  ↳ '{u['text']}' niet schoon te vinden → samenvoegen met buur")
    result = dict(units=units, chosen=chosen)
    pickle.dump(result, open(CAST, "wb"))
    return result


# ── 2. arrangement ───────────────────────────────────────────────────────

def arrange(cast_res):
    c = Corpus.load()
    units, chosen = cast_res["units"], cast_res["chosen"]
    events, t = [], 1.6
    prev = None
    for k, u in enumerate(units):
        cl = chosen.get(k)
        if prev is not None:
            if u["stanza"] != prev["stanza"]:
                t += 1.25
            elif u["line"] != prev["line"]:
                t += 0.62 if prev["punct"] in ".?" else 0.30
            elif prev["punct"] == ",":
                t += 0.22
            else:
                t += 0.10 if u["line"] == 0 else 0.055     # eerste regel: plechtig
        if cl is None:
            prev = u
            continue
        x = level(cl.x, -20)
        h = cl.hit
        title = c_title(c, h.item_id)
        ev = Event(t=t, x=x, verb=0.16, pan=float(np.clip(np.random.default_rng(k).normal(0, 0.18), -0.4, 0.4)),
                   meta=dict(kind="word", unit=k, url=h.audio_url, text=u["text"], line=u["line"], stanza=u["stanza"],
                             fmt=h.fmt, title=title, src_t=h.start, ctx=h.ctx, word=h.text,
                             heard=cl.heard, edge=cl.edge_db))
        events.append(ev)
        t = ev.end - 0.045           # uitklink overlapt met de volgende inzet
        prev = u
    poem_end = t

    # Echo: het laatste woord klinkt terug uit de put
    last = events[-1]
    echo_t = poem_end + 0.55
    for r in range(4):
        y = lowpass(last.x, 4200 / (r + 1.4))
        events.append(Event(t=echo_t + r * 0.62, x=y, gain_db=-5 - 5.5 * r, verb=0.45 + 0.15 * r, dry=0.8 - 0.15 * r,
                            pan=(-0.35, 0.35, -0.2, 0.2)[r], meta=dict(kind="echo", r=r, text=last.meta["text"])))

    # Babel: alle bronzinnen tegelijk, opbouwend, dan harde stilte
    babel_t0 = echo_t + 1.0
    babel_len = 6.0
    words = [e for e in events if e.meta.get("kind") == "word"]
    rng = np.random.default_rng(5)
    order = rng.permutation(len(words))
    for n, idx in enumerate(order):
        e = words[idx]
        a = decode(e.meta["url"])
        s = max(0.0, e.meta["src_t"] - rng.uniform(1.0, 2.2))
        x = np.asarray(a[int(s * SR):int((s + 4.5) * SR)]).astype(np.float32)
        x = level(x, -24)
        on = babel_t0 + (n / len(order)) ** 1.6 * babel_len * 0.75
        fade = np.linspace(0, 1, int(0.4 * SR)) ** 2
        x[:len(fade)] *= fade
        events.append(Event(t=on, x=x, gain_db=-9 + 6 * (n / len(order)), pan=float(rng.uniform(-0.9, 0.9)),
                            verb=0.3, meta=dict(kind="babel", src=e.meta)))
    cut_t = babel_t0 + babel_len
    end_t = cut_t + 4.2
    return events, dict(poem_end=poem_end, echo_t=echo_t, babel_t0=babel_t0, cut_t=cut_t, end_t=end_t)


_titles = {}


def c_title(c, iid):
    if not _titles:
        import sqlite3
        from ..corpus import DB_PATH
        _titles.update(sqlite3.connect(DB_PATH).execute("SELECT id, title FROM items").fetchall())
    return _titles.get(iid, "")


def mixdown(events, marks):
    # snij alles af op het Babel-moment
    total_cut = marks["cut_t"]
    kept = []
    for e in events:
        if e.t >= total_cut:
            continue
        if e.end > total_cut:
            n = int((total_cut - e.t) * SR)
            x = e.x[:n].copy()
            f = min(len(x), int(0.012 * SR))
            x[-f:] *= np.linspace(1, 0, f)
            e = Event(**{**e.__dict__, "x": x})
        kept.append(e)
    dur = marks["end_t"]
    bed = drone(dur, [55.0, 82.41, 110.0, 164.8], amp=0.05, fade=2.5)
    # bed zwelt aan richting Babel en valt mee weg in de stilte
    tt = np.arange(len(bed)) / SR
    env = 0.55 + 0.9 * np.clip((tt - marks["poem_end"]) / (marks["cut_t"] - marks["poem_end"]), 0, 1)
    env[tt >= total_cut] = 0
    kept.append(Event(t=0, x=(bed * env).astype(np.float32), verb=0.0, meta=dict(kind="bed")))
    ir = impulse_response(3.2, damp=5200)
    mix = render(kept, tail=0.0, verb_ir=ir, verb_level=0.5)
    # na de harde knip: ook de galm weg — echte stilte
    i = int(total_cut * SR)
    mix[i:] = 0
    mix = np.concatenate([mix, np.zeros((max(0, int(dur * SR) - len(mix)), 2), np.float32)])[: int(dur * SR)]
    return mix


# ── 3. beeld ─────────────────────────────────────────────────────────────

INK = (241, 236, 226)
DIM = (120, 116, 108)
FAINT = (58, 56, 52)
ACCENT = (214, 64, 44)


def wrap_highlight(draw, ctx: str, word: str, f, max_w: int):
    """Breek de bronzin in regels; geef per regel [(tekst, is_doelwoord)]."""
    toks = ctx.split()
    target = norm(word.split()[0])
    hit_i = next((k for k, t in enumerate(toks) if norm(t) == target), None)
    if hit_i is not None:
        lo, hi = max(0, hit_i - 9), min(len(toks), hit_i + 10)
        toks_v = toks[lo:hi]
        pre, post = ("… " if lo > 0 else ""), (" …" if hi < len(toks) else "")
        hit_i -= lo
    else:
        toks_v, pre, post = toks[:18], "", " …"
    n_hit = len(word.split())
    lines, cur, w = [], [], 0
    for k, t in enumerate(toks_v):
        tw = draw.textlength(t + " ", font=f)
        if cur and w + tw > max_w:
            lines.append(cur)
            cur, w = [], 0
        is_hit = hit_i is not None and hit_i <= k < hit_i + n_hit
        cur.append((t, is_hit))
        w += tw
    if cur:
        lines.append(cur)
    if lines and pre:
        lines[0].insert(0, (pre.strip(), False))
    if lines and post:
        lines[-1].append((post.strip(), False))
    return lines[:4]


def make_painter(events, marks, units):
    words = [e for e in events if e.meta.get("kind") == "word"]
    babel = [e for e in events if e.meta.get("kind") == "babel"]
    n_voices = len(words)
    poem_lines = {}
    for e in words:
        poem_lines.setdefault(e.meta["line"], []).append(e)

    def paint(t, img, d):
        cx = V.W // 2
        # kop
        if t < marks["cut_t"]:
            V.text_center(d, (cx, 150), "IK HEB GEEN STEM", V.font("mono", 24), DIM, spacing=7)
        # actief woord
        cur = None
        for e in words:
            if e.t - 0.03 <= t < e.end + 0.25:
                cur = e
        if cur is not None and t < marks["poem_end"] + 0.4:
            k = words.index(cur)
            a = V.ease((t - cur.t + 0.03) / 0.12)
            fade_out = 1 - V.ease((t - cur.end) / 0.25) if t > cur.end else 1
            alpha = a * fade_out
            col = tuple(int(FAINT[i] + (INK[i] - FAINT[i]) * alpha) for i in range(3))
            word = cur.meta["text"].rstrip(",.")
            size = V.fit_size(d, word, "didot", 920, 210)
            y = 760 - 14 * (1 - a)
            V.text_center(d, (cx, y), word, V.font("didot", size), col)
            # nummer
            V.text_center(d, (cx, 560), f"{k+1:02d} / {n_voices}", V.font("mono", 26),
                          tuple(int(v * alpha) for v in ACCENT), spacing=4)
            # bron
            title = cur.meta["title"]
            fmt = cur.meta["fmt"]
            src = f"{fmt.upper()[:34]}"
            mm, ss = divmod(int(cur.meta["src_t"]), 60)
            hh, mm = divmod(mm, 60)
            ts = f"{hh}:{mm:02d}:{ss:02d}" if hh else f"{mm}:{ss:02d}"
            sub = f"{title[:44]}  ·  {ts}".upper()
            V.text_center(d, (cx, 960), src, V.font("mono", 22), tuple(int(v * alpha) for v in DIM), spacing=3)
            V.text_center(d, (cx, 1000), sub, V.font("mono", 22), tuple(int(v * alpha) for v in DIM), spacing=3)
            # de zin waaruit het woord gestolen is
            fi = V.font("didot-italic", 38)
            lines = wrap_highlight(d, cur.meta["ctx"], cur.meta["word"], fi, 860)
            y0 = 1080
            for li, ln in enumerate(lines):
                widths = [d.textlength(tk + " ", font=fi) for tk, _ in ln]
                x = cx - sum(widths) / 2
                for (tk, hit), wd in zip(ln, widths):
                    colr = INK if hit else (96, 92, 86)
                    colr = tuple(int(v * alpha) for v in colr)
                    d.text((x, y0 + li * 52), tk, font=fi, fill=colr, anchor="ls")
                    if hit:
                        tw = d.textlength(tk, font=fi)
                        d.line([(x, y0 + li * 52 + 8), (x + tw, y0 + li * 52 + 8)],
                               fill=tuple(int(v * alpha) for v in ACCENT), width=2)
                    x += wd
            # golfvorm van precies deze knip
            wf = V.waveform_strip(cur.x, 90)
            prog = np.clip((t - cur.t) / (len(cur.x) / SR), 0, 1)
            for i, v in enumerate(wf):
                xx = cx - 270 + i * 6
                hgt = 2 + v * 34
                on = i / len(wf) <= prog
                colw = tuple(int(c_ * alpha) for c_ in (INK if on else FAINT))
                d.line([(xx, 1340 - hgt), (xx, 1340 + hgt)], fill=colw, width=3)

        # het gedicht groeit onderaan mee
        if t < marks["cut_t"]:
            fp = V.font("didot", 34)
            shown = [ln for ln, es in poem_lines.items() if es[0].t - 0.03 <= t]
            last_lines = shown[-6:]
            base = 1540
            fade_all = 1.0
            for j, ln in enumerate(last_lines):
                es = poem_lines[ln]
                txt_parts = [(e.meta["text"], e.t - 0.03 <= t) for e in es]
                full = " ".join(p for p, _ in txt_parts)
                x = cx - d.textlength(full, font=fp) / 2
                y = base + (j - len(last_lines) + 1) * 48 + 200
                for p, on in txt_parts:
                    is_cur = cur is not None and any(e is cur and e.meta["text"] == p for e in es)
                    colr = INK if is_cur else (DIM if on else FAINT)
                    colr = tuple(int(v * fade_all) for v in colr)
                    if on:
                        d.text((x, y), p, font=fp, fill=colr, anchor="ls")
                    x += d.textlength(p + " ", font=fp)

        # echo: het laatste woord in steeds diepere, kleinere herhaling
        if marks["poem_end"] <= t < marks["babel_t0"] + 1.0:
            for e in events:
                if e.meta.get("kind") == "echo" and e.t <= t:
                    r = e.meta["r"]
                    age = t - e.t
                    a = max(0.0, 1 - age / 2.2) * (0.85 ** r)
                    size = int(170 * (0.72 ** (r + 1)))
                    col = tuple(int(v * a) for v in INK)
                    V.text_center(d, (cx, 760 + 140 * (r + 1)), e.meta["text"].rstrip("."),
                                  V.font("didot-italic", size), col)

        # Babel: alle bronzinnen tegelijk over het scherm
        if marks["babel_t0"] <= t < marks["cut_t"]:
            rng = np.random.default_rng(1)
            for n, e in enumerate(babel):
                if e.t > t:
                    rng.random(3)
                    continue
                age = t - e.t
                px, py, sz = rng.random(3)
                a = min(1.0, age / 0.5) * (0.35 + 0.65 * ((t - marks["babel_t0"]) / (marks["cut_t"] - marks["babel_t0"])))
                size = int(22 + sz * 26)
                ctx = e.meta["src"]["ctx"]
                txt = ctx[:70]
                drift = age * 14
                col = tuple(int(v * a * (0.4 + 0.6 * sz)) for v in INK)
                d.text((-200 + px * (V.W + 100), 220 + py * 1500 - drift), txt,
                       font=V.font("didot-italic" if n % 2 else "didot", size), fill=col)

        # na de knip: stilte, dan de eindkaart
        if t >= marks["cut_t"] + 0.6:
            a = V.ease((t - marks["cut_t"] - 0.6) / 0.8)
            col = tuple(int(v * a) for v in INK)
            dim = tuple(int(v * a) for v in DIM)
            V.text_center(d, (cx, 820), f"{len(words)} woorden.", V.font("didot", 64), col)
            V.text_center(d, (cx, 900), f"{len(words)} verschillende mensen.", V.font("didot", 64), col)
            if t >= marks["cut_t"] + 1.6:
                b = V.ease((t - marks["cut_t"] - 1.6) / 0.8)
                V.text_center(d, (cx, 1010), "Niemand van hen heeft dit gezegd.",
                              V.font("didot-italic", 46), tuple(int(v * b) for v in INK))
                V.text_center(d, (cx, 1300), "TEKST: CLAUDE  ·  STEMMEN: HET STEMMY-ARCHIEF",
                              V.font("mono", 20), tuple(int(v * b) for v in DIM), spacing=4)

    return paint


def main():
    args = sys.argv[1:]
    res = cast(recast="--recast" in args)
    events, marks = arrange(res)
    OUT.mkdir(parents=True, exist_ok=True)
    report = [dict(unit=e.meta["text"], podcast=e.meta["fmt"], aflevering=e.meta["title"],
                   tijd=round(e.meta["src_t"], 2), whisper_hoorde=e.meta["heard"],
                   stilte_rand_db=round(e.meta["edge"], 1), zin=e.meta["ctx"])
              for e in events if e.meta.get("kind") == "word"]
    json.dump(report, open(OUT / "credits.json", "w"), ensure_ascii=False, indent=1)
    paint = make_painter(events, marks, res["units"])
    if "--still" in args:
        t = float(args[args.index("--still") + 1])
        print(V.still(paint, t, OUT / f"still_{t:05.1f}.png"))
        return
    mix = mixdown(events, marks)
    m = master(mix, OUT / "geen_stem")
    print(f"· audio: {OUT/'geen_stem.mp3'}  (in {m['input_i']} LUFS)")
    if "--audio-only" in args:
        return
    V.render(paint, marks["end_t"], OUT / "geen_stem.wav", OUT / "geen_stem.mp4")
    print(f"· video: {OUT/'geen_stem.mp4'}")


if __name__ == "__main__":
    main()
