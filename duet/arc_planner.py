"""
duet/arc_planner.py — Volledige arc pre-planning voor HET DUET 3.0.

De LLM plant 10-15 beats vooruit als een complete dramaturgische boog,
inclusief beat-types (stilte, interrupt, montage, callback, contrapunt),
stemtoewijzingen en thematische motieven.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duet.state import SceneState
from duet.state import BeatPlan

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
)

DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-6"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"

ARC_SYSTEM_PROMPT = """Je bent dramaturg van HET DUET 3.0 — een postmoderne, abstracte audio-installatie.

Je taak: schrijf een complete dramaturgische boog van {n_beats} beats voor een meerstemmige installatie.

SFEER: postmodern, absurdistisch, conceptueel, puzzelend. De bezoeker valt in een vreemd maar enigszins coherent groepsgesprek. Coherentie is impliciet, nooit uitgelegd. Vermijd clichés.

STEMMEN (beschikbaar):
{voices_desc}

THEMA: {theme}
STIJL: {script_style}
FRAGMENT-SAMENVATTING (wat er in het archief zit over dit thema):
{palette_summary}

BEAT-TYPES (gebruik ze creatief en gevarieerd):
- normal: stem spreekt + archieffragment volgt
- silence: opzettelijke pauze (1.5-4 sec), geen audio
- interrupt: archieffragment kapt de stem halverwege af
- montage: 2-3 archieffragmenten snel achter elkaar (geen narrator)
- callback: herhaal een eerder archieffragment (verwijs naar beat_index)
- contrapuntal: stem en archief overlappen elkaar kort
- micro: extraheer slechts 1-5 woorden uit een fragment (Echo-stijl)

REGELS:
1. Maximaal 18 woorden per spoken_line. Schrijf in het Nederlands.
2. Geen meta-commentaar, geen uitleg van de structuur.
3. Gebruik elk beat-type minstens 1x.
4. Wissel stemmen af (nooit dezelfde stem 3x achter elkaar).
5. Callbacks (callback_fragment_id) mogen alleen verwijzen naar eerder geplande beats via beat_index.
6. Geef de montage_queries voor montage-beats (2-3 zoekopdrachten).
7. Geef micro_target_words voor micro-beats (1-5 woorden om te zoeken).
8. Geef een lijst van thematische motieven (motifs) voor de hele boog.

Geef ALLEEN een JSON-object terug met deze structuur:
{
  "motifs": ["motief1", "motief2", ...],
  "script_style_chosen": "string",
  "beats": [
    {
      "spoken_line": "string",
      "search_brief": "string",
      "fallback_brief": "string",
      "semantic_expansions": ["string"],
      "intent": "opening|vraag|tegenstem|verdieping|wending|landing",
      "transition_style": "cut|crossfade|overlap|dissolve|breath",
      "search_mode": "text|hybrid",
      "energy_level": 0.0-1.0,
      "voice_id": "stem_0|stem_1|...",
      "beat_type": "normal|silence|interrupt|montage|callback|contrapuntal|micro",
      "callback_beat_index": null_or_int,
      "montage_queries": [],
      "micro_target_words": [],
      "notes": []
    }
  ]
}
"""

VALID_BEAT_TYPES = {"normal", "silence", "interrupt", "montage", "callback", "contrapuntal", "micro"}
VALID_INTENTS = {"opening", "vraag", "tegenstem", "verdieping", "wending", "landing"}
VALID_TRANSITIONS = {"cut", "crossfade", "overlap", "dissolve", "breath"}


@dataclass
class ArcPlan:
    beats: list[BeatPlan]
    motifs: list[str]
    script_style: str
    callback_map: dict[int, int] = field(default_factory=dict)  # beat_index → source_beat_index
    created_at: float = field(default_factory=time.time)
    provider: str = ""
    model: str = ""


def _build_arc_prompt(scene: "SceneState") -> str:
    from duet.ensemble import EnsembleConfig
    n_beats = 12

    # Stem-beschrijvingen voor de LLM
    if scene.ensemble and scene.ensemble.voices:
        voices_lines = []
        for v in scene.ensemble.voices:
            voices_lines.append(f'  - {v.id} ({v.name}): register={v.register}, kleur={v.color}')
        voices_desc = "\n".join(voices_lines)
    else:
        voices_desc = "  - stem_0: register=vlak (enige stem)"

    palette_summary = ""
    if scene.fragment_palette:
        palette_summary = scene.fragment_palette.summary or ""
    if not palette_summary:
        palette_summary = f"(nog niet beschikbaar — gebruik thema '{scene.theme}' als leidraad)"

    style = scene.script_style or "vrij"

    return ARC_SYSTEM_PROMPT.format(
        n_beats=n_beats,
        voices_desc=voices_desc,
        theme=scene.theme,
        script_style=style,
        palette_summary=palette_summary,
    )


def _parse_beat(raw: dict, beat_index: int, n_voices: int, callback_map: dict[int, int]) -> BeatPlan:
    """Converteer een raw dict van de LLM naar een BeatPlan."""
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
    # Valideer voice_id
    if not re.match(r"^stem_\d+$", voice_id):
        voice_id = f"stem_{beat_index % max(n_voices, 1)}"

    spoken_line = str(raw.get("spoken_line") or "").strip()
    # Silence en montage beats hebben geen spoken_line nodig
    if beat_type in {"silence", "montage"} and not spoken_line:
        spoken_line = "—"
    # Trim op 18 woorden
    words = spoken_line.split()
    if len(words) > 18:
        spoken_line = " ".join(words[:18]).rstrip(",;:") + "."

    search_brief = str(raw.get("search_brief") or "").strip()
    fallback_brief = str(raw.get("fallback_brief") or search_brief).strip()

    # Callback: registreer in callback_map
    callback_fragment_id = None
    cb_idx = raw.get("callback_beat_index")
    if beat_type == "callback" and cb_idx is not None:
        try:
            src = int(cb_idx)
            if 0 <= src < beat_index:  # mag alleen naar eerder verwijzen
                callback_map[beat_index] = src
        except (TypeError, ValueError):
            pass

    montage_queries = [str(q).strip() for q in (raw.get("montage_queries") or []) if str(q).strip()][:3]
    micro_words = [str(w).strip() for w in (raw.get("micro_target_words") or []) if str(w).strip()][:5]

    return BeatPlan(
        spoken_line=spoken_line,
        search_brief=search_brief,
        fallback_brief=fallback_brief or search_brief,
        semantic_expansions=[str(s).strip() for s in (raw.get("semantic_expansions") or []) if str(s).strip()][:6],
        intent=intent,
        transition_style=transition,
        search_mode=str(raw.get("search_mode") or "hybrid").strip(),
        energy_level=energy,
        stage="opening",  # wordt later dynamisch ingesteld
        notes=[str(n).strip() for n in (raw.get("notes") or []) if str(n).strip()][:4],
        voice_id=voice_id,
        beat_type=beat_type,
        callback_fragment_id=callback_fragment_id,
        montage_queries=montage_queries,
        micro_target_words=micro_words,
    )


def _call_claude_arc(prompt: str) -> tuple[str, str]:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return "", "missing ANTHROPIC_API_KEY"
    model = os.environ.get("DUET_MODEL", DEFAULT_CLAUDE_MODEL)
    payload = json.dumps({
        "model": model,
        "max_tokens": 2000,
        "system": prompt,
        "messages": [{"role": "user", "content": "Genereer de arc nu als JSON."}],
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
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read())
            return data["content"][0]["text"].strip(), ""
    except Exception as exc:
        return "", str(exc)


def _call_gemini_arc(prompt: str) -> tuple[str, str]:
    api_key = os.environ.get("GOOGLE_API_KEY", "")
    if not api_key:
        return "", "missing GOOGLE_API_KEY"
    model = os.environ.get("DUET_GEMINI_TEXT_MODEL", DEFAULT_GEMINI_MODEL)
    payload = json.dumps({
        "system_instruction": {"parts": [{"text": prompt}]},
        "contents": [{"parts": [{"text": "Genereer de arc nu als JSON."}]}],
        "generationConfig": {
            "temperature": 1.0,
            "responseMimeType": "application/json",
            "maxOutputTokens": 2200,
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
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read())
            return data["candidates"][0]["content"]["parts"][0]["text"].strip(), ""
    except Exception as exc:
        return "", str(exc)


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S | re.I)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except Exception:
            pass
    match = re.search(r"\{.*\}", text, flags=re.S)
    if match:
        try:
            return json.loads(match.group(0))
        except Exception:
            pass
    return None


def plan_full_arc(scene: "SceneState") -> ArcPlan | None:
    """
    Bouwt een volledige dramaturgische arc voor de sessie.
    Wordt in een achtergrondthread aangeroepen.
    Retourneert None bij een fout.
    """
    prompt = _build_arc_prompt(scene)
    provider = scene.planner_provider
    n_voices = len(scene.ensemble.voices) if scene.ensemble else 1
    model_name = os.environ.get("DUET_MODEL", DEFAULT_CLAUDE_MODEL)

    if provider == "claude":
        raw, err = _call_claude_arc(prompt)
    else:
        raw, err = _call_gemini_arc(prompt)
        model_name = os.environ.get("DUET_GEMINI_TEXT_MODEL", DEFAULT_GEMINI_MODEL)

    if err or not raw:
        return None

    parsed = _extract_json(raw)
    if not parsed or "beats" not in parsed:
        return None

    raw_beats = parsed.get("beats") or []
    if not isinstance(raw_beats, list) or len(raw_beats) < 3:
        return None

    callback_map: dict[int, int] = {}
    beats: list[BeatPlan] = []
    for i, rb in enumerate(raw_beats[:15]):
        if not isinstance(rb, dict):
            continue
        try:
            bp = _parse_beat(rb, i, n_voices, callback_map)
            beats.append(bp)
        except Exception:
            continue

    if len(beats) < 3:
        return None

    motifs = [str(m).strip() for m in (parsed.get("motifs") or []) if str(m).strip()][:10]
    style = str(parsed.get("script_style_chosen") or scene.script_style or "vrij")

    return ArcPlan(
        beats=beats,
        motifs=motifs,
        script_style=style,
        callback_map=callback_map,
        provider=provider,
        model=model_name,
    )
