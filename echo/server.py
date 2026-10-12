#!/usr/bin/env python3
"""
GRAFSTEMMING — Roep een woord, hoor de echo vanuit het podcastarchief.

Mythologie: Echo was een nimf die door Hera werd vervloekt om nooit zelf te spreken —
alleen de laatste woorden van anderen kon ze herhalen. Stierf van liefdesverdriet totdat
alleen haar stem overbleef. Dit is haar graf.

Start:
    python3 echo/server.py          → http://localhost:7843/echo
    python3 echo/server.py --format <uuid>   Filter op één podcast
"""

import argparse, json, os, random, re, sqlite3, subprocess
import sys, tempfile, threading, time, webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT      = Path(__file__).resolve().parent.parent
CACHE_DIR = Path(__file__).resolve().parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)
ECHO_STATIC_DIR = Path(__file__).resolve().parent / "static"
MUSIC_DIR = ROOT / "src" / "stemmy_cli" / "static" / "music"

sys.path.insert(0, str(ROOT / "src"))
os.environ["PATH"] = os.environ.get("PATH","") + ":/opt/homebrew/bin:/usr/local/bin"

# Laad .env zodat OPENAI_API_KEY etc. beschikbaar zijn
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(ROOT / ".env",       override=True)
    _load_dotenv(ROOT / ".env.local", override=True)
except ImportError:
    pass

# ── Word index ─────────────────────────────────────────────────────────────────

_word_index: dict[str, list] = {}   # word → [{"word_start","word_end","audio_url"}, …]
_word_freq:  dict[str, int]  = {}   # word → frequency count
_word_list:  list[str]       = []   # all words (for random sampling)
_gap_index:  list[dict]      = []   # stilte/non-woord fragmenten: hmm, lach, adem
_index_lock  = threading.Lock()
_index_ready = threading.Event()

# ── Podcast catalogue ──────────────────────────────────────────────────────────
_formats_cache: list[dict] = []   # [{id, title, frags}, ...]
_db_path_global: str = ""
_active_format_id: str | None = None   # huidig actief filter (voor cache-naamruimte)

def _build_index(db_path: str, format_id: str | None):
    global _db_path_global, _active_format_id
    _db_path_global   = db_path
    _active_format_id = format_id

    print("⏳ Woordenindex opbouwen…", flush=True)
    t0   = time.time()
    conn = sqlite3.connect(db_path)

    # Laad podcast-catalogus (eenmalig, altijd volledig)
    if not _formats_cache:
        try:
            rows_f = conn.execute("""
                SELECT f.id, f.title, COUNT(DISTINCT frag.id) as n
                FROM formats f
                JOIN items i ON i.format_id = f.id
                JOIN fragments frag ON frag.item_id = i.id
                WHERE frag.words IS NOT NULL
                GROUP BY f.id ORDER BY n DESC
            """).fetchall()
            _formats_cache.extend({"id": r[0], "title": r[1], "frags": r[2]} for r in rows_f)
        except Exception:
            pass

    sql  = """
        SELECT f.words, f.item_audio_url
        FROM fragments f
        JOIN items i ON f.item_id = i.id
        WHERE f.words IS NOT NULL AND f.words != '[]'
          AND f.item_audio_url IS NOT NULL
    """
    params: tuple = ()
    if format_id:
        sql += " AND i.format_id = ?"
        params = (format_id,)

    rows  = conn.execute(sql, params).fetchall()
    conn.close()

    index: dict[str, list] = {}
    gaps:  list[dict]      = []    # stilte/niet-woord regio's (hmm, lach, adem…)

    for words_json, audio_url in rows:
        try:
            words = json.loads(words_json)
        except Exception:
            continue
        for i, w in enumerate(words):
            raw   = w.get("text", "")
            clean = raw.strip(".,!?:;\"'()—…-").lower()
            if not clean or len(clean) < 2:
                continue
            if clean not in index:
                index[clean] = []
            # Context: 4 woorden voor en na voor visuele weergave
            ctx_b = " ".join(x.get("text","") for x in words[max(0,i-4):i])
            ctx_a = " ".join(x.get("text","") for x in words[i+1:i+5])
            index[clean].append({
                "word_start": int(w.get("start", 0)),
                "word_end":   int(w.get("end",   0)),
                "audio_url":  audio_url,
                "ctx_before": ctx_b,
                "ctx_after":  ctx_a,
            })

            # Tussenruimte detecteren: gap van 600ms-3s = hmm/lach/adem
            if i < len(words) - 1:
                w_end      = int(w.get("end", 0))
                next_start = int(words[i+1].get("start", 0))
                gap_ms     = next_start - w_end
                if 600 <= gap_ms <= 3000:
                    gaps.append({
                        "start_ms": w_end + 50,     # iets na het woord instappen
                        "end_ms":   next_start - 50,
                        "audio_url": audio_url,
                    })

    freq  = {w: len(occs) for w, occs in index.items()}
    wlist = list(index.keys())
    # Beperk gap-index tot 5000 willekeurige samples (geheugenbesparend)
    if len(gaps) > 5000:
        random.shuffle(gaps)
        gaps = gaps[:5000]

    with _index_lock:
        _word_index.clear(); _word_index.update(index)
        _word_freq.clear();  _word_freq.update(freq)
        _word_list.clear();  _word_list.extend(wlist)
        _gap_index.clear();  _gap_index.extend(gaps)
    _index_ready.set()
    label = format_id[:8] + "…" if format_id else "alle podcasts"
    print(f"✓ Index klaar: {len(index):,} woorden ({label}) in {time.time()-t0:.1f}s", flush=True)


# ── Sentence → best-word resolution ───────────────────────────────────────────

def _resolve_query(query: str) -> tuple[str, str]:
    """
    Geeft (matched_word, original_query) terug.
    Voor een zin: kies het zeldzaamste woord dat in de index staat.
    """
    parts = re.split(r"\s+", query.strip())
    if len(parts) == 1:
        return parts[0].lower(), query

    candidates = []
    for p in parts:
        clean = p.strip(".,!?:;\"'()—…-").lower()
        if clean in _word_freq:
            candidates.append((clean, _word_freq[clean]))

    if not candidates:
        return parts[0].lower(), query          # niets gevonden, probeer eerste woord

    # Kies zeldzaamste (laagste frequentie = interessantst)
    candidates.sort(key=lambda x: x[1])
    return candidates[0][0], query


# ── Echo filter presets ────────────────────────────────────────────────────────

_ECHO_FILTERS = {
    0: "volume=1.6",
    1: "aecho=0.7:0.5:120|350:0.25|0.10,lowpass=f=12000,volume=1.6",
    2: "aecho=1.0:0.85:120|350|700|1400:0.55|0.38|0.22|0.10,lowpass=f=9500,volume=1.6",
    3: "aecho=1.0:0.95:120|350|700|1400|2800:0.60|0.42|0.26|0.14|0.06,lowpass=f=8000,volume=1.6",
}
_GAP_ECHO_FILTERS = {
    0: "volume=2.2",
    1: "aecho=0.6:0.4:80|200:0.2|0.08,lowpass=f=9000,volume=2.2",
    2: "aecho=0.8:0.6:80|200:0.3|0.15,lowpass=f=7000,volume=2.2",
    3: "aecho=0.9:0.8:80|200|450:0.4|0.2|0.08,lowpass=f=7000,volume=2.2",
}

# ── Audio extractie + echo ─────────────────────────────────────────────────────

def _extract_and_echo(word: str, variant: int = 0, echo_level: int = 2) -> tuple[bytes | None, int, str, str]:
    """Geeft (mp3_bytes, frequency, ctx_before, ctx_after) terug."""
    clean     = re.sub(r"\s+", " ", word.strip().lower())
    cache_key = re.sub(r"[^a-z0-9]", "_", clean)
    suffix    = f"_v{variant}" if variant > 0 else ""

    # Cache-naamruimte per actief podcast-filter (voorkomt cross-podcast cache hits)
    fmt_ns  = re.sub(r"[^a-z0-9]", "", (_active_format_id or "all")[:8])
    fmt_dir = CACHE_DIR / fmt_ns
    fmt_dir.mkdir(exist_ok=True)
    echo_sfx = f"_e{echo_level}" if echo_level != 2 else ""
    cached  = fmt_dir / f"{cache_key}{suffix}{echo_sfx}.mp3"

    freq = _word_freq.get(clean, 0)

    if cached.exists() and cached.stat().st_size > 500:
        # Context ophalen uit index (neem eerste occurrence)
        occs = _word_index.get(clean, [])
        ctx_b = occs[0].get("ctx_before","") if occs else ""
        ctx_a = occs[0].get("ctx_after","")  if occs else ""
        return cached.read_bytes(), freq, ctx_b, ctx_a

    if not _index_ready.wait(60):
        return None, 0, "", ""

    occs = _word_index.get(clean, [])
    if not occs:
        return None, 0, "", ""

    # Multi-echo deduplicatie: groepeer op audio_url, neem Nth unieke bron
    # zodat variant 0/1/2 uit verschillende podcast-afleveringen komen.
    # BELANGRIJK: gesorteerde (deterministische) volgorde — geen random.shuffle —
    # zodat parallelle requests voor v=0/1/2 altijd verschillende URLs pakken.
    url_groups: dict[str, list] = {}
    for occ in occs:
        url_groups.setdefault(occ["audio_url"], []).append(occ)
    unique_urls = sorted(url_groups.keys())   # deterministisch, niet random

    # Pak variant % len zodat single-echo (hoge varianten) nooit None geeft
    target_url = unique_urls[variant % len(unique_urls)]
    ordered = url_groups[target_url]

    for occ in ordered[:6]:
        result = _try_extract(occ, cached, variant=variant, echo_level=echo_level)
        if result:
            return result, freq, occ.get("ctx_before",""), occ.get("ctx_after","")

    return None, freq, "", ""


def _try_extract(occ: dict, cache_path: Path, variant: int = 0, echo_level: int = 2) -> bytes | None:
    try:
        from stemmy_cli.audio.parallel_extractor import ParallelExtractor
    except ImportError:
        return None

    # Variabele context: soms 1 woord, soms 4-5 contextwoorden (~mix)
    # variant 0 = kort, 1 = middel, 2 = lang
    pads = [(0.22, 0.22), (0.8, 0.5), (2.0, 1.2), (3.0, 1.8)]
    pad_pre, pad_post = pads[min(variant, len(pads)-1)]
    # Kleine willekeurige variatie zodat herhaalde aanroepen niet identiek zijn
    pad_pre  += random.uniform(-0.05, 0.25)
    pad_post += random.uniform(-0.05, 0.15)

    seg = [{"id": "echo_0", "audio_url": occ["audio_url"],
            "start_time": max(0.0, occ["word_start"]/1000 - pad_pre),
            "end_time":   occ["word_end"]/1000 + pad_post}]

    with tempfile.TemporaryDirectory(prefix="echo_") as tmp:
        try:
            from stemmy_cli.audio.parallel_extractor import ParallelExtractor
            extractor = ParallelExtractor(max_workers=4, output_dir=Path(tmp))
            successes, _ = extractor.extract_segments(seg)
        except Exception:
            return None

        if not successes:
            return None
        src = Path(successes[0].output_path)
        if not src.exists() or src.stat().st_size < 500:
            return None

        af = _ECHO_FILTERS.get(echo_level, _ECHO_FILTERS[2])
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", str(src),
             "-af", af,
             "-c:a", "libmp3lame", "-q:a", "2", str(cache_path)],
            capture_output=True
        )

    if r.returncode == 0 and cache_path.exists() and cache_path.stat().st_size > 500:
        return cache_path.read_bytes()
    return None


def _random_word() -> tuple[str, int]:
    """Kies een willekeurig woord (gericht op zeldzame, korte woorden voor fluisteren)."""
    if not _word_list:
        return "", 0
    # Voorkeur voor woorden die minder vaak voorkomen (mysterieuzer)
    sample = random.sample(_word_list, min(30, len(_word_list)))
    sample.sort(key=lambda w: _word_freq.get(w, 0))
    word = sample[0]
    return word, _word_freq.get(word, 0)


def _extract_gap(echo_level: int = 2) -> bytes | None:
    """Extraheer een random stilte-fragment (hmm, lach, adem) als echo-MP3."""
    with _index_lock:
        if not _gap_index:
            return None
        occ = random.choice(_gap_index)

    try:
        from stemmy_cli.audio.parallel_extractor import ParallelExtractor
    except ImportError:
        return None

    seg = [{"id": "gap_0", "audio_url": occ["audio_url"],
            "start_time": occ["start_ms"] / 1000,
            "end_time":   occ["end_ms"]   / 1000}]

    with tempfile.TemporaryDirectory(prefix="echo_gap_") as tmp:
        try:
            extractor = ParallelExtractor(max_workers=2, output_dir=Path(tmp))
            successes, _ = extractor.extract_segments(seg)
        except Exception:
            return None
        if not successes:
            return None
        src = Path(successes[0].output_path)
        if not src.exists() or src.stat().st_size < 200:
            return None

        out = Path(tmp) / "gap_echo.mp3"
        gaf = _GAP_ECHO_FILTERS.get(echo_level, _GAP_ECHO_FILTERS[2])
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", str(src),
             "-af", gaf,
             "-c:a", "libmp3lame", "-q:a", "3", str(out)],
            capture_output=True
        )
        if r.returncode == 0 and out.exists() and out.stat().st_size > 200:
            return out.read_bytes()
    return None


# ── Spelmodi helpers ───────────────────────────────────────────────────────────

def _dutch_rhyme_key(word: str) -> str:
    """Rijmsleutel voor Nederlands: laatste 4 tekens (≥5 letters) anders 3."""
    clean = re.sub(r"[^a-zA-Z]", "", word.lower())
    return clean[-4:] if len(clean) >= 5 else clean[-3:] if len(clean) >= 3 else clean


def _get_rhyme_candidates(target: str, max_candidates: int = 30) -> list:
    """Vind woorden in de index die rijmen op 'target'. Geeft [(woord, occs), ...]."""
    key = _dutch_rhyme_key(target)
    if len(key) < 2:
        return []
    candidates = []
    with _index_lock:
        for word, occs in _word_index.items():
            if word != target and _dutch_rhyme_key(word) == key:
                candidates.append((word, list(occs)))
    candidates.sort(key=lambda x: len(x[1]))   # zeldzamer = eerder
    return candidates[:max_candidates]


_MAX_FRAG_MS = 4000   # maximale fragmentduur voor semantische modi

def _try_extract_segment(audio_url: str, start_ms: int, end_ms: int, echo_level: int = 2) -> bytes | None:
    """Extraheer een volledig fragment (start→end ms) en pas echo-filter toe. Met caching.
    Fragments worden gecapt op _MAX_FRAG_MS om megafragmenten te voorkomen."""
    import hashlib
    if end_ms <= start_ms:
        return None
    # Cap fragment op max duur — center het venster
    if end_ms - start_ms > _MAX_FRAG_MS:
        mid      = (start_ms + end_ms) // 2
        start_ms = max(0, mid - _MAX_FRAG_MS // 2)
        end_ms   = start_ms + _MAX_FRAG_MS
    url_hash   = hashlib.md5(audio_url.encode()).hexdigest()[:8]
    cache_key  = f"seg_{url_hash}_{start_ms}_{end_ms}_e{echo_level}"
    fmt_ns     = re.sub(r"[^a-z0-9]", "", (_active_format_id or "all")[:8])
    fmt_dir    = CACHE_DIR / fmt_ns
    fmt_dir.mkdir(exist_ok=True)
    cache_path = fmt_dir / f"{cache_key}.mp3"

    if cache_path.exists() and cache_path.stat().st_size > 500:
        return cache_path.read_bytes()

    try:
        from stemmy_cli.audio.parallel_extractor import ParallelExtractor
    except ImportError:
        return None

    seg = [{"id": "seg_0", "audio_url": audio_url,
            "start_time": max(0.0, start_ms / 1000 - 0.3),
            "end_time":   end_ms / 1000 + 0.5}]

    with tempfile.TemporaryDirectory(prefix="echo_seg_") as tmp:
        try:
            extractor = ParallelExtractor(max_workers=2, output_dir=Path(tmp))
            successes, _ = extractor.extract_segments(seg)
        except Exception:
            return None
        if not successes:
            return None
        src = Path(successes[0].output_path)
        if not src.exists() or src.stat().st_size < 500:
            return None

        af = _ECHO_FILTERS.get(echo_level, _ECHO_FILTERS[2])
        r  = subprocess.run(
            ["ffmpeg", "-y", "-i", str(src),
             "-af", af, "-c:a", "libmp3lame", "-q:a", "2", str(cache_path)],
            capture_output=True
        )

    if r.returncode == 0 and cache_path.exists() and cache_path.stat().st_size > 500:
        return cache_path.read_bytes()
    return None


def _ffmpeg_concat(clips: list) -> bytes | None:
    """Concateneer meerdere MP3-bestanden via ffmpeg concat demuxer."""
    if not clips:
        return None
    if len(clips) == 1:
        return clips[0]
    with tempfile.TemporaryDirectory(prefix="echo_concat_") as tmp:
        tmp_path = Path(tmp)
        clip_paths = []
        for i, clip_bytes in enumerate(clips):
            p = tmp_path / f"clip_{i}.mp3"
            p.write_bytes(clip_bytes)
            clip_paths.append(p)
        list_file = tmp_path / "concat.txt"
        list_file.write_text("\n".join(f"file '{p}'" for p in clip_paths))
        out = tmp_path / "combined.mp3"
        r = subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
             "-i", str(list_file),
             "-c:a", "libmp3lame", "-q:a", "2", str(out)],
            capture_output=True
        )
        if r.returncode == 0 and out.exists() and out.stat().st_size > 500:
            return out.read_bytes()
    return None


def _ffmpeg_mix(clips: list) -> bytes | None:
    """Mix meerdere MP3s GELIJKTIJDIG via ffmpeg amix — voor koor/chamber effect."""
    if not clips:
        return None
    if len(clips) == 1:
        return clips[0]
    with tempfile.TemporaryDirectory(prefix="echo_mix_") as tmp:
        tmp_path = Path(tmp)
        inputs = []
        for i, clip_bytes in enumerate(clips):
            p = tmp_path / f"clip_{i}.mp3"
            p.write_bytes(clip_bytes)
            inputs += ["-i", str(p)]
        n    = len(clips)
        amix = f"amix=inputs={n}:duration=longest:normalize=0"
        out  = tmp_path / "mixed.mp3"
        r = subprocess.run(
            ["ffmpeg", "-y"] + inputs +
            ["-filter_complex", amix, "-c:a", "libmp3lame", "-q:a", "2", str(out)],
            capture_output=True
        )
        if r.returncode == 0 and out.exists() and out.stat().st_size > 500:
            return out.read_bytes()
    return _ffmpeg_concat(clips)   # fallback op concat


def _ffmpeg_crossfade(clips: list, fade_s: float = 0.7) -> bytes | None:
    """Concateneer clips met crossfade (vloeiende overgang) — voor drift-effect."""
    if not clips:
        return None
    if len(clips) == 1:
        return clips[0]
    with tempfile.TemporaryDirectory(prefix="echo_xfade_") as tmp:
        tmp_path = Path(tmp)
        for i, clip_bytes in enumerate(clips):
            (tmp_path / f"clip_{i}.mp3").write_bytes(clip_bytes)
        inputs = []
        for i in range(len(clips)):
            inputs += ["-i", str(tmp_path / f"clip_{i}.mp3")]
        # Bouw filter_complex op voor n clips
        n = len(clips)
        if n == 2:
            filt    = f"[0][1]acrossfade=d={fade_s}:c1=exp:c2=exp[out]"
            map_arg = "[out]"
        elif n == 3:
            filt    = (f"[0][1]acrossfade=d={fade_s}:c1=exp:c2=exp[cf01];"
                       f"[cf01][2]acrossfade=d={fade_s}:c1=exp:c2=exp[out]")
            map_arg = "[out]"
        else:
            return _ffmpeg_concat(clips)   # fallback voor 4+
        out = tmp_path / "xfaded.mp3"
        r = subprocess.run(
            ["ffmpeg", "-y"] + inputs +
            ["-filter_complex", filt, "-map", map_arg,
             "-c:a", "libmp3lame", "-q:a", "2", str(out)],
            capture_output=True
        )
        if r.returncode == 0 and out.exists() and out.stat().st_size > 500:
            return out.read_bytes()
    return _ffmpeg_concat(clips)   # fallback


# ── Embedding cache (lazy, eenmalig laden) ─────────────────────────────────────

_emb_cache:         list = []
_emb_cache_lock          = threading.Lock()
_emb_cache_loaded: bool  = False


def _ensure_emb_cache():
    """Laad fragment-embeddings eenmalig in geheugen (thread-safe, lazy)."""
    global _emb_cache_loaded
    if _emb_cache_loaded:
        return
    with _emb_cache_lock:
        if _emb_cache_loaded:
            return
        try:
            import numpy as np
            if not _db_path_global:
                return
            conn = sqlite3.connect(_db_path_global)
            rows = conn.execute("""
                SELECT fe.embedding, f.text, f.item_audio_url,
                       CAST(f.start_time_seconds * 1000 AS INTEGER),
                       CAST(f.end_time_seconds   * 1000 AS INTEGER)
                FROM fragment_embeddings fe
                JOIN fragments f ON f.id = fe.fragment_id
                WHERE f.item_audio_url IS NOT NULL
                  AND f.start_time_seconds IS NOT NULL
            """).fetchall()
            conn.close()
            for emb_blob, text, audio_url, start_ms, end_ms in rows:
                try:
                    emb = np.array(json.loads(emb_blob), dtype=float)
                    _emb_cache.append({
                        "text":      text or "",
                        "audio_url": audio_url,
                        "start_ms":  int(start_ms or 0),
                        "end_ms":    int(end_ms   or 0),
                        "emb":       emb,
                    })
                except Exception:
                    continue
            print(f"✓ Embedding cache: {len(_emb_cache):,} fragmenten", flush=True)
        except Exception as e:
            print(f"⚠️  Embedding cache niet beschikbaar: {e}", flush=True)
        finally:
            _emb_cache_loaded = True


def _similarity_search(query: str, limit: int = 20) -> list:
    """Semantische zoektocht via in-memory embedding cache."""
    try:
        import numpy as np
        from stemmy_cli.embeddings.service import EmbeddingService
        _ensure_emb_cache()
        if not _emb_cache:
            return []
        # Detecteer provider op basis van dimensies in de cache
        # (voorkomt mismatch als embeddings met OpenAI zijn gemaakt)
        emb_dim  = len(_emb_cache[0]["emb"])
        provider = "openai" if emb_dim > 500 else "local"
        svc      = EmbeddingService(provider=provider)
        results  = svc.embed_texts([query])
        if not results:
            return []
        q_arr  = np.array(results[0].embedding, dtype=float)
        q_norm = np.linalg.norm(q_arr) + 1e-9
        scored = []
        for item in _emb_cache:
            b   = item["emb"]
            sim = float(np.dot(q_arr, b) / (q_norm * (np.linalg.norm(b) + 1e-9)))
            scored.append({
                "text":       item["text"],
                "audio_url":  item["audio_url"],
                "start_ms":   item["start_ms"],
                "end_ms":     item["end_ms"],
                "similarity": sim,
            })
        scored.sort(key=lambda x: x["similarity"], reverse=True)
        return scored[:limit]
    except Exception as e:
        print(f"Semantic search error: {e}", flush=True)
        return []


# ── Gemini multimodale embeddings ──────────────────────────────────────────────

_gemini_emb_cache:        list = []
_gemini_emb_lock               = threading.Lock()
_gemini_emb_loaded:       bool = False


def _gemini_transcribe(audio_bytes: bytes, mime_type: str = "audio/webm") -> str:
    """Laat Gemini-Flash de audio transcriberen naar tekst. Geeft getranscribeerde tekst of ''."""
    import urllib.request as _req, base64 as _b64, json as _json
    api_key = os.environ.get("GOOGLE_API_KEY", "")
    if not api_key or not audio_bytes:
        return ""
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           "gemini-2.0-flash-lite:generateContent?key=" + api_key)
    b64 = _b64.b64encode(audio_bytes).decode()
    payload = {"contents": [{"parts": [
        {"text": "Transcribeer exact wat er gezegd wordt in de audio. "
                 "Geef alleen de gesproken woorden terug, geen uitleg of leestekens."},
        {"inlineData": {"mimeType": mime_type, "data": b64}}
    ]}]}
    try:
        rq = _req.Request(url, data=_json.dumps(payload).encode(),
                          headers={"Content-Type": "application/json"}, method="POST")
        with _req.urlopen(rq, timeout=15) as resp:
            data = _json.loads(resp.read())
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        print(f"  ⚠ Gemini transcribe: {e}", flush=True)
        return ""


def _gemini_embed(audio_bytes: bytes | None = None, text: str = "") -> list | None:
    """Embed audio + tekst via Gemini REST API. Geeft lijst van floats of None."""
    import urllib.request, base64 as _b64
    api_key = os.environ.get("GOOGLE_API_KEY", "")
    if not api_key:
        return None
    parts = []
    if text:
        parts.append({"text": text})
    if audio_bytes:
        parts.append({
            "inlineData": {
                "mimeType": "audio/mpeg",
                "data": _b64.b64encode(audio_bytes).decode()
            }
        })
    if not parts:
        return None
    body = json.dumps({"content": {"parts": parts}}).encode()
    url  = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-embedding-2-preview:embedContent?key={api_key}"
    )
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            return data.get("embedding", {}).get("values")
    except Exception as e:
        print(f"Gemini embed error: {e}", flush=True)
        return None


def _ensure_gemini_emb_cache():
    """Laad pre-computed Gemini audio-embeddings in geheugen (lazy, thread-safe)."""
    global _gemini_emb_loaded
    if _gemini_emb_loaded:
        return
    with _gemini_emb_lock:
        if _gemini_emb_loaded:
            return
        try:
            import numpy as np
            if not _db_path_global:
                return
            conn = sqlite3.connect(_db_path_global)
            rows = conn.execute("""
                SELECT ge.embedding, f.text, f.item_audio_url,
                       CAST(f.start_time_seconds * 1000 AS INTEGER),
                       CAST(f.end_time_seconds   * 1000 AS INTEGER)
                FROM fragment_gemini_embeddings ge
                JOIN fragments f ON f.id = ge.fragment_id
                WHERE f.item_audio_url IS NOT NULL
                  AND f.start_time_seconds IS NOT NULL
            """).fetchall()
            conn.close()
            for emb_blob, text, audio_url, start_ms, end_ms in rows:
                try:
                    emb = np.array(json.loads(emb_blob), dtype=float)
                    _gemini_emb_cache.append({
                        "text":      text or "",
                        "audio_url": audio_url,
                        "start_ms":  int(start_ms or 0),
                        "end_ms":    int(end_ms   or 0),
                        "emb":       emb,
                    })
                except Exception:
                    continue
            n = len(_gemini_emb_cache)
            if n: print(f"✓ Gemini embedding cache: {n:,} fragmenten", flush=True)
        except Exception as e:
            if "no such table" not in str(e):
                print(f"⚠️  Gemini cache: {e}", flush=True)
        finally:
            _gemini_emb_loaded = True


def _gemini_similarity_search(query_emb: list, limit: int = 20,
                               mode: str = "similar") -> list:
    """Zoek via Gemini embeddings. Als cache leeg: fallback op tekst-cache."""
    try:
        import numpy as np
        _ensure_gemini_emb_cache()
        cache = _gemini_emb_cache if _gemini_emb_cache else _emb_cache
        if not cache:
            return []
        q     = np.array(query_emb, dtype=float)
        q_dim = len(q)
        q_norm = np.linalg.norm(q) + 1e-9
        scored = []
        for item in cache:
            b = item["emb"]
            if len(b) != q_dim:
                continue
            sim = float(np.dot(q, b) / (q_norm * (np.linalg.norm(b) + 1e-9)))
            scored.append({**{k: v for k, v in item.items() if k != "emb"},
                           "similarity": sim})
        scored.sort(key=lambda x: x["similarity"], reverse=True)
        return scored[:limit]
    except Exception as e:
        print(f"Gemini similarity error: {e}", flush=True)
        return []


# ── HTML ───────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="nl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GRAFSTEMMING</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Cinzel:wght@300;400;700&family=Raleway:wght@200;300;400&display=swap');

*{margin:0;padding:0;box-sizing:border-box;}
:root{
  --text:rgba(215,215,245,0.92);
  --muted:rgba(135,135,175,0.58);
  --accent:rgba(90,90,210,0.78);
}

html,body{
  height:100%;background:#000;overflow:hidden;
  display:flex;flex-direction:column;
  align-items:center;justify-content:center;
  color:var(--text);font-family:'Raleway',sans-serif;font-weight:300;
  user-select:none;
}

/* ── Particles ── */
#particles{position:fixed;inset:0;pointer-events:none;z-index:0;}
.particle{
  position:absolute;border-radius:50%;
  background:rgba(50,50,120,0.3);
  animation:drift linear infinite;
}
@keyframes drift{
  0%  {transform:translateY(105vh) translateX(0) scale(1);opacity:0;}
  5%  {opacity:1;}
  95% {opacity:0.2;}
  100%{transform:translateY(-5vh) translateX(var(--dx)) scale(0.4);opacity:0;}
}

/* ── App layout ── */
#app{
  position:relative;z-index:10;
  display:flex;flex-direction:column;
  align-items:center;width:100%;padding:0.8rem 1rem;
}

/* ── Header ── */
h1{
  font-family:'Cinzel',serif;font-weight:300;
  font-size:clamp(0.46rem,0.78vw,0.68rem);
  letter-spacing:0.42em;text-transform:uppercase;
  color:rgba(105,105,150,0.32);
  text-shadow:0 0 18px rgba(30,30,90,0.12);
  pointer-events:none;
  text-align:center;
  margin-top:0.4rem;
}
.tagline{display:none;}

/* ── Pit ── */
@keyframes pit-breathe{
  0%,100%{transform:scale(1);    filter:brightness(1);}
  40%    {transform:scale(1.028);filter:brightness(1.06);}
  70%    {transform:scale(1.014);filter:brightness(1.03);}
}
#pit-wrap{
  position:relative;
  width:clamp(300px,68vmin,580px);
  height:clamp(300px,68vmin,580px);
  flex-shrink:0;
  /* Adem-animatie op de wrapper, zodat de put-content meebeweegt */
  animation:pit-breathe 5.8s ease-in-out infinite;
  transform-origin:center;
}
#pit{
  width:100%;height:100%;border-radius:50%;
  background:radial-gradient(ellipse at 48% 45%,
    #01010e 0%,#03031c 18%,#06062a 35%,
    #0a0a38 52%,#0e0e48 68%,#121258 80%,
    #161668 90%,#1a1a78 100%
  );
  box-shadow:
    0 0 80px rgba(40,40,120,0.35),
    0 0 160px rgba(30,30,100,0.18),
    inset 0 0 120px rgba(0,0,8,0.88);
  position:relative;overflow:hidden;
  cursor:pointer;
}
#pit:active{transform:scale(0.99);}

.ring{
  position:absolute;border-radius:50%;border:1px solid;
  top:50%;left:50%;transform:translate(-50%,-50%);
  animation:ring-breathe 5s ease-in-out infinite;
}
@keyframes ring-breathe{
  0%,100%{opacity:0.15;}50%{opacity:0.35;}
}

.ripple{
  position:absolute;border-radius:50%;border:1.5px solid;
  top:50%;left:50%;
  transform:translate(-50%,-50%) scale(0);
  animation:ripple-out 2.4s cubic-bezier(0.2,0.6,0.4,1) forwards;
  pointer-events:none;
}
@keyframes ripple-out{
  0%  {transform:translate(-50%,-50%) scale(0.05);opacity:0.9;}
  100%{transform:translate(-50%,-50%) scale(2.2); opacity:0;}
}

#pit-word{
  position:absolute;top:50%;left:50%;
  transform:translate(-50%,-50%) scale(1);
  font-family:'Cinzel',serif;
  font-size:clamp(0.85rem,3.5vmin,2rem);
  font-weight:400;letter-spacing:0.12em;
  color:rgba(210,210,255,0.92);
  text-shadow:0 0 15px rgba(100,100,255,0.7),0 0 40px rgba(60,60,200,0.4);
  text-align:center;pointer-events:none;
  transition:all 1.6s cubic-bezier(0.4,0,0.2,1);
  white-space:nowrap;max-width:90%;
}

/* ── Controls ── */
#controls{
  display:flex;align-items:center;gap:10px;
  margin-top:1.6rem;
}
#word-input{
  background:rgba(12,12,30,0.88);
  border:1px solid rgba(50,50,120,0.4);
  border-radius:999px;padding:9px 20px;
  color:var(--text);font-family:'Raleway',sans-serif;
  font-weight:300;font-size:0.88rem;letter-spacing:0.05em;
  outline:none;width:220px;
  transition:border-color 0.3s,box-shadow 0.3s;
  caret-color:rgba(120,120,220,0.9);
}
#word-input::placeholder{color:rgba(80,80,120,0.5);}
#word-input:focus{
  border-color:rgba(80,80,180,0.6);
  box-shadow:0 0 18px rgba(60,60,160,0.2);
}
.icon-btn{
  width:48px;height:48px;border-radius:50%;border:1.5px solid;
  background:rgba(10,10,25,0.9);cursor:pointer;
  display:flex;align-items:center;justify-content:center;
  font-size:1.2rem;transition:all 0.25s;flex-shrink:0;
  text-decoration:none;
}
#mic-btn{border-color:rgba(90,40,40,0.55);color:rgba(190,120,120,0.75);}
#mic-btn:hover{border-color:rgba(180,60,60,0.7);box-shadow:0 0 22px rgba(160,40,40,0.3);}
#mic-btn.listening{
  border-color:rgba(220,70,70,0.9);color:rgba(240,160,160,0.95);
  box-shadow:0 0 30px rgba(200,50,50,0.5);
  animation:mic-pulse 1s ease-in-out infinite;
}
@keyframes mic-pulse{
  0%,100%{box-shadow:0 0 18px rgba(200,50,50,0.3);}
  50%    {box-shadow:0 0 40px rgba(200,50,50,0.6);}
}
#send-btn{border-color:rgba(40,40,100,0.55);color:rgba(130,130,210,0.7);font-size:1rem;}
#send-btn:hover{border-color:rgba(80,80,200,0.7);box-shadow:0 0 20px rgba(60,60,180,0.25);}
#multi-btn{border-color:rgba(60,40,100,0.45);color:rgba(140,100,200,0.5);font-size:1.1rem;}
#multi-btn:hover{border-color:rgba(100,60,180,0.7);box-shadow:0 0 20px rgba(80,40,160,0.25);}
#multi-btn.active{
  border-color:rgba(120,70,220,0.85);color:rgba(170,130,255,0.9);
  box-shadow:0 0 22px rgba(100,50,200,0.35);
}
#sound-btn{
  border-color:rgba(40,80,40,0.45);color:rgba(100,160,100,0.5);
  font-size:1rem;transition:all 0.3s;
}
#sound-btn.active{
  border-color:rgba(60,140,60,0.7);color:rgba(120,200,120,0.8);
  box-shadow:0 0 18px rgba(40,120,40,0.25);
}
#auto-btn{border-color:rgba(40,80,80,0.4);color:rgba(100,160,160,0.45);font-size:1.1rem;}
#auto-btn:hover{border-color:rgba(60,130,130,0.6);box-shadow:0 0 18px rgba(40,110,110,0.2);}
#auto-btn.active{
  border-color:rgba(60,180,180,0.7);color:rgba(120,220,220,0.85);
  box-shadow:0 0 20px rgba(40,160,160,0.28);
}

/* ── Status ── */
#status{
  margin-top:1rem;
  font-size:0.65rem;letter-spacing:0.2em;text-transform:uppercase;
  color:rgba(155,155,195,0.68);min-height:1.2em;
  transition:opacity 0.4s;text-align:center;
}

/* ── Score panel ── */
#score-panel{
  display:flex;align-items:baseline;gap:0.9rem;
  justify-content:center;flex-wrap:wrap;
  font-family:'Cinzel',serif;
  margin-bottom:0.6rem;
}
#score-val{
  font-size:1.1rem;font-weight:300;
  color:rgba(185,185,238,0.85);
  text-shadow:0 0 18px rgba(80,80,180,0.4);
  line-height:1;
}
#streak-val{
  font-size:0.58rem;letter-spacing:0.15em;
  color:rgba(200,160,60,0.65);
}
#highscore-val{
  font-size:0.5rem;letter-spacing:0.1em;
  color:rgba(120,120,165,0.48);
}

/* ── Points popup ── */
.pts-popup{
  position:fixed;
  font-family:'Cinzel',serif;font-size:1.2rem;font-weight:400;
  color:rgba(185,210,80,0.95);
  text-shadow:0 0 18px rgba(140,185,40,0.65);
  pointer-events:none;z-index:50;
  text-align:center;
  animation:pts-float 3.8s ease-out forwards;
}
.pts-breakdown{
  font-size:0.58rem;letter-spacing:0.1em;
  color:rgba(150,170,65,0.75);
  font-family:'Raleway',sans-serif;font-weight:300;
  line-height:1.7;margin-top:3px;
}
@keyframes pts-float{
  0%  {opacity:0;transform:translate(-50%,-50%) scale(0.7);}
  12% {opacity:1;transform:translate(-50%,-50%) scale(1.08);}
  100%{opacity:0;transform:translate(-50%,-50%) translateY(-150px) scale(0.65);}
}
.pts-popup.neg{color:rgba(205,65,65,0.85);}

/* ── Freq badge ── */
#freq-badge{
  font-size:0.55rem;letter-spacing:0.15em;
  color:rgba(100,100,155,0.58);
  margin-top:0.2rem;min-height:1em;text-align:center;
  font-style:normal;transition:opacity 0.4s;
}

/* ── Podcast selector ── */
#podcast-wrap{
  text-align:center;
  margin-top:0.3rem;
  z-index:20;position:relative;
}
#podcast-btn{
  background:rgba(8,8,20,0.88);
  border:1px solid rgba(40,40,90,0.4);
  border-radius:999px;padding:5px 14px;
  color:rgba(90,90,140,0.55);
  font-family:'Raleway',sans-serif;font-weight:300;
  font-size:0.6rem;letter-spacing:0.12em;
  cursor:pointer;transition:all 0.2s;
}
#podcast-btn:hover{border-color:rgba(70,70,150,0.6);color:rgba(120,120,180,0.75);}
#podcast-menu{
  display:none;position:absolute;bottom:2.2rem;left:50%;transform:translateX(-50%);
  background:rgba(6,6,18,0.97);
  border:1px solid rgba(40,40,90,0.4);
  border-radius:12px;padding:8px 0;min-width:240px;
  box-shadow:0 8px 32px rgba(0,0,0,0.8);
}
#podcast-menu.open{display:block;}
.pm-item{
  padding:7px 16px;font-size:0.65rem;letter-spacing:0.08em;
  color:rgba(110,110,170,0.7);cursor:pointer;
  font-family:'Raleway',sans-serif;
  transition:background 0.15s,color 0.15s;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;
}
.pm-item:hover{background:rgba(40,40,100,0.25);color:rgba(160,160,220,0.9);}
.pm-item.active{color:rgba(180,180,255,0.9);}

/* ── Ghost words ── */
.ghost-word{
  position:fixed;
  font-family:'Cinzel',serif;
  font-size:clamp(0.5rem,1.2vmin,0.8rem);
  letter-spacing:0.15em;
  color:rgba(105,105,160,0.32);
  animation:ghost-rise 7s ease-out forwards;
  pointer-events:none;z-index:5;
  white-space:nowrap;
}
@keyframes ghost-rise{
  0%  {opacity:0;transform:translate(-50%,-50%) translateY(5px);}
  15% {opacity:1;}
  80% {opacity:0.2;}
  100%{opacity:0;transform:translate(-50%,-50%) translateY(-35px);}
}

/* ── History ── */
#history{
  position:fixed;bottom:1.5rem;left:50%;
  transform:translateX(-50%);
  display:flex;gap:1.2rem;flex-wrap:wrap;
  justify-content:center;max-width:80vw;
  pointer-events:none;
}
.h-word{
  font-family:'Cinzel',serif;font-size:0.65rem;
  letter-spacing:0.12em;color:var(--muted);
  animation:fadein 0.6s ease;
}
@keyframes fadein{from{opacity:0;transform:translateY(4px);}to{opacity:1;transform:translateY(0);}}

/* ── Info overlay ── */
#info-overlay{
  position:fixed;inset:0;z-index:300;
  background:rgba(0,0,8,0.92);
  display:flex;align-items:center;justify-content:center;
  opacity:0;pointer-events:none;
  transition:opacity 0.4s;
}
#info-overlay.open{opacity:1;pointer-events:all;}
#info-box{
  background:rgba(3,3,18,0.98);
  border:1px solid rgba(60,60,130,0.32);
  border-radius:18px;
  padding:2.4rem 2.8rem;
  max-width:500px;width:90%;
  text-align:center;
  font-family:'Raleway',sans-serif;font-weight:300;
  position:relative;
  max-height:88vh;overflow-y:auto;
}
#info-box h2{
  font-family:'Cinzel',serif;font-weight:300;
  font-size:clamp(0.9rem,2vw,1.3rem);
  letter-spacing:0.4em;text-transform:uppercase;
  color:rgba(180,180,240,0.85);
  margin-bottom:0.5rem;
}
.info-sub{
  font-size:0.6rem;letter-spacing:0.2em;text-transform:uppercase;
  color:rgba(120,120,175,0.45);
  margin-bottom:1.8rem;
}
.info-myth{
  font-size:0.78rem;line-height:1.9;
  color:rgba(170,170,210,0.7);
  margin-bottom:1.6rem;
  font-style:italic;
}
.info-section{
  margin-bottom:1.4rem;text-align:left;
}
.info-section h3{
  font-family:'Cinzel',serif;font-size:0.6rem;
  letter-spacing:0.3em;text-transform:uppercase;
  color:rgba(140,140,200,0.55);
  margin-bottom:0.7rem;
}
.info-row{
  display:flex;align-items:baseline;gap:0.8rem;
  margin-bottom:0.45rem;
}
.info-icon{
  font-size:0.9rem;min-width:1.4rem;text-align:center;
  flex-shrink:0;
}
.info-desc{
  font-size:0.72rem;line-height:1.6;
  color:rgba(160,160,205,0.72);
}
#info-close{
  margin-top:1.6rem;
  background:none;
  border:1px solid rgba(60,60,120,0.35);
  border-radius:999px;
  padding:0.5rem 2rem;
  color:rgba(130,130,185,0.6);
  font-family:'Raleway',sans-serif;font-size:0.65rem;
  letter-spacing:0.2em;text-transform:uppercase;
  cursor:pointer;transition:all 0.25s;
}
#info-close:hover{
  border-color:rgba(90,90,180,0.6);color:rgba(170,170,230,0.85);
}
#info-btn{border-color:rgba(50,50,100,0.4);color:rgba(110,110,170,0.45);font-size:0.9rem;}
#info-btn:hover{border-color:rgba(80,80,160,0.6);box-shadow:0 0 16px rgba(60,60,140,0.2);}

/* ── Loading overlay ── */
#loading{
  position:fixed;inset:0;z-index:100;background:#000;
  display:flex;flex-direction:column;
  align-items:center;justify-content:center;gap:1.4rem;
  transition:opacity 1.2s;
}
#loading h2{
  font-family:'Cinzel',serif;font-weight:300;
  font-size:0.9rem;letter-spacing:0.35em;
  color:rgba(120,120,190,0.55);
}
.l-rings{position:relative;width:72px;height:72px;}
.l-ring{
  position:absolute;border-radius:50%;
  border:1px solid rgba(60,60,130,0.35);
  top:50%;left:50%;
  animation:l-spin linear infinite;
}
@keyframes l-spin{
  from{transform:translate(-50%,-50%) rotate(0deg);}
  to  {transform:translate(-50%,-50%) rotate(360deg);}
}

/* ── How to play ── */
#howto{
  position:fixed;top:50%;left:50%;z-index:25;
  transform:translate(-50%,-50%) translateY(calc(clamp(150px,34vmin,290px) + 2.8rem));
  text-align:center;pointer-events:none;
  transition:opacity 1.8s;
}
#howto.hidden{opacity:0;}
#howto-text{
  font-family:'Cinzel',serif;
  font-size:clamp(0.44rem,0.8vw,0.62rem);
  letter-spacing:0.18em;line-height:2.6;
  color:rgba(110,110,165,0.26);
  white-space:nowrap;
}

/* ── Echo control ── */
#echo-ctrl{
  display:flex;align-items:center;gap:0.55rem;
  justify-content:center;
  margin-top:0.45rem;
  opacity:0.45;transition:opacity 0.3s;
}
#echo-ctrl:hover{opacity:0.92;}
.echo-lbl{
  font-family:'Raleway',sans-serif;font-size:0.5rem;
  letter-spacing:0.2em;text-transform:uppercase;
  color:rgba(110,110,160,0.85);
}
.echo-dot{
  background:none;border:none;
  color:rgba(60,60,110,0.4);
  font-size:1rem;cursor:pointer;
  padding:0 2px;line-height:1;
  transition:color 0.2s,text-shadow 0.2s;
}
.echo-dot.active{
  color:rgba(155,155,238,0.88);
  text-shadow:0 0 8px rgba(100,100,220,0.5);
}
.echo-dot:hover{color:rgba(120,120,200,0.7);}

/* ── Sentence hint ── */
#sent-hint{
  font-size:0.6rem;letter-spacing:0.1em;
  color:rgba(100,100,150,0.4);
  margin-top:0.3rem;min-height:1em;text-align:center;
  font-style:italic;
}

/* ── Context line ── */
#ctx-line{
  font-size:0.72rem;letter-spacing:0.05em;
  color:rgba(115,115,165,0.58);
  font-family:'Raleway',sans-serif;font-weight:300;
  margin-top:0.5rem;min-height:1.3em;text-align:center;
  font-style:italic;transition:opacity 0.6s;
  max-width:360px;line-height:1.6;
}
.ctx-hl{
  color:rgba(175,175,245,0.75);font-weight:400;
  font-style:normal;
  text-decoration:underline;text-underline-offset:3px;
  text-decoration-color:rgba(90,90,190,0.35);
}

/* ── Spelmodi pills ── */
#mode-row{
  display:flex;align-items:center;gap:5px;
  flex-wrap:wrap;justify-content:center;
  margin-top:0.55rem;opacity:0.5;transition:opacity 0.3s;
}
#mode-row:hover{opacity:1;}
.mode-pill{
  background:rgba(6,6,18,0.9);
  border:1px solid rgba(35,35,80,0.35);
  border-radius:999px;padding:3px 11px;
  color:rgba(65,65,110,0.6);
  font-family:'Raleway',sans-serif;font-weight:300;
  font-size:0.48rem;letter-spacing:0.18em;text-transform:uppercase;
  cursor:pointer;transition:all 0.2s;
}
.mode-pill:hover{
  border-color:rgba(60,60,140,0.6);
  color:rgba(115,115,185,0.8);
}
.mode-pill.active{
  border-color:rgba(90,72,190,0.72);
  color:rgba(160,142,248,0.92);
  box-shadow:0 0 10px rgba(70,52,168,0.22);
}
</style>
</head>
<body>

<div id="loading">
  <div class="l-rings">
    <div class="l-ring" style="width:72px;height:72px;animation-duration:3s;"></div>
    <div class="l-ring" style="width:48px;height:48px;animation-duration:2s;border-color:rgba(60,60,130,0.25);"></div>
    <div class="l-ring" style="width:26px;height:26px;animation-duration:1.2s;border-color:rgba(60,60,130,0.15);"></div>
  </div>
  <h2 id="loading-text">stemmen laden…</h2>
</div>

<div id="particles"></div>

<div id="app">
  <!-- Score — boven de put -->
  <div id="score-panel">
    <div id="score-val">0</div>
    <div id="streak-val"></div>
    <div id="highscore-val"></div>
  </div>

  <div id="pit-wrap">
    <div id="pit">
      <div id="pit-word"></div>
    </div>
  </div>

  <div id="controls">
    <button class="icon-btn" id="mic-btn" title="Spreek (NL)">🎙</button>
    <input type="text" id="word-input" placeholder="woord of zin…" autocomplete="off" spellcheck="false">
    <button class="icon-btn" id="send-btn" title="Verstuur">↵</button>
    <button class="icon-btn" id="multi-btn" title="Drie echo's tegelijk">≋</button>
    <button class="icon-btn" id="sound-btn" title="Achtergrondgeluid aan/uit">♪</button>
    <button class="icon-btn" id="auto-btn" title="Auto-mode: computer roept woorden">∿</button>
    <button class="icon-btn" id="info-btn" title="Over Grafstemming">ℹ</button>
    <a class="icon-btn" href="/echo/3d" title="Open 3D-variant">3D</a>
  </div>

  <!-- Spelmodi pills -->
  <div id="mode-row">
    <button class="mode-pill active" data-mode="word"     title="Woordecho — exact woord">woord</button>
    <button class="mode-pill"        data-mode="rhyme"    title="Rijmecho — antwoord rijmt">rijm</button>
    <button class="mode-pill"        data-mode="semantic" title="Semantisch — zoek op betekenis">semantisch</button>
    <button class="mode-pill"        data-mode="sentence" title="Zinecho — spreek een zin">zin</button>
    <button class="mode-pill"        data-mode="chamber"  title="Echo Chamber — meerdere bronnen">chamber</button>
    <button class="mode-pill"        data-mode="drift"    title="Echo Drift — betekenis vervaagt">drift</button>
    <button class="mode-pill"        data-mode="anti"     title="Anti-echo — de tegenstem">anti</button>
    <button class="mode-pill"        data-mode="canon"    title="Stemmen-canon — koor van unieke stemmen">canon</button>
    <button class="mode-pill"        data-mode="frag"     title="Fragment-echo — twee fragmenten botsen">◈</button>
    <button class="mode-pill"        data-mode="audio"    title="Microfoon — spreek in, hoor een echo">🎙</button>
  </div>

  <!-- Echo dots — centered below controls -->
  <div id="echo-ctrl">
    <span class="echo-lbl">echo</span>
    <button class="echo-dot" data-level="0" title="droog">◌</button>
    <button class="echo-dot" data-level="1" title="licht">◌</button>
    <button class="echo-dot active" data-level="2" title="normaal">◉</button>
    <button class="echo-dot" data-level="3" title="diep">◌</button>
    <span class="echo-lbl" id="echo-val-lbl">normaal</span>
  </div>

  <div id="status">wacht op een woord…</div>
  <div id="sent-hint"></div>
  <div id="freq-badge"></div>
  <div id="ctx-line"></div>

  <!-- Podcast selector — centered below status -->
  <div id="podcast-wrap">
    <button id="podcast-btn">📻 archief</button>
    <div id="podcast-menu">
      <div class="pm-item active" data-id="">alle podcasts</div>
    </div>
  </div>

  <h1>Grafstemming</h1>
</div>

<div id="history"></div>

<!-- How to play -->
<div id="howto">
  <div id="howto-text">
    spreek een woord in het duister<br>
    luister naar zijn echo uit het verleden<br>
    klik de put &nbsp;·&nbsp; tik &nbsp;·&nbsp; of spreek
  </div>
</div>

<audio id="player" style="display:none"></audio>

<!-- Info overlay -->
<div id="info-overlay">
  <div id="info-box">
    <h2>Grafstemming</h2>
    <p class="info-sub">een werk over stem, echo en vergankelijkheid</p>
    <p class="info-myth">
      Echo was een nimf die door Hera werd vervloekt om nooit zelf te spreken —
      alleen de laatste woorden van anderen kon ze herhalen.
      Ze stierf van liefdesverdriet, totdat alleen haar stem overbleef.
      <em>Dit is haar graf.</em>
    </p>
    <div class="info-section">
      <h3>Het archief</h3>
      <p class="info-desc">
        Duizenden podcast-afleveringen zijn gefragmenteerd en geïndexeerd op woord.
        Elk woord dat jij spreekt of typt roept een echo op — een fragment
        uit het verleden, de stem van iemand die dit woord ooit uitsprak.
        Sommige woorden zijn zeldzaam. Andere zijn overal.
      </p>
    </div>
    <div class="info-section">
      <h3>Hoe te spelen</h3>
      <div class="info-row">
        <span class="info-icon">🎙</span>
        <span class="info-desc">Klik de microfoon of de put en spreek een woord</span>
      </div>
      <div class="info-row">
        <span class="info-icon">⌨</span>
        <span class="info-desc">Typ een woord of zin en druk Enter</span>
      </div>
      <div class="info-row">
        <span class="info-icon">≋</span>
        <span class="info-desc">Drie echo's tegelijk — uit drie verschillende afleveringen</span>
      </div>
      <div class="info-row">
        <span class="info-icon">∿</span>
        <span class="info-desc">Auto-mode — de computer roept woorden, jij kan meedoen</span>
      </div>
      <div class="info-row">
        <span class="info-icon">◉</span>
        <span class="info-desc">Echo-intensiteit instellen: van droog tot diep nagalmend</span>
      </div>
    </div>
    <div class="info-section">
      <h3>Scoren</h3>
      <p class="info-desc">
        Zeldzame woorden leveren meer punten op. Lange woorden ook.
        Bouw een streak op voor bonuspunten.
        Woorden die niet in het archief rusten breken de streak.
      </p>
    </div>
    <div class="info-section">
      <h3>Spelmodi</h3>
      <div class="info-row">
        <span class="info-icon">🎵</span>
        <span class="info-desc"><strong>Rijm</strong> — het archief antwoordt met een rijmwoord</span>
      </div>
      <div class="info-row">
        <span class="info-icon">🧠</span>
        <span class="info-desc"><strong>Semantisch / Zin</strong> — zoek op betekenis, niet op exact woord</span>
      </div>
      <div class="info-row">
        <span class="info-icon">🔄</span>
        <span class="info-desc"><strong>Chamber</strong> — hetzelfde woord, meerdere bronnen na elkaar</span>
      </div>
      <div class="info-row">
        <span class="info-icon">🌀</span>
        <span class="info-desc"><strong>Drift</strong> — de echo vervaagt: van dichtbij naar ver weg</span>
      </div>
      <div class="info-row">
        <span class="info-icon">⚡</span>
        <span class="info-desc"><strong>Anti</strong> — de tegenstem, wat de echokamer verzwijgt</span>
      </div>
      <div class="info-row">
        <span class="info-icon">🎙️</span>
        <span class="info-desc"><strong>Canon</strong> — vele stemmen spreken hetzelfde woord als koor</span>
      </div>
    </div>
    <button id="info-close">sluiten</button>
  </div>
</div>

<script>
// ═══════════════════════════════════════════════════════════════════
// PIT RINGS
// ═══════════════════════════════════════════════════════════════════
const pit = document.getElementById('pit');
[92,78,64,51,39,28,18,10].forEach((pct,i) => {
  const r = document.createElement('div');
  r.className = 'ring';
  const b = 60 + i*12;
  r.style.cssText = `width:${pct}%;height:${pct}%;border-color:rgba(40,40,${b},${0.12+i*0.028});animation-delay:${i*0.55}s;animation-duration:${4+i*0.4}s;`;
  pit.insertBefore(r, pit.firstChild);
});

// ═══════════════════════════════════════════════════════════════════
// PARTICLES
// ═══════════════════════════════════════════════════════════════════
const pCont = document.getElementById('particles');
for (let i=0;i<20;i++){
  const p = document.createElement('div');
  p.className='particle';
  const sz=(1+Math.random()*2.5).toFixed(1);
  const dx=((Math.random()-0.5)*90).toFixed(1)+'px';
  const dur=(10+Math.random()*14).toFixed(1);
  const del=(Math.random()*15).toFixed(1);
  p.style.cssText=`left:${(Math.random()*96+2).toFixed(1)}vw;bottom:-5vh;width:${sz}px;height:${sz}px;--dx:${dx};animation-duration:${dur}s;animation-delay:-${del}s;`;
  pCont.appendChild(p);
}

// ═══════════════════════════════════════════════════════════════════
// LOADING POLL
// ═══════════════════════════════════════════════════════════════════
function pollReady(){
  fetch('/echo/ready').then(r=>r.json()).then(d=>{
    if(d.ready){
      document.getElementById('loading-text').textContent=`${d.words.toLocaleString('nl')} stemmen geladen`;
      setTimeout(()=>{
        const l=document.getElementById('loading');
        l.style.opacity='0';
        setTimeout(()=>{l.remove();startGhostVoices();},1200);
      },900);
    } else {
      document.getElementById('loading-text').textContent=d.words>0?`${d.words.toLocaleString('nl')} stemmen…`:'stemmen laden…';
      setTimeout(pollReady,1200);
    }
  }).catch(()=>setTimeout(pollReady,2000));
}
pollReady();

// ═══════════════════════════════════════════════════════════════════
// WEB AUDIO — ambient drone
// ═══════════════════════════════════════════════════════════════════
let audioCtx=null, droneRunning=false, horrorSrc=null, horrorBuf=null;
const soundBtn=document.getElementById('sound-btn');

// Horror muziek prefetchen (asynchroon, niet blokkerend)
function prefetchHorror(){
  fetch('/echo/music/horror2.mp3')
    .then(r=>r.arrayBuffer())
    .then(buf=>{ if(audioCtx){ audioCtx.decodeAudioData(buf,b=>{ horrorBuf=b; startHorrorLoop(); }); } else { horrorBuf={raw:buf}; } })
    .catch(()=>{});
}

function startHorrorLoop(){
  if(!audioCtx||!horrorBuf||horrorBuf.raw) return;
  if(horrorSrc){ try{horrorSrc.stop();}catch(e){} }
  horrorSrc=audioCtx.createBufferSource();
  horrorSrc.buffer=horrorBuf;
  horrorSrc.loop=true;
  const hg=audioCtx.createGain();
  hg.gain.setValueAtTime(0,audioCtx.currentTime);
  hg.gain.linearRampToValueAtTime(0.09,audioCtx.currentTime+5);
  horrorSrc.connect(hg); hg.connect(audioCtx.destination);
  horrorSrc.start();
}

function initDrone(){
  if(audioCtx) return;
  audioCtx = new (window.AudioContext||window.webkitAudioContext)();
  const master = audioCtx.createGain();
  master.gain.setValueAtTime(0,audioCtx.currentTime);
  master.gain.linearRampToValueAtTime(0.05,audioCtx.currentTime+4);
  master.connect(audioCtx.destination);

  // Horror muziek starten/decoderen als buffer al geladen was
  if(horrorBuf && horrorBuf.raw){
    audioCtx.decodeAudioData(horrorBuf.raw, b=>{ horrorBuf=b; startHorrorLoop(); });
  } else if(horrorBuf){
    startHorrorLoop();
  }

  // Three detuned oscillators → beating low drone
  [[55,0.065],[55.4,0.048],[110.1,0.022]].forEach(([freq,vol],i)=>{
    const osc=audioCtx.createOscillator();
    const g=audioCtx.createGain();
    const filt=audioCtx.createBiquadFilter();
    osc.type='sine'; osc.frequency.value=freq;
    filt.type='lowpass'; filt.frequency.value=260+i*50;
    g.gain.value=vol;
    // Slow LFO per oscillator
    const lfo=audioCtx.createOscillator();
    const lfoG=audioCtx.createGain();
    lfo.frequency.value=0.05+i*0.02; lfoG.gain.value=4+i*2.5;
    lfo.connect(lfoG); lfoG.connect(osc.frequency); lfo.start();
    osc.connect(filt); filt.connect(g); g.connect(master);
    osc.start();
  });

  // Cave breath: filtered white noise (no tone, just air texture)
  function makeNoiseBuffer(){
    const buf=audioCtx.createBuffer(1,audioCtx.sampleRate*3,audioCtx.sampleRate);
    const d=buf.getChannelData(0);
    for(let i=0;i<d.length;i++) d[i]=Math.random()*2-1;
    return buf;
  }
  const noiseLoop=audioCtx.createBufferSource();
  noiseLoop.buffer=makeNoiseBuffer();
  noiseLoop.loop=true;
  const noiseFiltLo=audioCtx.createBiquadFilter();
  noiseFiltLo.type='lowpass'; noiseFiltLo.frequency.value=320;
  const noiseFiltHi=audioCtx.createBiquadFilter();
  noiseFiltHi.type='highpass'; noiseFiltHi.frequency.value=60;
  const noiseGain=audioCtx.createGain(); noiseGain.gain.value=0.018;
  noiseLoop.connect(noiseFiltHi); noiseFiltHi.connect(noiseFiltLo);
  noiseFiltLo.connect(noiseGain); noiseGain.connect(master);
  noiseLoop.start();

  droneRunning=true;
  soundBtn.classList.add('active');

  // Occasional deep rumble
  function rumble(){
    if(!droneRunning||!audioCtx) return;
    const n=audioCtx.createOscillator();
    const nG=audioCtx.createGain();
    const nF=audioCtx.createBiquadFilter();
    n.type='sine'; n.frequency.value=32+Math.random()*22;
    nF.type='lowpass'; nF.frequency.value=90;
    nG.gain.setValueAtTime(0,audioCtx.currentTime);
    nG.gain.linearRampToValueAtTime(0.07,audioCtx.currentTime+2.0);
    nG.gain.linearRampToValueAtTime(0,audioCtx.currentTime+6);
    n.connect(nF); nF.connect(nG); nG.connect(master);
    n.start(); n.stop(audioCtx.currentTime+6.5);
    // Drip echo
    const drip=audioCtx.createOscillator();
    const dG=audioCtx.createGain();
    drip.type='sine'; drip.frequency.setValueAtTime(420,audioCtx.currentTime);
    drip.frequency.exponentialRampToValueAtTime(80,audioCtx.currentTime+0.6);
    dG.gain.setValueAtTime(0.03,audioCtx.currentTime);
    dG.gain.linearRampToValueAtTime(0,audioCtx.currentTime+0.7);
    drip.connect(dG); dG.connect(master);
    drip.start(); drip.stop(audioCtx.currentTime+0.8);
    setTimeout(rumble,18000+Math.random()*22000);
  }
  setTimeout(rumble,6000);
}

function toggleDrone(){
  if(!droneRunning){
    initDrone();
  } else {
    droneRunning=false;
    soundBtn.classList.remove('active');
    if(horrorSrc){ try{horrorSrc.stop();}catch(e){} horrorSrc=null; }
    if(audioCtx){ audioCtx.close(); audioCtx=null; }
  }
}
soundBtn.addEventListener('click',()=>{ toggleDrone(); });

// Auto-init drone on first interaction
let droneInited=false;
function ensureDrone(){
  if(droneInited) return;
  droneInited=true;
  initDrone();
}
document.addEventListener('click',ensureDrone,{once:true});
document.addEventListener('keydown',ensureDrone,{once:true});

// Prefetch horror muziek zodra pagina geladen is (niet blokkeren)
setTimeout(prefetchHorror, 1500);

// ═══════════════════════════════════════════════════════════════════
// GHOST VOICES
// ═══════════════════════════════════════════════════════════════════
let ghostActive=true;
// Korte zinnen voor fluister-modus
const ghostPhrases=[
  'hoor je dat','wie spreekt daar','ben jij het','ik was hier','kom terug',
  'alles vergaat','luister goed','ze zijn hier','ik hoor je','de echo wacht'
];

function startGhostVoices(){
  function next(){
    const delay=ghostActive ? 1500+Math.random()*3000 : 5000;
    setTimeout(async()=>{
      // Altijd doorgaan, ook als dit rondje mislukt of index rebuildt
      try{ await doOneGhost(); }catch(e){}
      next();
    }, delay);
  }

  async function doOneGhost(){
    if(!ghostActive) return;

    const roll=Math.random();
    let buf=null, displayText='';

    if(roll < 0.40){
      // 40%: tussenruimte-audio (hmm/lach/adem) — geen tekst weergeven
      const r=await fetch(`/echo/gap?echo=${echoLevel}`);
      if(!r.ok) return;
      buf=await r.arrayBuffer();
      displayText='…';
    } else if(roll < 0.60){
      // 25%: korte Nederlandstalige spookzin
      const phrase=ghostPhrases[Math.floor(Math.random()*ghostPhrases.length)];
      const r=await fetch(`/echo/api?word=${encodeURIComponent(phrase)}&echo=${echoLevel}`);
      if(!r.ok) return;
      displayText=r.headers.get('X-Word-Matched')||phrase;
      buf=await r.arrayBuffer();
    } else {
      // 40%: zeldzaam woord uit het archief
      const rnd=await fetch('/echo/random').then(r=>r.ok?r.json():null);
      if(!rnd||!rnd.word) return;
      const r=await fetch(`/echo/api?word=${encodeURIComponent(rnd.word)}&echo=${echoLevel}`);
      if(!r.ok) return;
      displayText=r.headers.get('X-Word-Matched')||rnd.word;
      buf=await r.arrayBuffer();
    }

    if(!audioCtx||!buf) return;
    // Fluistereffect: bandpass + heel zacht volume
    const decoded=await audioCtx.decodeAudioData(buf);
    const src=audioCtx.createBufferSource();
    src.buffer=decoded;
    src.playbackRate.value=0.80+Math.random()*0.22;
    const bp=audioCtx.createBiquadFilter();
    bp.type='bandpass'; bp.frequency.value=800+Math.random()*600; bp.Q.value=0.45;
    const g=audioCtx.createGain();
    g.gain.value=0.05+Math.random()*0.04;
    src.connect(bp); bp.connect(g); g.connect(audioCtx.destination);
    src.start();
    if(displayText && displayText!=='…') showGhostWord(displayText);
  }

  next();
}

function showGhostWord(word){
  const el=document.createElement('div');
  el.className='ghost-word';
  el.textContent=word;
  const pitR=document.getElementById('pit-wrap').getBoundingClientRect();
  const cx=pitR.left+pitR.width/2, cy=pitR.top+pitR.height/2;
  const angle=Math.random()*Math.PI*2;
  const dist=pitR.width*0.6+Math.random()*pitR.width*0.35;
  el.style.cssText+=`left:${cx+Math.cos(angle)*dist}px;top:${cy+Math.sin(angle)*dist*0.65}px;`;
  document.body.appendChild(el);
  setTimeout(()=>el.remove(),7200);
}

// ═══════════════════════════════════════════════════════════════════
// SPEECH RECOGNITION
// ═══════════════════════════════════════════════════════════════════
const SR=window.SpeechRecognition||window.webkitSpeechRecognition;
let rec=null, listening=false;
const micBtn=document.getElementById('mic-btn');

if(SR){
  rec=new SR();
  rec.lang='nl-NL';
  rec.interimResults=false;
  rec.maxAlternatives=1;
  rec.onresult=e=>{
    stopListening();
    const q=e.results[0][0].transcript.trim();
    if(q) sendQuery(q);
  };
  rec.onerror=()=>{stopListening();setStatus('niet verstaan — probeer opnieuw');};
  rec.onend=()=>stopListening();
}else{
  micBtn.style.opacity='0.3';
  micBtn.title='Niet beschikbaar in deze browser';
}

function startListening(){
  if(!rec) return;
  ensureDrone();
  listening=true;
  micBtn.classList.add('listening');
  setStatus('luisteren…');
  addRipple('listen');
  try{rec.start();}catch(e){stopListening();}
}
function stopListening(){
  listening=false;
  micBtn.classList.remove('listening');
}
micBtn.addEventListener('click',()=>{ listening? (rec?.stop(),stopListening()) : startListening(); });

// ═══════════════════════════════════════════════════════════════════
// INPUT
// ═══════════════════════════════════════════════════════════════════
const input=document.getElementById('word-input');
input.addEventListener('keydown',e=>{if(e.key==='Enter'){submit();}});
document.getElementById('send-btn').addEventListener('click',()=>{submit();});
pit.addEventListener('click',()=>{ if(!listening) startListening(); });

function submit(){
  const q=input.value.trim();
  input.value='';
  // Frag-modus heeft geen tekst nodig — Enter triggert direct een fragment-echo
  if(currentMode==='frag'){ sendFragEchoMode(); return; }
  if(q) sendModeQuery(q);
}

// ═══════════════════════════════════════════════════════════════════
// WHOOSH EFFECT
// ═══════════════════════════════════════════════════════════════════
function playWhoosh(){
  if(!audioCtx) return;
  const dur=0.65;
  const buf=audioCtx.createBuffer(1,Math.floor(audioCtx.sampleRate*dur),audioCtx.sampleRate);
  const d=buf.getChannelData(0);
  for(let i=0;i<d.length;i++) d[i]=(Math.random()*2-1)*(1-i/d.length)*0.9;
  const src=audioCtx.createBufferSource(); src.buffer=buf;
  const filt=audioCtx.createBiquadFilter();
  filt.type='bandpass';
  filt.frequency.setValueAtTime(180,audioCtx.currentTime);
  filt.frequency.linearRampToValueAtTime(2800,audioCtx.currentTime+0.35);
  filt.Q.value=0.7;
  const g=audioCtx.createGain();
  g.gain.setValueAtTime(0.18,audioCtx.currentTime);
  g.gain.linearRampToValueAtTime(0,audioCtx.currentTime+dur);
  src.connect(filt); filt.connect(g); g.connect(audioCtx.destination);
  src.start();
}

// ═══════════════════════════════════════════════════════════════════
// CONTEXT DISPLAY
// ═══════════════════════════════════════════════════════════════════
function showContext(before,word,after){
  const el=document.getElementById('ctx-line');
  if(!before&&!after){el.textContent='';return;}
  const b=before?before.trim()+' ':'';
  const a=after?' '+after.trim():'';
  el.innerHTML=`${b}<span class="ctx-hl">${word}</span>${a}`;
  el.style.opacity='0';
  setTimeout(()=>el.style.opacity='1',120);
}

// ═══════════════════════════════════════════════════════════════════
// MULTI-ECHO MODE
// ═══════════════════════════════════════════════════════════════════
let multiMode=false;
const multiBtn=document.getElementById('multi-btn');
multiBtn.addEventListener('click',()=>{
  multiMode=!multiMode;
  multiBtn.classList.toggle('active',multiMode);
  multiBtn.title=multiMode?'Drie echo\'s aan (klik om uit te zetten)':'Drie echo\'s tegelijk';
});

// ═══════════════════════════════════════════════════════════════════
// AUTO-MODE
// ═══════════════════════════════════════════════════════════════════
const autoWords={
  common:['dood','graf','stem','echo','tijd','vergeten','nacht','aarde','stof','woord','herinnering','stilte','vuur','water','licht','stem','rust','diepte','grond','as'],
  original:['vergankelijk','onderaards','weergalm','eeuwigheid','schaduw','fluistering','asem','resonantie','verweer','grensgebied','dageraad','verstomming','galm','afgrond','schemer']
};
let autoMode=false, autoTimer=null;
const autoBtn=document.getElementById('auto-btn');

autoBtn.addEventListener('click',()=>{
  autoMode=!autoMode;
  autoBtn.classList.toggle('active',autoMode);
  autoBtn.title=autoMode?'Auto-mode aan (klik om uit te zetten)':'Auto-mode: computer roept woorden';
  if(autoMode) scheduleAutoWord();
  else{ clearTimeout(autoTimer); autoTimer=null; }
});

function scheduleAutoWord(){
  if(!autoMode) return;
  autoTimer=setTimeout(autoTypeWord, 8000+Math.random()*7000);
}

async function autoTypeWord(){
  if(!autoMode) return;
  // Frag-modus: sla woordtypen over, fire direct fragment-echo
  if(currentMode==='frag'){
    await sendFragEchoMode();
    scheduleAutoWord();
    return;
  }
  const inp=document.getElementById('word-input');
  // Sla over als gebruiker al aan het typen is
  if(inp.value.trim().length>0){ scheduleAutoWord(); return; }
  const pool=Math.random()<0.55 ? autoWords.common : autoWords.original;
  const word=pool[Math.floor(Math.random()*pool.length)];
  inp.value='';
  for(let i=0;i<word.length;i++){
    inp.value+=word[i];
    await new Promise(r=>setTimeout(r,55+Math.random()*55));
  }
  await new Promise(r=>setTimeout(r,620));
  if(autoMode){ submit(); scheduleAutoWord(); }
}

// Auto-mode stopt NIET bij gebruikersinput — player-vs-computer modus

// ═══════════════════════════════════════════════════════════════════
// ECHO LEVEL
// ═══════════════════════════════════════════════════════════════════
let echoLevel=2;
const echoLabels=['droog','licht','normaal','diep'];
const echoValLbl=document.getElementById('echo-val-lbl');
document.querySelectorAll('.echo-dot').forEach(btn=>{
  btn.addEventListener('click',()=>{
    echoLevel=parseInt(btn.dataset.level);
    document.querySelectorAll('.echo-dot').forEach(b=>{
      b.classList.toggle('active', parseInt(b.dataset.level)===echoLevel);
      b.textContent=parseInt(b.dataset.level)===echoLevel?'◉':'◌';
    });
    echoValLbl.textContent=echoLabels[echoLevel];
  });
});

// ═══════════════════════════════════════════════════════════════════
// SPELMODI
// ═══════════════════════════════════════════════════════════════════
let currentMode='word';
let _micPrevMode='word';   // modus vóór mic — zodat mic rijm/woord etc. onthouden wordt
const modeMsgs={
  word:'wacht op een woord…', rhyme:'rijmwoord verwacht…',
  semantic:'concept of idee…', sentence:'spreek een zin…',
  chamber:'woord galmt van alle kanten…', drift:'concept vervaagt in de echo…',
  anti:'zoek de tegenstem…', canon:'stemmen als koor…',
  frag:'fragment-echo…', audio:'druk op Enter om op te nemen…'
};

document.querySelectorAll('.mode-pill').forEach(btn=>{
  btn.addEventListener('click',()=>{
    const newMode=btn.dataset.mode;
    // Microfoon-pill: sla huidige modus op zodat rijm/woord etc. bewaard blijft
    if(newMode==='audio' && currentMode!=='audio') _micPrevMode=currentMode;
    currentMode=newMode;
    document.querySelectorAll('.mode-pill').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
    setStatus(modeMsgs[currentMode]||'wacht op een woord…');
    setSentHint('');
    // Frag-modus: meteen een fragment-echo triggeren
    if(currentMode==='frag') sendFragEchoMode();
  });
});

// ═══════════════════════════════════════════════════════════════════
// FRAG MODE: random fragment → verwant fragment echoot terug
// ═══════════════════════════════════════════════════════════════════
async function sendFragEchoMode(){
  if(_activeCtrl){ _activeCtrl.abort(); }
  _activeCtrl=new AbortController();
  ensureDrone();
  playWhoosh();

  setStatus('fragment ophalen…');
  setSentHint('');
  addRipple('fall');

  try{
    // 1. Random fragment ophalen (Gemini cache als beschikbaar)
    const fragResp=await fetch('/echo/randomfrag',{signal:_activeCtrl.signal});
    if(!fragResp.ok){ handleNotFound('fragment'); return; }
    const frag=await fragResp.json();

    const firstWord=(frag.text||'◈').split(' ')[0];
    wordFall(firstWord);
    setStatus('fragmenten koppelen…');

    const enc=encodeURIComponent;
    const qp=`audio_url=${enc(frag.audio_url)}&start_ms=${frag.start_ms}&end_ms=${frag.end_ms}`;

    // 2. Bron-audio + echo-audio parallel ophalen
    const [srcResp,echoResp]=await Promise.all([
      fetch(`/echo/fragsound?${qp}&echo=0`,{signal:_activeCtrl.signal}),
      fetch(`/echo/fragecho?${qp}&mode=similar&echo=${echoLevel}`,{signal:_activeCtrl.signal}),
    ]);
    if(!srcResp.ok||!echoResp.ok){ handleNotFound('fragment'); return; }

    const echoTextHdr=echoResp.headers.get('X-Fragment-Text')||'';
    const similarity=echoResp.headers.get('X-Similarity')||'';
    const [srcBlob,echoBlob]=await Promise.all([srcResp.blob(),echoResp.blob()]);

    // Status
    if(similarity)
      setStatus(`fragment-echo — ${Math.round(parseFloat(similarity)*100)}% verwant`);
    else
      setStatus('fragment-echo');

    if(frag.text) setSentHint(frag.text.slice(0,80));

    addToHistory('◈');
    addPoints('◈',0);

    // 3. Speel bron, dan — na korte pauze — de echo
    const srcUrl=URL.createObjectURL(srcBlob);
    const echoUrl=URL.createObjectURL(echoBlob);
    const srcAudio=new Audio(srcUrl);
    const echoAudio=new Audio(echoUrl);
    srcAudio.volume=0.80;
    echoAudio.volume=1.0;

    srcAudio.play().catch(()=>{});
    addRipple('fall');

    srcAudio.onended=()=>{
      URL.revokeObjectURL(srcUrl);
      setTimeout(()=>{
        // Echo-fragment tekst tonen
        if(echoTextHdr){
          try{ setSentHint(decodeURIComponent(echoTextHdr)); }catch(e){}
        }
        const echoFirst=(echoTextHdr
          ? decodeURIComponent(echoTextHdr).split(' ')[0]
          : '◈');
        wordEcho(echoFirst);
        addRipple('echo');
        addRipple('echo',380);
        echoAudio.play().catch(()=>{});
      },350);
    };
    echoAudio.onended=()=>{
      URL.revokeObjectURL(echoUrl);
      setStatus('fragment-echo klaar');
      setTimeout(wordVanish,1200);
    };

  }catch(e){
    if(e.name==='AbortError') return;
    setStatus('geen verbinding'); wordVanish();
  }
}

// ═══════════════════════════════════════════════════════════════════
// AUDIO MODE: microfoon opname → multimodale echo
// ═══════════════════════════════════════════════════════════════════
let _audioRecorder=null;
let _audioChunks=[];
let _audioStream=null;
let _audioRecording=false;

async function sendAudioQuery(){
  // Als al aan het opnemen: stop en stuur
  if(_audioRecording && _audioRecorder && _audioRecorder.state==='recording'){
    _audioRecorder.stop();
    return;
  }
  if(_activeCtrl){ _activeCtrl.abort(); }
  _activeCtrl=new AbortController();
  ensureDrone();
  playWhoosh();

  try{
    _audioStream=await navigator.mediaDevices.getUserMedia({audio:true,video:false});
  }catch(e){
    setStatus('microfoon niet beschikbaar'); wordVanish(); return;
  }

  _audioChunks=[];
  const mime=MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
    ?'audio/webm;codecs=opus':'audio/webm';
  _audioRecorder=new MediaRecorder(_audioStream,{mimeType:mime});
  _audioRecorder.ondataavailable=e=>{ if(e.data.size>0) _audioChunks.push(e.data); };
  _audioRecorder.onstop=async()=>{
    _audioRecording=false;
    _audioStream.getTracks().forEach(t=>t.stop());
    const blob=new Blob(_audioChunks,{type:mime});
    document.querySelector('.mode-pill[data-mode="audio"]').textContent='🎙';
    await _submitAudioToServer(blob);
  };
  _audioRecorder.start(200); // 200ms chunks
  _audioRecording=true;
  document.querySelector('.mode-pill[data-mode="audio"]').textContent='⏹';
  wordFall('🎙');
  addRipple('fall');

  let remaining=6;
  setStatus(`opnemen… (${remaining}s — Enter om te stoppen)`);
  const countdown=setInterval(()=>{
    remaining--;
    if(remaining<=0){ clearInterval(countdown); return; }
    setStatus(`opnemen… (${remaining}s — Enter om te stoppen)`);
  },1000);

  // Auto-stop na 6 seconden
  setTimeout(()=>{
    clearInterval(countdown);
    if(_audioRecorder&&_audioRecorder.state==='recording') _audioRecorder.stop();
  },6000);
}

async function _submitAudioToServer(blob){
  setStatus('geluid analyseren…');
  try{
    const arrayBuf=await blob.arrayBuffer();
    // Stuur de vorige modus mee zodat rijm/woord/semantisch via mic werken
    const micMode=_micPrevMode||'audio';
    const r=await fetch(`/echo/audiomatch?echo=${echoLevel}&mode=${encodeURIComponent(micMode)}`,{
      method:'POST',
      headers:{'Content-Type':blob.type||'audio/webm'},
      body:arrayBuf,
      signal:_activeCtrl.signal,
    });
    if(!r.ok){ handleNotFound('geluid'); return; }
    const fragmentText=r.headers.get('X-Fragment-Text')||'';
    const rhymeWord   =r.headers.get('X-Rhyme-Word')||'';
    const wordMatched =r.headers.get('X-Word-Matched')||'';
    const transcript  =r.headers.get('X-Transcript')||'';
    const similarity  =r.headers.get('X-Similarity')||'';
    const audioBlob=await r.blob();
    // Status tonen op basis van modus
    if(rhymeWord)
      setStatus(`rijm: ${rhymeWord}`);
    else if(wordMatched)
      setStatus(`woord: ${wordMatched}`);
    else if(similarity)
      setStatus(`geluidsverwantschap: ${Math.round(parseFloat(similarity)*100)}%`);
    else
      setStatus('echo van jouw geluid');
    // Transcript tonen als hint
    const hintTxt=fragmentText||rhymeWord||wordMatched||'';
    if(hintTxt){ try{ setSentHint(decodeURIComponent(hintTxt)); }catch(e){} }
    const displayWord=rhymeWord||wordMatched||(transcript?decodeURIComponent(transcript).split(' ')[0]:'🎙');
    addToHistory('🎙');
    addPoints('🎙',0);
    playEcho(audioBlob, displayWord);
  }catch(e){
    if(e.name==='AbortError') return;
    setStatus('geen verbinding'); wordVanish();
  }
}

async function sendModeQuery(query){
  if(currentMode==='word'){ sendQuery(query); return; }
  if(currentMode==='frag'){ await sendFragEchoMode(); return; }
  if(currentMode==='audio'){ await sendAudioQuery(); return; }

  if(_activeCtrl){ _activeCtrl.abort(); }
  _activeCtrl=new AbortController();
  ensureDrone();
  playWhoosh();

  const q=encodeURIComponent(query);
  const modeUrls={
    rhyme:    `/echo/rhyme?word=${q}&echo=${echoLevel}`,
    semantic: `/echo/semantic?query=${q}&echo=${echoLevel}`,
    sentence: `/echo/sentence?query=${q}&echo=${echoLevel}`,
    chamber:  `/echo/chamber?word=${q}&echo=${echoLevel}`,
    drift:    `/echo/drift?query=${q}&echo=${echoLevel}`,
    anti:     `/echo/anti?query=${q}&echo=${echoLevel}`,
    canon:    `/echo/canon?word=${q}&n=5&echo=${echoLevel}`,
  };
  const url=modeUrls[currentMode];
  if(!url){ sendQuery(query); return; }

  const modeLoadMsgs={
    rhyme:'rijmwoord zoeken…', semantic:'betekenis doorzoeken…',
    sentence:'zin-echo zoeken…', chamber:'alle bronnen raadplegen…',
    drift:'echo drift berekenen…', anti:'tegenstem zoeken…', canon:'koor samenstellen…',
    audio:'geluid analyseren…'
  };
  setStatus(modeLoadMsgs[currentMode]||'zoeken…');
  wordFall(query.split(' ')[0]);
  addRipple('fall');

  try{
    const r=await fetch(url,{signal:_activeCtrl.signal});
    if(!r.ok){ handleNotFound(query); return; }

    const rhymeWord   =r.headers.get('X-Rhyme-Word')||'';
    const fragmentText=r.headers.get('X-Fragment-Text')||'';
    const chamberSrcs =r.headers.get('X-Chamber-Sources')||'';
    const driftLayers =r.headers.get('X-Drift-Layers')||'';
    const antiSim     =r.headers.get('X-Anti-Similarity')||'';
    const canonVoices =r.headers.get('X-Canon-Voices')||'';
    const wordMatched =r.headers.get('X-Word-Matched')||query;
    const similarity  =r.headers.get('X-Similarity')||'';

    const blob=await r.blob();

    if(currentMode==='rhyme'&&rhymeWord)
      setStatus(`rijmt: '${query}' → '${rhymeWord}'`);
    else if((currentMode==='semantic'||currentMode==='sentence')&&similarity)
      setStatus(`verwantschap: ${Math.round(parseFloat(similarity)*100)}%`);
    else if(currentMode==='chamber'&&chamberSrcs)
      setStatus(`${chamberSrcs} bronnen weerkaatsen '${wordMatched}'`);
    else if(currentMode==='drift'&&driftLayers)
      setStatus(`echo drift — ${driftLayers} lagen`);
    else if(currentMode==='anti'&&antiSim)
      setStatus(`tegenstem — ${Math.round(parseFloat(antiSim)*100)}% verwant`);
    else if(currentMode==='canon'&&canonVoices)
      setStatus(`${canonVoices} stemmen als koor`);
    else
      setStatus(`echo van '${wordMatched}'`);

    if(fragmentText){
      try{ setSentHint(decodeURIComponent(fragmentText)); }
      catch(e){ setSentHint(''); }
    }

    const displayWord=rhymeWord||wordMatched||query;
    addToHistory(displayWord);
    addPoints(displayWord,0);
    playEcho(blob,displayWord);
  }catch(e){
    if(e.name==='AbortError') return;
    setStatus('geen verbinding');
    wordVanish();
  }
}

// ═══════════════════════════════════════════════════════════════════
// CORE: send query
// ═══════════════════════════════════════════════════════════════════
let currentUrl=null;
let _activeCtrl=null; // AbortController voor in-flight requests

function sendQuery(query){
  // Annuleer vorige request (lost ook streak-bug op: elke submit registreert nu)
  if(_activeCtrl){ _activeCtrl.abort(); }
  _activeCtrl=new AbortController();

  ensureDrone();
  playWhoosh();

  const words=query.trim().split(/\s+/);
  const isPhrase=words.length>1;

  setStatus(isPhrase?`zin ontvangen — diepste echo zoeken…`:`zoeken naar '${query}'…`);
  setSentHint('');
  document.getElementById('freq-badge').textContent='';
  document.getElementById('ctx-line').textContent='';
  wordFall(isPhrase?query.split(' ')[0]:query);
  addRipple('fall');

  if(multiMode){
    sendMultiQuery(query, isPhrase, _activeCtrl.signal);
    return;
  }

  // Willekeurige context-variant (0=kort, 1=middel, 2=lang, 3=extra lang)
  const variant=Math.floor(Math.random()*4);

  fetch(`/echo/api?word=${encodeURIComponent(query)}&v=${variant}&echo=${echoLevel}`,
        {signal:_activeCtrl.signal})
    .then(r=>{
      if(r.status===503){setStatus('even geduld…');wordVanish();return null;}
      const freq=parseInt(r.headers.get('X-Word-Frequency')||'0');
      const matched=r.headers.get('X-Word-Matched')||query;
      const ctxB=decodeURIComponent(r.headers.get('X-Context-Before')||'');
      const ctxA=decodeURIComponent(r.headers.get('X-Context-After')||'');
      if(!r.ok){
        handleNotFound(query);
        return null;
      }
      return r.blob().then(b=>[b,freq,matched,ctxB,ctxA]);
    })
    .then(res=>{
      if(!res) return;
      const [blob,freq,matched,ctxB,ctxA]=res;
      if(isPhrase && matched!==query.toLowerCase()){
        setSentHint(`diepste echo: '${matched}'`);
      }
      addToHistory(matched);
      setStatus(`echo van '${matched}'`);
      addPoints(matched, freq);
      showContext(ctxB, matched, ctxA);
      playEcho(blob, matched);
    })
    .catch(e=>{
      if(e.name==='AbortError') return; // nieuwe query geannuleerd vorige
      setStatus('geen verbinding');
      wordVanish();
    });
}

async function sendMultiQuery(query, isPhrase, signal){
  try{
    // Haal 3 varianten op tegelijk — elke variant uit een ANDERE podcast-aflevering
    const fetches=await Promise.allSettled([0,1,2].map(v=>
      fetch(`/echo/api?word=${encodeURIComponent(query)}&v=${v}&echo=${echoLevel}`,{signal})
        .then(r=>{
          if(!r.ok) return null;
          const freq=parseInt(r.headers.get('X-Word-Frequency')||'0');
          const matched=r.headers.get('X-Word-Matched')||query;
          const ctxB=decodeURIComponent(r.headers.get('X-Context-Before')||'');
          const ctxA=decodeURIComponent(r.headers.get('X-Context-After')||'');
          return r.blob().then(b=>({blob:b,freq,matched,ctxB,ctxA}));
        })
    ));
    const raw=fetches.filter(f=>f.status==='fulfilled'&&f.value).map(f=>f.value);
    // Dedup op blob-grootte: zelfde grootte (±600 bytes) = zelfde geluidsfragment
    const results=[];
    for(const r of raw){
      if(!results.some(x=>Math.abs(x.blob.size-r.blob.size)<600)) results.push(r);
    }
    if(!results.length){ handleNotFound(query); return; }

    const {freq,matched,ctxB,ctxA}=results[0];
    addToHistory(matched);
    setStatus(`${results.length}× echo van '${matched}'`);
    addPoints(matched, freq);
    showContext(ctxB, matched, ctxA);
    if(isPhrase && matched!==query.toLowerCase()) setSentHint(`diepste echo: '${matched}'`);

    // Speel ze met staggering af: 0s, 1.4s, 3.2s — elk zachter + licht anders
    const delays=[0, 1400, 3200];
    results.forEach((res,i)=>{
      const url=URL.createObjectURL(res.blob);
      const a=new Audio(url);
      a.volume=Math.max(0.15, 1.0-i*0.25);
      a.playbackRate=0.95+i*0.03;
      setTimeout(()=>{
        a.play().catch(()=>{});
        addRipple('echo', 0);
        addRipple('echo', 450);
        if(i===0) wordEcho(matched);
      }, delays[i]);
      a.onended=()=>URL.revokeObjectURL(url);
    });
  }catch(e){
    if(e.name==='AbortError') return;
    setStatus('geen verbinding');
    wordVanish();
  }
}

function handleNotFound(query){
  breakStreak();
  setStatus(`'${query}' rust niet in dit graf`);
  addRipple('miss');
  wordVanish();
}

// ═══════════════════════════════════════════════════════════════════
// PIT WORD ANIMATION
// ═══════════════════════════════════════════════════════════════════
const pitWord=document.getElementById('pit-word');

function wordFall(word){
  pitWord.textContent=word;
  pitWord.style.transition='none';
  pitWord.style.opacity='1';
  pitWord.style.transform='translate(-50%,-50%) scale(1)';
  requestAnimationFrame(()=>requestAnimationFrame(()=>{
    pitWord.style.transition='all 1.8s cubic-bezier(0.5,0,0.8,1)';
    pitWord.style.opacity='0.08';
    pitWord.style.transform='translate(-50%,-50%) scale(0.12)';
  }));
}
function wordEcho(word){
  pitWord.textContent=word;
  pitWord.style.transition='none';
  pitWord.style.opacity='0.06';
  pitWord.style.transform='translate(-50%,-50%) scale(0.1)';
  requestAnimationFrame(()=>requestAnimationFrame(()=>{
    pitWord.style.transition='all 1.3s cubic-bezier(0.1,0,0.4,1)';
    pitWord.style.opacity='0.92';
    pitWord.style.transform='translate(-50%,-50%) scale(1.08)';
  }));
  setTimeout(()=>{
    pitWord.style.transition='opacity 2s';
    pitWord.style.opacity='0';
  },3800);
}
function wordVanish(){
  pitWord.style.transition='opacity 0.6s';
  pitWord.style.opacity='0';
}

// ═══════════════════════════════════════════════════════════════════
// AUDIO PLAYBACK
// ═══════════════════════════════════════════════════════════════════
const player=document.getElementById('player');

function playEcho(blob,word){
  if(currentUrl) URL.revokeObjectURL(currentUrl);
  currentUrl=URL.createObjectURL(blob);
  player.src=currentUrl;
  setTimeout(()=>{
    wordEcho(word);
    addRipple('echo');
    addRipple('echo',380);
    addRipple('echo',760);
  },320);
  player.play().catch(()=>{});
  player.onended=()=>{
    setStatus('wacht op een woord…');
    setTimeout(wordVanish,1200);
  };
}

// ═══════════════════════════════════════════════════════════════════
// RIPPLES
// ═══════════════════════════════════════════════════════════════════
function addRipple(type,delay=0){
  setTimeout(()=>{
    const el=document.createElement('div');
    el.className='ripple';
    const c={listen:'rgba(180,60,60,0.5)',fall:'rgba(70,70,150,0.45)',echo:'rgba(90,90,230,0.55)',miss:'rgba(150,40,40,0.4)'};
    el.style.cssText=`width:16%;height:16%;border-color:${c[type]||c.echo};`;
    pit.appendChild(el);
    setTimeout(()=>el.remove(),2700);
  },delay);
}

// ═══════════════════════════════════════════════════════════════════
// SCORING
// ═══════════════════════════════════════════════════════════════════
let score=0, streak=0;
let highScore=parseInt(localStorage.getItem('grafstemming_hs')||'0');

function calcPoints(word,freq){
  const rarity = freq>0 ? Math.max(1, Math.round(500/Math.log10(freq+2))) : 100;
  const len    = Math.max(0,(word.length-3)*3);
  const strk   = Math.floor(streak*2);
  return {rarity,len,strk,total:rarity+len+strk};
}

function addPoints(word,freq){
  const {rarity,len,strk,total}=calcPoints(word,freq);
  score+=total; streak++;
  if(score>highScore){ highScore=score; localStorage.setItem('grafstemming_hs',highScore); }
  updateScoreUI();
  // Breakdown popup
  const lines=[];
  if(rarity>=50)  lines.push(`⚰ Zeldzaam woord +${rarity}`);
  else             lines.push(`Woord +${rarity}`);
  if(len>0)        lines.push(`📏 Lang woord +${len}`);
  if(strk>0)       lines.push(`🔥 Streak bonus +${strk}`);
  showPtsPopup(`+${total}`, lines);
  // Freq badge
  const badge=document.getElementById('freq-badge');
  if(freq>0) badge.textContent=`${freq}× in archief`;
  else badge.textContent='';
}

function breakStreak(){
  if(streak>0) showPtsPopup(`💀 streak ${streak}× gebroken`,['neg']);
  streak=0; updateScoreUI();
}

function updateScoreUI(){
  document.getElementById('score-val').textContent=score;
  document.getElementById('streak-val').textContent=streak>1?`🔥 ${streak}× streak`:'';
  document.getElementById('highscore-val').textContent=highScore>0?`best: ${highScore}`:'';
}

function showPtsPopup(total,lines=[]){
  const el=document.createElement('div');
  // lines kan ['neg'] zijn (class) of echte breakdown-regels
  const isNeg=lines.length===1&&lines[0]==='neg';
  el.className='pts-popup'+(isNeg?' neg':'');
  const breakdown=isNeg?[]:lines;
  el.innerHTML=`<div>${total}</div>`+(breakdown.length?`<div class="pts-breakdown">${breakdown.join('<br>')}</div>`:'');
  const pitR=document.getElementById('pit-wrap').getBoundingClientRect();
  const cx=pitR.left+pitR.width/2, cy=pitR.top+pitR.height/2;
  el.style.cssText=`top:${cy}px;left:${cx}px;`;
  document.body.appendChild(el);
  setTimeout(()=>el.remove(),4000);
}

// ═══════════════════════════════════════════════════════════════════
// STATUS + HINT
// ═══════════════════════════════════════════════════════════════════
function setStatus(text){
  const el=document.getElementById('status');
  el.style.opacity='0';
  setTimeout(()=>{el.textContent=text;el.style.opacity='1';},200);
}
function setSentHint(text){
  document.getElementById('sent-hint').textContent=text;
}

// ═══════════════════════════════════════════════════════════════════
// HISTORY
// ═══════════════════════════════════════════════════════════════════
const histEl=document.getElementById('history');
function addToHistory(word){
  const s=document.createElement('span');
  s.className='h-word'; s.textContent=word;
  histEl.appendChild(s);
  while(histEl.children.length>9) histEl.removeChild(histEl.firstChild);
  Array.from(histEl.children).forEach((el,i,a)=>{
    el.style.opacity=(0.1+(i/a.length)*0.5).toFixed(2);
  });
}

// ═══════════════════════════════════════════════════════════════════
// PODCAST SELECTOR
// ═══════════════════════════════════════════════════════════════════
const podBtn=document.getElementById('podcast-btn');
const podMenu=document.getElementById('podcast-menu');
let activePodcastId='', activePodcastLabel='alle podcasts';

podBtn.addEventListener('click',(e)=>{
  e.stopPropagation();
  podMenu.classList.toggle('open');
});
document.addEventListener('click',()=>podMenu.classList.remove('open'));

// Laad podcast-lijst
fetch('/echo/podcasts').then(r=>r.json()).then(list=>{
  list.slice(0,14).forEach(p=>{
    const d=document.createElement('div');
    d.className='pm-item';
    d.dataset.id=p.id;
    d.textContent=`${p.title} (${(p.frags/1000).toFixed(1)}K)`;
    d.title=p.title;
    d.addEventListener('click',()=>selectPodcast(p.id, p.title));
    podMenu.appendChild(d);
  });
}).catch(()=>{});

function selectPodcast(id, label){
  podMenu.classList.remove('open');
  activePodcastId=id; activePodcastLabel=label||'alle podcasts';
  // Markeer actief
  podMenu.querySelectorAll('.pm-item').forEach(el=>{
    el.classList.toggle('active', el.dataset.id===id);
  });
  podBtn.textContent=`📻 ${activePodcastLabel.substring(0,22)}`;
  // Verzoek index rebuild
  setStatus('archief wisselen…');
  fetch(`/echo/filter?id=${encodeURIComponent(id)}`).then(()=>{
    // Poll totdat index klaar is
    const doct=document.getElementById('loading');
    if(doct) doct.style.opacity='1'; // als loading overlay nog bestaat
    function poll(){
      fetch('/echo/ready').then(r=>r.json()).then(d=>{
        if(d.ready){
          setStatus(`${d.words.toLocaleString('nl')} stemmen geladen`);
        } else {
          setStatus(`${d.words.toLocaleString('nl')} stemmen laden…`);
          setTimeout(poll,800);
        }
      }).catch(()=>setTimeout(poll,1500));
    }
    setTimeout(poll,500);
  });
}

// ═══════════════════════════════════════════════════════════════════
// INFO OVERLAY
// ═══════════════════════════════════════════════════════════════════
const infoOverlay=document.getElementById('info-overlay');
document.getElementById('info-btn').addEventListener('click',()=>{
  infoOverlay.classList.add('open');
});
document.getElementById('info-close').addEventListener('click',()=>{
  infoOverlay.classList.remove('open');
});
infoOverlay.addEventListener('click',(e)=>{
  if(e.target===infoOverlay) infoOverlay.classList.remove('open');
});
document.addEventListener('keydown',(e)=>{
  if(e.key==='Escape') infoOverlay.classList.remove('open');
});

// ═══════════════════════════════════════════════════════════════════
// HOW TO PLAY
// ═══════════════════════════════════════════════════════════════════
const howtoEl=document.getElementById('howto');

function hideHowto(){
  howtoEl.classList.add('hidden');
}

// Fade na 14s
setTimeout(hideHowto, 14000);

// Verberg direct bij eerste user-interactie
document.getElementById('word-input').addEventListener('focus', hideHowto, {once:true});
document.getElementById('pit').addEventListener('click', hideHowto, {once:true});
</script>
</body>
</html>
"""


# ── Rate limiter (simpel, in-memory) ───────────────────────────────────────────

_rate_window: dict[str, list] = {}   # ip → [timestamp, ...]
_rate_lock = threading.Lock()

MAX_REQ_PER_10S = 40  # genereus voor kunstinstallatie; aanpassen voor public deploy

def _check_rate(ip: str) -> bool:
    """True = toegestaan, False = te snel."""
    now = time.time()
    with _rate_lock:
        times = _rate_window.get(ip, [])
        times = [t for t in times if now - t < 10]
        times.append(now)
        _rate_window[ip] = times
        # Ruim oude IPs op (iedere 1000ste verzoek)
        if len(_rate_window) > 500:
            _rate_window.clear()
    return len(times) <= MAX_REQ_PER_10S


# ── HTTP Handler ───────────────────────────────────────────────────────────────

class EchoHandler(BaseHTTPRequestHandler):
    db_path: str = ""

    def do_GET(self):
        # Rate limiting
        ip = self.client_address[0]
        if not _check_rate(ip):
            self._send_error(429, "Te veel verzoeken")
            return

        parsed = urlparse(self.path)
        # Normaliseer path: verwijder dubbele slashes, geen ../ traversal
        path   = re.sub(r"/+", "/", parsed.path.rstrip("/"))
        if ".." in path:
            self._send_error(400, "Ongeldig pad"); return
        qs     = parse_qs(parsed.query)

        if path in ("", "/echo", "/echo/index.html"):
            self._send(200, "text/html; charset=utf-8", HTML.encode())

        elif path == "/echo/3d":
            fpath = ECHO_STATIC_DIR / "echo3d.html"
            if not fpath.exists():
                self._send_error(404, "Niet gevonden"); return
            self._send(200, "text/html; charset=utf-8", fpath.read_bytes())

        elif path.startswith("/echo/static/"):
            rel = path.removeprefix("/echo/static/").strip("/")
            if not rel or ".." in rel:
                self._send_error(403, "Verboden"); return
            fpath = (ECHO_STATIC_DIR / rel).resolve()
            try:
                fpath.relative_to(ECHO_STATIC_DIR.resolve())
            except ValueError:
                self._send_error(403, "Verboden"); return
            if not fpath.exists() or not fpath.is_file():
                self._send_error(404, "Niet gevonden"); return
            ctype = {
                ".html": "text/html; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".json": "application/json; charset=utf-8",
            }.get(fpath.suffix.lower(), "application/octet-stream")
            self._send(200, ctype, fpath.read_bytes())

        elif path == "/echo/ready":
            with _index_lock:
                n = len(_word_index)
            data = json.dumps({"ready": _index_ready.is_set(), "words": n}).encode()
            self._send(200, "application/json", data)

        elif path == "/echo/podcasts":
            data = json.dumps(_formats_cache).encode()
            self._send(200, "application/json", data)

        elif path == "/echo/filter":
            format_id = unquote(qs.get("id", [""])[0]).strip() or None
            _index_ready.clear()
            threading.Thread(
                target=_build_index,
                args=(self.db_path, format_id),
                daemon=True
            ).start()
            data = json.dumps({"status": "rebuilding", "format": format_id}).encode()
            self._send(200, "application/json", data)

        elif path == "/echo/random":
            if not _index_ready.is_set():
                self._send(503, "text/plain", b"Index not ready")
                return
            word, freq = _random_word()
            data = json.dumps({"word": word, "frequency": freq}).encode()
            self._send(200, "application/json", data)

        elif path == "/echo/gap":
            if not _index_ready.is_set():
                self._send(503, "text/plain", b"Niet klaar")
                return
            gap_echo_raw = qs.get("echo", ["2"])[0]
            try:
                gap_echo_level = max(0, min(3, int(gap_echo_raw)))
            except (ValueError, TypeError):
                gap_echo_level = 2
            audio = _extract_gap(echo_level=gap_echo_level)
            if audio is None:
                self._send_error(404, "Geen gap gevonden")
                return
            self._send(200, "audio/mpeg", audio)

        elif path.startswith("/echo/music/"):
            from urllib.parse import quote as urlquote
            fname = Path(path).name
            if not re.match(r'^[\w\-]+\.mp3$', fname):
                self._send_error(403, "Verboden"); return
            fpath = MUSIC_DIR / fname
            if not fpath.exists():
                self._send_error(404, "Niet gevonden"); return
            data = fpath.read_bytes()
            self._send(200, "audio/mpeg", data)

        elif path == "/echo/api":
            from urllib.parse import quote as urlquote
            raw_query = unquote(qs.get("word", [""])[0]).strip()
            # Invoervalidatie: max 120 tekens, alleen veilige karakters
            raw_query = raw_query[:120]
            raw_query = re.sub(r"[^\w\s\-',.]", "", raw_query, flags=re.UNICODE)
            variant_raw = qs.get("v", ["0"])[0]
            try:
                variant = max(0, min(3, int(variant_raw)))
            except (ValueError, TypeError):
                variant = 0
            echo_raw = qs.get("echo", ["2"])[0]
            try:
                echo_level = max(0, min(3, int(echo_raw)))
            except (ValueError, TypeError):
                echo_level = 2
            if not raw_query:
                self._send_error(400, "Geen woord opgegeven"); return
            if not _index_ready.is_set():
                self._send_error(503, "Index nog niet klaar"); return

            # Zinnen: zoek zeldzaamste woord
            matched, original = _resolve_query(raw_query)
            audio, freq, ctx_b, ctx_a = _extract_and_echo(matched, variant=variant, echo_level=echo_level)
            if audio is None:
                self._send_error(404, f"'{raw_query}' niet gevonden"); return

            self.send_response(200)
            self.send_header("Content-Type",         "audio/mpeg")
            self.send_header("Content-Length",       str(len(audio)))
            self.send_header("X-Word-Matched",       matched)
            self.send_header("X-Word-Frequency",     str(freq))
            # Context URL-encoded om non-ASCII te handelen
            self.send_header("X-Context-Before",     urlquote(ctx_b))
            self.send_header("X-Context-After",      urlquote(ctx_a))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Expose-Headers",
                             "X-Word-Matched, X-Word-Frequency, X-Context-Before, X-Context-After")
            self.end_headers()
            self.wfile.write(audio)

        # ── Fragment echo endpoints ─────────────────────────────────────────────

        elif path == "/echo/randomfrag":
            # Geeft een willekeurig fragment terug — liefst uit Gemini cache (audio-embeddings)
            _ensure_gemini_emb_cache()
            cache = _gemini_emb_cache if _gemini_emb_cache else None
            if not cache:
                _ensure_emb_cache()
                cache = _emb_cache
            if not cache:
                self._send_error(503, "Geen embeddings geladen"); return
            item = random.choice(cache)
            data = json.dumps({
                "audio_url": item["audio_url"],
                "start_ms":  item["start_ms"],
                "end_ms":    item["end_ms"],
                "text":      item.get("text", "")[:120],
                "has_audio_emb": bool(_gemini_emb_cache),
            }).encode()
            self._send(200, "application/json", data)

        elif path == "/echo/fragsound":
            # Speelt een fragment af (bron zelf, geen similarity search)
            from urllib.parse import quote as urlquote
            audio_url  = unquote(qs.get("audio_url", [""])[0]).strip()
            start_ms   = int(qs.get("start_ms", ["0"])[0] or 0)
            end_ms     = int(qs.get("end_ms",   ["0"])[0] or 0)
            echo_raw   = qs.get("echo", ["2"])[0]
            try: echo_level = max(0, min(3, int(echo_raw)))
            except: echo_level = 2
            if not audio_url or end_ms <= start_ms:
                self._send_error(400, "Ongeldige parameters"); return
            audio = _try_extract_segment(audio_url, start_ms, end_ms, echo_level)
            if not audio:
                self._send_error(404, "Fragment niet beschikbaar"); return
            self._send(200, "audio/mpeg", audio)

        elif path == "/echo/fragecho":
            # Fragment-to-fragment echo via tekst-embeddings
            from urllib.parse import quote as urlquote
            audio_url  = unquote(qs.get("audio_url", [""])[0]).strip()
            start_ms   = int(qs.get("start_ms", ["0"])[0] or 0)
            end_ms     = int(qs.get("end_ms",   ["0"])[0] or 0)
            mode       = qs.get("mode", ["similar"])[0]
            echo_raw   = qs.get("echo", ["2"])[0]
            try: echo_level = max(0, min(3, int(echo_raw)))
            except: echo_level = 2
            if not audio_url:
                self._send_error(400, "Geen audio_url"); return
            # Probeer Gemini audio-embeddings eerst (betere sound-matching)
            _ensure_gemini_emb_cache()
            use_cache = _gemini_emb_cache if _gemini_emb_cache else None
            if not use_cache:
                _ensure_emb_cache()
                use_cache = _emb_cache
            # Zoek het bronFragment in de cache op audio_url + timing
            src_item = None
            for item in use_cache:
                if item["audio_url"] == audio_url and abs(item["start_ms"] - start_ms) < 500:
                    src_item = item; break
            # Fallback: zoek in tekst-cache als Gemini cache niet matcht
            if src_item is None and use_cache is _gemini_emb_cache:
                _ensure_emb_cache()
                use_cache = _emb_cache
                for item in use_cache:
                    if item["audio_url"] == audio_url and abs(item["start_ms"] - start_ms) < 500:
                        src_item = item; break
            if src_item is None:
                self._send_error(404, "Fragment niet in embedding cache"); return
            try:
                import numpy as np
                q     = src_item["emb"]
                q_norm = np.linalg.norm(q) + 1e-9
                scored = []
                for item in use_cache:
                    if item["audio_url"] == audio_url:
                        continue  # zelfde bron overslaan
                    b   = item["emb"]
                    sim = float(np.dot(q, b) / (q_norm * (np.linalg.norm(b) + 1e-9)))
                    scored.append({**{k: v for k, v in item.items() if k != "emb"},
                                   "similarity": sim})
                scored.sort(key=lambda x: x["similarity"], reverse=True)
                if mode == "anti":
                    pool = [s for s in scored if 0.08 <= s["similarity"] <= 0.45]
                    candidate = random.choice(pool[:20]) if pool else scored[-1]
                else:
                    candidate = scored[0]
            except Exception as e:
                self._send_error(500, f"Similarity error: {e}"); return
            audio = _try_extract_segment(
                candidate["audio_url"], candidate["start_ms"],
                candidate["end_ms"], echo_level=echo_level
            )
            if not audio:
                self._send_error(404, "Echo-fragment niet beschikbaar"); return
            self.send_response(200)
            self.send_header("Content-Type",    "audio/mpeg")
            self.send_header("Content-Length",  str(len(audio)))
            self.send_header("X-Similarity",    f"{candidate['similarity']:.2f}")
            self.send_header("X-Fragment-Text", urlquote(candidate.get("text","")[:80]))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Expose-Headers", "X-Similarity, X-Fragment-Text")
            self.end_headers()
            self.wfile.write(audio)

        # ── Spelmodi endpoints ──────────────────────────────────────────────────

        elif path == "/echo/rhyme":
            raw_word = unquote(qs.get("word", [""])[0]).strip()[:60]
            raw_word = re.sub(r"[^\w\-]", "", raw_word.lower())
            echo_raw = qs.get("echo", ["2"])[0]
            try: echo_level = max(0, min(3, int(echo_raw)))
            except: echo_level = 2
            if not raw_word:
                self._send_error(400, "Geen woord opgegeven"); return
            if not _index_ready.is_set():
                self._send_error(503, "Index niet klaar"); return
            candidates = _get_rhyme_candidates(raw_word)
            if not candidates:
                self._send_error(404, f"Geen rijmwoord voor '{raw_word}'"); return
            random.shuffle(candidates)
            audio, rhyme_word = None, ""
            for word, occs in candidates[:10]:
                occ     = random.choice(occs)
                fmt_ns  = re.sub(r"[^a-z0-9]", "", (_active_format_id or "all")[:8])
                fmt_dir = CACHE_DIR / fmt_ns; fmt_dir.mkdir(exist_ok=True)
                ck      = re.sub(r"[^a-z0-9]", "_", word)
                cp      = fmt_dir / f"{ck}_rhyme_e{echo_level}.mp3"
                audio   = _try_extract(occ, cp, variant=0, echo_level=echo_level)
                if audio: rhyme_word = word; break
            if not audio:
                self._send_error(404, "Geen rijmfragment gevonden"); return
            self.send_response(200)
            self.send_header("Content-Type",    "audio/mpeg")
            self.send_header("Content-Length",  str(len(audio)))
            self.send_header("X-Rhyme-Word",    rhyme_word)
            self.send_header("X-Original-Word", raw_word)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Expose-Headers", "X-Rhyme-Word, X-Original-Word")
            self.end_headers()
            self.wfile.write(audio)

        elif path in ("/echo/semantic", "/echo/sentence"):
            from urllib.parse import quote as urlquote
            raw_query = unquote(qs.get("query", [""])[0]).strip()[:200]
            raw_query = re.sub(r"[^\w\s\-',.]", "", raw_query, flags=re.UNICODE)
            echo_raw  = qs.get("echo", ["2"])[0]
            try: echo_level = max(0, min(3, int(echo_raw)))
            except: echo_level = 2
            is_sentence = path == "/echo/sentence"
            if not raw_query:
                self._send_error(400, "Geen zoekopdracht"); return
            # Zin-modus met 1 woord → val terug op gewone woordecho
            if is_sentence and len(raw_query.split()) == 1:
                if not _index_ready.is_set():
                    self._send_error(503, "Index niet klaar"); return
                matched, _ = _resolve_query(raw_query)
                audio, freq, ctx_b, ctx_a = _extract_and_echo(matched, variant=0, echo_level=echo_level)
                if audio is None:
                    self._send_error(404, "Niet gevonden"); return
                self.send_response(200)
                self.send_header("Content-Type",   "audio/mpeg")
                self.send_header("Content-Length", str(len(audio)))
                self.send_header("X-Word-Matched", matched)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Expose-Headers", "X-Word-Matched")
                self.end_headers()
                self.wfile.write(audio)
                return
            results = _similarity_search(raw_query, limit=5)
            if not results:
                self._send_error(404, "Geen semantisch resultaat"); return
            audio, top = None, None
            for candidate in results:
                audio = _try_extract_segment(
                    candidate["audio_url"], candidate["start_ms"],
                    candidate["end_ms"], echo_level=echo_level
                )
                if audio: top = candidate; break
            if not audio:
                self._send_error(404, "Fragment niet beschikbaar"); return
            self.send_response(200)
            self.send_header("Content-Type",    "audio/mpeg")
            self.send_header("Content-Length",  str(len(audio)))
            self.send_header("X-Similarity",    f"{top['similarity']:.2f}")
            self.send_header("X-Fragment-Text", urlquote(top.get("text","")[:80]))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Expose-Headers", "X-Similarity, X-Fragment-Text")
            self.end_headers()
            self.wfile.write(audio)

        elif path == "/echo/chamber":
            raw_word = unquote(qs.get("word", [""])[0]).strip()[:120]
            raw_word = re.sub(r"[^\w\s\-',.]", "", raw_word, flags=re.UNICODE)
            echo_raw = qs.get("echo", ["2"])[0]
            try: echo_level = max(0, min(3, int(echo_raw)))
            except: echo_level = 2
            if not raw_word:
                self._send_error(400, "Geen woord opgegeven"); return
            if not _index_ready.is_set():
                self._send_error(503, "Index niet klaar"); return
            matched, _ = _resolve_query(raw_word)
            occs = _word_index.get(matched, [])
            if not occs:
                self._send_error(404, f"'{raw_word}' niet gevonden"); return
            url_groups: dict[str, list] = {}
            for occ in occs:
                url_groups.setdefault(occ["audio_url"], []).append(occ)
            unique_urls = sorted(url_groups.keys())   # deterministisch
            clips = []
            for url in unique_urls[:6]:
                if len(clips) >= 4: break
                occ     = random.choice(url_groups[url])
                fmt_ns  = re.sub(r"[^a-z0-9]", "", (_active_format_id or "all")[:8])
                fmt_dir = CACHE_DIR / fmt_ns; fmt_dir.mkdir(exist_ok=True)
                ck      = re.sub(r"[^a-z0-9]", "_", matched)
                cp      = fmt_dir / f"{ck}_ch{len(clips)}_e{echo_level}.mp3"
                a       = _try_extract(occ, cp, variant=len(clips) % 4, echo_level=echo_level)
                if a: clips.append(a)
            if not clips:
                self._send_error(404, "Geen audio gevonden"); return
            # Chamber: stemmen tegelijk (mix) — echokamer-gevoel
            combined = _ffmpeg_mix(clips) if len(clips) > 1 else clips[0]
            if not combined:
                self._send_error(500, "Mix mislukt"); return
            self.send_response(200)
            self.send_header("Content-Type",      "audio/mpeg")
            self.send_header("Content-Length",    str(len(combined)))
            self.send_header("X-Chamber-Sources", str(len(clips)))
            self.send_header("X-Word-Matched",    matched)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Expose-Headers", "X-Chamber-Sources, X-Word-Matched")
            self.end_headers()
            self.wfile.write(combined)

        elif path == "/echo/drift":
            from urllib.parse import quote as urlquote
            raw_query = unquote(qs.get("query", [""])[0]).strip()[:200]
            raw_query = re.sub(r"[^\w\s\-',.]", "", raw_query, flags=re.UNICODE)
            echo_raw  = qs.get("echo", ["2"])[0]
            try: echo_level = max(0, min(3, int(echo_raw)))
            except: echo_level = 2
            if not raw_query:
                self._send_error(400, "Geen zoekopdracht"); return
            results = _similarity_search(raw_query, limit=60)
            if not results:
                self._send_error(404, "Onvoldoende resultaten voor drift"); return
            close  = [r for r in results if r["similarity"] >= 0.65]
            medium = [r for r in results if 0.40 <= r["similarity"] < 0.65]
            far    = [r for r in results if r["similarity"] < 0.40]
            if not close:  close  = results[:2]
            if not medium: medium = results[len(close):len(close)+2]
            if not far:    far    = results[len(close)+len(medium):len(close)+len(medium)+2]
            clips, sims = [], []
            for layer in (close[:2], medium[:2], far[:2]):
                for candidate in layer:
                    a = _try_extract_segment(
                        candidate["audio_url"], candidate["start_ms"],
                        candidate["end_ms"], echo_level=echo_level
                    )
                    if a:
                        clips.append(a)
                        sims.append(f"{candidate['similarity']:.2f}")
                        break
            if not clips:
                self._send_error(404, "Geen audio gevonden"); return
            # Drift: crossfade tussen lagen — semantisch glijden
            combined = _ffmpeg_crossfade(clips) if len(clips) > 1 else clips[0]
            if not combined:
                self._send_error(500, "Drift crossfade mislukt"); return
            self.send_response(200)
            self.send_header("Content-Type",         "audio/mpeg")
            self.send_header("Content-Length",       str(len(combined)))
            self.send_header("X-Drift-Layers",       str(len(clips)))
            self.send_header("X-Drift-Similarities", ",".join(sims))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Expose-Headers", "X-Drift-Layers, X-Drift-Similarities")
            self.end_headers()
            self.wfile.write(combined)

        elif path == "/echo/anti":
            from urllib.parse import quote as urlquote
            raw_query = unquote(qs.get("query", [""])[0]).strip()[:200]
            raw_query = re.sub(r"[^\w\s\-',.]", "", raw_query, flags=re.UNICODE)
            echo_raw  = qs.get("echo", ["2"])[0]
            try: echo_level = max(0, min(3, int(echo_raw)))
            except: echo_level = 2
            if not raw_query:
                self._send_error(400, "Geen zoekopdracht"); return
            results = _similarity_search(raw_query, limit=300)
            if not results:
                self._send_error(404, "Geen anti-echo gevonden"); return
            anti = [r for r in results if 0.08 <= r["similarity"] <= 0.40]
            if not anti:
                anti = results[-20:]   # fallback: laagste scores
            candidate = random.choice(anti[:20])
            audio = _try_extract_segment(
                candidate["audio_url"], candidate["start_ms"],
                candidate["end_ms"], echo_level=echo_level
            )
            if not audio:
                self._send_error(404, "Anti-echo fragment niet beschikbaar"); return
            self.send_response(200)
            self.send_header("Content-Type",       "audio/mpeg")
            self.send_header("Content-Length",     str(len(audio)))
            self.send_header("X-Anti-Similarity",  f"{candidate['similarity']:.2f}")
            self.send_header("X-Fragment-Text",    urlquote(candidate.get("text","")[:80]))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Expose-Headers", "X-Anti-Similarity, X-Fragment-Text")
            self.end_headers()
            self.wfile.write(audio)

        elif path == "/echo/canon":
            raw_word = unquote(qs.get("word", [""])[0]).strip()[:120]
            raw_word = re.sub(r"[^\w\s\-',.]", "", raw_word, flags=re.UNICODE)
            echo_raw = qs.get("echo", ["2"])[0]
            n_raw    = qs.get("n",    ["5"])[0]
            try: echo_level = max(0, min(3, int(echo_raw)))
            except: echo_level = 2
            try: n_voices = max(2, min(10, int(n_raw)))
            except: n_voices = 5
            if not raw_word:
                self._send_error(400, "Geen woord opgegeven"); return
            if not _index_ready.is_set():
                self._send_error(503, "Index niet klaar"); return
            matched, _ = _resolve_query(raw_word)
            occs = _word_index.get(matched, [])
            if not occs:
                self._send_error(404, f"'{raw_word}' niet gevonden"); return
            url_groups: dict[str, list] = {}
            for occ in occs:
                url_groups.setdefault(occ["audio_url"], []).append(occ)
            unique_urls = list(url_groups.keys())
            random.shuffle(unique_urls)
            clips = []
            for url in unique_urls[:n_voices * 2]:
                if len(clips) >= n_voices: break
                occ     = random.choice(url_groups[url])
                fmt_ns  = re.sub(r"[^a-z0-9]", "", (_active_format_id or "all")[:8])
                fmt_dir = CACHE_DIR / fmt_ns; fmt_dir.mkdir(exist_ok=True)
                ck      = re.sub(r"[^a-z0-9]", "_", matched)
                cp      = fmt_dir / f"{ck}_can{len(clips)}_e{echo_level}.mp3"
                a       = _try_extract(occ, cp, variant=0, echo_level=echo_level)
                if a: clips.append(a)
            if not clips:
                self._send_error(404, "Geen stemmen gevonden"); return
            # Canon: stemmen tegelijk (koor) — mix op elkaar gelegd
            combined = _ffmpeg_mix(clips) if len(clips) > 1 else clips[0]
            if not combined:
                self._send_error(500, "Canon mix mislukt"); return
            self.send_response(200)
            self.send_header("Content-Type",   "audio/mpeg")
            self.send_header("Content-Length", str(len(combined)))
            self.send_header("X-Canon-Voices", str(len(clips)))
            self.send_header("X-Word-Matched", matched)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Expose-Headers", "X-Canon-Voices, X-Word-Matched")
            self.end_headers()
            self.wfile.write(combined)

        else:
            self._send_error(404, "Niet gevonden")

    def do_POST(self):
        from urllib.parse import quote as urlquote
        parsed = urlparse(self.path)
        path   = parsed.path
        qs     = parse_qs(parsed.query)

        if path == "/echo/audiomatch":
            echo_raw = qs.get("echo", ["2"])[0]
            try: echo_level = max(0, min(3, int(echo_raw)))
            except: echo_level = 2

            content_length = int(self.headers.get("Content-Length", 0))
            if content_length > 20_000_000:
                self._send_error(413, "Audio te groot"); return
            audio_in = self.rfile.read(content_length) if content_length > 0 else b""

            text      = unquote(qs.get("text", [""])[0]).strip()[:200]
            req_mode  = qs.get("mode", ["audio"])[0]   # doormodus: woord/rijm/semantisch/…
            mime_type = self.headers.get("Content-Type", "audio/webm").split(";")[0].strip()

            # ── Transcriptie-modus: mic als invoermethode voor andere modi ──────
            if req_mode != "audio" and audio_in:
                transcript = _gemini_transcribe(audio_in, mime_type) or text
                if not transcript:
                    self._send_error(503, "Transcriptie mislukt"); return
                print(f"  🎙 Transcriptie ({req_mode}): {transcript!r}", flush=True)

                if req_mode == "rhyme":
                    # Rijm-modus: zoek rijmkandidaten op getranscribeerd woord
                    target_word = transcript.split()[0] if transcript else ""
                    candidates  = _get_rhyme_candidates(target_word, max_candidates=15)
                    if not candidates:
                        self._send_error(404, f"Geen rijm gevonden voor '{target_word}'"); return
                    cand_word, occs = random.choice(candidates)
                    occ   = random.choice(occs)
                    mic_dir = CACHE_DIR / "mic"; mic_dir.mkdir(exist_ok=True)
                    audio = _try_extract(occ,
                                         mic_dir / f"rhyme_{re.sub(r'[^a-z0-9]','_',cand_word)}.mp3",
                                         variant=0, echo_level=echo_level)
                    if not audio:
                        self._send_error(404, "Rijmfragment niet beschikbaar"); return
                    from urllib.parse import quote as urlquote
                    self.send_response(200)
                    self.send_header("Content-Type",    "audio/mpeg")
                    self.send_header("Content-Length",  str(len(audio)))
                    self.send_header("X-Rhyme-Word",    cand_word)
                    self.send_header("X-Transcript",    urlquote(transcript))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Access-Control-Expose-Headers",
                                     "X-Rhyme-Word, X-Transcript")
                    self.end_headers()
                    self.wfile.write(audio); return

                elif req_mode in ("word",):
                    # Woord-modus: exacte woordzoekopdracht
                    matched, _ = _resolve_query(transcript.split()[0] if transcript else "")
                    audio_w, freq, ctx_b, ctx_a = _extract_and_echo(matched, variant=0, echo_level=echo_level)
                    if not audio_w:
                        self._send_error(404, f"'{transcript}' niet gevonden"); return
                    from urllib.parse import quote as urlquote
                    self.send_response(200)
                    self.send_header("Content-Type",    "audio/mpeg")
                    self.send_header("Content-Length",  str(len(audio_w)))
                    self.send_header("X-Word-Matched",  matched)
                    self.send_header("X-Transcript",    urlquote(transcript))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Access-Control-Expose-Headers",
                                     "X-Word-Matched, X-Transcript")
                    self.end_headers()
                    self.wfile.write(audio_w); return

                else:
                    # Alle andere modi: semantisch zoeken op transcript
                    results_t = _similarity_search(transcript, limit=10)
                    if not results_t:
                        self._send_error(404, "Geen match gevonden"); return
                    audio_s, top_s = None, None
                    for cand in results_t[:6]:
                        audio_s = _try_extract_segment(
                            cand["audio_url"], cand["start_ms"],
                            cand["end_ms"], echo_level=echo_level
                        )
                        if audio_s: top_s = cand; break
                    if not audio_s:
                        self._send_error(404, "Fragment niet beschikbaar"); return
                    from urllib.parse import quote as urlquote
                    self.send_response(200)
                    self.send_header("Content-Type",    "audio/mpeg")
                    self.send_header("Content-Length",  str(len(audio_s)))
                    self.send_header("X-Similarity",    f"{top_s['similarity']:.2f}")
                    self.send_header("X-Fragment-Text", urlquote(top_s.get("text","")[:80]))
                    self.send_header("X-Transcript",    urlquote(transcript))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Access-Control-Expose-Headers",
                                     "X-Similarity, X-Fragment-Text, X-Transcript")
                    self.end_headers()
                    self.wfile.write(audio_s); return

            # ── Standaard audio-embedding modus ─────────────────────────────────
            # Probeer Gemini multimodale embedding
            emb = None
            if audio_in:
                emb = _gemini_embed(audio_bytes=audio_in, text=text)
                if emb:
                    print(f"  🎙 Gemini audio-embedding OK (dim={len(emb)})", flush=True)

            if emb:
                results = _gemini_similarity_search(emb, limit=20)
            elif text:
                # Fallback: tekst-only semantisch zoeken
                results = _similarity_search(text, limit=10)
            else:
                self._send_error(400, "Geen bruikbare audio of tekst ontvangen"); return

            if not results:
                self._send_error(404, "Geen echo gevonden"); return

            audio, top = None, None
            for candidate in results[:8]:
                audio = _try_extract_segment(
                    candidate["audio_url"], candidate["start_ms"],
                    candidate["end_ms"], echo_level=echo_level
                )
                if audio: top = candidate; break

            if not audio:
                self._send_error(404, "Fragment niet beschikbaar"); return

            self.send_response(200)
            self.send_header("Content-Type",    "audio/mpeg")
            self.send_header("Content-Length",  str(len(audio)))
            self.send_header("X-Similarity",    f"{top['similarity']:.2f}")
            self.send_header("X-Fragment-Text", urlquote(top.get("text","")[:80]))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Expose-Headers",
                             "X-Similarity, X-Fragment-Text")
            self.end_headers()
            self.wfile.write(audio)

        else:
            self._send_error(404, "Niet gevonden")

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, code, msg):
        body = msg.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        line = fmt % args if args else fmt
        # Alleen relevante API calls loggen (niet de gap/random/ready polling)
        if "/echo/api" in line and "GET" in line:
            word = ""
            try: word = line.split("word=")[1].split(" ")[0].split("&")[0]
            except: pass
            # Sanitize log output om log-injection te voorkomen
            safe = re.sub(r"[^\w\s\-',.]", "", unquote(word))[:40]
            print(f"  🔊 {safe}", flush=True)
        elif "429" in line:
            ip = self.client_address[0]
            print(f"  ⚠️  rate-limit {ip}", flush=True)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Stemgraf server")
    parser.add_argument("--port",       type=int, default=7843)
    parser.add_argument("--db",         default="data/stemmy-replica.db")
    parser.add_argument("--format",     default=None)
    parser.add_argument("--browser", action="store_true")
    args = parser.parse_args()

    db_path = str(ROOT / args.db) if not Path(args.db).is_absolute() else args.db

    threading.Thread(target=_build_index, args=(db_path, args.format), daemon=True).start()

    EchoHandler.db_path = db_path
    server = HTTPServer(("", args.port), EchoHandler)
    url    = f"http://localhost:{args.port}/echo"

    print(f"\n  ⬜ GRAFSTEMMING")
    print(f"  ─────────────────────────────")
    print(f"  {url}")
    print(f"  {db_path}")
    print(f"  Ctrl+C om te stoppen\n")

    try:
        from stemmy_cli.audio.parallel_extractor import ParallelExtractor  # noqa: F401
    except ImportError as e:
        print(f"  ⚠️  Audio-extractie ontbreekt ({e}).", flush=True)
        print("     Zonder dit geven /echo/api en /echo/gap 404. Installeer o.a. requests:\n"
              "     pip install requests\n", flush=True)

    if args.browser:
        threading.Timer(1.2, webbrowser.open, args=(url,)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Grafstemming gesloten.")

if __name__ == "__main__":
    main()
