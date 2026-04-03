#!/usr/bin/env python3
"""
Stemmy Dictionary — Van Dale-achtig woordenboek van podcast-audio.

Voor elke letter: alle unieke woorden op alfabet, elk afspeelbaar als:
  1. Los woord  (exacte uitspraak, incl. meerdere als anders uitgesproken)
  2. In context (de zin waarin het woord gezegd werd)

Gebruik:
  python scripts/dictionary.py --letter P --db data/stemmy-replica.db
  python scripts/dictionary.py --letter P --limit 60 --clips 3 --out output/dictionary/
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

# Zorg dat de src/ map beschikbaar is
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))


# ── Zin-splitsing ──────────────────────────────────────────────────────────────

def split_sentences(words: list[dict], max_gap_ms: float = 800) -> list[dict]:
    """
    Groepeer aaneengesloten woorden tot zinnen op basis van:
      - Leestekens aan het einde van een woord (.!?)
      - Pauze > max_gap_ms ms tussen woorden
    Geeft terug: [{"text": "...", "start": ms, "end": ms, "words": [...]}]
    """
    if not words:
        return []
    sentences = []
    cur_words = [words[0]]
    for prev, w in zip(words, words[1:]):
        gap = w["start"] - prev["end"]
        ends_sent = re.search(r"[.!?]$", prev["text"].strip())
        if ends_sent or gap > max_gap_ms:
            sentences.append(cur_words)
            cur_words = [w]
        else:
            cur_words.append(w)
    sentences.append(cur_words)

    result = []
    for grp in sentences:
        text = " ".join(w["text"] for w in grp).strip()
        result.append({
            "text": text,
            "start": grp[0]["start"],
            "end":   grp[-1]["end"],
            "words": grp,
        })
    return result


# ── DB-query ───────────────────────────────────────────────────────────────────

def collect_word_occurrences(db_path: str, format_id: str, letter: str) -> dict:
    """
    Verzamel alle voorkomens van woorden die beginnen met `letter` in de
    fragmenten van het opgegeven format.

    Geeft terug: {
        "podcast": [
            {
                "word_clean":  "podcast",
                "word_text":   "podcast",      # originele case
                "word_start":  1234,            # ms
                "word_end":    1567,            # ms
                "sent_text":   "Het is een podcast over...",
                "sent_start":  1100,            # ms
                "sent_end":    3200,            # ms
                "audio_url":   "https://...",
                "fragment_id": "abc",
                "item_title":  "Aflevering 12",
            },
            ...
        ],
        ...
    }
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    letter_lc = letter.lower()

    sql = """
        SELECT
            f.id         AS frag_id,
            f.words      AS words_json,
            f.item_audio_url,
            f.start_time AS frag_start_ms,
            f.end_time   AS frag_end_ms,
            i.title      AS item_title
        FROM fragments f
        JOIN items i ON f.item_id = i.id
        WHERE i.format_id = ?
          AND f.words IS NOT NULL
          AND f.words != '[]'
    """
    rows = conn.execute(sql, (format_id,)).fetchall()
    conn.close()

    occurrences: dict[str, list] = defaultdict(list)

    for row in rows:
        try:
            words = json.loads(row["words_json"])
        except (json.JSONDecodeError, TypeError):
            continue

        audio_url = row["item_audio_url"]
        if not audio_url:
            continue

        sentences = split_sentences(words)

        for sent in sentences:
            for idx, w in enumerate(sent["words"]):
                raw = w["text"]
                clean = raw.strip(".,!?:;\"'()-…").lower()
                if not clean or clean[0] != letter_lc:
                    continue

                # sent_end: gebruik het maximum van de sentence-end en de
                # fragment-end (minus kleine marge). Zo worden zinnen die tot
                # aan het einde van een fragment lopen niet voortijdig afgekapt.
                try:
                    frag_end = int(row["frag_end_ms"]) if row["frag_end_ms"] else sent["end"]
                except (TypeError, ValueError):
                    frag_end = sent["end"]
                sent_end = max(sent["end"], frag_end - 50)

                occurrences[clean].append({
                    "word_clean":  clean,
                    "word_text":   raw,
                    "word_start":  w["start"],          # ms
                    "word_end":    w["end"],             # ms
                    "sent_text":   sent["text"],
                    "sent_start":  sent["start"],        # ms
                    "sent_end":    sent_end,             # ms – gecorrigeerd
                    "audio_url":   audio_url,
                    "fragment_id": row["frag_id"],
                    "item_title":  row["item_title"] or "",
                })

    return occurrences


# ── Audio-extractie ────────────────────────────────────────────────────────────

def extract_clips(occurrences: dict, top_n: int, clips_per_word: int,
                  out_dir: Path, letter: str,
                  comp_max: int = 25) -> tuple:
    """
    Extraheer word-clips, context-clips én compilatie-clips via ParallelExtractor.

    Geeft terug: (
        clips:      {word: [{"word_path": Path, "ctx_path": Path, "ctx_text": str}, ...]},
        comp_clips: {word: [Path, ...]}   ← alle word-clips voor compilatie
    )
    """
    from stemmy_cli.audio.parallel_extractor import ParallelExtractor

    audio_dir = out_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    # Sorteer op frequentie, pak top_n
    sorted_words = sorted(occurrences.items(), key=lambda x: -len(x[1]))[:top_n]

    # Binnen elk woord:
    #   selected  = clips_per_word occurrences → losse uitspraken + context
    #   comp_occs = comp_max occurrences       → compilatie (alleen woord)
    selected  = {}
    comp_occs = {}
    for word, occs in sorted_words:
        selected[word]  = occs[:clips_per_word]
        comp_occs[word] = occs[:comp_max]

    # Bouw segmenten-lijst voor extractor
    segments: list[dict] = []
    seg_meta: list[dict] = []  # parallel aan segments

    WORD_PAD_MS = 80    # kleine buffer rondom woord
    CTX_PAD_MS  = 600   # ruimere buffer zodat laatste woord volledig uitspeelt
    COMP_PAD_MS = 150   # buffer voor compilatie-woord

    for word, occs in selected.items():
        word_safe = re.sub(r"[^a-z0-9]", "_", word)
        for i, occ in enumerate(occs):
            # Losse woord-clip (voor Uitspraken-sectie)
            w_start = max(0.0, (occ["word_start"] - WORD_PAD_MS) / 1000)
            w_end   = (occ["word_end"] + WORD_PAD_MS) / 1000
            segments.append({"id": f"w_{word_safe}_{i}", "audio_url": occ["audio_url"],
                              "start_time": w_start, "end_time": w_end})
            seg_meta.append({"type": "word", "word": word, "idx": i,
                             "ctx_text": occ["sent_text"],
                             "out_path": audio_dir / f"{word_safe}_{i}_word.mp3"})

            # Context-clip
            c_start = max(0.0, (occ["sent_start"] - CTX_PAD_MS) / 1000)
            c_end   = (occ["sent_end"] + CTX_PAD_MS) / 1000
            segments.append({"id": f"c_{word_safe}_{i}", "audio_url": occ["audio_url"],
                              "start_time": c_start, "end_time": c_end})
            seg_meta.append({"type": "ctx", "word": word, "idx": i,
                             "out_path": audio_dir / f"{word_safe}_{i}_ctx.mp3"})

    for word, occs in comp_occs.items():
        word_safe = re.sub(r"[^a-z0-9]", "_", word)
        for i, occ in enumerate(occs):
            # Compilatie-woord-clip (alle voorkomens, iets ruimere padding)
            w_start = max(0.0, (occ["word_start"] - COMP_PAD_MS) / 1000)
            w_end   = (occ["word_end"] + COMP_PAD_MS) / 1000
            segments.append({"id": f"k_{word_safe}_{i}", "audio_url": occ["audio_url"],
                              "start_time": w_start, "end_time": w_end})
            seg_meta.append({"type": "comp", "word": word, "idx": i,
                             "out_path": audio_dir / f"{word_safe}_{i}_comp.mp3"})

    if not segments:
        print("Geen segmenten om te extraheren.")
        return {}, {}

    n_comp = sum(len(v) for v in comp_occs.values())
    n_indiv = sum(len(v) for v in selected.values())
    print(f"  → {len(segments)} clips extraheren "
          f"({n_indiv} uitspraken+context + {n_comp} compilatie-clips) …")

    # Extraheer
    import shutil
    with tempfile.TemporaryDirectory(prefix="dict_") as tmp:
        tmp_dir = Path(tmp)
        extractor = ParallelExtractor(max_workers=20, output_dir=tmp_dir)

        def prog(cur, tot, _msg):
            pct = int(cur / max(tot, 1) * 50)
            bar = "█" * pct + "░" * (50 - pct)
            print(f"\r  [{bar}] {cur}/{tot}", end="", flush=True)

        successes, failures = extractor.extract_segments(segments, progress_callback=prog)
        print()

        if failures:
            print(f"  ⚠ {len(failures)} clips mislukt (worden overgeslagen)")

        success_by_id = {r.segment_id: Path(r.output_path) for r in successes}
        for seg, meta in zip(segments, seg_meta):
            src = success_by_id.get(seg["id"])
            if src and src.exists():
                shutil.copy2(src, meta["out_path"])

    # Bouw clips-resultaat: {word: [{word_path, ctx_path, ctx_text}, ...]}
    clips: dict[str, list] = defaultdict(list)
    word_clips: dict[tuple, dict] = {}
    for seg, meta in zip(segments, seg_meta):
        if meta["type"] not in ("word", "ctx"):
            continue
        key = (meta["word"], meta["idx"])
        if key not in word_clips:
            word_clips[key] = {"ctx_text": meta.get("ctx_text", ""),
                               "word_path": None, "ctx_path": None}
        if meta["type"] == "word":
            word_clips[key]["word_path"] = meta["out_path"]
            word_clips[key]["ctx_text"]  = meta.get("ctx_text", "")
        else:
            word_clips[key]["ctx_path"] = meta["out_path"]

    for (word, idx), clip in sorted(word_clips.items(), key=lambda x: (x[0][0], x[0][1])):
        clips[word].append(clip)

    # Bouw comp_clips-resultaat: {word: [Path, ...]}
    comp_clips: dict[str, list] = defaultdict(list)
    for seg, meta in zip(segments, seg_meta):
        if meta["type"] == "comp" and meta["out_path"].exists():
            comp_clips[meta["word"]].append(meta["out_path"])

    return dict(clips), dict(comp_clips)


# ── Compilatie bouwen ──────────────────────────────────────────────────────────

def build_compilaties(comp_clips: dict, out_dir: Path,
                      music_path: Path, music_vol: float = 0.18) -> dict:
    """
    Bouw per woord een compilatie-MP3: alle woord-clips achter elkaar + muziek eronder.
    Geeft terug: {word: Path}
    """
    import subprocess

    audio_dir = out_dir / "audio"
    comp_paths: dict[str, Path] = {}

    total = len(comp_clips)
    print(f"  → {total} compilaties bouwen …")

    for i, (word, paths) in enumerate(sorted(comp_clips.items()), 1):
        # Valideer elke clip: moet bestaan, voldoende groot én meetbare duur hebben.
        # Stille/corrupte clips (verkeerde byte-range bij niet-128kbps audio)
        # worden zo uitgesloten zodat de compilatie nooit alleen muziek bevat.
        def _clip_ok(p: Path) -> bool:
            if not p.exists() or p.stat().st_size < 800:
                return False
            r = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(p)],
                capture_output=True, text=True
            )
            try:
                return float(r.stdout.strip()) > 0.08  # minimaal 80ms
            except (ValueError, TypeError):
                return False

        valid = [p for p in paths if _clip_ok(p)]
        if not valid:
            continue

        word_safe   = re.sub(r"[^a-z0-9]", "_", word)
        concat_path = audio_dir / f"{word_safe}_concat.mp3"
        final_path  = audio_dir / f"{word_safe}_compilatie.mp3"

        # Stap 1 — Concat alle woord-clips
        files_txt = audio_dir / f"{word_safe}_files.txt"
        files_txt.write_text(
            "\n".join(f"file '{p.resolve()}'" for p in valid),
            encoding="utf-8"
        )
        r1 = subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
             "-i", str(files_txt),
             "-af", "aresample=async=1:first_pts=0",
             "-c:a", "libmp3lame", "-q:a", "2", str(concat_path)],
            capture_output=True
        )
        files_txt.unlink(missing_ok=True)

        if r1.returncode != 0 or not concat_path.exists():
            continue

        # Stap 2 — Mix met achtergrondmuziek
        r2 = subprocess.run(
            ["ffmpeg", "-y",
             "-i", str(concat_path),
             "-stream_loop", "-1", "-i", str(music_path),
             "-filter_complex",
             f"[1:a]volume={music_vol}[music];[0:a][music]amix=inputs=2:duration=first[out]",
             "-map", "[out]",
             "-c:a", "libmp3lame", "-q:a", "2", str(final_path)],
            capture_output=True
        )
        concat_path.unlink(missing_ok=True)  # opruimen

        if r2.returncode == 0 and final_path.exists():
            comp_paths[word] = final_path

        print(f"\r  [{i}/{total}] {word:<20}", end="", flush=True)

    print()
    return comp_paths


# ── HTML-generatie ─────────────────────────────────────────────────────────────

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="nl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Stemmy Woordenboek — {letter} | POM Podcast</title>
<style>
:root {{
  --bg:      #faf9f7;
  --card:    #ffffff;
  --border:  #e8e4dc;
  --text:    #1a1a1a;
  --muted:   #666;
  --accent:  #2563eb;
  --accent2: #1d4ed8;
  --play-bg: #eff6ff;
  --ctx-bg:  #f8faff;
  --word-color: #1e3a5f;
  --shadow:  0 1px 3px rgba(0,0,0,.08), 0 4px 12px rgba(0,0,0,.04);
  --radius:  10px;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  font-family: 'Georgia', 'Times New Roman', serif;
  background: var(--bg);
  color: var(--text);
  line-height: 1.6;
}}
header {{
  background: var(--word-color);
  color: #fff;
  padding: 28px 40px 22px;
  display: flex;
  align-items: baseline;
  gap: 16px;
}}
header h1 {{ font-size: 1.5rem; font-weight: 400; letter-spacing: .04em; }}
header .sub {{ font-size: .9rem; opacity: .6; font-family: sans-serif; }}
.letter-nav {{
  position: sticky;
  top: 0;
  background: var(--word-color);
  padding: 10px 40px 12px;
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  z-index: 100;
  border-top: 1px solid rgba(255,255,255,.1);
}}
.letter-nav a {{
  color: rgba(255,255,255,.7);
  text-decoration: none;
  font-family: sans-serif;
  font-size: .82rem;
  padding: 2px 6px;
  border-radius: 4px;
  transition: background .15s, color .15s;
}}
.letter-nav a:hover,
.letter-nav a.active {{ background: rgba(255,255,255,.2); color: #fff; }}
.main {{ max-width: 860px; margin: 0 auto; padding: 40px 24px; }}
.section-title {{
  font-family: sans-serif;
  font-size: .78rem;
  font-weight: 600;
  letter-spacing: .12em;
  color: var(--muted);
  text-transform: uppercase;
  margin-bottom: 20px;
  padding-bottom: 8px;
  border-bottom: 2px solid var(--border);
}}
.word-entry {{
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  margin-bottom: 16px;
  box-shadow: var(--shadow);
  overflow: hidden;
}}
.word-header {{
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 18px 22px 14px;
  border-bottom: 1px solid var(--border);
  background: #fdfdfc;
}}
.word-text {{
  font-size: 1.7rem;
  font-weight: 700;
  color: var(--word-color);
  letter-spacing: -.01em;
}}
.word-freq {{
  font-family: sans-serif;
  font-size: .78rem;
  color: var(--muted);
  background: #f1f0ee;
  padding: 2px 9px;
  border-radius: 99px;
  margin-left: auto;
}}
.comp-player {{
  padding: 14px 22px;
  background: linear-gradient(135deg, #1e3a5f 0%, #2563eb 100%);
  display: flex;
  align-items: center;
  gap: 14px;
  border-bottom: 1px solid var(--border);
}}
.comp-btn {{
  display: inline-flex;
  align-items: center;
  gap: 8px;
  background: rgba(255,255,255,.15);
  border: 1.5px solid rgba(255,255,255,.35);
  color: #fff;
  border-radius: 999px;
  padding: 8px 20px;
  font-family: sans-serif;
  font-size: .88rem;
  font-weight: 600;
  cursor: pointer;
  transition: background .15s;
  white-space: nowrap;
}}
.comp-btn:hover {{ background: rgba(255,255,255,.28); }}
.comp-btn.playing {{ background: rgba(255,255,255,.35); border-color: #fff; }}
.comp-btn svg {{ width: 16px; height: 16px; fill: currentColor; flex-shrink: 0; }}
.comp-label {{
  font-family: sans-serif;
  font-size: .78rem;
  color: rgba(255,255,255,.65);
}}
.word-body {{ padding: 16px 22px 18px; }}
.clip-row {{
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}}
.clip-row:last-child {{ margin-bottom: 0; }}
.play-btn {{
  display: inline-flex;
  align-items: center;
  gap: 6px;
  background: var(--play-bg);
  border: 1px solid #bfdbfe;
  color: var(--accent);
  border-radius: 999px;
  padding: 5px 14px 5px 10px;
  font-family: sans-serif;
  font-size: .82rem;
  cursor: pointer;
  transition: background .12s, border-color .12s;
  flex-shrink: 0;
  font-weight: 500;
}}
.play-btn:hover {{ background: #dbeafe; border-color: #93c5fd; }}
.play-btn.playing {{ background: #dbeafe; border-color: var(--accent); color: var(--accent2); }}
.play-btn svg {{ width: 14px; height: 14px; fill: currentColor; }}
.ctx-section {{
  margin-top: 14px;
  padding-top: 14px;
  border-top: 1px dashed var(--border);
}}
.ctx-label {{
  font-family: sans-serif;
  font-size: .7rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: .1em;
  color: var(--muted);
  margin-bottom: 10px;
}}
.ctx-row {{
  display: flex;
  align-items: flex-start;
  gap: 10px;
  margin-bottom: 10px;
  background: var(--ctx-bg);
  border: 1px solid #e8eef8;
  border-radius: 7px;
  padding: 10px 14px;
}}
.ctx-row:last-child {{ margin-bottom: 0; }}
.ctx-text {{
  font-size: .92rem;
  color: #333;
  flex: 1;
  font-style: italic;
  line-height: 1.5;
}}
.word-highlight {{
  background: #fef9c3;
  border-radius: 3px;
  padding: 0 2px;
  font-style: normal;
  font-weight: 600;
  color: #713f12;
}}
footer {{
  text-align: center;
  padding: 40px;
  font-family: sans-serif;
  font-size: .8rem;
  color: var(--muted);
  border-top: 1px solid var(--border);
  margin-top: 40px;
}}
</style>
</head>
<body>
<header>
  <h1>📖 Stemmy Woordenboek</h1>
  <span class="sub">POM — Een podcast over media, cultuur, technologie en ondernemen</span>
</header>
<nav class="letter-nav">
  {nav_links}
</nav>
<main class="main">
  <div class="section-title">Letter {letter} — {word_count} unieke woorden</div>
  {entries}
</main>
<footer>
  Gegenereerd met Stemmy · Audio © POM Podcast · Demo prototype
</footer>
<script>
let currentAudio = null;
let currentBtn   = null;

function togglePlay(btn, path) {{
  if (currentAudio && !currentAudio.paused && currentBtn === btn) {{
    currentAudio.pause();
    currentAudio.currentTime = 0;
    btn.classList.remove('playing');
    btn.querySelector('.label').textContent = btn.dataset.labelPlay;
    currentAudio = null; currentBtn = null;
    return;
  }}
  if (currentAudio) {{
    currentAudio.pause();
    currentAudio.currentTime = 0;
    if (currentBtn) {{
      currentBtn.classList.remove('playing');
      currentBtn.querySelector('.label').textContent = currentBtn.dataset.labelPlay;
    }}
  }}
  currentAudio = new Audio(path);
  currentBtn   = btn;
  btn.classList.add('playing');
  btn.querySelector('.label').textContent = btn.dataset.labelStop;
  currentAudio.play();
  currentAudio.onended = () => {{
    btn.classList.remove('playing');
    btn.querySelector('.label').textContent = btn.dataset.labelPlay;
    currentAudio = null; currentBtn = null;
  }};
}}
</script>
</body>
</html>
"""

PLAY_ICON = '<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>'
STOP_ICON = "⏹"


def highlight_word(text: str, word: str) -> str:
    """Markeer het woord in de zin-tekst (case-insensitive)."""
    pattern = re.compile(r'(\b' + re.escape(word) + r'\b)', re.IGNORECASE)
    return pattern.sub(r'<mark class="word-highlight">\1</mark>', text)


def make_button(label_play: str, label_stop: str, audio_path: str, classes: str = "") -> str:
    rel = audio_path.replace("\\", "/")
    return (
        f'<button class="play-btn {classes}" data-label-play="{label_play}" '
        f'data-label-stop="{label_stop}" onclick="togglePlay(this, \'{rel}\')">'
        f'{PLAY_ICON}<span class="label">{label_play}</span>'
        f'</button>'
    )


def generate_html(clips: dict, occurrences: dict, letter: str, out_dir: Path,
                  comp_paths: dict = None) -> Path:
    """Genereer de dictionary HTML pagina."""
    rel_audio  = "audio"
    comp_paths = comp_paths or {}

    # Bouw navigatie (alle letters)
    all_letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    nav_links = ""
    for l in all_letters:
        active = 'class="active"' if l == letter.upper() else ""
        nav_links += f'<a href="#{l}" {active}>{l}</a> '

    # Bouw entries per woord (gesorteerd op alfabet)
    entries_html = ""
    sorted_words = sorted(clips.keys())

    for word in sorted_words:
        word_clips = clips[word]
        freq = len(occurrences.get(word, []))

        # ─ Word header
        entries_html += f"""
<div class="word-entry" id="word-{word}">
  <div class="word-header">
    <span class="word-text">{word}</span>
    <span class="word-freq">{freq}× uitgesproken</span>
  </div>
"""
        # ─ Compilatie-player (prominent bovenaan, onder header)
        comp_path = comp_paths.get(word)
        if comp_path and Path(comp_path).exists():
            rel_comp = f"{rel_audio}/{Path(comp_path).name}"
            n_comp   = freq  # totaal aantal keer uitgesproken
            entries_html += f"""  <div class="comp-player">
    <button class="comp-btn" data-label-play="▶ Compilatie — alle {n_comp}× afspelen"
            data-label-stop="■ stop"
            onclick="togglePlay(this, '{rel_comp}')">
      {PLAY_ICON}<span class="label">▶ Compilatie — alle {n_comp}× afspelen</span>
    </button>
    <span class="comp-label">🎵 met muziek</span>
  </div>
"""

        entries_html += '  <div class="word-body">\n'

        # ─ Losse uitspraken
        valid_word_clips = [c for c in word_clips
                            if c["word_path"] and Path(c["word_path"]).exists()]
        if valid_word_clips:
            entries_html += (
                '<div class="clip-label" style="font-family:sans-serif;font-size:.7rem;'
                'font-weight:600;text-transform:uppercase;letter-spacing:.1em;color:#666;'
                'margin-bottom:8px;">Uitspraken</div>'
                '<div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:4px;">'
            )
            for i, clip in enumerate(valid_word_clips, 1):
                rel_path = f"{rel_audio}/{Path(clip['word_path']).name}"
                entries_html += make_button(f"▶ #{i}", "■ stop", rel_path)
            entries_html += "</div>"

        # ─ Context (dedupliceer op zinsniveau: zelfde tekst = zelfde zin)
        seen_ctx: set[str] = set()
        valid_ctx_clips = []
        for c in word_clips:
            if not (c["ctx_path"] and Path(c["ctx_path"]).exists()):
                continue
            txt = c.get("ctx_text", "").strip()
            if txt in seen_ctx:
                continue
            seen_ctx.add(txt)
            valid_ctx_clips.append(c)

        if valid_ctx_clips:
            entries_html += '<div class="ctx-section"><div class="ctx-label">In context</div>'
            for clip in valid_ctx_clips:
                rel_path = f"{rel_audio}/{Path(clip['ctx_path']).name}"
                ctx_text = highlight_word(clip.get("ctx_text", ""), word)
                entries_html += f"""
<div class="ctx-row">
  {make_button("▶", "■", rel_path)}
  <span class="ctx-text">"{ctx_text}"</span>
</div>"""
            entries_html += "</div>"

        entries_html += "  </div>\n</div>\n"  # word-body, word-entry

    html = HTML_TEMPLATE.format(
        letter=letter.upper(),
        word_count=len(sorted_words),
        nav_links=nav_links,
        entries=entries_html,
    )

    out_path = out_dir / f"woordenboek_{letter.upper()}.html"
    out_path.write_text(html, encoding="utf-8")
    return out_path


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Stemmy Van Dale-achtig woordenboek")
    parser.add_argument("--letter", default="P", help="Letter om te indexeren (default: P)")
    parser.add_argument("--db",     default="data/stemmy-replica.db", help="Pad naar SQLite DB")
    parser.add_argument("--format", default="dcacd05c-ff24-4403-84b1-c89857052dcb",
                        help="Format UUID (default: POM)")
    parser.add_argument("--limit",  type=int, default=60,
                        help="Max unieke woorden (gesorteerd op frequentie, default: 60)")
    parser.add_argument("--clips",  type=int, default=3,
                        help="Clips per woord (default: 3)")
    parser.add_argument("--out",    default="output/dictionary",
                        help="Output map voor HTML + audio (default: output/dictionary)")
    parser.add_argument("--music",  default=None,
                        help="Muziekbestand voor compilaties (default: interviewmusic2.mp3)")
    parser.add_argument("--music-vol", type=float, default=0.18,
                        help="Muziekvolume 0.0–1.0 (default: 0.18)")
    parser.add_argument("--comp-max", type=int, default=25,
                        help="Max clips per compilatie (default: 25)")
    args = parser.parse_args()

    letter = args.letter.strip()[0].upper()
    db_path = str(ROOT / args.db)
    out_dir = ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    # Bepaal muziekpad
    music_dir = ROOT / "src" / "stemmy_cli" / "static" / "music"
    if args.music:
        music_path = Path(args.music)
    else:
        music_path = music_dir / "interviewmusic2.mp3"
    if not music_path.exists():
        # Pak eerste beschikbare muziek
        candidates = list(music_dir.glob("*.mp3"))
        music_path = candidates[0] if candidates else None

    print(f"📖 Stemmy Woordenboek — letter {letter}")
    print(f"   DB:     {db_path}")
    print(f"   Output: {out_dir}")
    print(f"   Muziek: {music_path.name if music_path else '(geen)'}")
    print()

    # 1. Verzamel woord-voorkomens
    print(f"[1/4] Woorden verzamelen uit DB …")
    occurrences = collect_word_occurrences(db_path, args.format, letter)
    print(f"      {len(occurrences)} unieke woorden gevonden die beginnen met '{letter}'")

    if not occurrences:
        print("Geen woorden gevonden. Klaar.")
        return

    freq_sorted = sorted(occurrences.items(), key=lambda x: -len(x[1]))
    print(f"      Top 10: {', '.join(f'{w}({len(o)}×)' for w, o in freq_sorted[:10])}")
    print()

    # 2. Extraheer audio
    print(f"[2/4] Audio extraheren (top {args.limit} woorden, {args.clips} clips/woord, "
          f"max {args.comp_max} compilatie-clips) …")
    clips, comp_clips = extract_clips(
        occurrences, top_n=args.limit, clips_per_word=args.clips,
        out_dir=out_dir, letter=letter, comp_max=args.comp_max
    )
    actual = sum(1 for wc in clips.values()
                 for c in wc if c.get("word_path") and Path(c["word_path"]).exists())
    print(f"      {actual} uitspraken-clips beschikbaar voor {len(clips)} woorden")
    print()

    # 3. Bouw compilaties
    comp_paths = {}
    if music_path and comp_clips:
        print(f"[3/4] Compilaties bouwen met muziek …")
        comp_paths = build_compilaties(comp_clips, out_dir, music_path, args.music_vol)
        print(f"      {len(comp_paths)} compilaties gebouwd")
        print()
    else:
        print(f"[3/4] Compilaties overgeslagen (geen muziek of clips)")
        print()

    # 4. Genereer HTML
    print(f"[4/4] HTML genereren …")
    html_path = generate_html(clips, occurrences, letter, out_dir, comp_paths=comp_paths)
    print(f"      ✓ {html_path}")
    print()
    print(f"🎉 Klaar! Start de server:")
    print(f"   python3 dict/server.py --letter {letter}")
    print(f"   → http://localhost:7842/woordenboek_{letter}.html")


if __name__ == "__main__":
    main()
