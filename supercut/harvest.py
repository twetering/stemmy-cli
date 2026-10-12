"""
Harvester: vind voor een tekst-eenheid ('stem', 'een woord van') de best
knipbare voorkomens in het archief.

Drie lagen bovenop de ASR-timestamps, omdat die alleen niet genoeg zijn:

1. Akoestische eilanden — rond de ASR-tijd zoeken we een aaneengesloten
   spraakeiland met echte stilte aan beide kanten. Dat is wat een knip
   'schoon' maakt; ASR-gaps zijn bij geïnterpoleerde transcripts waardeloos.
2. Blinde verificatie — Whisper luistert de knip terug en moet exact de
   doeltekst horen (geen restje van een buurwoord).
3. Stemherkenning — wespeaker-embeddings, zodat een stuk met "40 stemmen"
   ook echt 40 verschillende mensen bevat (en niet 15× de host).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

import numpy as np

from .audio import SR, _energy, decode
from .corpus import Corpus, Hit, norm
from .mix import highpass


# ── akoestische eilanden ─────────────────────────────────────────────────

HOP = 240  # 5ms


def syllables(text: str) -> int:
    return max(1, len(re.findall(r"[aeiouyàáèéëïóöü]+", text.lower())))


@dataclass
class Clip:
    hit: Hit
    x: np.ndarray
    t0: float                 # absolute starttijd in bron
    t1: float
    edge_db: float            # stilte-diepte aan de randen t.o.v. piek
    prosody: float            # 0..1 match met gewenste positie in de zin
    score: float = 0.0
    heard: str = ""
    verdict: str = ""
    emb: np.ndarray | None = None
    f0: float = 0.0
    meta: dict = field(default_factory=dict)


def find_island(a, start: float, end: float, text: str, search: float = 0.35):
    """
    Zoek het spraakeiland (of een rij aaneengesloten eilanden) rond [start, end]
    dat het best past bij de verwachte duur van `text`.
    → (t0, t1, edge_db) of None.
    """
    w0 = max(0.0, start - search)
    w1 = min(len(a) / SR, end + search)
    seg = np.asarray(a[int(w0 * SR):int(w1 * SR)])
    if len(seg) < SR * 0.2:
        return None
    seg = highpass(seg, 120)
    e = _energy(seg, hop=HOP, win=480)
    peak = np.percentile(e, 98)
    floor = np.percentile(e, 8)
    thr = max(peak - 30, floor + 9)
    on = e > thr
    # dicht micro-gaatjes (<35ms) binnen een woord: plosieven, stemloze klanken
    k = 7
    for i in range(1, len(on) - 1):
        if not on[i]:
            j = i
            while j < len(on) and not on[j]:
                j += 1
            if j - i < k and i > 0 and j < len(on) and on[i - 1] and on[j]:
                on[i:j] = True
    # eilanden
    isl, i = [], 0
    while i < len(on):
        if on[i]:
            j = i
            while j < len(on) and on[j]:
                j += 1
            isl.append((i, j))
            i = j
        else:
            i += 1
    if not isl:
        return None
    t = lambda f: w0 + (f * HOP + 240) / SR
    exp = 0.11 + 0.17 * syllables(text)      # verwachte duur (s)
    best, best_cost = None, 1e9
    for a_i in range(len(isl)):
        for b_i in range(a_i, min(a_i + 4, len(isl))):
            s, f = t(isl[a_i][0]), t(isl[b_i][1])
            dur = f - s
            if dur < 0.6 * exp * 0.6 or dur > exp * 2.4:
                continue
            # moet het ASR-midden bevatten
            mid = (start + end) / 2
            if not (s - 0.12 <= mid <= f + 0.12):
                continue
            cost = abs(np.log(dur / exp)) * 2 + abs(s - start) * 2.5 + abs(f - end) * 1.5
            if cost < best_cost:
                best, best_cost = (a_i, b_i), cost
    if best is None:
        return None
    s_f, f_f = isl[best[0]][0], isl[best[1]][1]
    before = e[max(0, s_f - 8):s_f]
    after = e[f_f:f_f + 8]
    edge = peak - max(before.max() if len(before) else floor, after.max() if len(after) else floor)
    # pre-roll 18ms (in de stilte), post-roll 45ms (natuurlijke uitklink)
    return t(s_f) - 0.018 - 0.0025, t(f_f) + 0.045, float(edge)


def prosody_score(c: Corpus, rec_i: int, n: int, want: str) -> float:
    """Past de positie in de oorspronkelijke zin bij wat we nodig hebben?"""
    first, last = c.texts[rec_i], c.texts[rec_i + n - 1]
    prev = c.texts[rec_i - 1] if rec_i > 0 else "."
    ends_stop = last.rstrip().endswith((".", "!"))
    ends_q = last.rstrip().endswith("?")
    ends_comma = last.rstrip().endswith(",")
    initial = first[:1].isupper() or prev.rstrip().endswith((".", "?", "!"))
    if want == "final":
        return 1.0 if ends_stop else (0.4 if ends_comma else 0.0)
    if want == "question":
        return 1.0 if ends_q else 0.1
    if want == "initial":
        return (0.7 if initial else 0.2) + (0.3 if not (ends_stop or ends_q) else 0)
    if want == "comma":
        return 1.0 if ends_comma else (0.5 if not (ends_stop or ends_q) else 0.2)
    # mid
    return 1.0 if not (ends_stop or ends_q or ends_comma) else 0.3


def candidates(c: Corpus, unit: str, want: str = "mid", items: set | None = None,
               min_conf: float = 0.5, per_item: int = 3, limit: int = 400):
    """Corpus-hits voor een eenheid, met recordindex, gefilterd op afleveringen."""
    toks = [norm(t) for t in unit.split() if norm(t)]
    out = []
    for i in c.vocab.get(toks[0], []):
        j = i + len(toks) - 1
        if j >= len(c.rec) or c.rec[j]["item"] != c.rec[i]["item"]:
            continue
        if any(norm(c.texts[i + k]) != toks[k] for k in range(1, len(toks))):
            continue
        iid = c.item_list[c.rec[i]["item"]]
        if items is not None and iid not in items:
            continue
        h = c._hit(i, j)
        if h.conf < min_conf or not h.audio_url:
            continue
        out.append((prosody_score(c, i, len(toks), want), h))
    # per aflevering max `per_item`, beste prosodie eerst
    out.sort(key=lambda p: (-p[0], -p[1].conf))
    seen, res = {}, []
    for p, h in out:
        if seen.get(h.item_id, 0) >= per_item:
            continue
        seen[h.item_id] = seen.get(h.item_id, 0) + 1
        res.append((p, h))
        if len(res) >= limit:
            break
    return res


def make_clip(h: Hit, unit: str, prosody: float) -> Clip | None:
    a = decode(h.audio_url)
    isl = find_island(a, h.start, h.end, unit)
    if not isl:
        return None
    t0, t1, edge = isl
    x = np.asarray(a[int(t0 * SR):int(t1 * SR)]).astype(np.float32)
    x = highpass(x, 70)
    fi, fo = int(0.006 * SR), int(0.03 * SR)
    if len(x) <= fi + fo:
        return None
    x[:fi] *= np.linspace(0, 1, fi) ** 2
    x[-fo:] *= np.linspace(1, 0, fo) ** 2
    score = min(edge, 30) / 30 * 0.55 + prosody * 0.3 + h.conf * 0.15
    return Clip(hit=h, x=x, t0=t0, t1=t1, edge_db=edge, prosody=prosody, score=score)


# ── stemherkenning ───────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _spk():
    import warnings
    warnings.filterwarnings("ignore")
    from pyannote.audio import Inference, Model
    return Inference(Model.from_pretrained("pyannote/wespeaker-voxceleb-resnet34-LM"), window="whole")


def voice(x: np.ndarray) -> np.ndarray:
    """256-d genormaliseerde stem-embedding van een clip (48k mono)."""
    import torch
    from scipy.signal import resample_poly
    y = resample_poly(x, 1, 3).astype(np.float32)
    if len(y) < 16000:                       # te kort: herhaal (embedding wil ≥1s)
        y = np.tile(y, int(np.ceil(16000 / max(1, len(y)))))
    v = _spk()({"waveform": torch.from_numpy(y[None]), "sample_rate": 16000})
    v = np.asarray(v, dtype=np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


def context_voice(h: Hit, span: float = 2.5, embed: bool = True):
    """Stem-embedding van de directe omgeving (robuuster dan één woord)."""
    a = decode(h.audio_url)
    mid = round((h.start + h.end) / 2, 2)
    x = np.asarray(a[int(max(0, mid - span / 2) * SR):int((mid + span / 2) * SR)]).astype(np.float32)
    return voice(x) if embed else None
