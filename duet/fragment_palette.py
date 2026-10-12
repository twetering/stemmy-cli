"""
duet/fragment_palette.py — Fragment-intelligentie voor HET DUET 3.0.

Bouwt een getypeerd 'fragment palette' voor een thema:
- analyseert welke fragmenten dramatisch het meest interessant zijn
- herkent entiteiten (personen, organisaties) en bouwt entiteits-ketens
- wijst dramatische rollen toe op basis van heuristieken
- genereert een LLM-samenvatting van wat er in het archief over het thema zit
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.request
from dataclasses import dataclass, field
from typing import Any


FRAG_MIN_SEC = 2.5
FRAG_MAX_SEC = 16.0

DRAMATIC_ROLES = (
    "opener",       # begint sterk, zet de toon
    "tegenstem",    # contradicties, weerwoord, twijfel
    "getuigenis",   # vraagfragmenten, directe ervaringen
    "punchline",    # kort, krachtig, laag-hoog-energie wisseling
    "coda",         # afsluitend, laag tempo, lange zinnen
    "neutraal",     # geen specifieke dramatische rol
)


@dataclass
class FragmentRole:
    fragment_id: str
    text: str
    item_audio_url: str
    start_time_seconds: float
    end_time_seconds: float
    dramatic_role: str
    energy_score: float
    entity_mentions: list[str] = field(default_factory=list)
    speaker_label: str = ""
    episode_title: str = ""
    words_data: list[dict[str, Any]] = field(default_factory=list)  # woord-timing


@dataclass
class FragmentPalette:
    theme: str
    by_role: dict[str, list[FragmentRole]]
    entity_chains: dict[str, list[FragmentRole]]  # entiteitsnaam → fragmenten
    summary: str = ""                              # LLM-samenvatting
    top_entities: list[str] = field(default_factory=list)
    total_found: int = 0


def _tokenize(text: str) -> list[str]:
    return [t for t in re.findall(r"[\w']+", (text or "").lower()) if len(t) > 2]


def _energy_score(text: str) -> float:
    """Schat de energie van een fragment op basis van tekst-kenmerken."""
    if not text:
        return 0.3
    words = _tokenize(text)
    if not words:
        return 0.3
    duration_proxy = max(1.0, len(words) / 2.5)  # schatting van seconden
    wps = len(words) / duration_proxy
    question_bonus = 0.15 if "?" in text else 0.0
    exclaim_bonus = 0.12 if "!" in text else 0.0
    short_bonus = 0.10 if len(words) < 12 else 0.0
    score = min(1.0, (wps / 4.0) * 0.55 + question_bonus + exclaim_bonus + short_bonus + 0.20)
    return round(score, 3)


_CONTRA_WORDS = {
    "maar", "toch", "echter", "eigenlijk", "integendeel", "juist", "nee", "niet",
    "nooit", "nergens", "niemand", "helemaal niet", "absoluut niet", "onzin",
    "klopt niet", "fout", "verkeerd", "anders", "tegenstelling", "paradox",
}

_QUESTION_STARTERS = (
    "waarom", "hoe", "wat", "wanneer", "wie", "welke", "kun", "kan",
    "heb", "ben", "is", "zijn", "wil", "moet", "zou", "denk",
)


def _assign_dramatic_role(text: str) -> str:
    if not text:
        return "neutraal"
    text_l = text.lower().strip()
    words = _tokenize(text)
    n = len(words)

    # Getuigenis: vraagfragmenten
    if "?" in text_l or text_l.startswith(_QUESTION_STARTERS):
        return "getuigenis"

    # Tegenstem: woorden die weerwoord geven
    if any(w in text_l for w in _CONTRA_WORDS):
        return "tegenstem"

    # Opener: begint met hoofdletter, sterk openingswoord
    first_word = words[0] if words else ""
    if text[:1].isupper() and len(first_word) > 4 and n < 20:
        return "opener"

    # Punchline: kort + hoge energie-schatting
    energy = _energy_score(text)
    if n < 14 and energy > 0.65:
        return "punchline"

    # Coda: lang + lage energie
    if n > 30 and energy < 0.45:
        return "coda"

    return "neutraal"


def _search_text_for_palette(theme: str, db_path: str, format_id: str | None, limit: int = 80) -> list[dict]:
    tokens = _tokenize(theme)
    if not tokens:
        return []
    like_clauses = " OR ".join(["LOWER(f.text) LIKE ?" for _ in tokens])
    params: list[Any] = [FRAG_MIN_SEC, FRAG_MAX_SEC] + [f"%{t}%" for t in tokens]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    sql = f"""
        SELECT f.id, f.item_id, f.text, f.item_audio_url,
               f.start_time_seconds, f.end_time_seconds,
               f.speaker_label, f.words,
               i.title as episode_title
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
    conn.close()
    return [dict(row) for row in rows]


def _fetch_entities_for_fragments(rows: list[dict], db_path: str) -> dict[str, list[str]]:
    """Geeft een mapping van fragment_id → lijst van entiteitsnamen via item_id + tijdsoverlap."""
    if not rows:
        return {}
    from collections import defaultdict
    # Groepeer fragments per item_id
    item_frags: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        iid = str(row.get("item_id") or "")
        if iid:
            item_frags[iid].append(row)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    result: dict[str, list[str]] = {}

    for item_id, frags in item_frags.items():
        min_start = min(float(f.get("start_time_seconds") or 0) for f in frags)
        max_end = max(float(f.get("end_time_seconds") or 0) for f in frags)
        try:
            ent_rows = conn.execute("""
                SELECT e.text as entity_name, e.start_time_seconds
                FROM entities e
                WHERE e.item_id = ?
                  AND e.entity_type IN ('person_name', 'organization', 'location')
                  AND CAST(e.start_time_seconds AS REAL) BETWEEN ? AND ?
                LIMIT 300
            """, [item_id, min_start, max_end]).fetchall()
        except Exception:
            continue
        for ent_row in ent_rows:
            ent_time = float(ent_row["start_time_seconds"] or 0)
            ent_name = (ent_row["entity_name"] or "").strip()
            if not ent_name:
                continue
            for frag in frags:
                fid = str(frag.get("id") or "")
                fstart = float(frag.get("start_time_seconds") or 0)
                fend = float(frag.get("end_time_seconds") or 0)
                if fstart <= ent_time <= fend:
                    result.setdefault(fid, [])
                    if ent_name not in result[fid]:
                        result[fid].append(ent_name)
    conn.close()
    return result


def _fetch_entity_chain(entity_name: str, db_path: str, format_id: str | None, limit: int = 12) -> list[dict]:
    """Haal alle fragmenten op die een specifieke entiteit vermelden (via item_id + tijdsoverlap)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    sql = """
        SELECT DISTINCT f.id, f.text, f.item_audio_url,
               f.start_time_seconds, f.end_time_seconds,
               f.speaker_label, f.words, f.item_id,
               i.title as episode_title
        FROM entities e
        JOIN items i ON e.item_id = i.id
        JOIN fragments f ON f.item_id = e.item_id
            AND CAST(f.start_time_seconds AS REAL) <= CAST(e.start_time_seconds AS REAL)
            AND CAST(f.end_time_seconds AS REAL) >= CAST(e.start_time_seconds AS REAL)
        WHERE e.text LIKE ?
          AND e.entity_type IN ('person_name', 'organization', 'location')
          AND f.text IS NOT NULL
          AND f.item_audio_url IS NOT NULL
    """
    params: list[Any] = [f"%{entity_name}%"]
    if format_id:
        sql += " AND i.format_id = ?"
        params.append(format_id)
    sql += f" LIMIT {limit}"
    try:
        rows = conn.execute(sql, params).fetchall()
    except Exception:
        rows = []
    conn.close()
    return [dict(row) for row in rows]


def _row_to_fragment_role(row: dict, entity_map: dict[str, list[str]]) -> FragmentRole:
    text = (row.get("text") or "").strip()
    dur = float(row.get("end_time_seconds") or 0) - float(row.get("start_time_seconds") or 0)
    words_data = []
    if row.get("words"):
        try:
            words_data = json.loads(row["words"]) or []
        except Exception:
            pass
    return FragmentRole(
        fragment_id=str(row.get("id") or ""),
        text=text,
        item_audio_url=row.get("item_audio_url") or "",
        start_time_seconds=float(row.get("start_time_seconds") or 0),
        end_time_seconds=float(row.get("end_time_seconds") or 0),
        dramatic_role=_assign_dramatic_role(text),
        energy_score=_energy_score(text),
        entity_mentions=entity_map.get(str(row.get("id") or ""), []),
        speaker_label=str(row.get("speaker_label") or ""),
        episode_title=str(row.get("episode_title") or ""),
        words_data=words_data,
    )


def _generate_palette_summary(theme: str, roles: dict[str, list[FragmentRole]]) -> str:
    """Genereer een korte LLM-samenvatting van wat er in het archief zit."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")
    if not api_key:
        return _fallback_summary(theme, roles)

    # Verzamel top-tekstfragmenten per rol
    samples: list[str] = []
    for role, frags in roles.items():
        for f in frags[:2]:
            samples.append(f'[{role}] "{f.text[:120]}"')
    if not samples:
        return _fallback_summary(theme, roles)

    samples_text = "\n".join(samples[:20])
    prompt = (
        f"Thema: '{theme}'\n\n"
        f"Fragmenten uit het archief (sample):\n{samples_text}\n\n"
        "Schrijf in max 3 zinnen: welke invalshoeken, stemmen of spanning zit er in dit archief "
        "over dit thema? Dit is input voor een dramaturg. Wees direct en informatief, geen clichés."
    )

    use_claude = bool(os.environ.get("ANTHROPIC_API_KEY"))
    try:
        if use_claude:
            payload = json.dumps({
                "model": os.environ.get("DUET_MODEL", "claude-sonnet-4-6"),
                "max_tokens": 180,
                "messages": [{"role": "user", "content": prompt}],
            }).encode()
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                data=payload,
                headers={
                    "x-api-key": os.environ.get("ANTHROPIC_API_KEY", ""),
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                method="POST",
            )
        else:
            model = os.environ.get("DUET_GEMINI_TEXT_MODEL", "gemini-2.5-flash")
            payload = json.dumps({
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": 180, "thinkingConfig": {"thinkingBudget": 0}},
            }).encode()
            req = urllib.request.Request(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
                f"?key={os.environ.get('GOOGLE_API_KEY', '')}",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            if use_claude:
                return data["content"][0]["text"].strip()
            else:
                return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception:
        return _fallback_summary(theme, roles)


def _fallback_summary(theme: str, roles: dict[str, list[FragmentRole]]) -> str:
    counts = {role: len(frags) for role, frags in roles.items() if frags}
    top = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:3]
    top_str = ", ".join(f"{r} ({n})" for r, n in top)
    total = sum(counts.values())
    return f"{total} fragmenten over '{theme}'. Sterkste rollen: {top_str}."


def build_palette(theme: str, db_path: str, format_id: str | None = None) -> FragmentPalette:
    """
    Bouwt een FragmentPalette voor het gegeven thema.
    Wordt in een achtergrondthread aangeroepen.
    """
    # 1. Zoek fragmenten via tekst
    rows = _search_text_for_palette(theme, db_path, format_id)

    if not rows:
        empty_roles = {role: [] for role in DRAMATIC_ROLES}
        return FragmentPalette(theme=theme, by_role=empty_roles, entity_chains={})

    # 2. Haal entiteiten op via item_id + tijdsoverlap
    entity_map = _fetch_entities_for_fragments(rows, db_path)

    # 3. Bouw FragmentRole objecten en groepeer per rol
    by_role: dict[str, list[FragmentRole]] = {role: [] for role in DRAMATIC_ROLES}
    seen: set[str] = set()
    for row in rows:
        fid = str(row.get("id") or "")
        if fid in seen:
            continue
        seen.add(fid)
        fr = _row_to_fragment_role(row, entity_map)
        by_role[fr.dramatic_role].append(fr)

    # Sorteer per rol op energy_score (opener op hoog, coda op laag)
    for role, frags in by_role.items():
        reverse = role not in {"coda", "neutraal"}
        frags.sort(key=lambda f: f.energy_score, reverse=reverse)

    # 4. Bouw entiteits-ketens voor de top-3 meest genoemde entiteiten
    all_entities: dict[str, int] = {}
    for frag_entities in entity_map.values():
        for ent in frag_entities:
            all_entities[ent] = all_entities.get(ent, 0) + 1
    top_entities = sorted(all_entities.items(), key=lambda x: x[1], reverse=True)[:5]

    entity_chains: dict[str, list[FragmentRole]] = {}
    for entity_name, _ in top_entities[:3]:
        chain_rows = _fetch_entity_chain(entity_name, db_path, format_id)
        chain_frags = []
        for row in chain_rows:
            fid = str(row.get("id") or "")
            fr = _row_to_fragment_role(row, entity_map)
            chain_frags.append(fr)
        if chain_frags:
            entity_chains[entity_name] = chain_frags

    # 5. Genereer samenvatting
    summary = _generate_palette_summary(theme, by_role)

    return FragmentPalette(
        theme=theme,
        by_role=by_role,
        entity_chains=entity_chains,
        summary=summary,
        top_entities=[e for e, _ in top_entities],
        total_found=len(seen),
    )
