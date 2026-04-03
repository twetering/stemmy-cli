from __future__ import annotations

import json
import importlib.util
import math
import os
import random
import re
import sqlite3
import threading
from pathlib import Path

from duet.state import BeatPlan, Candidate, SceneState
from duet.fragment_palette import FragmentPalette, FragmentRole

# Mapping van BeatPlan.intent → gewenste dramatische rol in de palette
_INTENT_TO_ROLE: dict[str, str] = {
    "opening":    "opener",
    "vraag":      "getuigenis",
    "tegenstem":  "tegenstem",
    "verdieping": "neutraal",
    "wending":    "punchline",
    "landing":    "coda",
}


FRAG_MIN_SEC = 2.5
FRAG_MAX_SEC = 16.0
MAX_SEARCH_RESULTS = 32
GAP_MIN_MS = 450
GAP_MAX_MS = 2600

_formats_cache: list[dict] = []
_formats_lock = threading.Lock()
_gap_cache: dict[str, list[dict]] = {}
_gap_lock = threading.Lock()
_emb_cache: dict[str, list[dict]] = {}
_emb_lock = threading.Lock()
_query_cache: dict[str, list[Candidate]] = {}
_query_cache_lock = threading.Lock()
LATENCY_PROFILE = os.environ.get("DUET_LATENCY_PROFILE", "fast").lower()


def load_formats(db_path: str) -> list[dict]:
    with _formats_lock:
        if _formats_cache:
            return list(_formats_cache)
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            """
            SELECT f.id, f.title, COUNT(DISTINCT frag.id) as n
            FROM formats f
            JOIN items i ON i.format_id = f.id
            JOIN fragments frag ON frag.item_id = i.id
            WHERE frag.text IS NOT NULL AND frag.item_audio_url IS NOT NULL
            GROUP BY f.id
            ORDER BY n DESC
            """
        ).fetchall()
        conn.close()
        _formats_cache.extend({"id": row[0], "title": row[1], "frags": row[2]} for row in rows)
        return list(_formats_cache)


def _tokenize(query: str) -> list[str]:
    return [token for token in re.findall(r"[\w']+", (query or "").lower()) if len(token) > 2]


def _is_questionish(text: str) -> bool:
    text_norm = (text or "").strip().lower()
    if not text_norm:
        return False
    if "?" in text_norm:
        return True
    prefixes = (
        "waarom",
        "hoe",
        "wat",
        "wanneer",
        "wie",
        "welke",
        "kun",
        "kan",
        "heb",
        "ben",
        "is",
        "zijn",
        "wil",
        "moet",
        "zou",
        "denk",
    )
    return text_norm.startswith(prefixes)


def _looks_like_bad_fragment(text: str) -> bool:
    text_norm = " ".join((text or "").strip().lower().split())
    if not text_norm:
        return True
    banned_phrases = (
        "non-speech segment",
        "muzieksegment",
        "muziek segment",
        "applaus segment",
        "tussenruimte segment",
        "advertentie",
        "promo",
        "jingle",
    )
    if any(phrase in text_norm for phrase in banned_phrases):
        return True
    tokens = _tokenize(text_norm)
    if len(tokens) < 3:
        return True
    if len(set(tokens)) <= 2 and len(tokens) >= 4:
        return True
    letters = sum(ch.isalpha() for ch in text_norm)
    if letters < max(8, len(text_norm) * 0.45):
        return True
    return False


def _open_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _search_text(query: str, db_path: str, format_id: str | None, limit: int) -> list[Candidate]:
    tokens = _tokenize(query)
    if not tokens:
        return []
    like_clauses = " OR ".join(["LOWER(f.text) LIKE ?" for _ in tokens])
    token_and = " AND ".join(["LOWER(f.text) LIKE ?" for _ in tokens])
    params = [FRAG_MIN_SEC, FRAG_MAX_SEC] + [f"%{token}%" for token in tokens]
    conn = _open_conn(db_path)
    sql = f"""
        SELECT f.id, f.text, f.item_audio_url, f.start_time_seconds, f.end_time_seconds
        FROM fragments f
        JOIN items i ON f.item_id = i.id
        WHERE f.text IS NOT NULL
          AND f.item_audio_url IS NOT NULL
          AND f.start_time_seconds IS NOT NULL
          AND f.end_time_seconds IS NOT NULL
          AND (CAST(f.end_time_seconds AS REAL) - CAST(f.start_time_seconds AS REAL)) BETWEEN ? AND ?
          AND ({like_clauses})
    """
    if format_id:
        sql += " AND i.format_id = ?"
        params.append(format_id)
    sql += " ORDER BY ABS((CAST(f.end_time_seconds AS REAL) - CAST(f.start_time_seconds AS REAL)) - 7.0) LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    if len(rows) < limit and len(tokens) > 1:
        params2 = [FRAG_MIN_SEC, FRAG_MAX_SEC] + [f"%{token}%" for token in tokens]
        sql2 = f"""
            SELECT f.id, f.text, f.item_audio_url, f.start_time_seconds, f.end_time_seconds
            FROM fragments f
            JOIN items i ON f.item_id = i.id
            WHERE f.text IS NOT NULL
              AND f.item_audio_url IS NOT NULL
              AND f.start_time_seconds IS NOT NULL
              AND f.end_time_seconds IS NOT NULL
              AND (CAST(f.end_time_seconds AS REAL) - CAST(f.start_time_seconds AS REAL)) BETWEEN ? AND ?
              AND {" AND ".join(["LOWER(f.text) LIKE ?" for _ in tokens])}
        """
        if format_id:
            sql2 += " AND i.format_id = ?"
            params2.append(format_id)
        sql2 += " LIMIT ?"
        params2.append(limit)
        try:
            rows.extend(conn.execute(sql2, params2).fetchall())
        except Exception:
            pass
    conn.close()
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for row in rows:
        if row["id"] in seen:
            continue
        text = (row["text"] or "").strip()
        if _looks_like_bad_fragment(text):
            continue
        seen.add(row["id"])
        dur = float(row["end_time_seconds"]) - float(row["start_time_seconds"])
        candidates.append(
            Candidate(
                id=str(row["id"]),
                text=text,
                item_audio_url=row["item_audio_url"],
                start_time_seconds=float(row["start_time_seconds"]),
                end_time_seconds=float(row["end_time_seconds"]),
                source="text",
                similarity=0.52,
                duration_seconds=dur,
                question_score=1.0 if _is_questionish(text) else 0.0,
            )
        )
    return candidates[:limit]


def _load_embedding_cache(db_path: str, table: str) -> list[dict]:
    cache_key = f"{db_path}:{table}"
    with _emb_lock:
        if cache_key in _emb_cache:
            return _emb_cache[cache_key]
        conn = _open_conn(db_path)
        rows = conn.execute(
            f"""
            SELECT e.embedding, f.id, f.text, f.item_audio_url, f.start_time_seconds, f.end_time_seconds
            FROM {table} e
            JOIN fragments f ON f.id = e.fragment_id
            WHERE f.text IS NOT NULL
              AND f.item_audio_url IS NOT NULL
              AND f.start_time_seconds IS NOT NULL
              AND f.end_time_seconds IS NOT NULL
            """
        ).fetchall()
        conn.close()
        parsed = []
        for row in rows:
            try:
                emb = json.loads(row["embedding"])
            except Exception:
                continue
            parsed.append(
                {
                    "embedding": [float(x) for x in emb],
                    "id": str(row["id"]),
                    "text": (row["text"] or "").strip(),
                    "item_audio_url": row["item_audio_url"],
                    "start_time_seconds": float(row["start_time_seconds"]),
                    "end_time_seconds": float(row["end_time_seconds"]),
                }
            )
        _emb_cache[cache_key] = parsed
        return parsed


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    denom_a = math.sqrt(sum(x * x for x in a)) or 1e-9
    denom_b = math.sqrt(sum(x * x for x in b)) or 1e-9
    n = min(len(a), len(b))
    return sum(a[i] * b[i] for i in range(n)) / (denom_a * denom_b)


def _embed_query_text(query: str, db_path: str) -> list[float] | None:
    try:
        from stemmy_cli.embeddings.service import EmbeddingService
    except Exception:
        return None
    has_openai = bool(os.environ.get("OPENAI_API_KEY"))
    has_local = importlib.util.find_spec("sentence_transformers") is not None
    if not has_openai and not has_local:
        return None
    try:
        cache = _load_embedding_cache(db_path, "fragment_embeddings")
        provider = "openai" if cache and len(cache[0]["embedding"]) > 500 else "local"
        if provider == "openai" and not has_openai:
            return None
        if provider == "local" and not has_local:
            return None
        service = EmbeddingService(provider=provider)
        result = service.embed_text(query)
        return result.embedding if result else None
    except Exception:
        return None


def _search_semantic(query: str, db_path: str, format_id: str | None, limit: int) -> list[Candidate]:
    query_embedding = _embed_query_text(query, db_path)
    if not query_embedding:
        return []
    rows = _load_embedding_cache(db_path, "fragment_embeddings")
    scored = []
    for row in rows:
        if _looks_like_bad_fragment(row["text"]):
            continue
        if format_id:
            pass
        sim = _cosine_similarity(query_embedding, row["embedding"])
        dur = float(row["end_time_seconds"]) - float(row["start_time_seconds"])
        if dur < FRAG_MIN_SEC or dur > FRAG_MAX_SEC:
            continue
        scored.append(
            Candidate(
                id=row["id"],
                text=row["text"],
                item_audio_url=row["item_audio_url"],
                start_time_seconds=row["start_time_seconds"],
                end_time_seconds=row["end_time_seconds"],
                source="semantic",
                similarity=sim,
                duration_seconds=dur,
                question_score=1.0 if _is_questionish(row["text"]) else 0.0,
            )
        )
    scored.sort(key=lambda item: item.similarity, reverse=True)
    if format_id:
        conn = _open_conn(db_path)
        allowed = {
            str(row[0])
            for row in conn.execute(
                """
                SELECT f.id
                FROM fragments f
                JOIN items i ON f.item_id = i.id
                WHERE i.format_id = ?
                """,
                (format_id,),
            ).fetchall()
        }
        conn.close()
        scored = [item for item in scored if item.id in allowed]
    return scored[:limit]


def _search_gemini_semantic(query: str, db_path: str, format_id: str | None, limit: int) -> list[Candidate]:
    try:
        import urllib.request
    except Exception:
        return []
    api_key = os.environ.get("GOOGLE_API_KEY", "")
    if not api_key:
        return []
    try:
        payload = json.dumps({"content": {"parts": [{"text": query}]}}).encode()
        req = urllib.request.Request(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-2-preview:embedContent?key="
            + api_key,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
        query_embedding = data.get("embedding", {}).get("values")
        if not query_embedding:
            return []
    except Exception:
        return []
    rows = _load_embedding_cache(db_path, "fragment_gemini_embeddings")
    scored: list[Candidate] = []
    for row in rows:
        if _looks_like_bad_fragment(row["text"]):
            continue
        dur = float(row["end_time_seconds"]) - float(row["start_time_seconds"])
        if dur < FRAG_MIN_SEC or dur > FRAG_MAX_SEC:
            continue
        sim = _cosine_similarity(query_embedding, row["embedding"])
        scored.append(
            Candidate(
                id=row["id"],
                text=row["text"],
                item_audio_url=row["item_audio_url"],
                start_time_seconds=row["start_time_seconds"],
                end_time_seconds=row["end_time_seconds"],
                source="audio-semantic",
                similarity=sim,
                duration_seconds=dur,
                question_score=1.0 if _is_questionish(row["text"]) else 0.0,
            )
        )
    scored.sort(key=lambda item: item.similarity, reverse=True)
    if format_id:
        conn = _open_conn(db_path)
        allowed = {
            str(row[0])
            for row in conn.execute(
                """
                SELECT f.id
                FROM fragments f
                JOIN items i ON f.item_id = i.id
                WHERE i.format_id = ?
                """,
                (format_id,),
            ).fetchall()
        }
        conn.close()
        scored = [item for item in scored if item.id in allowed]
    return scored[:limit]


def _opening_strength(text: str) -> float:
    text = (text or "").strip()
    if not text:
        return 0.0
    words = _tokenize(text[:48])
    if not words:
        return 0.1
    return min(1.0, 0.25 + len(words[0]) / 10 + (0.2 if text[:1].isupper() else 0.0))


def _ending_strength(text: str) -> float:
    text = (text or "").strip()
    if not text:
        return 0.0
    bonus = 0.2 if text.endswith(("?", "!", ".")) else 0.0
    tail = _tokenize(text[-36:])
    return min(1.0, 0.2 + (len(tail[-1]) / 10 if tail else 0.1) + bonus)


def _search_entity(entity_name: str, db_path: str, format_id: str | None, limit: int) -> list[Candidate]:
    """Zoek fragmenten via de entities-tabel (voor entity search mode)."""
    conn = _open_conn(db_path)
    sql = """
        SELECT DISTINCT f.id, f.text, f.item_audio_url, f.start_time_seconds, f.end_time_seconds
        FROM entities e
        JOIN fragments f ON f.id = e.fragment_id
        JOIN items i ON f.item_id = i.id
        WHERE e.text LIKE ?
          AND e.entity_type IN ('person_name', 'organization', 'location')
          AND f.text IS NOT NULL
          AND f.item_audio_url IS NOT NULL
          AND f.start_time_seconds IS NOT NULL
          AND f.end_time_seconds IS NOT NULL
          AND (CAST(f.end_time_seconds AS REAL) - CAST(f.start_time_seconds AS REAL)) BETWEEN ? AND ?
    """
    params: list = [f"%{entity_name}%", FRAG_MIN_SEC, FRAG_MAX_SEC]
    if format_id:
        sql += " AND i.format_id = ?"
        params.append(format_id)
    sql += f" LIMIT {limit}"
    try:
        rows = conn.execute(sql, params).fetchall()
    except Exception:
        rows = []
    conn.close()
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for row in rows:
        if row["id"] in seen:
            continue
        text = (row["text"] or "").strip()
        if _looks_like_bad_fragment(text):
            continue
        seen.add(row["id"])
        dur = float(row["end_time_seconds"]) - float(row["start_time_seconds"])
        candidates.append(Candidate(
            id=str(row["id"]),
            text=text,
            item_audio_url=row["item_audio_url"],
            start_time_seconds=float(row["start_time_seconds"]),
            end_time_seconds=float(row["end_time_seconds"]),
            source="entity",
            similarity=0.60,
            duration_seconds=dur,
            question_score=1.0 if _is_questionish(text) else 0.0,
        ))
    return candidates


def _apply_palette_scoring(candidates: list[Candidate], plan: BeatPlan, scene: SceneState) -> None:
    """Pas palette-gebaseerde score-bonussen toe op candidates (in-place)."""
    palette: FragmentPalette | None = scene.fragment_palette  # type: ignore[attr-defined]
    if not palette:
        return
    preferred_role = _INTENT_TO_ROLE.get(plan.intent, "neutraal")
    # Bouw een snelle lookup: fragment_id → FragmentRole
    role_ids: set[str] = set()
    for fr in palette.by_role.get(preferred_role, []):
        role_ids.add(fr.fragment_id)
    # Entiteits-keten bonus: recent_fragment_ids kan entiteiten gedeeld hebben
    # Haal entiteiten op die in de laatste 3 beats voorkwamen
    recent_entities: set[str] = set()
    if palette.entity_chains:
        for ent, chain_frags in palette.entity_chains.items():
            chain_ids = {f.fragment_id for f in chain_frags}
            if chain_ids & set(scene.recent_fragment_ids[-3:]):
                recent_entities.add(ent)
    # Bouw entity_id lookup
    entity_bonus_ids: set[str] = set()
    for ent in recent_entities:
        for fr in palette.entity_chains.get(ent, []):
            entity_bonus_ids.add(fr.fragment_id)
    for c in candidates:
        if c.id in role_ids:
            c.score += 0.25
            c.atmosphere_hint = preferred_role
        if c.id in entity_bonus_ids:
            c.score += 0.12


def retrieve_candidates(plan: BeatPlan, scene: SceneState, db_path: str, limit: int = MAX_SEARCH_RESULTS) -> list[Candidate]:
    query = plan.search_brief or plan.fallback_brief or scene.theme
    query_key = f"{scene.conversation_mode}|{scene.format_id or 'all'}|{plan.search_mode}|{query.lower()}"
    with _query_cache_lock:
        cached = _query_cache.get(query_key)
    if cached:
        results = [Candidate(**candidate.to_dict()) for candidate in cached[:limit]]
        _apply_palette_scoring(results, plan, scene)
        return results

    # Entity search mode: zoek direct via de entities-tabel
    entity_results: list[Candidate] = []
    if plan.search_mode == "entity" or (
        hasattr(scene, "fragment_palette")
        and scene.fragment_palette
        and scene.fragment_palette.top_entities
        and any(ent.lower() in query.lower() for ent in scene.fragment_palette.top_entities)
    ):
        entity_results = _search_entity(query, db_path, scene.format_id, limit // 2)

    text_results = _search_text(query, db_path, scene.format_id, limit)
    need_semantic = plan.search_mode == "semantic" or (plan.search_mode == "hybrid" and len(text_results) < 10)
    if LATENCY_PROFILE == "fast" and len(text_results) >= 6:
        need_semantic = False
    semantic_results = _search_semantic(query, db_path, scene.format_id, max(8, limit // 2)) if need_semantic else []
    need_audio_semantic = need_semantic and len(text_results) < 5 and LATENCY_PROFILE != "fastest"
    audio_results = _search_gemini_semantic(query, db_path, scene.format_id, max(6, limit // 3)) if need_audio_semantic else []
    merged: dict[str, Candidate] = {}
    for result in semantic_results + audio_results + text_results + entity_results:
        existing = merged.get(result.id)
        if existing is None or result.similarity > existing.similarity:
            merged[result.id] = result
    candidates = list(merged.values())
    for candidate in candidates:
        candidate.opening_strength = _opening_strength(candidate.text)
        candidate.ending_strength = _ending_strength(candidate.text)
        length_bonus = 1.0 - min(abs(candidate.duration_seconds - 7.0) / 7.0, 1.0)
        repeat_penalty = 0.3 if candidate.id in scene.recent_fragment_ids[-6:] else 0.0
        motif_bonus = 0.15 if any(m.lower() in candidate.text.lower() for m in scene.motifs[-4:]) else 0.0
        question_bonus = 0.22 if scene.conversation_mode == "interview" and candidate.question_score > 0 else 0.0
        answer_penalty = 0.08 if scene.conversation_mode == "interview" and candidate.question_score <= 0 else 0.0
        garbage_penalty = 0.18 if _looks_like_bad_fragment(candidate.text) else 0.0
        candidate.score = (
            candidate.similarity * 0.45
            + length_bonus * 0.2
            + candidate.opening_strength * 0.12
            + candidate.ending_strength * 0.12
            + motif_bonus
            + question_bonus
            - repeat_penalty
            - answer_penalty
            - garbage_penalty
        )
    candidates.sort(key=lambda item: item.score, reverse=True)
    with _query_cache_lock:
        _query_cache[query_key] = [Candidate(**candidate.to_dict()) for candidate in candidates[:limit]]
        if len(_query_cache) > 256:
            for key in list(_query_cache.keys())[:64]:
                _query_cache.pop(key, None)
    _apply_palette_scoring(candidates, plan, scene)
    candidates.sort(key=lambda item: item.score, reverse=True)
    return candidates[:limit]


def pick_candidate(candidates: list[Candidate], scene: SceneState) -> Candidate | None:
    if not candidates:
        return None
    sample = candidates[: min(8, len(candidates))]
    weights = []
    for idx, item in enumerate(sample):
        novelty = 0.7 if item.id in scene.recent_fragment_ids[-6:] else 1.0
        weights.append(max(0.05, item.score * novelty * (1.0 - idx * 0.04)))
    return random.choices(sample, weights=weights, k=1)[0]


def _load_gap_index(db_path: str, format_id: str | None) -> list[dict]:
    cache_key = f"{db_path}:{format_id or 'all'}"
    with _gap_lock:
        if cache_key in _gap_cache:
            return _gap_cache[cache_key]
    conn = _open_conn(db_path)
    sql = """
        SELECT f.words, f.item_audio_url
        FROM fragments f
        JOIN items i ON f.item_id = i.id
        WHERE f.words IS NOT NULL
          AND f.words != '[]'
          AND f.item_audio_url IS NOT NULL
    """
    params: tuple = ()
    if format_id:
        sql += " AND i.format_id = ?"
        params = (format_id,)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    gaps: list[dict] = []
    for row in rows:
        try:
            words = json.loads(row["words"])
        except Exception:
            continue
        for idx in range(len(words) - 1):
            gap_ms = int(words[idx + 1].get("start", 0)) - int(words[idx].get("end", 0))
            if GAP_MIN_MS <= gap_ms <= GAP_MAX_MS:
                gaps.append(
                    {
                        "audio_url": row["item_audio_url"],
                        "start_ms": int(words[idx].get("end", 0)) + 40,
                        "end_ms": int(words[idx + 1].get("start", 0)) - 40,
                        "kind": "tussenruimte",
                    }
                )
    random.shuffle(gaps)
    gaps = gaps[:4000]
    with _gap_lock:
        _gap_cache[cache_key] = gaps
    return gaps


def retrieve_pool(theme: str, db_path: str, format_id: str | None, n: int = 60) -> list[dict]:
    """
    Haalt n thematisch relevante fragmenten op voor de Monteur-modus.
    Combineert tekst-search en embedding-search voor diversiteit.
    Retourneert dicts met: id, text, start_time_seconds, end_time_seconds, item_audio_url, duration
    """
    # Gebruik tekst-search als basis
    text_cands = _search_text(theme, db_path, format_id, limit=max(n, 40))

    # Probeer ook semantische search voor diversiteit
    semantic_cands = _search_semantic(theme, db_path, format_id, limit=max(n // 2, 20))
    gemini_cands = _search_gemini_semantic(theme, db_path, format_id, limit=max(n // 3, 10))

    # Merge op basis van kandidaat-ID; bewaar hoogste similarity
    merged: dict[str, Candidate] = {}
    for cand in text_cands + semantic_cands + gemini_cands:
        existing = merged.get(cand.id)
        if existing is None or cand.similarity > existing.similarity:
            merged[cand.id] = cand

    # Filter op duur (2.5–14s)
    POOL_MAX_SEC = 14.0
    filtered = [
        c for c in merged.values()
        if FRAG_MIN_SEC <= (c.end_time_seconds - c.start_time_seconds) <= POOL_MAX_SEC
        and not _looks_like_bad_fragment(c.text)
        and c.item_audio_url
    ]

    # Diversiteits-filter: max 4 fragmenten per item_audio_url prefix (per item)
    # Gebruik de URL als proxy voor item_id
    url_counts: dict[str, int] = {}
    diverse: list[Candidate] = []
    for c in sorted(filtered, key=lambda x: x.similarity, reverse=True):
        # Gebruik de eerste 60 chars van de URL als sleutel voor het item
        url_key = c.item_audio_url[:60]
        if url_counts.get(url_key, 0) < 4:
            diverse.append(c)
            url_counts[url_key] = url_counts.get(url_key, 0) + 1
        if len(diverse) >= n:
            break

    # Als we te weinig hebben: vul aan met willekeurige fragmenten uit de DB
    if len(diverse) < n // 2:
        try:
            conn = _open_conn(db_path)
            extra_sql = """
                SELECT f.id, f.text, f.item_audio_url, f.start_time_seconds, f.end_time_seconds
                FROM fragments f
                JOIN items i ON f.item_id = i.id
                WHERE f.text IS NOT NULL
                  AND f.item_audio_url IS NOT NULL
                  AND f.start_time_seconds IS NOT NULL
                  AND f.end_time_seconds IS NOT NULL
                  AND (CAST(f.end_time_seconds AS REAL) - CAST(f.start_time_seconds AS REAL)) BETWEEN ? AND ?
            """
            extra_params: list = [FRAG_MIN_SEC, POOL_MAX_SEC]
            if format_id:
                extra_sql += " AND i.format_id = ?"
                extra_params.append(format_id)
            extra_sql += " ORDER BY RANDOM() LIMIT ?"
            extra_params.append(n)
            rows = conn.execute(extra_sql, extra_params).fetchall()
            conn.close()
            existing_ids = {c.id for c in diverse}
            for row in rows:
                if str(row["id"]) in existing_ids:
                    continue
                text = (row["text"] or "").strip()
                if _looks_like_bad_fragment(text):
                    continue
                dur = float(row["end_time_seconds"]) - float(row["start_time_seconds"])
                diverse.append(Candidate(
                    id=str(row["id"]),
                    text=text,
                    item_audio_url=row["item_audio_url"],
                    start_time_seconds=float(row["start_time_seconds"]),
                    end_time_seconds=float(row["end_time_seconds"]),
                    source="random",
                    similarity=0.1,
                    duration_seconds=dur,
                ))
                if len(diverse) >= n:
                    break
        except Exception:
            pass

    # Converteer naar dicts
    result = []
    for c in diverse[:n]:
        dur = c.end_time_seconds - c.start_time_seconds
        result.append({
            "id": c.id,
            "text": c.text,
            "start_time_seconds": c.start_time_seconds,
            "end_time_seconds": c.end_time_seconds,
            "item_audio_url": c.item_audio_url,
            "duration": round(dur, 2),
        })
    return result


def pick_gap(scene: SceneState, db_path: str) -> dict | None:
    gaps = _load_gap_index(db_path, scene.format_id)
    if not gaps:
        return None
    sample = gaps[: min(64, len(gaps))]
    for gap in sample:
        if gap["audio_url"] not in scene.recent_gap_urls[-6:]:
            return gap
    return random.choice(sample)
