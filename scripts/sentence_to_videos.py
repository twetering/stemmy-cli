#!/usr/bin/env python3
"""
Sentence → per-word video clips.

Zin: "Robot op de vloer, hij telt de seconden van elk voorbijgaand uur."
Per woord: audio extracten uit stemmy-db + Pixabay beeld → .mp4

Gebruik:
    python scripts/sentence_to_videos.py

Configuratie via .env:
    PIXABAY_API_KEY   – al aanwezig in .env
    VIDEO_SIZE        – bijv. "1280x720" (default)
"""

import json
import os
import subprocess
import sys
from pathlib import Path

# ── project root op sys.path zetten ──────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

from stemmy_cli.adapters.sqlite import SQLiteAdapter
from stemmy_cli.audio.local_extract import extract_segment_local

# ── configuratie ──────────────────────────────────────────────────────────────
SENTENCE = "Robot op de vloer, hij telt de seconden van elk voorbijgaand uur."
OUTPUT_DIR = PROJECT_ROOT / "output" / "sentence_words"
VIDEO_SIZE = os.getenv("VIDEO_SIZE", "1280x720")
WORD_BUFFER_MS = 80
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY", "")

# Engelse Pixabay-zoekterm per woord (betere resultaten dan Nederlandstalig)
WORD_TO_QUERY: dict[str, str] = {
    "robot":        "robot technology",
    "op":           "rising movement",
    "de":           "letters alphabet",
    "vloer":        "wooden floor tiles",
    "hij":          "man silhouette",
    "telt":         "counting numbers",
    "seconden":     "clock seconds timer",
    "van":          "journey travel",
    "elk":          "every each collection",
    "voorbijgaand": "passing time ephemeral",
    "uur":          "hour clock watch",
}

# ── helpers ───────────────────────────────────────────────────────────────────

def clean_word(word: str) -> str:
    return word.strip(".,!?:;\"'").lower()


def find_db_match(adapter: SQLiteAdapter, word: str) -> dict | None:
    """Eerste match van het woord in fragment word-arrays."""
    frags = adapter.execute_raw(
        "SELECT id, words, item_audio_url FROM fragments "
        "WHERE words IS NOT NULL AND words != '' AND words != '[]' "
        "ORDER BY id",
        [],
    )
    word_lower = word.lower()
    for frag in frags:
        if not frag.get("item_audio_url"):
            continue
        try:
            word_list = json.loads(frag["words"])
        except (json.JSONDecodeError, TypeError):
            continue
        for w in word_list:
            text = w.get("text", "").strip(".,!?:;\"'").lower()
            if text == word_lower:
                start_ms = w.get("start", 0)
                end_ms = w.get("end", 0)
                if end_ms <= start_ms:
                    continue
                return {
                    "audio_url": frag["item_audio_url"],
                    "start_time": max(0.0, (start_ms - WORD_BUFFER_MS) / 1000.0),
                    "end_time": (end_ms + WORD_BUFFER_MS) / 1000.0,
                }
    return None


def fetch_pixabay_image(word: str, output_path: Path) -> bool:
    """Download een passend beeld van Pixabay."""
    if not PIXABAY_API_KEY:
        print(f"  [IMG] Geen PIXABAY_API_KEY")
        return False
    import httpx

    query = WORD_TO_QUERY.get(word.lower(), word)
    try:
        resp = httpx.get(
            "https://pixabay.com/api/",
            params={
                "key": PIXABAY_API_KEY,
                "q": query,
                "image_type": "photo",
                "orientation": "horizontal",
                "per_page": 5,
                "safesearch": "true",
                "lang": "en",
            },
            timeout=10,
            follow_redirects=True,
        )
        resp.raise_for_status()
        hits = resp.json().get("hits", [])
        if not hits:
            print(f"  [IMG] Geen Pixabay resultaten voor '{query}'")
            return False
        img_url = hits[0].get("webformatURL") or hits[0].get("largeImageURL")
        if not img_url:
            return False
        img_resp = httpx.get(img_url, timeout=15, follow_redirects=True)
        img_resp.raise_for_status()
        output_path.write_bytes(img_resp.content)
        print(f"  [IMG] Beeld opgeslagen → {output_path.name}")
        return True
    except Exception as e:
        print(f"  [IMG] Fout bij Pixabay voor '{word}': {e}")
        return False


def make_fallback_image(word: str, output_path: Path) -> Path:
    """Donkerblauw PNG met woordtekst als fallback."""
    w, h = VIDEO_SIZE.split("x")
    escaped = word.replace("'", "\\'").replace(":", "\\:")
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c=0x1a1a2e:size={w}x{h}:rate=1",
        "-vframes", "1",
        "-vf", (
            f"drawtext=text='{escaped}'"
            ":fontsize=140:fontcolor=white"
            ":x=(w-text_w)/2:y=(h-text_h)/2"
            ":shadowcolor=black:shadowx=4:shadowy=4"
        ),
        str(output_path),
    ]
    subprocess.run(cmd, capture_output=True, check=False)
    return output_path


def make_video(image_path: Path, audio_path: Path, word: str, output_path: Path) -> bool:
    """Combineer beeld + audio naar .mp4 met woord-overlay onderaan."""
    w, h = VIDEO_SIZE.split("x")
    escaped = word.replace("'", "\\'").replace(":", "\\:")
    # Scale naar VIDEO_SIZE (handelt oneven dimensies af) + tekstbalk
    text_filter = (
        f"[0:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,"
        f"setsar=1,"
        f"drawtext=text='{escaped}'"
        ":fontsize=72"
        ":fontcolor=white"
        ":x=(w-text_w)/2"
        ":y=h-text_h-40"
        ":box=1"
        ":boxcolor=black@0.6"
        ":boxborderw=20"
        "[vout]"
    )
    cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-i", str(image_path),
        "-i", str(audio_path),
        "-filter_complex", text_filter,
        "-map", "[vout]", "-map", "1:a",
        "-c:v", "libx264", "-tune", "stillimage",
        "-c:a", "aac", "-b:a", "192k",
        "-pix_fmt", "yuv420p",
        "-shortest",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  [VIDEO] ffmpeg fout: {result.stderr[-300:]}")
        return False
    return True


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"\n{'='*60}")
    print(f"  Sentence to Videos")
    print(f"  Zin: {SENTENCE}")
    print(f"  Output: {OUTPUT_DIR}")
    print(f"{'='*60}\n")

    raw_words = SENTENCE.split()
    words_with_index: list[tuple[str, str, int]] = []
    word_counter: dict[str, int] = {}
    for rw in raw_words:
        clean = clean_word(rw)
        word_counter[clean] = word_counter.get(clean, 0) + 1
        words_with_index.append((rw, clean, word_counter[clean]))

    adapter = SQLiteAdapter()
    results = []

    for i, (original, word, occurrence) in enumerate(words_with_index, start=1):
        suffix = f"_{occurrence}" if occurrence > 1 else ""
        base_name = f"{i:02d}_{word}{suffix}"
        audio_path = OUTPUT_DIR / f"{base_name}.mp3"
        image_path = OUTPUT_DIR / f"{base_name}.jpg"
        video_path = OUTPUT_DIR / f"{base_name}.mp4"

        print(f"[{i}/{len(words_with_index)}] '{original}' → '{word}'")

        if video_path.exists():
            print(f"  Bestaat al → {video_path.name}")
            results.append({"word": original, "video": str(video_path), "status": "skipped"})
            continue

        # ── audio ──────────────────────────────────────────────────────
        audio_ok = False
        if not audio_path.exists():
            db_match = find_db_match(adapter, word)
            if db_match:
                print(f"  [DB] Match gevonden, extracting...")
                result = extract_segment_local(
                    audio_url=db_match["audio_url"],
                    start_time=db_match["start_time"],
                    end_time=db_match["end_time"],
                    output_path=audio_path,
                )
                audio_ok = result.get("success", False)
                if audio_ok:
                    print(f"  [DB] Audio ({result.get('duration', 0):.2f}s) → {audio_path.name}")
                else:
                    print(f"  [DB] Extractie mislukt: {result.get('error')}")
            else:
                print(f"  [DB] '{word}' niet gevonden — overgeslagen")
        else:
            audio_ok = True
            print(f"  [AUDIO] Bestaat al → {audio_path.name}")

        if not audio_ok:
            results.append({"word": original, "video": None, "status": "no_audio"})
            continue

        # ── beeld ──────────────────────────────────────────────────────
        if not image_path.exists():
            img_ok = fetch_pixabay_image(word, image_path)
            if not img_ok:
                fallback = image_path.with_suffix(".png")
                make_fallback_image(word, fallback)
                image_path = fallback

        # ── video ──────────────────────────────────────────────────────
        ok = make_video(image_path, audio_path, original.strip(".,!?"), video_path)
        if ok:
            print(f"  [VIDEO] → {video_path.name}")
            results.append({"word": original, "video": str(video_path), "status": "ok"})
        else:
            results.append({"word": original, "video": None, "status": "video_error"})

    # ── samenvatting ───────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("  Resultaten")
    print(f"{'='*60}")
    ok_count = sum(1 for r in results if r["status"] in ("ok", "skipped"))
    for r in results:
        icon = "✓" if r["status"] in ("ok", "skipped") else "✗"
        print(f"  {icon} {r['word']:15s} → {r.get('video') or r['status']}")
    print(f"\n  {ok_count}/{len(results)} videos succesvol")
    print(f"  Output: {OUTPUT_DIR}\n")


if __name__ == "__main__":
    main()
