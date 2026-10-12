"""
Sample-nauwkeurige audio-uitsnedes via HTTP range-requests.

Geen volledige afleveringen: per knip halen we alleen een venster van een paar
seconden op, als sliceable array (handig voor woordniveau-analyse). Voor
gewone zin-knips volstaat ParallelExtractor (ffprobe-bitrate). Hier lezen we
bitrate en ID3-offset uit de mp3-header zelf (gemeten fout ≤ 25ms, binnen wat
`refine`/eilanddetectie corrigeert).
VBR-bestanden (Xing-header) vallen terug op een volledige download.
"""

from __future__ import annotations

import hashlib
import subprocess
import threading
import time
from functools import lru_cache
from pathlib import Path

import numpy as np
import requests

from .corpus import CACHE, Hit

SR = 48000
SRC_DIR = CACHE / "src"


def source_path(url: str) -> Path:
    """
    Hele aflevering lokaal, als mono Opus 80k (spraak-transparant, ~35MB/uur).
    Schijf is krap: het origineel wordt na transcoderen weggegooid.
    """
    SRC_DIR.mkdir(parents=True, exist_ok=True)
    stem = hashlib.sha1(url.encode()).hexdigest()[:16]
    p = SRC_DIR / (stem + ".opus")
    if p.exists() and p.stat().st_size > 10_000:
        return p
    mp3 = SRC_DIR / (stem + ".mp3")
    if not mp3.exists() or mp3.stat().st_size < 10_000:
        tmp = SRC_DIR / (stem + ".part")
        with requests.get(url, stream=True, timeout=60, headers={"User-Agent": "stemmy-supercut"}) as r:
            r.raise_for_status()
            with open(tmp, "wb") as fh:
                for chunk in r.iter_content(1 << 20):
                    fh.write(chunk)
        tmp.rename(mp3)
    tmp = SRC_DIR / (stem + ".tmp.opus")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(mp3), "-ac", "1", "-ar", "48000",
                    "-c:a", "libopus", "-b:a", "80k", "-application", "audio", str(tmp)], check=True)
    tmp.rename(p)
    mp3.unlink()
    _evict()
    return p


def _evict(cap_bytes: int = 3_000_000_000):
    """Houd de broncache onder ~3GB: oudste afleveringen eerst weg."""
    files = sorted(SRC_DIR.glob("*.opus"), key=lambda f: f.stat().st_mtime)
    total = sum(f.stat().st_size for f in files)
    while total > cap_bytes and len(files) > 4:
        f = files.pop(0)
        total -= f.stat().st_size
        f.unlink()


_BITRATES = {  # MPEG1 Layer III, MPEG2/2.5 Layer III (kbps)
    1: [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
    2: [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
}
_RATES = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}


_tls = threading.local()


def _get(url: str, b0: int, b1: int) -> tuple[bytes, int]:
    """Range-GET met keep-alive per thread en retries (S3 reset bij veel parallel)."""
    if not hasattr(_tls, "s"):
        _tls.s = requests.Session()
    for attempt in range(5):
        try:
            r = _tls.s.get(url, headers={"Range": f"bytes={b0}-{b1}", "User-Agent": "stemmy-supercut"},
                           timeout=30, allow_redirects=True)
            r.raise_for_status()
            break
        except requests.RequestException:
            if attempt == 4:
                raise
            _tls.s = requests.Session()
            time.sleep(0.5 * 2 ** attempt)
    total = int(r.headers.get("Content-Range", "/0").split("/")[-1] or 0) or len(r.content)
    return r.content, total


@lru_cache(maxsize=2048)
def mp3_info(url: str) -> dict | None:
    """Bitrate, ID3-offset en totale grootte uit de eerste bytes. None = VBR/onbekend."""
    head, total = _get(url, 0, 65535)
    off = 0
    if head[:3] == b"ID3":
        sz = (head[6] << 21) | (head[7] << 14) | (head[8] << 7) | head[9]
        off = 10 + sz + (10 if head[5] & 0x10 else 0)
        if off + 4096 > len(head):
            head = b"\0" * off + _get(url, off, off + 8191)[0]
    def hdr(j):
        if j + 4 > len(head) or head[j] != 0xFF or head[j + 1] & 0xE0 != 0xE0:
            return None
        b1, b2 = head[j + 1], head[j + 2]
        ver, layer = (b1 >> 3) & 3, (b1 >> 1) & 3
        if layer != 1 or ver == 1:          # alleen Layer III
            return None
        br = _BITRATES[1 if ver == 3 else 2][(b2 >> 4) & 15]
        sr = _RATES.get(ver, [0, 0, 0])[(b2 >> 2) & 3] if (b2 >> 2) & 3 < 3 else 0
        if not br or not sr:
            return None
        size = (144 if ver == 3 else 72) * br * 1000 // sr + ((b2 >> 1) & 1)
        return br, sr, size

    # zoek eerste plek waar 30 frames achter elkaar kloppen (geen valse sync)
    i = off
    while i < len(head) - 4:
        chain, j = [], i
        while len(chain) < 30 and (h := hdr(j)):
            chain.append(h)
            j += h[2]
        if len(chain) >= 30 or (chain and j >= len(head) - 4 and len(chain) >= 8):
            break
        i += 1
    else:
        return None
    rates = {h[0] for h in chain[1:]}         # frame 0 kan de Info/Xing-frame zijn
    if len(rates) != 1 or b"Xing" in head[i:i + 200]:
        return None                           # VBR
    br, sr = chain[1][0], chain[1][1]
    return dict(off=i, bps=br * 1000 / 8, total=total, sr=sr)


class Remote:
    """
    Gedraagt zich als een sample-array van de hele aflevering (mono @48k),
    maar haalt bij slicen alleen het benodigde byte-venster op.
    """
    def __init__(self, url: str):
        self.url = url
        self.info = mp3_info(url)
        if self.info is None:
            raise ValueError("VBR/onbekend formaat")
        self.n = int((self.info["total"] - self.info["off"]) / self.info["bps"] * SR)

    def __len__(self):
        return self.n

    def __getitem__(self, sl: slice) -> np.ndarray:
        t0, t1 = max(0, sl.start or 0) / SR, min(self.n, sl.stop or self.n) / SR
        return _window(self.url, round(t0, 3), round(t1, 3))


@lru_cache(maxsize=512)
def _window(url: str, t0: float, t1: float) -> np.ndarray:
    inf = mp3_info(url)
    margin = 0.5                            # decoder-inloop + framegrens
    s = max(0.0, t0 - margin)
    b0 = inf["off"] + int(s * inf["bps"])
    b1 = inf["off"] + int((t1 + margin) * inf["bps"])
    data, _ = _get(url, b0, b1)
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-f", "mp3", "-i", "pipe:0", "-ac", "1", "-ar", str(SR),
                          "-f", "f32le", "-"], input=data, capture_output=True).stdout
    x = np.frombuffer(pcm, dtype=np.float32)
    i0 = int((t0 - s) * SR)
    return x[i0:i0 + int((t1 - t0) * SR)].copy()


class _I16:
    """Int16-buffer die zich bij slicen als float32 gedraagt (halveert RAM)."""
    def __init__(self, a): self.a = a
    def __len__(self): return len(self.a)
    def __getitem__(self, s): return self.a[s].astype(np.float32) / 32768.0


@lru_cache(maxsize=64)
def decode(url: str):
    """Aflevering als sliceable sample-array. Range-requests; VBR → volledige download."""
    try:
        return Remote(url)
    except Exception:
        return _full(url)


@lru_cache(maxsize=2)
def _full(url: str) -> _I16:
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(source_path(url)), "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
        capture_output=True, check=True).stdout
    return _I16(np.frombuffer(raw, dtype="<i2"))


def _energy(x: np.ndarray, hop: int = 240, win: int = 480) -> np.ndarray:
    """RMS-envelope per 5ms-hop (in dB)."""
    if len(x) < win:
        return np.full(1, -120.0)
    n = 1 + (len(x) - win) // hop
    idx = np.arange(win)[None, :] + hop * np.arange(n)[:, None]
    rms = np.sqrt(np.mean(x[idx] ** 2, axis=1) + 1e-12)
    return 20 * np.log10(rms)


def refine(a: np.ndarray, start: float, end: float, pre: float = 0.10, post: float = 0.16,
           gap_before: float = 9.0, gap_after: float = 9.0) -> tuple[float, float]:
    """
    Schuif grenzen naar het stilste punt in de buurt.
    Start: zoek in [start-pre, start+0.05]; eind: in [end-0.04, end+post].
    Bias naar de ASR-grens zodat we niet een half woord eraf knippen.
    """
    hop = 240

    def best(t0: float, t1: float, anchor: float, after: bool) -> float:
        i0, i1 = max(0, int(t0 * SR)), min(len(a), int(t1 * SR))
        if i1 - i0 < 960:
            return anchor
        e = _energy(np.asarray(a[i0:i1]), hop=hop)
        times = t0 + (np.arange(len(e)) * hop + 240) / SR
        # Kosten = energie + straf voor afstand tot het anker (in dB per 10ms)
        dist = (times - anchor) * (1 if after else -1)
        penalty = np.where(dist < 0, -dist * 600, dist * 40)   # het woord in snijden kost veel
        k = int(np.argmin(e + penalty))
        return float(times[k])

    # Nooit verder zoeken dan tot halverwege het buurwoord.
    pre = min(pre, max(0.015, gap_before * 0.6))
    post = min(post, max(0.02, gap_after * 0.6))
    s = best(start - pre, start + 0.05, start, after=False)
    e = best(end - 0.04, end + post, end, after=True)
    return s, max(e, s + 0.05)


def cut(h: Hit, pad_before: float = 0.0, pad_after: float = 0.0, refine_edges: bool = True,
        fade_in: float = 0.004, fade_out: float = 0.015) -> np.ndarray:
    """Knip een Hit uit de bron, verfijnd en met micro-fades tegen klikken."""
    a = decode(h.audio_url)
    s, e = (refine(a, h.start, h.end, gap_before=h.gap_before, gap_after=h.gap_after)
            if refine_edges else (h.start, h.end))
    s, e = max(0.0, s - pad_before), min(len(a) / SR, e + pad_after)
    x = np.array(a[int(s * SR):int(e * SR)], dtype=np.float32)
    x -= x.mean() if len(x) else 0
    fi, fo = int(fade_in * SR), int(fade_out * SR)
    if len(x) > fi + fo:
        x[:fi] *= np.linspace(0, 1, fi) ** 2
        x[-fo:] *= np.linspace(1, 0, fo) ** 2
    return x


def loudness(x: np.ndarray) -> float:
    """Gating-RMS in dB (ruwe LUFS-benadering: negeer stille frames)."""
    e = _energy(x)
    e = e[e > e.max() - 30] if len(e) else e
    return float(10 * np.log10(np.mean(10 ** (e / 10)) + 1e-12)) if len(e) else -120.0


def level(x: np.ndarray, target_db: float = -20.0, peak: float = 0.95) -> np.ndarray:
    g = 10 ** ((target_db - loudness(x)) / 20)
    y = x * g
    m = np.abs(y).max() if len(y) else 0
    return y * (peak / m) if m > peak else y


def write_wav(path: Path, x: np.ndarray, sr: int = SR):
    import wave
    x = np.asarray(x, dtype=np.float32)
    ch = 1 if x.ndim == 1 else x.shape[1]
    pcm = (np.clip(x, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
