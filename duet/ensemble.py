"""
duet/ensemble.py — Multi-stem configuratie voor HET DUET 3.0.

Beheert een ensemble van 2–4 ElevenLabs-stemmen met abstracte identiteiten
(geen expliciete roles — de bezoeker moet puzzelen wie er spreekt).
"""
from __future__ import annotations

import os
import random
import sqlite3
from dataclasses import dataclass, field


# Abstracte, niet-verklarende namen voor de stemmen
_ABSTRACT_NAMES = ["I", "II", "III", "IV", "het", "zij", "hij", "men", "—", "∅"]

# Kleuren per stempositie (glow voor de UI-orbs)
_VOICE_COLORS = ["#8fd3ff", "#ffb347", "#b8ff8f", "#ff8fb3", "#c9b3ff", "#ffe08f"]


@dataclass
class Voice:
    id: str                 # intern ID: "stem_0", "stem_1", …
    elevenlabs_id: str      # ElevenLabs voice_id uit de DB
    name: str               # abstracte weergavenaam in UI
    register: str           # omschrijving voor de LLM: "laag", "hoog", "schor", "vlak", "warm"
    color: str              # CSS hex voor UI-glow
    db_name: str = ""       # originele naam in de DB (voor debug)


@dataclass
class EnsembleConfig:
    voices: list[Voice]
    script_style: str = "vrij"
    simultaneity_prob: float = 0.15   # kans op overlappende beats
    silence_prob: float = 0.12        # kans op stille beats
    interrupt_prob: float = 0.10      # kans op interrupt-beats
    montage_prob: float = 0.08        # kans op montage-beats
    callback_prob: float = 0.07       # kans op callback-beats


# Stijl-specifieke kansen (overschrijven defaults)
_STYLE_PROBS: dict[str, dict[str, float]] = {
    "kakofonie": {
        "simultaneity_prob": 0.35,
        "silence_prob": 0.05,
        "interrupt_prob": 0.22,
        "montage_prob": 0.18,
        "callback_prob": 0.05,
    },
    "verhoor": {
        "simultaneity_prob": 0.05,
        "silence_prob": 0.20,
        "interrupt_prob": 0.15,
        "montage_prob": 0.05,
        "callback_prob": 0.12,
    },
    "requiem": {
        "simultaneity_prob": 0.18,
        "silence_prob": 0.28,
        "interrupt_prob": 0.04,
        "montage_prob": 0.04,
        "callback_prob": 0.20,
    },
    "vrij": {
        "simultaneity_prob": 0.15,
        "silence_prob": 0.12,
        "interrupt_prob": 0.10,
        "montage_prob": 0.08,
        "callback_prob": 0.07,
    },
}


def _infer_register(name: str, tags: str, category: str) -> str:
    """Leid een register-omschrijving af uit de DB-metadata."""
    combined = f"{name} {tags or ''} {category or ''}".lower()
    if any(k in combined for k in ("bass", "deep", "laag", "low", "dark", "gravel")):
        return "laag"
    if any(k in combined for k in ("soprano", "high", "hoog", "bright", "young", "licht")):
        return "hoog"
    if any(k in combined for k in ("schor", "raspy", "rough", "husky", "hoarse")):
        return "schor"
    if any(k in combined for k in ("warm", "smooth", "zacht", "gentle", "soft")):
        return "warm"
    return "vlak"


def select_ensemble_voices(
    db_path: str,
    n: int = 3,
    script_style: str = "vrij",
    voice_assignments: dict[str, str] | None = None,  # {"stem_0": elevenlabs_id, ...}
) -> EnsembleConfig:
    """
    Haalt n stemmen op uit de voices-tabel in de DB.

    Prioriteit:
    1. voice_assignments dict (als opgegeven): expliciete stem-koppelingen per stem_id
    2. DUET_VOICE_ID env var (als eerste stem)
    3. Nederlandse stemmen (language='nl')
    4. Stemmen met een category (niet leeg)
    5. Random shuffle van de rest
    """
    n = max(2, min(4, n))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    preferred_voice_id = os.environ.get("DUET_VOICE_ID", "")

    # Haal alle bruikbare stemmen op
    rows = conn.execute("""
        SELECT v.id, v.voice_id, v.name, v.tags, v.category, v.language, v.elevenlabs_id
        FROM voices v
        WHERE COALESCE(v.elevenlabs_id, v.voice_id) IS NOT NULL
          AND COALESCE(v.elevenlabs_id, v.voice_id) != ''
          AND v.name IS NOT NULL
        ORDER BY
          CASE WHEN v.language = 'nl' THEN 0 ELSE 1 END,
          CASE WHEN v.category IS NOT NULL AND v.category != '' THEN 0 ELSE 1 END,
          RANDOM()
        LIMIT 80
    """).fetchall()
    conn.close()

    if not rows:
        return _fallback_ensemble(n, script_style)

    # Bouw een lookup elevenlabs_id → row voor voice_assignments
    elevenlabs_lookup: dict[str, sqlite3.Row] = {}
    for row in rows:
        eid = row["elevenlabs_id"] or row["voice_id"]
        if eid:
            elevenlabs_lookup[eid] = row

    # Bouw Voice-objecten
    all_voices: list[Voice] = []
    first_voice: Voice | None = None

    for i, row in enumerate(rows):
        elevenlabs_id = row["elevenlabs_id"] or row["voice_id"]
        if not elevenlabs_id:
            continue
        register = _infer_register(
            row["name"] or "",
            row["tags"] or "",
            row["category"] or "",
        )
        v = Voice(
            id=f"stem_{len(all_voices)}",
            elevenlabs_id=elevenlabs_id,
            name=_ABSTRACT_NAMES[len(all_voices) % len(_ABSTRACT_NAMES)],
            register=register,
            color=_VOICE_COLORS[len(all_voices) % len(_VOICE_COLORS)],
            db_name=row["name"] or "",
        )
        # Preferred voice als eerste
        if preferred_voice_id and elevenlabs_id == preferred_voice_id and first_voice is None:
            first_voice = v
        else:
            all_voices.append(v)

    # Herschik: preferred voice vooraan
    if first_voice:
        first_voice.id = "stem_0"
        first_voice.name = _ABSTRACT_NAMES[0]
        first_voice.color = _VOICE_COLORS[0]
        candidates = [first_voice] + all_voices
    else:
        candidates = all_voices

    # Kies n stemmen met diverse registers
    selected = _pick_diverse(candidates, n)

    # Hernum IDs en namen
    for idx, v in enumerate(selected):
        v.id = f"stem_{idx}"
        v.name = _ABSTRACT_NAMES[idx % len(_ABSTRACT_NAMES)]
        v.color = _VOICE_COLORS[idx % len(_VOICE_COLORS)]

    # Pas voice_assignments toe: vervang elevenlabs_id voor de opgegeven stems
    if voice_assignments:
        for v in selected:
            assigned_id = voice_assignments.get(v.id)
            if assigned_id and assigned_id in elevenlabs_lookup:
                assigned_row = elevenlabs_lookup[assigned_id]
                v.elevenlabs_id = assigned_id
                v.register = _infer_register(
                    assigned_row["name"] or "",
                    assigned_row["tags"] or "",
                    assigned_row["category"] or "",
                )
                v.db_name = assigned_row["name"] or ""

    probs = _STYLE_PROBS.get(script_style, _STYLE_PROBS["vrij"])
    return EnsembleConfig(
        voices=selected,
        script_style=script_style,
        **probs,  # type: ignore[arg-type]
    )


def _pick_diverse(candidates: list[Voice], n: int) -> list[Voice]:
    """Kies n stemmen met zo divers mogelijke registers."""
    if len(candidates) <= n:
        return candidates[:n]
    registers = {}
    for v in candidates:
        registers.setdefault(v.register, []).append(v)
    selected: list[Voice] = []
    # Roteer door registers
    reg_keys = list(registers.keys())
    random.shuffle(reg_keys)
    idx = 0
    while len(selected) < n:
        reg = reg_keys[idx % len(reg_keys)]
        pool = registers[reg]
        if pool:
            selected.append(pool.pop(0))
        idx += 1
        if idx > n * len(reg_keys):
            break
    # Vul aan als nodig
    remaining = [v for v in candidates if v not in selected]
    while len(selected) < n and remaining:
        selected.append(remaining.pop(0))
    return selected[:n]


def _fallback_ensemble(n: int, script_style: str) -> EnsembleConfig:
    """Gebruik de default stem als de DB geen stemmen heeft."""
    default_id = os.environ.get("DUET_VOICE_ID", "PIpnKISoU0pJEeZu0rOq")
    voices = [
        Voice(
            id=f"stem_{i}",
            elevenlabs_id=default_id,
            name=_ABSTRACT_NAMES[i % len(_ABSTRACT_NAMES)],
            register="vlak",
            color=_VOICE_COLORS[i % len(_VOICE_COLORS)],
            db_name="fallback",
        )
        for i in range(n)
    ]
    probs = _STYLE_PROBS.get(script_style, _STYLE_PROBS["vrij"])
    return EnsembleConfig(voices=voices, script_style=script_style, **probs)  # type: ignore[arg-type]


def pick_next_voice(ensemble: EnsembleConfig, voice_history: list[str], suggested_id: str | None = None) -> Voice:
    """
    Kies de volgende stem op basis van:
    1. suggested_id van de LLM (als geldig)
    2. Rotatie: vermijd dezelfde stem 3x achter elkaar
    3. Anders: willekeurig
    """
    voices = ensemble.voices
    if not voices:
        return Voice("stem_0", "", "?", "vlak", "#8fd3ff")

    # Valideer LLM-suggestie
    if suggested_id:
        for v in voices:
            if v.id == suggested_id or v.elevenlabs_id == suggested_id:
                # Mag niet als de laatste 2 beats ook al deze stem was
                recent = voice_history[-2:] if len(voice_history) >= 2 else []
                if recent.count(v.id) < 2:
                    return v

    # Rotatie: vermijd stem die ≥2x in de laatste 3 beats voorkwam
    recent3 = voice_history[-3:]
    for v in voices:
        if recent3.count(v.id) < 2:
            return v

    # Fallback: random
    return random.choice(voices)
