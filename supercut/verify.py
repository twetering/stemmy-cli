"""
Kwaliteitspoort: luister elke uitsnede blind terug met Whisper.

Een knip is 'goed' als Whisper het doelwoord terughoort zonder extra
lettergrepen van buurwoorden. Zo meten we knipprecisie zonder oren.
"""

from __future__ import annotations

from functools import lru_cache

import numpy as np

from .audio import SR
from .corpus import norm


@lru_cache(maxsize=1)
def _model(size: str = "small"):
    from faster_whisper import WhisperModel
    return WhisperModel(size, device="cpu", compute_type="int8")


def hear(x: np.ndarray, lang: str | None = None) -> str:
    """Transcribeer een clip (met stilte-padding, 16k)."""
    from scipy.signal import resample_poly
    y = resample_poly(x, 1, 3).astype(np.float32)          # 48k → 16k
    pad = np.zeros(8000, dtype=np.float32)
    y = np.concatenate([pad, y, pad])
    segs, _ = _model().transcribe(y, language=lang, beam_size=5, vad_filter=False,
                                  condition_on_previous_text=False)
    return " ".join(s.text for s in segs).strip()


def judge(x: np.ndarray, target: str, lang: str | None = None) -> tuple[str, str]:
    """→ (oordeel, gehoord). oordeel ∈ exact | contains | miss."""
    heard = hear(x, lang)
    toks = [norm(t) for t in heard.split() if norm(t)]
    want = [norm(t) for t in target.split() if norm(t)]
    if toks == want:
        return "exact", heard
    joined = " ".join(toks)
    if " ".join(want) in joined:
        return "contains", heard
    return "miss", heard
