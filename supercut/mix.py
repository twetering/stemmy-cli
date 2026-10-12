"""
Partituur → stereo mix.

Een partituur is een lijst Events op een tijdlijn. Elk event is een clip
(numpy mono @48k) met plaatsing, gain, pan, pitch en galm-send. De renderer
mixt sample-nauwkeurig, stuurt een galmbus aan en mastert naar streaming-
loudness (-14 LUFS, true peak -1 dBTP).
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy.signal import butter, fftconvolve, sosfilt

from .audio import SR, write_wav


@dataclass
class Event:
    t: float                      # starttijd in seconden
    x: np.ndarray                 # mono clip
    gain_db: float = 0.0
    pan: float = 0.0              # -1 links … +1 rechts
    verb: float = 0.0             # galm-send 0..1
    dry: float = 1.0
    meta: dict = field(default_factory=dict)   # tekst, bron, … voor de video

    @property
    def end(self) -> float:
        return self.t + len(self.x) / SR


# ── clip-bewerkingen ─────────────────────────────────────────────────────

def highpass(x: np.ndarray, hz: float = 90) -> np.ndarray:
    return sosfilt(butter(2, hz, "hp", fs=SR, output="sos"), x).astype(np.float32)


def lowpass(x: np.ndarray, hz: float) -> np.ndarray:
    return sosfilt(butter(2, hz, "lp", fs=SR, output="sos"), x).astype(np.float32)


def bandpass(x: np.ndarray, lo: float, hi: float) -> np.ndarray:
    return sosfilt(butter(2, [lo, hi], "bp", fs=SR, output="sos"), x).astype(np.float32)


def _ffmpeg_filter(x: np.ndarray, af: str) -> np.ndarray:
    with tempfile.TemporaryDirectory() as d:
        i, o = Path(d) / "i.wav", Path(d) / "o.raw"
        write_wav(i, x)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(i), "-af", af,
                        "-ac", "1", "-ar", str(SR), "-f", "f32le", str(o)], check=True)
        return np.fromfile(o, dtype=np.float32)


def pitch(x: np.ndarray, semitones: float, formant: bool = True) -> np.ndarray:
    """Toonhoogte verschuiven zonder tempo te veranderen (rubberband)."""
    if abs(semitones) < 0.03:
        return x
    r = 2 ** (semitones / 12)
    f = ":formant=preserved" if formant else ""
    return _ffmpeg_filter(x, f"rubberband=pitch={r:.5f}{f}:transients=crisp")


def stretch(x: np.ndarray, factor: float) -> np.ndarray:
    """Tijdrekken (factor>1 = langer) zonder toonhoogte te veranderen."""
    if abs(factor - 1) < 0.01:
        return x
    return _ffmpeg_filter(x, f"rubberband=tempo={1/factor:.5f}:transients=smooth")


def f0(x: np.ndarray, fmin: float = 70, fmax: float = 400) -> float:
    """Mediane grondtoon (Hz) via autocorrelatie op stemhebbende frames; 0 = onbekend."""
    hop, win = 480, 1920
    lo, hi = int(SR / fmax), int(SR / fmin)
    vals = []
    for i in range(0, len(x) - win, hop):
        fr = x[i:i + win] * np.hanning(win)
        if np.sqrt(np.mean(fr ** 2)) < 0.01:
            continue
        ac = np.fft.irfft(np.abs(np.fft.rfft(fr, 2 * win)) ** 2)[:win]
        if ac[0] <= 0:
            continue
        ac /= ac[0]
        k = lo + int(np.argmax(ac[lo:hi]))
        if ac[k] > 0.45:
            vals.append(SR / k)
    return float(np.median(vals)) if len(vals) >= 2 else 0.0


# ── synthese (subtiele bedding) ──────────────────────────────────────────

def impulse_response(seconds: float = 2.8, predelay: float = 0.02, damp: float = 6000) -> np.ndarray:
    """Synthetische stereo-galm: gedempte ruisstaart, L/R gedecorreleerd."""
    n = int(seconds * SR)
    t = np.arange(n) / SR
    rng = np.random.default_rng(7)
    env = np.exp(-6.9 * t / seconds)
    ir = np.stack([rng.standard_normal(n) * env, rng.standard_normal(n) * env], 1).astype(np.float32)
    ir[:, 0] = lowpass(ir[:, 0], damp)
    ir[:, 1] = lowpass(ir[:, 1], damp)
    pd = int(predelay * SR)
    ir = np.concatenate([np.zeros((pd, 2), np.float32), ir])
    return ir / np.sqrt((ir ** 2).sum(0, keepdims=True))


def kick(dur: float = 0.45, f_hi: float = 120, f_lo: float = 42) -> np.ndarray:
    t = np.arange(int(dur * SR)) / SR
    f = f_lo + (f_hi - f_lo) * np.exp(-t * 28)
    ph = 2 * np.pi * np.cumsum(f) / SR
    return (np.sin(ph) * np.exp(-t * 7)).astype(np.float32)


def tick(dur: float = 0.04) -> np.ndarray:
    rng = np.random.default_rng(1)
    n = int(dur * SR)
    return (highpass(rng.standard_normal(n).astype(np.float32), 7000) * np.exp(-np.arange(n) / SR * 120)).astype(np.float32)


def drone(dur: float, freqs: list[float], amp: float = 0.08, fade: float = 3.0) -> np.ndarray:
    """Zachte zaagtand-pad, laagdoorlaat-gefilterd, met trage beweging."""
    t = np.arange(int(dur * SR)) / SR
    y = np.zeros_like(t)
    for k, fq in enumerate(freqs):
        for det in (-0.12, 0.0, 0.11):
            ff = fq * 2 ** (det / 12)
            ph = (t * ff + 0.37 * k) % 1.0
            y += (2 * ph - 1)
    y = lowpass(y.astype(np.float32), 900)
    y *= (0.75 + 0.25 * np.sin(2 * np.pi * t / 7.3))
    env = np.minimum(1, np.minimum(t / fade, (dur - t) / fade))
    return (y / (np.abs(y).max() + 1e-9) * amp * np.clip(env, 0, 1)).astype(np.float32)


# ── render ───────────────────────────────────────────────────────────────

def render(events: list[Event], tail: float = 2.5, verb_ir: np.ndarray | None = None,
           verb_level: float = 0.35) -> np.ndarray:
    total = int((max(e.end for e in events) + tail) * SR)
    dry = np.zeros((total, 2), np.float32)
    send = np.zeros((total, 2), np.float32)
    for e in events:
        g = 10 ** (e.gain_db / 20)
        # equal-power pan
        th = (e.pan + 1) * np.pi / 4
        lr = np.array([np.cos(th), np.sin(th)], np.float32) * np.sqrt(2)
        i0 = int(round(e.t * SR))
        if i0 < 0:
            continue
        seg = e.x[: total - i0]
        sig = seg[:, None] * lr[None, :] * g
        dry[i0:i0 + len(seg)] += sig * e.dry
        if e.verb > 0:
            send[i0:i0 + len(seg)] += sig * e.verb
    if send.any():
        ir = verb_ir if verb_ir is not None else impulse_response()
        wet = np.stack([fftconvolve(send[:, c], ir[:, c])[:total] for c in range(2)], 1)
        dry += wet.astype(np.float32) * verb_level
    return dry


def master(x: np.ndarray, out: Path, lufs: float = -14.0, tp: float = -1.0) -> dict:
    """Twee-pass EBU R128 loudnorm + export (wav én mp3). Geeft meetwaarden terug."""
    out = Path(out)
    with tempfile.TemporaryDirectory() as d:
        raw = Path(d) / "mix.wav"
        # zachte bus-compressie vóór normalisatie
        write_wav(raw, np.clip(x / max(1e-9, np.abs(x).max()) * 0.9, -1, 1))
        pre = "acompressor=threshold=-18dB:ratio=2.5:attack=8:release=180:makeup=2"
        r = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(raw), "-af",
                            f"{pre},loudnorm=I={lufs}:TP={tp}:LRA=11:print_format=json",
                            "-f", "null", "-"], capture_output=True, text=True)
        js = r.stderr[r.stderr.rfind("{"):r.stderr.rfind("}") + 1]
        m = json.loads(js)
        ln = (f"loudnorm=I={lufs}:TP={tp}:LRA=11:measured_I={m['input_i']}:measured_TP={m['input_tp']}"
              f":measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true")
        wav = out.with_suffix(".wav")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(raw), "-af", f"{pre},{ln}",
                        "-ar", str(SR), str(wav)], check=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(wav), "-b:a", "256k",
                        str(out.with_suffix(".mp3"))], check=True)
    return m
