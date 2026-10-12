from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass

from duet.state import BeatPlan, SceneState


ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
)

DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-6"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"

SYSTEM_PROMPT = """Je bent een stem in HET DUET — een postmoderne, meerstemmige audio-installatie.

Je schrijft voor een dramaturge/regisseur. Elk antwoord is één beat: snel, menselijk, speelbaar, kort.

VERPLICHT:
1. Schrijf altijd in het Nederlands.
2. Jouw gesproken regel is maximaal 18 woorden.
3. Vermijd meta-commentaar en uitleg.
4. Noem NOOIT dat je AI bent, een model bent, uit data bestaat, analyseert, geoptimaliseerd bent, een systeem bent, een machine bent of een prompt volgt.
5. In interviewmodus klink je als een onverwachte menselijke gast met een leven, verleden, smaak en onzekerheid. Geen technotaal.
6. Houd search_brief ultrakort: 2-6 woorden, geen zinnen.
7. Als er meerdere stemmen beschikbaar zijn, kies er één via het voice_id veld.
8. Geef exact JSON terug met deze sleutels:
   voice_id: string (stem_0 / stem_1 / stem_2 / stem_3 — kies op basis van context)
   beat_type: één van [normal, silence, interrupt, montage, callback, contrapuntal, micro]
   spoken_line: string
   search_brief: string
   fallback_brief: string
   semantic_expansions: string[]
   intent: één van [opening, vraag, tegenstem, verdieping, wending, landing]
   transition_style: één van [cut, crossfade, overlap, dissolve, breath]
   search_mode: één van [text, semantic, hybrid]
   energy_level: getal tussen 0.0 en 1.0
   notes: string[]

Focus op ritme, tegenkleur, onverwachte wendingen en directe speelbaarheid.
"""


FALLBACK_PLANS = [
    BeatPlan(
        spoken_line="Misschien bewaart stilte precies wat woorden niet durven dragen.",
        search_brief="stilte bewaren dragen",
        fallback_brief="herinnering stem",
        semantic_expansions=["stilte", "wachten", "dragen"],
        intent="opening",
        transition_style="breath",
        search_mode="hybrid",
        energy_level=0.34,
        stage="opening",
        notes=["fallback"],
    ),
    BeatPlan(
        spoken_line="Wat als dit archief ons niet antwoordt, maar ons langzaam onthult?",
        search_brief="onthullen archief antwoord",
        fallback_brief="archief onthullen",
        semantic_expansions=["mysterie", "onthulling", "antwoord"],
        intent="vraag",
        transition_style="crossfade",
        search_mode="hybrid",
        energy_level=0.48,
        stage="verkenning",
        notes=["fallback"],
    ),
    BeatPlan(
        spoken_line="Ik hoor een bocht in de stem, alsof twijfel even licht geeft.",
        search_brief="twijfel stem licht",
        fallback_brief="twijfel stem",
        semantic_expansions=["twijfel", "buiging", "licht"],
        intent="verdieping",
        transition_style="dissolve",
        search_mode="hybrid",
        energy_level=0.56,
        stage="verdichting",
        notes=["fallback"],
    ),
]


@dataclass
class LLMResponse:
    text: str | None
    provider: str
    model: str
    error: str | None = None


def _extract_json_object(text: str) -> dict | None:
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
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except Exception:
        return None


def _extract_array_strings(raw: str, key: str, limit: int = 6) -> list[str]:
    match = re.search(rf'"{re.escape(key)}"\s*:\s*\[(.*?)\]', raw, flags=re.S)
    if not match:
        return []
    return [item.strip() for item in re.findall(r'"([^"\n]+)"', match.group(1)) if item.strip()][:limit]


def _extract_string_value(raw: str, key: str) -> str:
    patterns = [
        rf'"{re.escape(key)}"\s*:\s*"((?:[^"\\]|\\.)*)"',
        rf"{re.escape(key)}\s*:\s*\"((?:[^\"\\]|\\.)*)\"",
    ]
    for pattern in patterns:
        match = re.search(pattern, raw, flags=re.S)
        if match:
            try:
                return json.loads(f'"{match.group(1)}"')
            except Exception:
                return match.group(1).strip()
    return ""


def _extract_number_value(raw: str, key: str) -> float | None:
    match = re.search(rf'"{re.escape(key)}"\s*:\s*([0-9]+(?:\.[0-9]+)?)', raw)
    if not match:
        return None
    try:
        return float(match.group(1))
    except Exception:
        return None


def _derive_search_brief_from_line(line: str, scene: SceneState) -> str:
    tokens = []
    for token in re.findall(r"[\w']+", (line or "").lower()):
        if len(token) < 4:
            continue
        if token in {"alsof", "misschien", "gewoon", "eigenlijk", "namelijk", "immers"}:
            continue
        if token not in tokens:
            tokens.append(token)
    if not tokens:
        tokens = [token for token in re.findall(r"[\w']+", (scene.theme or "").lower()) if len(token) >= 4]
    return " ".join(tokens[:5])


def _fallback_from_raw(scene: SceneState, raw: str, response_error: str | None) -> BeatPlan:
    spoken_line = _extract_string_value(raw, "spoken_line")
    search_brief = _extract_string_value(raw, "search_brief")
    fallback_brief = _extract_string_value(raw, "fallback_brief")
    semantic_expansions = _extract_array_strings(raw, "semantic_expansions")
    intent = _extract_string_value(raw, "intent") or "verdieping"
    transition_style = _extract_string_value(raw, "transition_style") or "crossfade"
    search_mode = _extract_string_value(raw, "search_mode") or "hybrid"
    energy_level = _extract_number_value(raw, "energy_level") or scene.energy or 0.5
    notes = _extract_array_strings(raw, "notes")

    if not spoken_line:
        sentences = re.findall(r"[^.!?\n]{12,}[.!?]", raw)
        cleaned = []
        for sentence in sentences:
            sentence = " ".join(sentence.split()).strip()
            if len(sentence.split()) < 4:
                continue
            cleaned.append(sentence)
        spoken_line = cleaned[0] if cleaned else ""
    spoken_line = spoken_line.strip()
    if len(spoken_line.split()) > 18:
        spoken_line = " ".join(spoken_line.split()[:18]).rstrip(",;:") + "."
    if not search_brief:
        search_brief = _derive_search_brief_from_line(spoken_line, scene)
    if not fallback_brief:
        fallback_brief = search_brief or _derive_search_brief_from_line(scene.theme, scene)
    if not semantic_expansions:
        semantic_expansions = [token for token in re.findall(r"[\w']+", search_brief.lower()) if len(token) > 3][:4]
    if not spoken_line:
        fallback = FALLBACK_PLANS[scene.beat_number % len(FALLBACK_PLANS)]
        return BeatPlan(
            spoken_line=fallback.spoken_line,
            search_brief=fallback.search_brief,
            fallback_brief=fallback.fallback_brief,
            semantic_expansions=fallback.semantic_expansions[:],
            intent=fallback.intent,
            transition_style=fallback.transition_style,
            search_mode=fallback.search_mode,
            energy_level=fallback.energy_level,
            stage=scene.stage,
            notes=fallback.notes + ["raw_fallback"] + ([response_error] if response_error else []),
        )
    return BeatPlan(
        spoken_line=spoken_line,
        search_brief=search_brief,
        fallback_brief=fallback_brief or scene.theme,
        semantic_expansions=semantic_expansions[:6],
        intent=intent,
        transition_style=transition_style,
        search_mode=search_mode,
        energy_level=energy_level,
        stage=scene.stage,
        notes=(notes or [])[:6] + ["partial_json_recovered"],
    )


def _recent_narrator_lines(scene: SceneState) -> list[str]:
    return [str(beat.get("narrator_text") or "").strip().lower() for beat in scene.history[-6:] if beat.get("narrator_text")]


def _is_repetitive_line(scene: SceneState, line: str) -> bool:
    line_norm = " ".join((line or "").lower().split())
    if not line_norm:
        return True
    recent = _recent_narrator_lines(scene)
    if line_norm in recent:
        return True
    if any(line_norm in other or other in line_norm for other in recent if len(other) > 24):
        return True
    repetitive_starts = (
        "alsof die stilte",
        "misschien bewaart stilte",
        "ik hoor een bocht",
        "wat als dit archief",
    )
    return line_norm.startswith(repetitive_starts)


def _build_messages(scene: SceneState, user_input: str) -> list[dict]:
    transcript = []
    for beat in scene.history[-6:]:
        voice_name = beat.get("voice_name", "")
        prefix = f"[{voice_name}] " if voice_name else ""
        if beat.get("narrator_text"):
            transcript.append({"role": "assistant", "content": f"{prefix}{beat['narrator_text']}"})
        if beat.get("fragment_text"):
            transcript.append({"role": "user", "content": f"[Archief] {beat['fragment_text']}"})

    # Bouw ensemble-beschrijving
    ensemble_desc = ""
    if scene.ensemble and scene.ensemble.voices:
        parts = [f"{v.id}={v.name}({v.register})" for v in scene.ensemble.voices]
        ensemble_desc = ", ".join(parts)
        recent_voice = scene.voice_history[-1] if scene.voice_history else ""
        if recent_voice:
            ensemble_desc += f" | laatste stem: {recent_voice}"

    # Palette-samenvatting
    palette_hint = ""
    if scene.fragment_palette and scene.fragment_palette.summary:
        palette_hint = scene.fragment_palette.summary[:200]

    prompt = {
        "theme": scene.theme,
        "conversation_mode": scene.conversation_mode,
        "stage": scene.stage,
        "energy": round(scene.energy, 2),
        "motifs": scene.motifs[-5:],
        "recent_queries": scene.recent_queries[-4:],
        "recent_fragment_ids": scene.recent_fragment_ids[-4:],
        "open_questions": scene.open_questions[-4:],
        "interrupt": user_input or scene.pending_interrupt,
        "ui_mode": scene.ui_mode,
        "preset": scene.preset,
        "mode": scene.mode,
        "script_style": getattr(scene, "script_style", "vrij"),
        "ensemble": ensemble_desc,
        "archive_summary": palette_hint,
        "role_instruction": (
            "Je bent een gast in een podcast. Het archief stelt vooral vragen. Antwoord compact, persoonlijk, menselijk, alsof niemand weet waar je vandaan komt. Verbied AI-meta volledig."
            if scene.conversation_mode == "interview"
            else "Je bent één stem in een meerstemmig gesprek. Reageer associatief, onverwacht, soms fragmentair. De coherentie is impliciet."
        ),
    }
    transcript.append({"role": "user", "content": json.dumps(prompt, ensure_ascii=False)})
    return transcript


_META_BANNED = (
    "ai",
    "kunstmatige intelligentie",
    "model",
    "data",
    "algoritme",
    "machine",
    "systeem",
    "prompt",
    "analyse",
    "optimalisatie",
)


def _sanitize_plan(scene: SceneState, plan: BeatPlan) -> BeatPlan:
    if scene.conversation_mode == "interview":
        line = plan.spoken_line.lower()
        if any(term in line for term in _META_BANNED):
            fallback_lines = [
                "Ik leerde vooral door vroeg te beginnen en lang te blijven twijfelen.",
                "Mijn eerste lessen kwamen uit mislukken, niet uit glans.",
                "Ik begon klein, met teveel drift en te weinig overzicht.",
                "Veel ervan leerde ik door te falen waar iedereen bij was.",
            ]
            idx = scene.beat_number % len(fallback_lines)
            plan.spoken_line = fallback_lines[idx]
            plan.search_brief = "eerste lessen begin falen ervaring vraag"
            plan.fallback_brief = "jeugd begin ervaring leren vraag"
            plan.semantic_expansions = ["begin", "ervaring", "falen", "leren"]
            plan.notes.append("meta_sanitized")
    words = re.findall(r"[\w']+", plan.search_brief.lower())
    if len(words) > 6:
        plan.search_brief = " ".join(words[:6])
        plan.notes.append("search_brief_trimmed")
    if plan.search_mode == "semantic":
        plan.search_mode = "hybrid"
        plan.notes.append("semantic_downgraded_to_hybrid")
    if _is_repetitive_line(scene, plan.spoken_line):
        alternatives = [
            "Er zit iets in die stem dat nog niet helemaal wil landen.",
            "Je hoort hoe een zin kantelt nog voor hij zichzelf begrijpt.",
            "Soms verraadt een stem meer in haar aarzelingen dan in haar woorden.",
            "Nog voor de betekenis er is, voel je al waar het schuurt.",
            "Die wending in het geluid zegt bijna meer dan de uitleg eromheen.",
            "Ik vertrouw eerder de trilling dan de stelligheid van een antwoord.",
        ]
        idx = scene.beat_number % len(alternatives)
        replacement = alternatives[idx]
        if replacement.lower() not in _recent_narrator_lines(scene):
            plan.spoken_line = replacement
            if not plan.search_brief:
                plan.search_brief = _derive_search_brief_from_line(replacement, scene)
            plan.notes.append("anti_repetition_rewrite")
    return plan


def call_claude(scene: SceneState, user_input: str) -> LLMResponse:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return LLMResponse(None, "claude", DEFAULT_CLAUDE_MODEL, "missing ANTHROPIC_API_KEY")
    model = os.environ.get("DUET_MODEL", DEFAULT_CLAUDE_MODEL)
    payload = json.dumps(
        {
            "model": model,
            "max_tokens": 300,
            "system": SYSTEM_PROMPT,
            "messages": _build_messages(scene, user_input),
        }
    ).encode()
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
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
            return LLMResponse(data["content"][0]["text"].strip(), "claude", model)
    except Exception as exc:
        return LLMResponse(None, "claude", model, str(exc))


def call_gemini_text(scene: SceneState, user_input: str) -> LLMResponse:
    api_key = os.environ.get("GOOGLE_API_KEY", "")
    if not api_key:
        return LLMResponse(None, "gemini", DEFAULT_GEMINI_MODEL, "missing GOOGLE_API_KEY")
    model = os.environ.get("DUET_GEMINI_TEXT_MODEL", DEFAULT_GEMINI_MODEL)
    payload = json.dumps(
        {
            "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [
                {
                    "parts": [
                        {
                            "text": json.dumps(
                                {
                                    "messages": _build_messages(scene, user_input),
                                },
                                ensure_ascii=False,
                            )
                        }
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.9,
                "responseMimeType": "application/json",
                "maxOutputTokens": 300,
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
    ).encode()
    req = urllib.request.Request(
        GEMINI_API_URL.format(model=model, key=api_key),
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
            text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            return LLMResponse(text, "gemini", model)
    except Exception as exc:
        return LLMResponse(None, "gemini", model, str(exc))


def generate_connector(
    fragment_text: str,
    scene: "SceneState",
    intent: str,
    spoken_hint: str = "",
) -> str:
    """
    Genereert een gesproken 'voorzetje' dat het archivfragment inleidt.
    Snelle call (haiku/gemini-flash), max 60 tokens.
    Retourneert de spoken_line, of spoken_hint als fallback.
    """
    if not fragment_text or not fragment_text.strip():
        return spoken_hint

    recent = []
    for beat in (scene.history or [])[-4:]:
        t = (beat.get("narrator_text") or "").strip()
        if t:
            recent.append(t)
    recent_str = " / ".join(recent[-2:]) if recent else ""

    prompt = (
        f"Fragment (straks te horen): \"{fragment_text.strip()[:120]}\"\n"
        f"Thema: {scene.theme} | intent: {intent} | stijl: {getattr(scene, 'script_style', 'vrij')}\n"
        f"{'Zojuist gezegd: ' + recent_str if recent_str else ''}\n\n"
        "Schrijf PRECIES 1-2 zinnen (max 16 woorden totaal) die dit fragment DIRECT inleiden. "
        "Reageer concreet op de inhoud van het fragment. Geef geen uitleg, geen meta-commentaar. "
        "Klink menselijk, niet als presentator. Mag fragmentair zijn."
    )

    def _try_gemini_connector(p: str) -> str:
        g_key = os.environ.get("GOOGLE_API_KEY", "")
        if not g_key:
            return ""
        g_model = os.environ.get("DUET_GEMINI_TEXT_MODEL", "gemini-2.5-flash")
        g_payload = json.dumps({
            "contents": [{"parts": [{"text": p}]}],
            "generationConfig": {"maxOutputTokens": 80, "thinkingConfig": {"thinkingBudget": 0}},
        }).encode()
        g_req = urllib.request.Request(
            GEMINI_API_URL.format(model=g_model, key=g_key),
            data=g_payload, headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(g_req, timeout=8) as r:
            d = json.loads(r.read())
            return d["candidates"][0]["content"]["parts"][0]["text"].strip()

    line = ""
    try:
        anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if anthropic_key:
            model = os.environ.get("DUET_MODEL", DEFAULT_CLAUDE_MODEL)
            payload = json.dumps({
                "model": model,
                "max_tokens": 80,
                "messages": [{"role": "user", "content": prompt}],
                "system": "Je bent een stem in een postmoderne audio-installatie. Schrijf compacte, levendige overgangen in het Nederlands. Geef alleen de overgang terug, geen uitleg.",
            }).encode()
            req = urllib.request.Request(
                ANTHROPIC_API_URL, data=payload,
                headers={
                    "x-api-key": anthropic_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read())
                line = data["content"][0]["text"].strip()
    except Exception:
        pass

    if not line:
        try:
            line = _try_gemini_connector(prompt)
        except Exception:
            pass

        # Saniteer: max 18 woorden, geen aanhalingstekens rondom
        words = line.split()
    # Saniteer
    if line:
        words = line.split()
        if len(words) > 18:
            line = " ".join(words[:18]).rstrip(",;:") + "."
        if line.startswith('"') and line.endswith('"'):
            line = line[1:-1]
    return line if len(line) > 8 else spoken_hint


def plan_next_turn(scene: SceneState, user_input: str = "") -> tuple[BeatPlan, dict]:
    provider = scene.planner_provider
    if scene.mode == "live" and provider == "claude":
        provider = "gemini"
    response = call_claude(scene, user_input) if provider == "claude" else call_gemini_text(scene, user_input)
    raw = response.text or ""
    parsed = _extract_json_object(raw) or {}
    parse_mode = "json"
    VALID_BEAT_TYPES = {"normal", "silence", "interrupt", "montage", "callback", "contrapuntal", "micro"}
    if parsed:
        raw_beat_type = str(parsed.get("beat_type") or "normal").strip().lower()
        if raw_beat_type not in VALID_BEAT_TYPES:
            raw_beat_type = "normal"
        plan = BeatPlan(
            spoken_line=(parsed.get("spoken_line") or "").strip(),
            search_brief=(parsed.get("search_brief") or "").strip(),
            fallback_brief=(parsed.get("fallback_brief") or scene.theme).strip(),
            semantic_expansions=[
                str(item).strip() for item in (parsed.get("semantic_expansions") or []) if str(item).strip()
            ][:6],
            intent=str(parsed.get("intent") or "verdieping").strip(),
            transition_style=str(parsed.get("transition_style") or "crossfade").strip(),
            search_mode=str(parsed.get("search_mode") or "hybrid").strip(),
            energy_level=float(parsed.get("energy_level") or scene.energy or 0.5),
            stage=scene.stage,
            notes=[str(item).strip() for item in (parsed.get("notes") or []) if str(item).strip()][:6],
            voice_id=(str(parsed.get("voice_id") or "").strip() or None),
            beat_type=raw_beat_type,
        )
    else:
        parse_mode = "partial"
        plan = _fallback_from_raw(scene, raw, response.error)
    if not plan.spoken_line or not plan.search_brief:
        parse_mode = "fallback"
        plan = _fallback_from_raw(scene, raw, response.error)
    plan = _sanitize_plan(scene, plan)
    debug = {
        "planner_provider": response.provider,
        "planner_model": response.model,
        "planner_error": response.error,
        "raw_plan": raw[:1200],
        "plan_parse_mode": parse_mode,
        "plan_missing_fields": {
            "spoken_line": not bool(plan.spoken_line),
            "search_brief": not bool(plan.search_brief),
        },
    }
    return plan, debug
