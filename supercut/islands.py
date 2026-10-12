"""Spraakeilanden in een bestaand audiobestand + per eiland transcriptie (voor visualisatie)."""
from __future__ import annotations

import json
import subprocess
import sys

import numpy as np
from scipy.signal import resample_poly

from .audio import SR, _energy
from .mix import highpass


def islands(path: str, model: str = "small", lang: str | None = None) -> list[dict]:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                         capture_output=True).stdout
    a = np.frombuffer(raw, np.float32).copy()
    e = _energy(highpass(a, 100), hop=240, win=480)
    thr = max(np.percentile(e, 99) - 32, np.percentile(e, 10) + 8)
    on = e > thr
    isl, i = [], 0
    while i < len(on):
        if on[i]:
            j = i
            while j < len(on) and on[j]:
                j += 1
            if isl and i - isl[-1][1] < 18:          # <90ms stilte: zelfde eiland
                isl[-1][1] = j
            else:
                isl.append([i, j])
            i = j
        else:
            i += 1
    isl = [m for m in isl if m[1] - m[0] >= 16]
    t = lambda f: (f * 240 + 240) / SR
    from faster_whisper import WhisperModel
    m = WhisperModel(model, device="cpu", compute_type="int8")
    out = []
    for s, f in isl:
        s0, f0 = max(0, t(s) - 0.03), t(f) + 0.06
        y = resample_poly(a[int(s0 * SR):int(f0 * SR)], 1, 3).astype(np.float32)
        y = np.concatenate([np.zeros(8000, np.float32), y, np.zeros(8000, np.float32)])
        txt = " ".join(x.text for x in m.transcribe(y, language=lang, beam_size=3,
                                                    condition_on_previous_text=False)[0]).strip()
        env = e[s:f]
        out.append(dict(s=round(s0, 3), e=round(f0, 3), text=txt, peak=round(float(env.max()), 1),
                        env=[round(float(v), 1) for v in env[::4]]))
        print(f"{s0:6.2f}-{f0:6.2f} {txt}", flush=True)
    return out


if __name__ == "__main__":
    json.dump(islands(sys.argv[1]), open(sys.argv[2], "w"), ensure_ascii=False)
