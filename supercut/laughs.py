"""
Lachjes vinden: inbetweens (echo-methode: gaps 600–3000ms) → aan elkaar plakken
→ één ElevenLabs STT-call met audio-event tagging → '[lacht]' terugmappen.
"""
from __future__ import annotations

import json
import os
import random
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import requests

from stemmy_cli.audio.parallel_extractor import ParallelExtractor

from .audio import SR, loudness, write_wav
from .corpus import ROOT


def load_mp3(path) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                         capture_output=True).stdout
    return np.frombuffer(raw, np.float32).copy()


def extract(segs: list[dict], retries: int = 2) -> dict[str, np.ndarray]:
    """ParallelExtractor met een herkansing voor S3-verbindingsfouten."""
    out, todo = {}, segs
    for _ in range(retries + 1):
        ok, bad = ParallelExtractor(max_workers=12, output_dir=Path(tempfile.mkdtemp())).extract_segments(todo)
        for r in ok:
            x = load_mp3(r.output_path)
            if len(x) > SR * 0.12:
                out[r.segment_id] = x
        failed = {r.segment_id for r in bad}
        todo = [s for s in todo if s["id"] in failed]
        if not todo:
            break
    return out


def stt(path: Path, events: bool = True) -> dict:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=True)
    r = requests.post("https://api.elevenlabs.io/v1/speech-to-text",
                      headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]},
                      data={"model_id": "scribe_v1", "language_code": "nld", "tag_audio_events": str(events).lower()},
                      files={"file": open(path, "rb")}, timeout=300)
    r.raise_for_status()
    return r.json()


def find_laughs(gaps: list[dict], n: int = 220, seed: int = 5) -> list[dict]:
    """→ gaps met 'laugh': True/False en de bijbehorende clip."""
    rng = random.Random(seed)
    cand = rng.sample(gaps, min(n, len(gaps)))
    segs = [dict(id=f"g{k}", audio_url=g["url"], start_time=g["start"], end_time=g["end"]) for k, g in enumerate(cand)]
    clips = extract(segs)
    keep = [(k, clips[f"g{k}"]) for k in range(len(cand)) if f"g{k}" in clips and loudness(clips[f"g{k}"]) > -42]
    sil = np.zeros(int(0.6 * SR), np.float32)
    parts, offs, t = [], [], 0.0
    for k, x in keep:
        x = x / (np.abs(x).max() + 1e-9) * 0.5
        offs.append((k, t, t + len(x) / SR))
        parts += [x, sil]
        t += len(x) / SR + 0.6
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "gaps.wav"
        write_wav(p, np.concatenate(parts))
        res = stt(p)
    events = [(w["start"], w["end"], w["text"]) for w in res.get("words", []) if w.get("type") == "audio_event"]
    speech = [(w["start"], w["end"]) for w in res.get("words", []) if w.get("type") == "word"]
    out = []
    for k, a, b in offs:
        ev = [e for e in events if e[0] < b and e[1] > a]
        sp = sum(1 for s in speech if s[0] < b and s[1] > a)
        g = dict(cand[k], x=clips[f"g{k}"], events=[e[2] for e in ev], n_words=sp)
        g["laugh"] = any("lach" in e[2].lower() or "laugh" in e[2].lower() for e in ev)
        out.append(g)
    return out
