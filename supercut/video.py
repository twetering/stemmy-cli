"""
Video uit dezelfde partituur als de audio.

Elke stuk levert een `paint(t, img, draw)`-functie; deze module levert de
frame-lus, typografie-helpers en de ffmpeg-pijp (raw RGB → H.264 + AAC).
Elke visuele gebeurtenis is aan een audio-event gekoppeld, dus beeld en klank
lopen per definitie frame-exact synchroon.
"""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H, FPS = 1080, 1920, 30

FONTS = {
    "didot": ("/System/Library/Fonts/Supplemental/Didot.ttc", 0),
    "didot-italic": ("/System/Library/Fonts/Supplemental/Didot.ttc", 1),
    "didot-bold": ("/System/Library/Fonts/Supplemental/Didot.ttc", 2),
    "helv": ("/System/Library/Fonts/HelveticaNeue.ttc", 0),
    "helv-bold": ("/System/Library/Fonts/HelveticaNeue.ttc", 1),
    "helv-light": ("/System/Library/Fonts/HelveticaNeue.ttc", 7),
    "helv-thin": ("/System/Library/Fonts/HelveticaNeue.ttc", 12),
    "helv-medium": ("/System/Library/Fonts/HelveticaNeue.ttc", 10),
    "helv-black": ("/System/Library/Fonts/HelveticaNeue.ttc", 9),
    "helv-cond": ("/System/Library/Fonts/HelveticaNeue.ttc", 4),
    "mono": ("/System/Library/Fonts/SFNSMono.ttf", 0),
    "comic": ("/System/Library/Fonts/Supplemental/Comic Sans MS.ttf", 0),
    "comic-bold": ("/System/Library/Fonts/Supplemental/Comic Sans MS Bold.ttf", 0),
    "tahoma": ("/System/Library/Fonts/Supplemental/Tahoma.ttf", 0),
    "tahoma-bold": ("/System/Library/Fonts/Supplemental/Tahoma Bold.ttf", 0),
}


@lru_cache(maxsize=256)
def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    path, idx = FONTS[name]
    return ImageFont.truetype(path, size, index=idx)


def ease(x: float) -> float:
    """Ease-out cubic, geclamped."""
    x = min(max(x, 0.0), 1.0)
    return 1 - (1 - x) ** 3


def ease_io(x: float) -> float:
    x = min(max(x, 0.0), 1.0)
    return 3 * x * x - 2 * x * x * x


def text_center(draw: ImageDraw.ImageDraw, xy, s: str, f, fill, anchor="mm", spacing: int = 0):
    if spacing:
        # handmatige letterspatiëring (voor mono-captions in kapitalen)
        widths = [draw.textlength(ch, font=f) for ch in s]
        total = sum(widths) + spacing * (len(s) - 1)
        x = xy[0] - total / 2 if anchor[0] == "m" else xy[0]
        for ch, w in zip(s, widths):
            draw.text((x, xy[1]), ch, font=f, fill=fill, anchor="l" + anchor[1])
            x += w + spacing
        return
    draw.text(xy, s, font=f, fill=fill, anchor=anchor)


def fit_size(draw, s: str, name: str, max_w: int, start: int, min_size: int = 20) -> int:
    size = start
    while size > min_size and draw.textlength(s, font=font(name, size)) > max_w:
        size -= 4
    return size


def waveform_strip(x: np.ndarray, n: int = 120) -> np.ndarray:
    """Piek-envelope in n bins, genormaliseerd 0..1."""
    if len(x) < n:
        return np.zeros(n)
    b = np.abs(x[: len(x) // n * n]).reshape(n, -1).max(1)
    return b / (b.max() + 1e-9)


def grain(seed: int, amount: int = 10) -> Image.Image:
    rng = np.random.default_rng(seed)
    g = rng.integers(0, amount, (H // 2, W // 2), dtype=np.uint8)
    return Image.fromarray(g, "L").resize((W, H), Image.NEAREST)


def render(paint: Callable[[float, Image.Image, ImageDraw.ImageDraw], None], duration: float,
           audio: Path, out: Path, bg=(10, 10, 11), film_grain: bool = True, fps: int = FPS,
           size: tuple[int, int] = (W, H)):
    """Render frames via `paint` en mux met audio."""
    w, h = size
    n = int(duration * fps)
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
           "-r", str(fps), "-i", "-", "-i", str(audio), "-map", "0:v", "-map", "1:a",
           "-c:v", "libx264", "-preset", "slow", "-crf", "16", "-pix_fmt", "yuv420p",
           "-tune", "film", "-c:a", "aac", "-b:a", "256k", "-shortest", "-movflags", "+faststart", str(out)]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    grains = [grain(s) for s in range(6)] if film_grain else None
    for i in range(n):
        t = i / fps
        img = Image.new("RGB", (w, h), bg)
        draw = ImageDraw.Draw(img)
        paint(t, img, draw)
        if grains:
            img = Image.composite(Image.new("RGB", (w, h), (255, 255, 255)), img,
                                  grains[i % 6].point(lambda v: v * 2))
        p.stdin.write(img.tobytes())
    p.stdin.close()
    p.wait()
    return out


def still(paint, t: float, out: Path, size=(W, H), bg=(10, 10, 11)):
    """Eén frame als PNG — om het ontwerp te beoordelen zonder te renderen."""
    img = Image.new("RGB", size, bg)
    paint(t, img, ImageDraw.Draw(img))
    img.save(out)
    return out
