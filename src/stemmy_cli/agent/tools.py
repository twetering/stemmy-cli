"""
Tool registry for AI agent integration.

Defines all CLI capabilities as callable tools for LLM orchestration.
Each tool has a structured schema for parameter validation and documentation.
"""

import sqlite3
import json
from typing import Any, Callable, Dict, List, Optional, TypedDict
from pathlib import Path

from stemmy_cli.config import get_database_path
from stemmy_cli.paths import get_output_dir, get_music_dir, get_project_root


def _fetch_rows(cursor: Any) -> List[Any]:
    """Fetch all rows from a cursor with defensive fallbacks."""
    if cursor is None:
        return []
    try:
        rows = cursor.fetchall()
    except Exception:
        try:
            rows = list(cursor)
        except Exception:
            return []
    if rows is cursor:
        try:
            rows = list(cursor)
        except Exception:
            return []
    return rows or []


def _row_to_dict(row: Any, columns: Optional[List[str]]) -> Dict[str, Any]:
    """Convert a row to dict across sqlite/libsql cursor formats."""
    if row is None:
        return {}
    if isinstance(row, dict):
        return row
    if hasattr(row, "keys"):
        try:
            return dict(row)
        except Exception:
            pass
    if columns and isinstance(row, (list, tuple)):
        return {columns[i]: row[i] for i in range(min(len(columns), len(row)))}
    return {"_row": row}


SENTENCE_PAUSE_MS = 750
DEFAULT_MAX_SENTENCE_SECONDS = 3.0


def _word_text(word: Dict[str, Any]) -> str:
    return word.get("text", word.get("word", "")) or ""


def _words_time_scale(words: List[Dict[str, Any]]) -> float:
    max_end = 0.0
    for w in words:
        val = w.get("end", w.get("end_time", 0)) or 0
        try:
            max_end = max(max_end, float(val))
        except (TypeError, ValueError):
            continue
    return 1000.0 if max_end <= 1000.0 else 1.0


def _word_time_ms(word: Dict[str, Any], key: str, scale: float) -> float:
    val = word.get(key, word.get(f"{key}_time", 0)) or 0
    try:
        return float(val) * scale
    except (TypeError, ValueError):
        return 0.0


def _split_sentences(
    words: List[Dict[str, Any]],
    max_sentence_seconds: float = DEFAULT_MAX_SENTENCE_SECONDS,
) -> List[Dict[str, Any]]:
    """Split words into sentence segments using punctuation and pauses."""
    if not words:
        return []

    scale = _words_time_scale(words)
    texts = [_word_text(w) for w in words]
    starts = [_word_time_ms(w, "start", scale) for w in words]
    ends = [_word_time_ms(w, "end", scale) for w in words]

    boundaries: List[tuple] = []
    start_idx = 0
    for i in range(len(words)):
        text = texts[i]
        end_of_sentence = any(p in text for p in ".!?")
        if i < len(words) - 1:
            pause_ms = starts[i + 1] - ends[i]
            if pause_ms > SENTENCE_PAUSE_MS:
                end_of_sentence = True
        if end_of_sentence:
            boundaries.append((start_idx, i))
            start_idx = i + 1
    if start_idx < len(words):
        boundaries.append((start_idx, len(words) - 1))

    results = []
    for s, e in boundaries:
        start_ms = starts[s]
        end_ms = ends[e]
        if end_ms <= start_ms:
            continue
        duration = (end_ms - start_ms) / 1000.0
        if max_sentence_seconds and duration > max_sentence_seconds:
            continue
        text = " ".join(t for t in texts[s : e + 1] if t).strip()
        if not text:
            continue
        last_text = texts[e]
        end_punct = "?" if "?" in last_text else "!" if "!" in last_text else "." if "." in last_text else ""
        results.append(
            {
                "text": text,
                "start_time": start_ms / 1000.0,
                "end_time": end_ms / 1000.0,
                "duration": duration,
                "end_punct": end_punct,
                "start_idx": s,
                "end_idx": e,
            }
        )
    return results


def _sentence_for_word_index(
    words: List[Dict[str, Any]],
    word_index: int,
    max_sentence_seconds: float = DEFAULT_MAX_SENTENCE_SECONDS,
) -> Optional[Dict[str, Any]]:
    """Get sentence segment containing a word index."""
    if word_index is None or word_index < 0:
        return None
    sentences = _split_sentences(words, max_sentence_seconds=max_sentence_seconds)
    for s in sentences:
        if s["start_idx"] <= word_index <= s["end_idx"]:
            return s
    return None


def _dedupe_segments(
    results: List[Dict[str, Any]],
    tolerance_ms: int = 50,
) -> List[Dict[str, Any]]:
    """Deduplicate by audio_url + timing (with tolerance)."""
    seen: List[tuple] = []
    unique: List[Dict[str, Any]] = []
    for r in results:
        audio_url = r.get("audio_url") or r.get("item_audio_url") or r.get("source_audio_url") or ""
        try:
            start_ms = int(float(r.get("start_time", 0) or 0) * 1000)
            end_ms = int(float(r.get("end_time", 0) or 0) * 1000)
        except (TypeError, ValueError):
            start_ms, end_ms = 0, 0
        is_dup = False
        for seen_url, seen_start, seen_end in seen:
            if seen_url == audio_url:
                if abs(seen_start - start_ms) <= tolerance_ms and abs(seen_end - end_ms) <= tolerance_ms:
                    is_dup = True
                    break
        if not is_dup:
            seen.append((audio_url, start_ms, end_ms))
            unique.append(r)
    return unique


def _get_db_path() -> Path:
    """Get database path."""
    return get_database_path()


def _get_output_dir() -> Path:
    """Get output directory for generated MP3s."""
    return get_output_dir()


def _get_music_dir() -> Path:
    """Get music directory (package or project static/music)."""
    return get_music_dir()


def _get_db_connection():
    """Get a database connection (Turso cloud or local SQLite)."""
    try:
        from stemmy_cli.adapters.connection import get_connection
        conn = get_connection(prefer_turso=True)  # Turso default, SQLite fallback
        try:
            conn.row_factory = sqlite3.Row
        except AttributeError:
            pass  # Turso/libsql doesn't support row_factory
        return conn
    except Exception:
        # Fallback to direct SQLite if connection module not available or fails
        db_path = _get_db_path()
        if not db_path.exists():
            raise FileNotFoundError(f"Database not found at {db_path}. Set STEMMY_DB_PATH environment variable.")
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn


class ToolParameter(TypedDict, total=False):
    """Schema for a tool parameter."""
    type: str
    description: str
    required: bool
    default: Any
    enum: List[str]


class Tool(TypedDict):
    """Schema for a tool definition."""
    name: str
    description: str
    category: str
    parameters: Dict[str, ToolParameter]
    handler: str  # Function name reference


# Tool implementations
def _search_fragments(
    query: str,
    limit: int = 20,
    item_id: Optional[str] = None,
    format_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Search audio fragments by text."""
    conn = _get_db_connection()
    query_norm = (query or "").strip().lower()
    if not query_norm:
        return []
    tokens = [t for t in query_norm.split() if t]
    
    # Always join with items to get audio_url fallback
    if format_id:
        base_sql = """
            SELECT f.id, f.text, f.start_time, f.end_time, 
                   f.item_id, f.item_audio_url, f.speaker_label, f.words,
                   i.audio_url as item_audio_url_fallback
            FROM fragments f
            JOIN items i ON f.item_id = i.id
        """
    else:
        base_sql = """
            SELECT f.id, f.text, f.start_time, f.end_time, 
                   f.item_id, f.item_audio_url, f.speaker_label, f.words,
                   i.audio_url as item_audio_url_fallback
            FROM fragments f
            LEFT JOIN items i ON f.item_id = i.id
        """

    def _run_query(where_sql: str, params: List[Any]) -> List[Dict[str, Any]]:
        sql = base_sql + " WHERE " + where_sql
        final_params = list(params)
        if format_id:
            sql += " AND i.format_id = ?"
            final_params.append(format_id)
        if item_id:
            sql += " AND f.item_id = ?"
            final_params.append(item_id)
        max_rows = max(limit * 5, limit + 100)
        sql += " LIMIT ?"
        final_params.append(max_rows)
        cursor = conn.execute(sql, final_params)
        columns = [c[0] for c in cursor.description] if getattr(cursor, "description", None) else None
        batch = []
        for row in _fetch_rows(cursor):
            r = _row_to_dict(row, columns)
            if not r.get("item_audio_url") and r.get("item_audio_url_fallback"):
                r["item_audio_url"] = r["item_audio_url_fallback"]
            batch.append(r)
        return batch

    # Primary search: full query in text or words JSON (plus no-space fallback)
    results = _run_query(
        "(LOWER(f.text) LIKE ? OR LOWER(COALESCE(f.words, '')) LIKE ? OR REPLACE(LOWER(f.text), ' ', '') LIKE ?)",
        [f"%{query_norm}%", f"%{query_norm}%", f"%{query_norm.replace(' ', '')}%"],
    )

    # Secondary search: tokenized AND on text (broadens phrases split by punctuation)
    if len(results) < limit and len(tokens) > 1:
        token_where = " AND ".join(["LOWER(f.text) LIKE ?"] * len(tokens))
        token_params = [f"%{t}%" for t in tokens]
        results.extend(_run_query(token_where, token_params))

    conn.close()
    results = _dedupe_segments(results, tolerance_ms=50)
    return results[:limit]


def _search_semantic(
    query: str,
    limit: int = 20,
    format_id: Optional[str] = None,
    provider: str = "openai",
) -> List[Dict[str, Any]]:
    """
    Semantic search: find fragments by meaning, not exact text.
    Uses embeddings to find conceptually similar content.
    Requires embeddings to be built (stemmy embeddings build).
    """
    try:
        from stemmy_cli.embeddings.service import FragmentEmbedder, check_embedding_status
    except ImportError:
        return []  # Embeddings not available
    
    db_path = str(_get_db_path())
    if not db_path or not Path(db_path).exists():
        return []
    
    embedder = FragmentEmbedder(provider=provider, db_path=db_path)

    # Quick embedding availability check
    status = check_embedding_status(db_path)
    if status.get("embedded_count", 0) <= 0:
        # Fallback to text search if no embeddings exist
        return _search_fragments(query, limit=limit, format_id=format_id)

    if format_id:
        try:
            import sqlite3
            conn = sqlite3.connect(db_path)
            cursor = conn.execute(
                """
                SELECT COUNT(*) FROM fragment_embeddings e
                JOIN fragments f ON f.id = e.fragment_id
                JOIN items i ON f.item_id = i.id
                WHERE i.format_id = ?
                """,
                (format_id,),
            )
            fmt_count = cursor.fetchone()[0]
            conn.close()
        except Exception:
            fmt_count = 0
        if fmt_count <= 0:
            # No embeddings for this podcast; fall back to text search within format
            return _search_fragments(query, limit=limit, format_id=format_id)
    
    try:
        # Filter by format_id at embedding level (only compare within that podcast)
        raw_results = embedder.similarity_search(
            query, top_k=limit, format_id=format_id
        )
    except Exception:
        return _search_fragments(query, limit=limit, format_id=format_id)
    
    if not raw_results:
        # Fallback to text search (still filtered by format_id)
        return _search_fragments(query, limit=limit, format_id=format_id)
    
    conn = _get_db_connection()
    
    results = []
    for r in raw_results:
        fid = r["fragment_id"]
        cursor = conn.execute("""
            SELECT f.id, f.text, f.words, f.start_time, f.end_time, f.item_id, f.item_audio_url,
                   i.audio_url as item_audio_url_fallback, i.format_id
            FROM fragments f
            LEFT JOIN items i ON f.item_id = i.id
            WHERE f.id = ?
        """, (fid,))
        row = cursor.fetchone()
        if not row:
            continue
        columns = [c[0] for c in cursor.description] if getattr(cursor, "description", None) else None
        d = _row_to_dict(row, columns)
        if not d.get("item_audio_url") and d.get("item_audio_url_fallback"):
            d["item_audio_url"] = d["item_audio_url_fallback"]
        d["similarity"] = r["similarity"]
        results.append(d)
    
    conn.close()
    return _dedupe_segments(results, tolerance_ms=50)


def _search_hybrid(
    query: str,
    limit: int = 25,
    format_id: Optional[str] = None,
    semantic_weight: float = 0.6,
) -> List[Dict[str, Any]]:
    """
    Hybrid search: combine semantic (meaning) + text (keyword) results.
    Merges and deduplicates, ranking by weighted combination.
    """
    semantic_results = _search_semantic(query, limit=limit, format_id=format_id)
    text_results = _search_fragments(query, limit=limit, format_id=format_id)
    
    seen = set()
    merged = []
    
    # Add semantic first (by similarity rank)
    for r in semantic_results:
        fid = r.get("id")
        if fid and fid not in seen:
            seen.add(fid)
            r["source"] = "semantic"
            merged.append(r)
    
    # Add text matches not already in semantic
    for r in text_results:
        fid = r.get("id")
        if fid and fid not in seen:
            seen.add(fid)
            r["source"] = "text"
            r["similarity"] = r.get("similarity", 0.5)  # Default for text match
            merged.append(r)
    
    merged = _dedupe_segments(merged, tolerance_ms=50)
    return merged[:limit]


def _search_entities(
    query: str,
    entity_type: Optional[str] = None,
    format_id: Optional[str] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Search entities from transcripts."""
    conn = _get_db_connection()
    
    sql = """
        SELECT id, text, entity_type, start_time, end_time,
               item_id, format_id, source_audio_url, fragment_audio_url, confidence
        FROM entities
        WHERE text LIKE ?
    """
    params = [f"%{query}%"]
    
    if entity_type:
        sql += " AND entity_type = ?"
        params.append(entity_type)
    
    if format_id:
        sql += " AND format_id = ?"
        params.append(format_id)
    
    sql += f" LIMIT {limit}"
    
    cursor = conn.execute(sql, params)
    columns = [c[0] for c in cursor.description] if getattr(cursor, "description", None) else None
    results = [_row_to_dict(row, columns) for row in _fetch_rows(cursor)]
    conn.close()
    return _dedupe_segments(results, tolerance_ms=50)[:limit]


def _list_formats(limit: int = 20) -> List[Dict[str, Any]]:
    """List available formats (podcasts/shows)."""
    conn = _get_db_connection()
    
    cursor = conn.execute("""
        SELECT id, title, description, source_url, author, language, created_at
        FROM formats
        ORDER BY created_at DESC
        LIMIT ?
    """, [limit])

    columns = [c[0] for c in cursor.description] if getattr(cursor, "description", None) else None
    results = [_row_to_dict(row, columns) for row in _fetch_rows(cursor)]
    conn.close()
    return results


def _get_format_items(format_id: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Get items (episodes) for a format."""
    conn = _get_db_connection()
    
    cursor = conn.execute("""
        SELECT id, title, audio_url, published_at, duration, transcript_status
        FROM items
        WHERE format_id = ?
        ORDER BY published_at DESC
        LIMIT ?
    """, [format_id, limit])

    columns = [c[0] for c in cursor.description] if getattr(cursor, "description", None) else None
    results = [_row_to_dict(row, columns) for row in _fetch_rows(cursor)]
    conn.close()
    return results


def _analyze_lexical(
    mode: str,
    pattern: str = "",
    item_id: Optional[str] = None,
    format_id: Optional[str] = None,
    limit: int = 50,
    max_sentence_seconds: float = DEFAULT_MAX_SENTENCE_SECONDS,
) -> List[Dict[str, Any]]:
    """Run lexical analysis on fragments.
    
    Searches across ALL fragments (not limited upfront) and returns up to `limit` matches.
    This "search wide, limit results" strategy finds more matches than limiting fragments first.
    
    Args:
        mode: Analysis mode (starts_with, ends_with, contains, alliteration, long_words, short_words)
        pattern: Pattern to match (for starts_with, ends_with, contains)
        item_id: Filter by specific item
        format_id: Filter by format (podcast) - finds all items belonging to this format
        limit: Max results to return
    """
    from stemmy_cli.analyzers.lexical import search_lexical_pattern
    
    conn = _get_db_connection()
    
    # Strategy: search across all matching fragments, stop early when we have enough results
    # This is more effective than LIMIT on fragments - we search broadly but return limited results
    sql = """
        SELECT f.id, f.text, f.words, f.item_audio_url, f.item_id, f.speaker_label,
               f.start_time, f.end_time,
               i.title as item_title, i.audio_url as fallback_audio_url,
               i.format_id
        FROM fragments f
        LEFT JOIN items i ON f.item_id = i.id
        WHERE f.words IS NOT NULL AND f.words != '[]'
    """
    params = []
    
    if item_id:
        sql += " AND f.item_id = ?"
        params.append(item_id)
    elif format_id:
        sql += " AND i.format_id = ?"
        params.append(format_id)
    
    # Order randomly to get diverse results, no hard limit on fragments
    sql += " ORDER BY RANDOM()"
    
    cursor = conn.execute(sql, params)
    columns = [c[0] for c in cursor.description] if getattr(cursor, "description", None) else None
    results = []
    
    # Streaming approach: process rows until we have enough results
    # This searches widely but stops early once limit is reached
    target_results = limit * 3  # Collect more than needed for variety
    pattern_norm = pattern.strip()
    sentence_prefix_search = (mode == "starts_with" and " " in pattern_norm)
    pattern_lc = pattern_norm.lower()
    
    for row in _fetch_rows(cursor):
        if len(results) >= target_results:
            break
        try:
            row_dict = _row_to_dict(row, columns)
            words_data = json.loads(row_dict.get("words", "[]")) if row_dict.get("words") else []
            audio_url = row_dict.get("item_audio_url") or row_dict.get("fallback_audio_url") or ""
            if not words_data:
                continue

            if sentence_prefix_search:
                for sent in _split_sentences(words_data, max_sentence_seconds=max_sentence_seconds):
                    if sent["text"].lower().startswith(pattern_lc):
                        results.append({
                            "id": f"{row_dict.get('id','')}_{int(sent['start_time']*1000)}_{int(sent['end_time']*1000)}",
                            "word": "",
                            "text": sent["text"],
                            "start_time": sent["start_time"],
                            "end_time": sent["end_time"],
                            "duration": sent["duration"],
                            "audio_url": audio_url,
                            "fragment_id": row_dict.get("id", ""),
                            "item_id": row_dict.get("item_id", ""),
                            "item_title": row_dict.get("item_title", ""),
                            "mode": mode,
                            "pattern": pattern,
                        })
                continue

            matches = search_lexical_pattern(
                words_data=words_data,
                mode=mode,
                pattern=pattern,
                audio_url=audio_url,
                fragment_id=row_dict.get("id", ""),
                item_id=row_dict.get("item_id", ""),
                item_title=row_dict.get("item_title", ""),
            )
            for m in matches:
                sent = _sentence_for_word_index(
                    words_data, m.get("word_index", -1), max_sentence_seconds=max_sentence_seconds
                )
                if not sent:
                    continue
                m["text"] = sent["text"]
                m["start_time"] = sent["start_time"]
                m["end_time"] = sent["end_time"]
                m["duration"] = sent["duration"]
                results.append(m)
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
    
    conn.close()
    
    # Deduplicate sentence-level results
    results = _dedupe_segments(results, tolerance_ms=50)
    
    # Shuffle to ensure variety across different sources
    import random
    random.shuffle(results)
    
    return results[:limit]


def _analyze_text(
    mode: str,
    metric: str = "",
    item_id: Optional[str] = None,
    format_id: Optional[str] = None,
    limit: int = 50,
    max_sentence_seconds: float = DEFAULT_MAX_SENTENCE_SECONDS,
) -> List[Dict[str, Any]]:
    """
    Run text analysis on fragments.
    
    For 'questions' and 'exclamations' modes, uses simple text matching.
    For other modes, uses word-level analysis.
    
    Args:
        mode: Analysis mode (questions, exclamations, etc.)
        metric: Additional metric parameter
        item_id: Filter by specific item
        format_id: Filter by format (podcast) - finds all items belonging to this format
        limit: Max results to return
    """
    conn = _get_db_connection()
    
    # Sentence-based modes: questions and exclamations
    if mode in ("questions", "exclamations"):
        sql = """
            SELECT f.id, f.words, f.item_audio_url, f.item_id,
                   i.audio_url as fallback_audio_url
            FROM fragments f
            LEFT JOIN items i ON f.item_id = i.id
            WHERE f.words IS NOT NULL AND f.words != '[]'
        """
        params = []
        if item_id:
            sql += " AND f.item_id = ?"
            params.append(item_id)
        elif format_id:
            sql += " AND i.format_id = ?"
            params.append(format_id)
        sql += " ORDER BY RANDOM()"
        
        cursor = conn.execute(sql, params)
        columns = [c[0] for c in cursor.description] if getattr(cursor, "description", None) else None
        results = []
        for row in _fetch_rows(cursor):
            if len(results) >= limit:
                break
            row_dict = _row_to_dict(row, columns)
            try:
                words_data = json.loads(row_dict.get("words", "[]")) if row_dict.get("words") else []
            except (json.JSONDecodeError, TypeError):
                continue
            audio_url = row_dict.get("item_audio_url") or row_dict.get("fallback_audio_url")
            for sent in _split_sentences(words_data, max_sentence_seconds=max_sentence_seconds):
                if mode == "questions" and sent["end_punct"] != "?":
                    continue
                if mode == "exclamations" and sent["end_punct"] != "!":
                    continue
                results.append({
                    "text": sent["text"],
                    "start_time": sent["start_time"],
                    "end_time": sent["end_time"],
                    "item_audio_url": audio_url,
                    "audio_url": audio_url,
                    "fragment_id": row_dict.get("id"),
                })
                if len(results) >= limit:
                    break
        conn.close()
        return _dedupe_segments(results, tolerance_ms=50)
    
    # Other modes use word-level analysis
    from stemmy_cli.analyzers.text import search_text_pattern
    
    sql = """
        SELECT f.id, f.text, f.words, f.item_audio_url, f.item_id, f.speaker_label,
               f.start_time, f.end_time,
               i.title as item_title, i.audio_url as fallback_audio_url
        FROM fragments f
        LEFT JOIN items i ON f.item_id = i.id
        WHERE f.words IS NOT NULL AND f.words != '[]'
    """
    params = []
    
    if item_id:
        sql += " AND f.item_id = ?"
        params.append(item_id)
    
    sql += " ORDER BY RANDOM()"
    
    cursor = conn.execute(sql, params)
    columns = [c[0] for c in cursor.description] if getattr(cursor, "description", None) else None
    results = []
    
    target_results = limit * 3
    for row in _fetch_rows(cursor):
        if len(results) >= target_results:
            break
        try:
            row_dict = _row_to_dict(row, columns)
            words_data = json.loads(row_dict.get("words", "[]")) if row_dict.get("words") else []
            audio_url = row_dict.get("item_audio_url") or row_dict.get("fallback_audio_url") or ""
            
            matches = search_text_pattern(
                words_data=words_data,
                mode=mode,
                metric=metric,
                audio_url=audio_url,
                fragment_id=row_dict.get("id", ""),
                item_id=row_dict.get("item_id", ""),
                item_title=row_dict.get("item_title", ""),
            )
            results.extend(matches)
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
    
    conn.close()
    return _dedupe_segments(results, tolerance_ms=50)[:limit]


def _get_database_stats() -> Dict[str, int]:
    """Get database statistics."""
    conn = _get_db_connection()
    
    stats = {}
    for table in ["formats", "items", "fragments", "entities", "sounds", "stemmies"]:
        try:
            cursor = conn.execute(f"SELECT COUNT(*) FROM {table}")
            stats[table] = cursor.fetchone()[0]
        except sqlite3.OperationalError:
            stats[table] = 0
    
    conn.close()
    return stats


def _extract_audio(
    audio_url: str,
    start_time: float,
    end_time: float,
    output_path: Optional[str] = None,
    padding_ms: float = 50,
    fade_ms: float = 15,
) -> Dict[str, Any]:
    """
    Extract audio segment using ffmpeg with anti-glitch measures.
    
    Args:
        padding_ms: Extra time to grab before/after segment (prevents cut-off words)
        fade_ms: Micro-fade in/out to prevent click/pop artifacts
    """
    import subprocess
    import tempfile
    import os
    
    if not output_path:
        output_dir = Path(tempfile.gettempdir()) / "stemmy_extracts"
        output_dir.mkdir(exist_ok=True)
        output_path = str(output_dir / f"extract_{int(start_time)}_{int(end_time)}.mp3")
    
    # Add padding to prevent cutting off word beginnings/endings
    padding_sec = padding_ms / 1000
    fade_sec = fade_ms / 1000
    
    padded_start = max(0, start_time - padding_sec)
    padded_duration = (end_time - start_time) + (2 * padding_sec)
    
    # Build ffmpeg command with re-encoding and micro-fade to prevent glitches
    # -c copy causes glitches because it doesn't cut on frame boundaries
    # Re-encoding with afade filter ensures clean cuts
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(padded_start),
        "-i", audio_url,
        "-t", str(padded_duration),
        "-af", f"afade=t=in:st=0:d={fade_sec},afade=t=out:st={padded_duration - fade_sec}:d={fade_sec}",
        "-c:a", "libmp3lame",
        "-q:a", "2",
        output_path
    ]
    
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if Path(output_path).exists() and Path(output_path).stat().st_size > 0:
            return {
                "success": True,
                "output_path": output_path,
                "duration": padded_duration,
            }
        else:
            return {"success": False, "error": "FFmpeg failed to create output file"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _batch_extract_audio(
    segments: List[Dict[str, Any]],
    output_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Extract multiple audio segments."""
    import tempfile
    
    if not output_dir:
        output_dir = str(Path(tempfile.gettempdir()) / "stemmy_batch")
    
    Path(output_dir).mkdir(exist_ok=True, parents=True)
    
    results = []
    for i, seg in enumerate(segments[:50]):  # Limit to 50 segments
        audio_url = seg.get("audio_url") or seg.get("item_audio_url") or seg.get("source_audio_url")
        start = float(seg.get("start_time", 0) or 0)
        end = float(seg.get("end_time", 0) or 0)
        
        # Convert milliseconds to seconds if values are large (> 10000 = 10 sec threshold)
        if start > 10000 or end > 10000:
            start = start / 1000.0
            end = end / 1000.0
        
        if end <= start:
            end = start + 1
        
        if not audio_url:
            results.append({"index": i, "success": False, "error": "No audio URL"})
            continue
        
        output_path = str(Path(output_dir) / f"segment_{i:03d}.mp3")
        extract_res = _extract_audio(audio_url, start, end, output_path)
        extract_res["index"] = i
        results.append(extract_res)
    
    successful = sum(1 for r in results if r.get("success"))
    return {
        "total": len(segments),
        "extracted": successful,
        "output_dir": output_dir,
        "results": results,  # Full list needed for transcript metadata order
    }


def _create_compilation(
    query: str,
    compilation_type: str = "word",
    entity_type: Optional[str] = None,
    format_id: Optional[str] = None,
    use_word_timing: bool = True,
    limit: int = 20,
    output_path: Optional[str] = None,
    search_mode: str = "text",
    segment_mode: str = "fragment",
) -> Dict[str, Any]:
    """
    Create an audio compilation from search results.
    
    Args:
        query: Text or concept to search for
        compilation_type: "word" (use word-level timing), "entity", or "fragment" (whole sentence)
        entity_type: Filter entities by type (person_name, organization, etc.)
        format_id: Filter to specific podcast (from list_formats)
        use_word_timing: If True, extract only the specific word (not whole fragment)
        limit: Max segments to include
        output_path: Where to save the MP3
        search_mode: "text" (keyword), "semantic" (meaning-based), or "entity"
        segment_mode: "fragment" (utterance-level) or "sentence" (split on .!?)
    """
    import subprocess
    import tempfile
    
    # Search for matching content
    if compilation_type == "entity":
        segments = _search_entities(query, entity_type=entity_type, format_id=format_id, limit=limit)
    elif search_mode == "semantic":
        segments = _search_semantic(query, format_id=format_id, limit=limit)
        use_word_timing = False
    elif search_mode == "hybrid":
        segments = _search_hybrid(query, format_id=format_id, limit=limit)
        use_word_timing = False
    else:
        segments = _search_fragments(query, format_id=format_id, limit=limit)
    
    if not segments:
        return {"success": False, "error": f"No matches found for '{query}'"}

    # Sentence-level: split fragments on .!? using words for timing (only for fragment-based search)
    if segment_mode == "sentence" and compilation_type != "entity":
        segments = _extract_sentences_from_fragments(segments)

    # If word timing requested, extract precise word timings from fragment words JSON
    word_timing_used = False
    if use_word_timing and compilation_type == "word":
        word_segments = _extract_word_timings(segments, query)
        if word_segments:
            segments = word_segments
            word_timing_used = True
        # If no word timings found, fall back to fragment-level extraction
        # This happens when fragments don't have words data
    
    # Create temp directory for extracts
    temp_dir = Path(tempfile.gettempdir()) / "stemmy_compilation"
    temp_dir.mkdir(exist_ok=True)
    
    # Clean old files
    for f in temp_dir.glob("*.mp3"):
        f.unlink()
    
    # Extract all segments
    extract_result = _batch_extract_audio(segments, str(temp_dir))
    
    if extract_result["extracted"] == 0:
        return {"success": False, "error": "Failed to extract any audio segments"}
    
    # Create file list for ffmpeg concat
    file_list = temp_dir / "files.txt"
    with open(file_list, "w") as f:
        for i in range(extract_result["extracted"]):
            seg_path = temp_dir / f"segment_{i:03d}.mp3"
            if seg_path.exists():
                f.write(f"file '{seg_path}'\n")
    
    # Output path - use output/ directory by default
    if not output_path:
        output_dir = _get_output_dir()
        safe_name = "".join(c if c.isalnum() or c in "_ -" else "_" for c in query[:20])
        output_path = str(output_dir / f"compilation_{safe_name}.mp3")
    elif not Path(output_path).is_absolute():
        # Relative paths go to output/ directory
        output_dir = _get_output_dir()
        output_path = str(output_dir / output_path)
    
    # Concatenate
    concat_cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(file_list),
        "-c", "copy",
        output_path
    ]
    
    try:
        subprocess.run(concat_cmd, capture_output=True, text=True, timeout=120)
        if Path(output_path).exists():
            _add_transcript_metadata(output_path, segments, extract_result)
            return {
                "success": True,
                "output_path": output_path,
                "segments_used": extract_result["extracted"],
                "query": query,
                "type": compilation_type,
                "format_id": format_id,
                "word_timing_used": word_timing_used,
                "segment_mode": segment_mode,
                "note": "Used word-level timing" if word_timing_used else "Fallback to fragment-level (no word timing data available)",
            }
        else:
            return {"success": False, "error": "FFmpeg concat failed"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _add_transcript_metadata(mp3_path: str, segments: List[Dict[str, Any]], extract_result: Dict[str, Any]) -> None:
    """
    Add transcript as MP3 metadata (comment tag) from segment texts.
    Uses the same order as concatenated segments (successful extractions only).
    """
    import subprocess

    transcript_parts = []
    for r in extract_result.get("results", []):
        if r.get("success"):
            idx = r.get("index", len(transcript_parts))
            if idx < len(segments):
                transcript_parts.append(segments[idx].get("text", ""))
    if not transcript_parts:
        return

    transcript = "\n".join(t.strip() for t in transcript_parts if t)
    if not transcript:
        return

    # FFmpeg can't overwrite in place; write to temp then replace
    temp_path = str(Path(mp3_path).with_suffix(".meta.mp3"))
    cmd = [
        "ffmpeg", "-y", "-i", mp3_path,
        "-metadata", f"comment={transcript[:65535]}",  # ID3 comment limit
        "-c", "copy",
        temp_path,
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if Path(temp_path).exists():
            Path(temp_path).replace(mp3_path)
    except Exception:
        pass  # Non-fatal: metadata is nice-to-have


def _extract_sentences_from_fragments(segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Split fragment-level segments into sentence-level using words JSON for timing.
    Splits on . ! ? and maps sentence boundaries to word timings.
    Falls back to original fragment if no words data.
    """
    import re

    sentence_segments = []
    for seg in segments:
        words_json = seg.get("words")
        text = seg.get("text", "").strip()
        audio_url = seg.get("item_audio_url") or seg.get("source_audio_url")

        if not words_json or not text:
            sentence_segments.append(seg)
            continue

        try:
            words = json.loads(words_json) if isinstance(words_json, str) else words_json
        except (json.JSONDecodeError, TypeError):
            sentence_segments.append(seg)
            continue

        if not words:
            sentence_segments.append(seg)
            continue

        # Build word char positions: (start_char, end_char, word_data)
        word_texts = [w.get("text", w.get("word", "")) for w in words]
        full_text = " ".join(word_texts)
        pos = 0
        word_positions = []
        for i, w in enumerate(words):
            t = word_texts[i]
            word_positions.append((pos, pos + len(t), w))
            pos += len(t) + (1 if i < len(words) - 1 else 0)

        # Split on sentence boundaries
        parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text) if p.strip()]
        if len(parts) <= 1:
            sentence_segments.append(seg)
            continue

        char_offset = 0
        for part in parts:
            idx = full_text.find(part, char_offset)
            if idx < 0:
                idx = char_offset
            start_char, end_char = idx, idx + len(part)

            first_w = last_w = None
            for (wc_start, wc_end, w) in word_positions:
                if wc_start < end_char and wc_end > start_char:
                    if first_w is None:
                        first_w = w
                    last_w = w

            if first_w and last_w:
                start = first_w.get("start", first_w.get("start_time"))
                end = last_w.get("end", last_w.get("end_time"))
                if start is not None and end is not None:
                    if start > 10000:
                        start, end = start / 1000, end / 1000
                    sentence_segments.append({
                        "text": part,
                        "start_time": start,
                        "end_time": end,
                        "item_audio_url": audio_url,
                        "item_id": seg.get("item_id"),
                    })
            char_offset = end_char

    return sentence_segments if sentence_segments else segments


def _extract_word_timings(segments: List[Dict[str, Any]], query: str) -> List[Dict[str, Any]]:
    """
    Extract precise word-level timings from fragment words JSON.
    
    Supports both single words AND multi-word phrases like "hoe dan ook".
    For phrases: finds consecutive words and returns start of first word to end of last word.
    """
    word_segments = []
    query_lower = query.lower().strip()
    query_words = query_lower.split()
    is_phrase = len(query_words) > 1
    
    for seg in segments:
        words_json = seg.get("words")
        if not words_json:
            continue
        
        try:
            words = json.loads(words_json) if isinstance(words_json, str) else words_json
        except (json.JSONDecodeError, TypeError):
            continue
        
        audio_url = seg.get("item_audio_url") or seg.get("source_audio_url")
        
        if is_phrase:
            # Multi-word phrase extraction: find consecutive sequence
            for i in range(len(words) - len(query_words) + 1):
                # Check if words[i:i+len(query_words)] match the phrase
                match = True
                for j, query_word in enumerate(query_words):
                    word_text = words[i + j].get("text", words[i + j].get("word", "")).lower()
                    # Strip punctuation for comparison
                    word_text_clean = ''.join(c for c in word_text if c.isalnum())
                    query_word_clean = ''.join(c for c in query_word if c.isalnum())
                    if word_text_clean != query_word_clean:
                        match = False
                        break
                
                if match:
                    # Found the phrase! Get timing from first to last word
                    first_word = words[i]
                    last_word = words[i + len(query_words) - 1]
                    
                    start = first_word.get("start", first_word.get("start_time"))
                    end = last_word.get("end", last_word.get("end_time"))
                    
                    if start is not None and end is not None:
                        # Convert milliseconds to seconds if needed
                        if start > 10000:
                            start = start / 1000
                            end = end / 1000
                        
                        # Combine text from all matched words
                        phrase_text = " ".join(
                            words[i + k].get("text", words[i + k].get("word", ""))
                            for k in range(len(query_words))
                        )
                        
                        word_segments.append({
                            "text": phrase_text,
                            "start_time": start,
                            "end_time": end,
                            "item_audio_url": audio_url,
                            "source_query": query,
                        })
        else:
            # Single word extraction (original logic)
            for word_data in words:
                word_text = word_data.get("text", word_data.get("word", ""))
                word_text_clean = ''.join(c for c in word_text.lower() if c.isalnum())
                query_clean = ''.join(c for c in query_lower if c.isalnum())
                
                if word_text_clean == query_clean or query_clean in word_text_clean:
                    start = word_data.get("start", word_data.get("start_time"))
                    end = word_data.get("end", word_data.get("end_time"))
                    
                    if start is not None and end is not None:
                        if start > 10000:
                            start = start / 1000
                            end = end / 1000
                        
                        word_segments.append({
                            "text": word_text,
                            "start_time": start,
                            "end_time": end,
                            "item_audio_url": audio_url,
                            "source_query": query,
                        })
    
    return word_segments


def _run_cli_command(
    command: str,
    args: List[str],
) -> Dict[str, Any]:
    """Run a stemmy CLI command and return the result."""
    import subprocess
    
    full_cmd = ["stemmy", command] + args
    
    try:
        result = subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(get_project_root()),
        )
        return {
            "success": result.returncode == 0,
            "stdout": result.stdout[:5000],
            "stderr": result.stderr[:1000] if result.returncode != 0 else "",
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Command timed out"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _create_shuffled_compilation(
    queries: List[str],
    compilation_type: str = "entity",
    entity_type: Optional[str] = None,
    format_id: Optional[str] = None,
    use_word_timing: bool = True,
    limit_per_query: int = 10,
    shuffle: bool = True,
    output_path: Optional[str] = None,
    search_mode: str = "text",
) -> Dict[str, Any]:
    """
    Create a compilation from multiple queries with results shuffled together.
    
    Args:
        queries: List of search terms (e.g., ["Google", "Microsoft", "Tesla"])
        compilation_type: "entity", "word", or "fragment"
        entity_type: Filter entities by type
        format_id: Filter to specific podcast (from list_formats)
        use_word_timing: If True, extract only the specific words (not whole fragments)
        limit_per_query: Max segments per query
        shuffle: Whether to shuffle results together
        output_path: Where to save the MP3
    """
    import subprocess
    import tempfile
    import random
    
    all_segments = []
    
    # Execute each query and collect results
    for query in queries:
        if compilation_type == "entity":
            segments = _search_entities(query, entity_type=entity_type, format_id=format_id, limit=limit_per_query)
        elif search_mode == "semantic":
            segments = _search_semantic(query, format_id=format_id, limit=limit_per_query)
            use_word_timing = False  # Semantic returns whole fragments
        else:
            segments = _search_fragments(query, format_id=format_id, limit=limit_per_query)
        
        # If word timing requested, extract precise timings
        if use_word_timing and compilation_type == "word" and search_mode != "semantic":
            segments = _extract_word_timings(segments, query)
        
        # Tag each segment with its source query
        for seg in segments:
            seg["source_query"] = query
        all_segments.extend(segments)
    
    if not all_segments:
        return {"success": False, "error": f"No matches found for any queries: {queries}"}
    
    # Shuffle if requested
    if shuffle:
        random.shuffle(all_segments)
    
    # Create temp directory
    temp_dir = Path(tempfile.gettempdir()) / "stemmy_shuffled"
    temp_dir.mkdir(exist_ok=True)
    
    # Extract all segments
    extract_result = _batch_extract_audio(all_segments, str(temp_dir))
    
    if extract_result["extracted"] == 0:
        return {"success": False, "error": "Failed to extract any audio segments"}
    
    # Create file list
    file_list = temp_dir / "files.txt"
    with open(file_list, "w") as f:
        for i in range(extract_result["extracted"]):
            seg_path = temp_dir / f"segment_{i:03d}.mp3"
            if seg_path.exists():
                f.write(f"file '{seg_path}'\n")
    
    # Output path - use output/ directory by default
    if not output_path:
        output_dir = _get_output_dir()
        query_tag = "_".join("".join(c if c.isalnum() else "_" for c in q[:10]) for q in queries[:3])
        output_path = str(output_dir / f"shuffled_{query_tag}.mp3")
    elif not Path(output_path).is_absolute():
        output_dir = _get_output_dir()
        output_path = str(output_dir / output_path)
    
    # Concatenate
    concat_cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(file_list),
        "-c", "copy",
        output_path
    ]
    
    try:
        subprocess.run(concat_cmd, capture_output=True, text=True, timeout=120)
        if Path(output_path).exists():
            _add_transcript_metadata(output_path, all_segments, extract_result)
            return {
                "success": True,
                "output_path": output_path,
                "segments_used": extract_result["extracted"],
                "total_segments": len(all_segments),
                "extracted": extract_result["extracted"],
                "queries": queries,
                "shuffled": shuffle,
            }
        else:
            return {"success": False, "error": "FFmpeg concat failed"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _apply_audio_effects(
    input_path: str,
    output_path: Optional[str] = None,
    fade_in: float = 0,
    fade_out: float = 0,
    normalize: bool = False,
    reverb: bool = False,
    speed: float = 1.0,
    crossfade: float = 0,
) -> Dict[str, Any]:
    """Apply audio effects to an audio file using ffmpeg."""
    import subprocess
    
    if not Path(input_path).exists():
        return {"success": False, "error": f"Input file not found: {input_path}"}
    
    if not output_path:
        output_path = str(Path(input_path).with_suffix(".fx.mp3"))
    
    # Build filter chain
    filters = []
    
    if speed != 1.0:
        filters.append(f"atempo={speed}")
    
    if fade_in > 0:
        filters.append(f"afade=t=in:d={fade_in}")
    
    if reverb:
        filters.append("aecho=0.8:0.9:1000:0.3")
    
    if normalize:
        filters.append("loudnorm=I=-16:TP=-1.5:LRA=11")
    
    # Get duration for fade out
    if fade_out > 0:
        probe_cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", input_path]
        probe_result = subprocess.run(probe_cmd, capture_output=True, text=True)
        try:
            duration = float(probe_result.stdout.strip())
            start_time = max(0, duration - fade_out)
            filters.append(f"afade=t=out:st={start_time}:d={fade_out}")
        except (ValueError, TypeError):
            pass
    
    # Build command
    cmd = ["ffmpeg", "-y", "-i", input_path]
    
    if filters:
        cmd.extend(["-af", ",".join(filters)])
    
    cmd.extend(["-c:a", "libmp3lame", "-b:a", "192k", output_path])
    
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if Path(output_path).exists():
            return {
                "success": True,
                "output_path": output_path,
                "effects_applied": {
                    "fade_in": fade_in,
                    "fade_out": fade_out,
                    "normalize": normalize,
                    "reverb": reverb,
                    "speed": speed,
                },
            }
        else:
            return {"success": False, "error": "FFmpeg processing failed"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _create_dj_compilation(
    query: str,
    preset: str = "hype",
    limit: int = 15,
    output_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a DJ-style compilation with effects based on preset."""
    import subprocess
    import tempfile
    
    # First create basic compilation
    base_result = _create_compilation(query, "word", None, limit, None)
    if not base_result.get("success"):
        return base_result
    
    base_path = base_result["output_path"]
    
    # Apply preset effects
    presets = {
        "hype": {"fade_in": 1.0, "fade_out": 2.0, "speed": 1.1, "normalize": True},
        "smooth": {"fade_in": 2.0, "fade_out": 3.0, "speed": 0.95, "normalize": True},
        "dramatic": {"fade_in": 3.0, "fade_out": 4.0, "reverb": True, "normalize": True},
        "chaos": {"speed": 1.3, "reverb": True, "normalize": True},
    }
    
    effects = presets.get(preset, presets["hype"])
    
    if not output_path:
        output_path = str(Path.cwd() / f"dj_{preset}_{query[:15]}.mp3")
    
    fx_result = _apply_audio_effects(base_path, output_path, **effects)
    
    if fx_result.get("success"):
        fx_result["preset"] = preset
        fx_result["original_segments"] = base_result.get("segments_used", 0)
    
    return fx_result


def _search_music(
    query: str = "background music",
    genre: Optional[str] = None,
    mood: Optional[str] = None,
    min_duration: int = 30,
    max_duration: int = 120,
    limit: int = 5,
) -> Dict[str, Any]:
    """Search for royalty-free music on Pixabay."""
    try:
        from stemmy_cli.services.music_service import search_music
        tracks = search_music(
            query=query,
            genre=genre,
            mood=mood,
            min_duration=min_duration,
            max_duration=max_duration,
            limit=limit,
        )
        return {
            "success": True,
            "tracks": tracks,
            "count": len(tracks),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def _download_music(
    query: str = "background music",
    genre: Optional[str] = None,
    mood: Optional[str] = None,
) -> Dict[str, Any]:
    """Search and download royalty-free background music."""
    try:
        from stemmy_cli.services.music_service import search_and_download
        result = search_and_download(
            query=query,
            genre=genre,
            mood=mood,
            duration_range=(30, 120),
        )
        return result
    except Exception as e:
        return {"success": False, "error": str(e)}


def _layer_audio_tracks(
    tracks: List[str],
    output_path: Optional[str] = None,
    normalize: bool = False,
) -> Dict[str, Any]:
    """Mix/layer multiple audio tracks together (kakofonie effect)."""
    import subprocess
    import tempfile
    
    # Validate tracks exist
    valid_tracks = [t for t in tracks if Path(t).exists()]
    if len(valid_tracks) < 2:
        return {"success": False, "error": "Need at least 2 valid audio files to layer"}
    
    if not output_path:
        output_path = str(Path(tempfile.gettempdir()) / "layered_mix.mp3")
    
    # Build ffmpeg command for mixing
    cmd = ["ffmpeg", "-y"]
    
    for track in valid_tracks:
        cmd.extend(["-i", track])
    
    # amix filter with longest duration
    amix_filter = f"amix=inputs={len(valid_tracks)}:duration=longest"
    if normalize:
        amix_filter += ":normalize=1"
    else:
        amix_filter += ":normalize=0"
    
    filter_complex = f"[0:a]"
    for i in range(1, len(valid_tracks)):
        filter_complex += f"[{i}:a]"
    filter_complex += amix_filter
    
    cmd.extend(["-filter_complex", filter_complex])
    cmd.extend(["-c:a", "libmp3lame", "-b:a", "192k", output_path])
    
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if Path(output_path).exists():
            return {
                "success": True,
                "output_path": output_path,
                "tracks_mixed": len(valid_tracks),
            }
        else:
            return {"success": False, "error": "FFmpeg mixing failed"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _get_audio_duration(file_path: str) -> float:
    """Get duration of an audio file in seconds using ffprobe."""
    import subprocess
    try:
        probe_cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "csv=p=0", file_path
        ]
        result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=30)
        return float(result.stdout.strip())
    except (ValueError, TypeError, subprocess.TimeoutExpired):
        return 0.0


def _mix_with_background_music(
    voice_track: str,
    music_track: str,
    output_path: Optional[str] = None,
    music_volume: float = 0.15,
    music_fade_in: float = 2.0,
    music_fade_out: float = 3.0,
    music_lead_in: float = 2.0,
    music_lead_out: float = 3.0,
    voice_boost: float = 1.0,
    normalize_final: bool = True,
    ducking: bool = True,
    ducking_threshold: float = 0.02,
    ducking_ratio: float = 0.3,
) -> Dict[str, Any]:
    """
    Professionally mix voice/compilation with background music.
    
    This is the SMART mixing tool that applies DJ best practices:
    - Music is softer than voice (default 15% volume)
    - Music has fade-in and fade-out
    - Music starts before voice and extends after voice
    - Total output length = voice_duration + lead_in + lead_out
    - Optional ducking: music automatically lowers when voice is present
    
    Args:
        voice_track: Path to main voice/compilation audio
        music_track: Path to background music file
        output_path: Output path (auto-generated if not provided)
        music_volume: Music volume level (0.0-1.0, default 0.15 = 15%)
        music_fade_in: Fade in duration for music in seconds
        music_fade_out: Fade out duration for music in seconds  
        music_lead_in: Seconds of music before voice starts
        music_lead_out: Seconds of music after voice ends
        voice_boost: Voice volume multiplier (default 1.0)
        normalize_final: Apply loudness normalization to final mix
        ducking: Auto-lower music when voice is present (sidechain compression)
        ducking_threshold: Voice detection threshold for ducking
        ducking_ratio: How much to reduce music during voice (0.3 = 30% of original)
    
    Returns:
        Dict with success status, output path, and mixing details
    """
    import subprocess
    import tempfile
    
    # Validate inputs
    if not Path(voice_track).exists():
        return {"success": False, "error": f"Voice track not found: {voice_track}"}
    
    # Resolve music track - check static/music/ if not found directly
    if not Path(music_track).exists():
        music_dir = _get_music_dir()
        music_in_static = music_dir / Path(music_track).name
        if music_in_static.exists():
            music_track = str(music_in_static)
        else:
            return {"success": False, "error": f"Music track not found: {music_track}"}
    
    # Get durations
    voice_duration = _get_audio_duration(voice_track)
    music_duration = _get_audio_duration(music_track)
    
    if voice_duration <= 0:
        return {"success": False, "error": "Could not determine voice track duration"}
    
    # Calculate target music duration (voice + lead_in + lead_out)
    target_music_duration = voice_duration + music_lead_in + music_lead_out
    
    # Generate output path if not provided
    if not output_path:
        voice_name = Path(voice_track).stem
        output_path = str(Path(voice_track).parent / f"{voice_name}_with_music.mp3")
    
    # Build complex filter graph
    # Step 1: Prepare music track (trim/loop to target length, adjust volume, apply fades)
    # Step 2: Add silence padding before voice (to match music lead-in)
    # Step 3: Mix voice and music
    # Step 4: Optionally apply ducking (sidechained compression)
    # Step 5: Normalize final output
    
    filter_parts = []
    
    # Music processing: trim/loop to target duration
    if music_duration < target_music_duration:
        # Loop music if it's shorter than needed
        loops_needed = int(target_music_duration / music_duration) + 1
        filter_parts.append(f"[1:a]aloop=loop={loops_needed}:size={int(music_duration * 44100)}[music_looped]")
        filter_parts.append(f"[music_looped]atrim=0:{target_music_duration}[music_trimmed]")
    else:
        # Trim music to target duration
        filter_parts.append(f"[1:a]atrim=0:{target_music_duration}[music_trimmed]")
    
    # Apply volume and fades to music
    music_filters = [f"volume={music_volume}"]
    if music_fade_in > 0:
        music_filters.append(f"afade=t=in:d={music_fade_in}")
    if music_fade_out > 0:
        fade_start = target_music_duration - music_fade_out
        music_filters.append(f"afade=t=out:st={fade_start}:d={music_fade_out}")
    
    filter_parts.append(f"[music_trimmed]{','.join(music_filters)}[music_ready]")
    
    # Voice processing: add lead-in silence so voice starts after music intro
    voice_filters = []
    if voice_boost != 1.0:
        voice_filters.append(f"volume={voice_boost}")
    
    # Add delay (silence) at the start of voice track to match music lead-in
    delay_ms = int(music_lead_in * 1000)
    voice_filter_str = f"[0:a]adelay={delay_ms}|{delay_ms}"
    if voice_filters:
        voice_filter_str += f",{','.join(voice_filters)}"
    voice_filter_str += "[voice_ready]"
    filter_parts.append(voice_filter_str)
    
    # Mix voice and music
    if ducking:
        # Advanced: use sidechaincompress for automatic ducking
        # Voice triggers compression of music when voice is present
        filter_parts.append(
            f"[music_ready][voice_ready]sidechaincompress="
            f"threshold={ducking_threshold}:ratio={int(1/ducking_ratio)}:"
            f"attack=0.1:release=0.5[music_ducked]"
        )
        filter_parts.append("[music_ducked][voice_ready]amix=inputs=2:duration=longest[mixed]")
    else:
        # Simple mix without ducking
        filter_parts.append("[voice_ready][music_ready]amix=inputs=2:duration=longest:normalize=0[mixed]")
    
    # Final processing
    if normalize_final:
        filter_parts.append("[mixed]loudnorm=I=-16:TP=-1.5:LRA=11[final]")
        final_label = "[final]"
    else:
        final_label = "[mixed]"
    
    # Build full filter complex
    filter_complex = ";".join(filter_parts)
    
    # Build ffmpeg command
    cmd = [
        "ffmpeg", "-y",
        "-i", voice_track,
        "-i", music_track,
        "-filter_complex", filter_complex,
        "-map", final_label,
        "-c:a", "libmp3lame", "-b:a", "192k",
        output_path
    ]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        
        if Path(output_path).exists() and Path(output_path).stat().st_size > 0:
            final_duration = _get_audio_duration(output_path)
            return {
                "success": True,
                "output_path": output_path,
                "voice_duration": round(voice_duration, 2),
                "music_duration": round(music_duration, 2),
                "final_duration": round(final_duration, 2),
                "settings": {
                    "music_volume": f"{int(music_volume * 100)}%",
                    "music_fade_in": music_fade_in,
                    "music_fade_out": music_fade_out,
                    "music_lead_in": music_lead_in,
                    "music_lead_out": music_lead_out,
                    "ducking": ducking,
                    "normalized": normalize_final,
                },
            }
        else:
            # Fallback to simpler mixing if complex filter fails
            return _mix_with_background_music_simple(
                voice_track, music_track, output_path,
                music_volume, music_fade_in, music_fade_out,
                music_lead_in, music_lead_out, normalize_final
            )
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Audio mixing timed out"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _mix_with_background_music_simple(
    voice_track: str,
    music_track: str,
    output_path: str,
    music_volume: float = 0.15,
    music_fade_in: float = 2.0,
    music_fade_out: float = 3.0,
    music_lead_in: float = 2.0,
    music_lead_out: float = 3.0,
    normalize_final: bool = True,
) -> Dict[str, Any]:
    """Simpler fallback mixing without advanced ducking."""
    import subprocess
    import tempfile
    
    voice_duration = _get_audio_duration(voice_track)
    target_duration = voice_duration + music_lead_in + music_lead_out
    
    # Step 1: Prepare music with correct length, volume, and fades
    temp_music = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    temp_music_path = temp_music.name
    temp_music.close()
    
    fade_start = max(0, target_duration - music_fade_out)
    music_filter = (
        f"atrim=0:{target_duration},"
        f"volume={music_volume},"
        f"afade=t=in:d={music_fade_in},"
        f"afade=t=out:st={fade_start}:d={music_fade_out}"
    )
    
    music_cmd = [
        "ffmpeg", "-y", "-stream_loop", "-1",
        "-i", music_track,
        "-af", music_filter,
        "-t", str(target_duration),
        "-c:a", "libmp3lame", "-b:a", "192k",
        temp_music_path
    ]
    
    subprocess.run(music_cmd, capture_output=True, text=True, timeout=60)
    
    # Step 2: Add silence to voice for lead-in
    temp_voice = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    temp_voice_path = temp_voice.name
    temp_voice.close()
    
    delay_ms = int(music_lead_in * 1000)
    voice_cmd = [
        "ffmpeg", "-y",
        "-i", voice_track,
        "-af", f"adelay={delay_ms}|{delay_ms}",
        "-c:a", "libmp3lame", "-b:a", "192k",
        temp_voice_path
    ]
    
    subprocess.run(voice_cmd, capture_output=True, text=True, timeout=60)
    
    # Step 3: Mix tracks
    mix_filter = "amix=inputs=2:duration=longest:normalize=0"
    if normalize_final:
        mix_filter += ",loudnorm=I=-16:TP=-1.5:LRA=11"
    
    mix_cmd = [
        "ffmpeg", "-y",
        "-i", temp_voice_path,
        "-i", temp_music_path,
        "-filter_complex", mix_filter,
        "-c:a", "libmp3lame", "-b:a", "192k",
        output_path
    ]
    
    try:
        subprocess.run(mix_cmd, capture_output=True, text=True, timeout=120)
        
        # Cleanup temp files
        Path(temp_music_path).unlink(missing_ok=True)
        Path(temp_voice_path).unlink(missing_ok=True)
        
        if Path(output_path).exists():
            final_duration = _get_audio_duration(output_path)
            return {
                "success": True,
                "output_path": output_path,
                "voice_duration": round(voice_duration, 2),
                "final_duration": round(final_duration, 2),
                "method": "simple_mix",
                "settings": {
                    "music_volume": f"{int(music_volume * 100)}%",
                    "music_fade_in": music_fade_in,
                    "music_fade_out": music_fade_out,
                    "music_lead_in": music_lead_in,
                    "music_lead_out": music_lead_out,
                },
            }
        else:
            return {"success": False, "error": "Simple mix failed"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# Tool handler mapping
TOOL_HANDLERS: Dict[str, Callable] = {
    "search_fragments": _search_fragments,
    "search_semantic": _search_semantic,
    "search_hybrid": _search_hybrid,
    "search_entities": _search_entities,
    "list_formats": _list_formats,
    "get_format_items": _get_format_items,
    "analyze_lexical": _analyze_lexical,
    "analyze_text": _analyze_text,
    "get_database_stats": _get_database_stats,
    "extract_audio": _extract_audio,
    "batch_extract_audio": _batch_extract_audio,
    "create_compilation": _create_compilation,
    "create_shuffled_compilation": _create_shuffled_compilation,
    "create_dj_compilation": _create_dj_compilation,
    "apply_audio_effects": _apply_audio_effects,
    "layer_audio_tracks": _layer_audio_tracks,
    "mix_with_background_music": _mix_with_background_music,
    "search_music": _search_music,
    "download_music": _download_music,
    "run_cli_command": _run_cli_command,
}


# Tool registry with OpenAI-compatible schemas
TOOL_REGISTRY: Dict[str, Tool] = {
    "search_fragments": {
        "name": "search_fragments",
        "description": "Search audio fragments by text content. Use this to find spoken words or phrases in podcast transcripts. Returns fragment text, start/end timing in seconds, and audio URL for extraction.",
        "category": "search",
        "parameters": {
            "query": {"type": "string", "description": "Text to search for in transcripts (case-insensitive, partial match)", "required": True},
            "limit": {"type": "integer", "description": "Maximum results to return (default 20)", "required": False, "default": 20},
            "item_id": {"type": "string", "description": "Filter to specific episode by item ID", "required": False},
            "format_id": {"type": "string", "description": "Filter to specific podcast by format ID", "required": False},
        },
        "handler": "search_fragments",
    },
    "search_semantic": {
        "name": "search_semantic",
        "description": "Semantic search: find fragments by meaning, not exact words. Use for conceptual queries like 'discussions about AI', 'skeptical reactions', 'passionate debates'. Returns fragment text, timing, audio URL. Requires embeddings (run stemmy embeddings build first).",
        "category": "search",
        "parameters": {
            "query": {"type": "string", "description": "Concept or topic to search for (natural language)", "required": True},
            "limit": {"type": "integer", "description": "Maximum results (default 20)", "required": False, "default": 20},
            "format_id": {"type": "string", "description": "Filter to specific podcast by format ID", "required": False},
            "provider": {"type": "string", "description": "Embedding provider: openai or local (default openai)", "required": False, "default": "openai"},
        },
        "handler": "search_semantic",
    },
    "search_hybrid": {
        "name": "search_hybrid",
        "description": "Combine semantic + keyword search. Use when you want both conceptual matches AND exact text matches. Returns merged, deduplicated results.",
        "category": "search",
        "parameters": {
            "query": {"type": "string", "description": "Search query (concept or keyword)", "required": True},
            "limit": {"type": "integer", "description": "Maximum results (default 25)", "required": False, "default": 25},
            "format_id": {"type": "string", "description": "Filter to specific podcast", "required": False},
        },
        "handler": "search_hybrid",
    },
    "search_entities": {
        "name": "search_entities",
        "description": "Search named entities extracted from transcripts. Use this to find mentions of people, companies, locations, dates, times, or money amounts. Returns entity text, type, timing, and audio URL.",
        "category": "search",
        "parameters": {
            "query": {"type": "string", "description": "Entity name or partial name to search", "required": True},
            "entity_type": {
                "type": "string",
                "description": "Filter by entity type: person_name (people), organization (companies), location (places), money_amount, date, time",
                "required": False,
                "enum": ["person_name", "organization", "location", "money_amount", "date", "time"],
            },
            "format_id": {"type": "string", "description": "Filter to specific podcast by format ID (get from list_formats)", "required": False},
            "limit": {"type": "integer", "description": "Maximum results (default 20)", "required": False, "default": 20},
        },
        "handler": "search_entities",
    },
    "list_formats": {
        "name": "list_formats",
        "description": "List all available podcasts/shows in the database. Returns format ID, title, description, RSS URL, author, and language.",
        "category": "browse",
        "parameters": {
            "limit": {"type": "integer", "description": "Maximum formats to return (default 20)", "required": False, "default": 20},
        },
        "handler": "list_formats",
    },
    "get_format_items": {
        "name": "get_format_items",
        "description": "Get episodes for a specific podcast. Returns item ID, title, audio URL, publish date, duration, and transcript status.",
        "category": "browse",
        "parameters": {
            "format_id": {"type": "string", "description": "Format ID (get from list_formats)", "required": True},
            "limit": {"type": "integer", "description": "Maximum episodes to return (default 10)", "required": False, "default": 10},
        },
        "handler": "get_format_items",
    },
    "analyze_lexical": {
        "name": "analyze_lexical",
        "description": "Analyze word patterns in transcripts. Find words by prefix/suffix, alliteration, palindromes, or length. Returns matching words with precise timing for audio extraction.",
        "category": "analyze",
        "parameters": {
            "mode": {
                "type": "string",
                "description": "Analysis mode: starts_with (prefix), ends_with (suffix), contains, exact, regex, alliteration (same starting sounds), palindrome, long_words (10+ chars), short_words (1-3 chars)",
                "required": True,
                "enum": ["starts_with", "ends_with", "contains", "exact", "regex", "alliteration", "palindrome", "long_words", "short_words"],
            },
            "pattern": {"type": "string", "description": "Pattern to match (required for starts_with, ends_with, contains, exact, regex)", "required": False, "default": ""},
            "item_id": {"type": "string", "description": "Filter to specific episode", "required": False},
            "format_id": {"type": "string", "description": "Filter to specific podcast by format ID", "required": False},
            "limit": {"type": "integer", "description": "Maximum results (default 50)", "required": False, "default": 50},
        },
        "handler": "analyze_lexical",
    },
    "analyze_text": {
        "name": "analyze_text",
        "description": "Analyze sentence structure and discourse patterns. Find questions, exclamations, conclusions, transitions, filler words, and more. Returns sentences with timing.",
        "category": "analyze",
        "parameters": {
            "mode": {
                "type": "string",
                "description": "Analysis mode: questions (sentences with ?), exclamations (!), complexity (sentence metrics), coherence (transitions), variation (vocabulary)",
                "required": True,
                "enum": ["questions", "exclamations", "complexity", "coherence", "variation"],
            },
            "metric": {"type": "string", "description": "Sub-metric for complexity/coherence modes", "required": False, "default": ""},
            "item_id": {"type": "string", "description": "Filter to specific episode", "required": False},
            "format_id": {"type": "string", "description": "Filter to specific podcast by format ID", "required": False},
            "limit": {"type": "integer", "description": "Maximum results (default 50)", "required": False, "default": 50},
        },
        "handler": "analyze_text",
    },
    "get_database_stats": {
        "name": "get_database_stats",
        "description": "Get statistics about the database: counts of formats (podcasts), items (episodes), fragments (transcribed sentences), entities (named entities), sounds, and stemmies (compositions).",
        "category": "info",
        "parameters": {},
        "handler": "get_database_stats",
    },
    "extract_audio": {
        "name": "extract_audio",
        "description": "Extract a single audio segment from a source audio file. Use this to cut out a specific portion of audio based on start/end times. Returns the path to the extracted audio file.",
        "category": "audio",
        "parameters": {
            "audio_url": {"type": "string", "description": "URL or path to the source audio file", "required": True},
            "start_time": {"type": "number", "description": "Start time in seconds", "required": True},
            "end_time": {"type": "number", "description": "End time in seconds", "required": True},
            "output_path": {"type": "string", "description": "Output file path (optional, auto-generated if not provided)", "required": False},
        },
        "handler": "extract_audio",
    },
    "batch_extract_audio": {
        "name": "batch_extract_audio",
        "description": "Extract multiple audio segments at once. Pass a list of segments with audio_url, start_time, end_time. Much faster than extracting one by one. Returns paths to all extracted files.",
        "category": "audio",
        "parameters": {
            "segments": {
                "type": "array",
                "description": "List of segment objects, each with audio_url/item_audio_url, start_time, end_time",
                "required": True,
                "items": {"type": "object"},
            },
            "output_dir": {"type": "string", "description": "Directory to save extracted files (optional)", "required": False},
        },
        "handler": "batch_extract_audio",
    },
    "create_compilation": {
        "name": "create_compilation",
        "description": "Create an audio compilation by searching for content and extracting audio. Use search_mode='semantic' for conceptual queries (e.g. 'passionate debates about technology'). Use format_id to filter to a specific podcast.",
        "category": "audio",
        "parameters": {
            "query": {"type": "string", "description": "Search query or concept - what to compile (e.g., 'AI', 'passionate debates', 'skeptical reactions')", "required": True},
            "compilation_type": {"type": "string", "description": "Type: 'word' (extract words with timing), 'entity' (named entities), 'fragment' (whole sentences)", "required": False, "default": "word", "enum": ["word", "entity", "fragment"]},
            "search_mode": {"type": "string", "description": "Search mode: 'text' (keyword), 'semantic' (meaning), 'hybrid' (both)", "required": False, "default": "text", "enum": ["text", "semantic", "hybrid"]},
            "entity_type": {"type": "string", "description": "For entity compilations: person_name, organization, location, etc.", "required": False, "enum": ["person_name", "organization", "location", "money_amount", "date", "time"]},
            "format_id": {"type": "string", "description": "Filter to specific podcast by format ID (IMPORTANT: get from list_formats first!)", "required": False},
            "use_word_timing": {"type": "boolean", "description": "If true, extract ONLY the specific word using word-level timing (not whole fragment). Default: true", "required": False, "default": True},
            "limit": {"type": "integer", "description": "Maximum segments to include (default 20)", "required": False, "default": 20},
            "output_path": {"type": "string", "description": "Output file path (optional)", "required": False},
            "segment_mode": {"type": "string", "description": "Segment granularity: 'fragment' (utterance) or 'sentence' (split on .!?)", "required": False, "default": "fragment", "enum": ["fragment", "sentence"]},
        },
        "handler": "create_compilation",
    },
    "run_cli_command": {
        "name": "run_cli_command",
        "description": "Run any stemmy CLI command directly. Use this for advanced operations not covered by other tools. Returns stdout/stderr.",
        "category": "system",
        "parameters": {
            "command": {"type": "string", "description": "The stemmy subcommand to run (e.g., 'analyze', 'audio', 'fragments')", "required": True},
            "args": {
                "type": "array",
                "description": "List of arguments to pass to the command",
                "required": True,
                "items": {"type": "string"},
            },
        },
        "handler": "run_cli_command",
    },
    "create_shuffled_compilation": {
        "name": "create_shuffled_compilation",
        "description": "Create a compilation from MULTIPLE search queries, shuffling results together. Use search_mode='semantic' for conceptual queries like ['passionate debates', 'skeptical reactions']. Use format_id to filter to a specific podcast.",
        "category": "audio",
        "parameters": {
            "queries": {
                "type": "array",
                "description": "List of search queries or concepts to combine (e.g., ['Google', 'Facebook'] or ['discussions about AI', 'skeptical reactions'])",
                "required": True,
                "items": {"type": "string"},
            },
            "compilation_type": {"type": "string", "description": "Type: 'word' (extract words), 'entity' (named entities), 'fragment' (whole sentences)", "required": False, "default": "entity", "enum": ["word", "entity", "fragment"]},
            "search_mode": {"type": "string", "description": "Search mode: 'text' (keyword), 'semantic' (meaning-based)", "required": False, "default": "text", "enum": ["text", "semantic"]},
            "entity_type": {"type": "string", "description": "For entity compilations: person_name, organization, location, etc.", "required": False, "enum": ["person_name", "organization", "location", "money_amount", "date", "time"]},
            "format_id": {"type": "string", "description": "Filter to specific podcast by format ID (IMPORTANT: get from list_formats first!)", "required": False},
            "use_word_timing": {"type": "boolean", "description": "If true, extract ONLY the specific word using word-level timing. Default: true", "required": False, "default": True},
            "limit_per_query": {"type": "integer", "description": "Max segments per query (default 10)", "required": False, "default": 10},
            "shuffle": {"type": "boolean", "description": "Shuffle results together (default true)", "required": False, "default": True},
            "output_path": {"type": "string", "description": "Output file path (optional)", "required": False},
        },
        "handler": "create_shuffled_compilation",
    },
    "apply_audio_effects": {
        "name": "apply_audio_effects",
        "description": "Apply audio effects to an audio file: fade in/out, reverb, normalize loudness, change speed. Use this for post-production polish on compilations.",
        "category": "audio",
        "parameters": {
            "input_path": {"type": "string", "description": "Path to input audio file", "required": True},
            "output_path": {"type": "string", "description": "Path for output file (optional, defaults to input.fx.mp3)", "required": False},
            "fade_in": {"type": "number", "description": "Fade in duration in seconds (default 0)", "required": False, "default": 0},
            "fade_out": {"type": "number", "description": "Fade out duration in seconds (default 0)", "required": False, "default": 0},
            "normalize": {"type": "boolean", "description": "Apply loudness normalization (default false)", "required": False, "default": False},
            "reverb": {"type": "boolean", "description": "Add reverb/echo effect (default false)", "required": False, "default": False},
            "speed": {"type": "number", "description": "Playback speed multiplier, e.g., 1.2 for 20% faster (default 1.0)", "required": False, "default": 1.0},
        },
        "handler": "apply_audio_effects",
    },
    "layer_audio_tracks": {
        "name": "layer_audio_tracks",
        "description": "Mix/layer multiple audio files on top of each other simultaneously for CHAOS/KAKOFONIE effect. WARNING: Do NOT use this for background music! Use mix_with_background_music instead which properly handles volume, fades, and timing.",
        "category": "audio",
        "parameters": {
            "tracks": {
                "type": "array",
                "description": "List of audio file paths to mix together for chaos effect",
                "required": True,
                "items": {"type": "string"},
            },
            "output_path": {"type": "string", "description": "Output file path (optional)", "required": False},
            "normalize": {"type": "boolean", "description": "Normalize mixed output (default false)", "required": False, "default": False},
        },
        "handler": "layer_audio_tracks",
    },
    "create_dj_compilation": {
        "name": "create_dj_compilation",
        "description": "Create a DJ-style compilation with preset effects. Presets: 'hype' (fast, energetic), 'smooth' (slower, mellow), 'dramatic' (reverb, slow fades), 'chaos' (fast + reverb).",
        "category": "audio",
        "parameters": {
            "query": {"type": "string", "description": "Search query for compilation content", "required": True},
            "preset": {"type": "string", "description": "DJ preset style", "required": False, "default": "hype", "enum": ["hype", "smooth", "dramatic", "chaos"]},
            "limit": {"type": "integer", "description": "Max segments to include (default 15)", "required": False, "default": 15},
            "output_path": {"type": "string", "description": "Output file path (optional)", "required": False},
        },
        "handler": "create_dj_compilation",
    },
    "search_music": {
        "name": "search_music",
        "description": "Search for royalty-free background music on Pixabay. Returns tracks with title, duration, artist, and download URLs.",
        "category": "audio",
        "parameters": {
            "query": {"type": "string", "description": "Search keywords (e.g., 'upbeat electronic', 'chill jazz')", "required": False, "default": "background music"},
            "genre": {"type": "string", "description": "Music genre filter", "required": False, "enum": ["electronic", "jazz", "rock", "classical", "ambient", "hip-hop", "pop"]},
            "mood": {"type": "string", "description": "Mood filter", "required": False, "enum": ["happy", "sad", "energetic", "calm", "dramatic", "romantic"]},
            "min_duration": {"type": "integer", "description": "Minimum duration in seconds (default 30)", "required": False, "default": 30},
            "max_duration": {"type": "integer", "description": "Maximum duration in seconds (default 120)", "required": False, "default": 120},
            "limit": {"type": "integer", "description": "Max results (default 5)", "required": False, "default": 5},
        },
        "handler": "search_music",
    },
    "download_music": {
        "name": "download_music",
        "description": "Search and download a royalty-free music track. Automatically picks the best match and downloads it locally for use in compilations.",
        "category": "audio",
        "parameters": {
            "query": {"type": "string", "description": "Search keywords (e.g., 'upbeat electronic')", "required": False, "default": "background music"},
            "genre": {"type": "string", "description": "Music genre filter", "required": False, "enum": ["electronic", "jazz", "rock", "classical", "ambient", "hip-hop", "pop"]},
            "mood": {"type": "string", "description": "Mood filter", "required": False, "enum": ["happy", "sad", "energetic", "calm", "dramatic", "romantic"]},
        },
        "handler": "download_music",
    },
    "mix_with_background_music": {
        "name": "mix_with_background_music",
        "description": "Professionally mix voice/compilation with background music using DJ best practices. Music is automatically: lowered in volume (15%), faded in/out, trimmed to match compilation length, and optionally ducked when voice is present. THIS IS THE RECOMMENDED TOOL FOR ADDING MUSIC TO COMPILATIONS.",
        "category": "audio",
        "parameters": {
            "voice_track": {"type": "string", "description": "Path to main voice/compilation audio file", "required": True},
            "music_track": {"type": "string", "description": "Path to background music file (from static/music or downloaded)", "required": True},
            "output_path": {"type": "string", "description": "Output file path (optional, auto-generated if not provided)", "required": False},
            "music_volume": {"type": "number", "description": "Music volume level 0.0-1.0 (default 0.15 = 15%, keeps voice prominent)", "required": False, "default": 0.15},
            "music_fade_in": {"type": "number", "description": "Fade in duration for music in seconds (default 2.0)", "required": False, "default": 2.0},
            "music_fade_out": {"type": "number", "description": "Fade out duration for music in seconds (default 3.0)", "required": False, "default": 3.0},
            "music_lead_in": {"type": "number", "description": "Seconds of music before voice starts (default 2.0)", "required": False, "default": 2.0},
            "music_lead_out": {"type": "number", "description": "Seconds of music after voice ends (default 3.0)", "required": False, "default": 3.0},
            "voice_boost": {"type": "number", "description": "Voice volume multiplier (default 1.0, increase to make voice louder)", "required": False, "default": 1.0},
            "normalize_final": {"type": "boolean", "description": "Apply loudness normalization to final mix (default true)", "required": False, "default": True},
            "ducking": {"type": "boolean", "description": "Auto-lower music when voice is present (default true)", "required": False, "default": True},
        },
        "handler": "mix_with_background_music",
    },
}


def get_tool(name: str) -> Optional[Tool]:
    """Get a tool definition by name."""
    return TOOL_REGISTRY.get(name)


def list_tools(category: Optional[str] = None) -> List[Tool]:
    """List all available tools, optionally filtered by category."""
    tools = list(TOOL_REGISTRY.values())
    if category:
        tools = [t for t in tools if t.get("category") == category]
    return tools


def execute_tool(name: str, **kwargs) -> Any:
    """Execute a tool by name with given parameters."""
    handler = TOOL_HANDLERS.get(name)
    if not handler:
        raise ValueError(f"Unknown tool: {name}")
    
    # Filter to only accepted parameters
    tool = TOOL_REGISTRY.get(name)
    if tool:
        accepted_params = set(tool["parameters"].keys())
        kwargs = {k: v for k, v in kwargs.items() if k in accepted_params}
    
    return handler(**kwargs)


def get_openai_tools_schema() -> List[Dict[str, Any]]:
    """Get tool definitions in OpenAI function calling format."""
    tools = []
    for tool in TOOL_REGISTRY.values():
        properties = {}
        required = []
        
        for param_name, param_def in tool["parameters"].items():
            prop = {"type": param_def.get("type", "string")}
            if "description" in param_def:
                prop["description"] = param_def["description"]
            if "enum" in param_def:
                prop["enum"] = param_def["enum"]
            if "items" in param_def:
                prop["items"] = param_def["items"]
            properties[param_name] = prop
            
            if param_def.get("required", False):
                required.append(param_name)
        
        tools.append({
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        })
    
    return tools
