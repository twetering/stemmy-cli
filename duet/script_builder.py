"""
duet/script_builder.py — Monteur-modus: fragment-first script planning.

De LLM schrijft een volledig script van 20-30 beats waarbij het WEET
welke fragmenten beschikbaar zijn. Dit geeft dramaturgische coherentie
die de reguliere arc-modus niet kan bieden.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from typing import TYPE_CHECKING, Any

from duet.arc_planner import (
    ArcPlan,
    ANTHROPIC_API_URL,
    GEMINI_API_URL,
    DEFAULT_CLAUDE_MODEL,
    DEFAULT_GEMINI_MODEL,
    VALID_BEAT_TYPES,
    VALID_INTENTS,
    VALID_TRANSITIONS,
    _extract_json,
    _parse_beat,
)
from duet.state import BeatPlan

if TYPE_CHECKING:
    from duet.ensemble import EnsembleConfig

SCRIPT_SYSTEM_PROMPT = """Je bent monteur van HET DUET 3.0 — een postmoderne meerstemmige audio-installatie.

Jouw taak: schrijf een compleet script van {n_beats} beats op basis van echte archief-fragmenten.

BESCHIKBARE FRAGMENTEN (kies er {n_beats_fragments} van):
{fragments_list}
(Formaat: [INDEX] id=<id> (duur in seconden) "tekst")

STEMMEN:
{voices_desc}

THEMA: {theme}
STIJL: {script_style}

REGELS:
1. Elk fragment mag MAX 1x gebruikt worden
2. Kies fragmenten die SAMEN een coherent verhaal vertellen of interessante spanning creëren
3. Schrijf spoken_line ALTIJD met kennis van het fragment: leid het in, reageer erop, of maak een associatieve sprong
4. Wissel stemmen af (max 2x dezelfde stem achter elkaar)
5. Spoken_line max 16 woorden, in het Nederlands
6. Gebruik beats creatief: silence, interrupt, montage zijn toegestaan
7. Bouw een dramaturgische arc: begin spannend, bouw op, zorg voor wendingen
8. Gebruik elk beat-type minstens 1x: normal, silence, interrupt, montage

Geef ALLEEN JSON terug:
{{
  "motifs": ["motief1", ...],
  "beats": [
    {{
      "fragment_index": 0,
      "voice_id": "stem_0",
      "spoken_line": "...",
      "beat_type": "normal|silence|interrupt|montage|contrapuntal|micro",
      "intent": "opening|vraag|tegenstem|verdieping|wending|landing",
      "transition_style": "cut|crossfade|dissolve|breath",
      "energy_level": 0.0,
      "notes": []
    }}
  ]
}}"""


def _build_script_prompt(
    theme: str,
    fragment_pool: list[dict],
    ensemble_config: "EnsembleConfig",
    script_style: str,
    n_beats: int,
) -> tuple[str, dict[int, str]]:
    """
    Bouwt het systeem-prompt voor de Monteur en retourneert tevens een
    index_to_id mapping (index → volledige fragment-ID).
    """
    # Begrens pool tot wat we in de prompt kwijt kunnen (max 80 fragmenten)
    pool = fragment_pool[:80]

    # index_to_id: sleutels zijn integers, waarden zijn volledige IDs
    index_to_id: dict[int, str] = {}
    fragments_lines: list[str] = []
    for idx, frag in enumerate(pool):
        frag_id = str(frag["id"])
        index_to_id[idx] = frag_id
        short_id = frag_id[:8]
        dur = round(float(frag.get("duration") or 0), 1)
        text = str(frag.get("text") or "").strip()
        # Kap tekst af op 100 tekens voor compactheid in de prompt
        if len(text) > 100:
            text = text[:97] + "…"
        fragments_lines.append(f'[{idx}] id={short_id} ({dur}s) "{text}"')

    fragments_list = "\n".join(fragments_lines)

    if ensemble_config and ensemble_config.voices:
        voices_lines = [
            f"  - {v.id} ({v.name}): register={v.register}"
            for v in ensemble_config.voices
        ]
        voices_desc = "\n".join(voices_lines)
    else:
        voices_desc = "  - stem_0: register=vlak (enige stem)"

    # Hoeveel fragmenten de LLM moet kiezen (gelijk aan n_beats, maar max pool grootte)
    n_beats_fragments = min(n_beats, len(pool))

    prompt = SCRIPT_SYSTEM_PROMPT.format(
        n_beats=n_beats,
        n_beats_fragments=n_beats_fragments,
        fragments_list=fragments_list,
        voices_desc=voices_desc,
        theme=theme,
        script_style=script_style,
    )
    return prompt, index_to_id


def _call_claude_script(prompt: str) -> tuple[str, str]:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return "", "missing ANTHROPIC_API_KEY"
    model = os.environ.get("DUET_MODEL", DEFAULT_CLAUDE_MODEL)
    payload = json.dumps({
        "model": model,
        "max_tokens": 3000,
        "system": prompt,
        "messages": [{"role": "user", "content": "Genereer het Monteur-script nu als JSON."}],
    }).encode()
    req = urllib.request.Request(
        ANTHROPIC_API_URL,
        data=payload,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
            return data["content"][0]["text"].strip(), ""
    except Exception as exc:
        return "", str(exc)


def _call_gemini_script(prompt: str) -> tuple[str, str]:
    api_key = os.environ.get("GOOGLE_API_KEY", "")
    if not api_key:
        return "", "missing GOOGLE_API_KEY"
    model = os.environ.get("DUET_GEMINI_TEXT_MODEL", DEFAULT_GEMINI_MODEL)
    payload = json.dumps({
        "system_instruction": {"parts": [{"text": prompt}]},
        "contents": [{"parts": [{"text": "Genereer het Monteur-script nu als JSON."}]}],
        "generationConfig": {
            "temperature": 1.0,
            "responseMimeType": "application/json",
            "maxOutputTokens": 3000,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }).encode()
    req = urllib.request.Request(
        GEMINI_API_URL.format(model=model, key=api_key),
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
            return data["candidates"][0]["content"]["parts"][0]["text"].strip(), ""
    except Exception as exc:
        return "", str(exc)


def _parse_script_beat(
    raw: dict,
    beat_index: int,
    n_voices: int,
    index_to_id: dict[int, str],
) -> BeatPlan | None:
    """
    Parseer een raw LLM-beat dict naar een BeatPlan met preselected_fragment_id.
    Retourneert None als het beat ongeldig is.
    """
    # Vertaal fragment_index → fragment_id
    fragment_index = raw.get("fragment_index")
    preselected_fragment_id: str | None = None
    if fragment_index is not None:
        try:
            fidx = int(fragment_index)
            preselected_fragment_id = index_to_id.get(fidx)
        except (TypeError, ValueError):
            pass

    beat_type = str(raw.get("beat_type") or "normal").lower()
    if beat_type not in VALID_BEAT_TYPES:
        beat_type = "normal"

    intent = str(raw.get("intent") or "verdieping").lower()
    if intent not in VALID_INTENTS:
        intent = "verdieping"

    transition = str(raw.get("transition_style") or "crossfade").lower()
    if transition not in VALID_TRANSITIONS:
        transition = "crossfade"

    energy = float(raw.get("energy_level") or 0.5)
    energy = max(0.0, min(1.0, energy))

    voice_id = str(raw.get("voice_id") or "stem_0")
    if not re.match(r"^stem_\d+$", voice_id):
        voice_id = f"stem_{beat_index % max(n_voices, 1)}"

    spoken_line = str(raw.get("spoken_line") or "").strip()
    if beat_type in {"silence", "montage"} and not spoken_line:
        spoken_line = "—"
    words = spoken_line.split()
    if len(words) > 16:
        spoken_line = " ".join(words[:16]).rstrip(",;:") + "."

    notes = [str(n).strip() for n in (raw.get("notes") or []) if str(n).strip()][:4]

    # Voor Monteur-beats gebruiken we de gekozen fragment_index als search_brief
    search_brief = f"monteur-beat-{beat_index}"
    fallback_brief = search_brief

    return BeatPlan(
        spoken_line=spoken_line,
        search_brief=search_brief,
        fallback_brief=fallback_brief,
        semantic_expansions=[],
        intent=intent,
        transition_style=transition,
        search_mode="text",
        energy_level=energy,
        stage="opening",
        notes=notes,
        voice_id=voice_id,
        beat_type=beat_type,
        callback_fragment_id=None,
        montage_queries=[],
        micro_target_words=[],
        preselected_fragment_id=preselected_fragment_id,
    )


def build_script(
    theme: str,
    fragment_pool: list[dict],
    ensemble_config: "EnsembleConfig",
    script_style: str = "vrij",
    n_beats: int = 22,
    planner_provider: str = "claude",
) -> "ArcPlan | None":
    """
    Stuurt fragment_pool naar LLM, die een volledig script maakt.
    Elk beat heeft een specifieke fragment_id (preselected_fragment_id).
    Retourneert een ArcPlan of None bij fout.
    """
    if not fragment_pool:
        return None

    prompt, index_to_id = _build_script_prompt(
        theme=theme,
        fragment_pool=fragment_pool,
        ensemble_config=ensemble_config,
        script_style=script_style,
        n_beats=n_beats,
    )

    if planner_provider == "gemini":
        raw, err = _call_gemini_script(prompt)
        model_name = os.environ.get("DUET_GEMINI_TEXT_MODEL", DEFAULT_GEMINI_MODEL)
    else:
        raw, err = _call_claude_script(prompt)
        model_name = os.environ.get("DUET_MODEL", DEFAULT_CLAUDE_MODEL)

    if err or not raw:
        return None

    parsed = _extract_json(raw)
    if not parsed or "beats" not in parsed:
        return None

    raw_beats = parsed.get("beats") or []
    if not isinstance(raw_beats, list) or len(raw_beats) < 3:
        return None

    n_voices = len(ensemble_config.voices) if ensemble_config and ensemble_config.voices else 1
    beats: list[BeatPlan] = []
    used_fragment_ids: set[str] = set()

    for i, rb in enumerate(raw_beats[:30]):
        if not isinstance(rb, dict):
            continue
        try:
            bp = _parse_script_beat(rb, i, n_voices, index_to_id)
            if bp is None:
                continue
            # Elke fragment mag maar 1x (Monteur-regel)
            if bp.preselected_fragment_id and bp.preselected_fragment_id in used_fragment_ids:
                bp.preselected_fragment_id = None
            if bp.preselected_fragment_id:
                used_fragment_ids.add(bp.preselected_fragment_id)
            beats.append(bp)
        except Exception:
            continue

    if len(beats) < 3:
        return None

    motifs = [str(m).strip() for m in (parsed.get("motifs") or []) if str(m).strip()][:10]

    return ArcPlan(
        beats=beats,
        motifs=motifs,
        script_style=script_style,
        callback_map={},
        provider=planner_provider,
        model=model_name,
    )
