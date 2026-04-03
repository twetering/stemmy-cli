from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any


ARC_STAGES = ["opening", "verkenning", "verdichting", "wending", "landing"]


class BeatType(str, Enum):
    NORMAL       = "normal"
    SILENCE      = "silence"        # opzettelijke pauze
    INTERRUPT    = "interrupt"      # fragment kapt verteller af halverwege
    MONTAGE      = "montage"        # 2-3 fragmenten snel achter elkaar
    CALLBACK     = "callback"       # eerder fragment herhaald
    CONTRAPUNTAL = "contrapuntal"   # verteller + fragment overlappen
    MICRO        = "micro"          # 1-5 woorden geëxtraheerd (Echo-stijl)


@dataclass
class BeatPlan:
    spoken_line: str
    search_brief: str
    fallback_brief: str
    semantic_expansions: list[str] = field(default_factory=list)
    intent: str = "verdieping"
    transition_style: str = "crossfade"
    search_mode: str = "hybrid"
    energy_level: float = 0.5
    stage: str = "opening"
    notes: list[str] = field(default_factory=list)
    voice_id: str | None = None        # welke ElevenLabs-stem dit beat levert
    beat_type: str = "normal"          # BeatType waarde
    callback_fragment_id: str | None = None  # voor CALLBACK beats
    montage_queries: list[str] = field(default_factory=list)  # voor MONTAGE beats
    micro_target_words: list[str] = field(default_factory=list)  # voor MICRO beats
    preselected_fragment_id: str | None = None  # voor Monteur-modus: vooraf geselecteerd fragment

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Candidate:
    id: str
    text: str
    item_audio_url: str
    start_time_seconds: float
    end_time_seconds: float
    source: str = "text"
    score: float = 0.0
    similarity: float = 0.0
    duration_seconds: float = 0.0
    opening_strength: float = 0.0
    ending_strength: float = 0.0
    question_score: float = 0.0
    atmosphere_hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RenderedTurn:
    narrator_audio_id: str | None
    archive_audio_id: str | None
    bridge_audio_id: str | None
    narrator_provider: str
    degraded_mode: str = "full"
    music_active: bool = False
    atmosphere: dict[str, Any] = field(default_factory=dict)
    beat_type: str = "normal"
    silence_ms: int = 0
    montage_audio_ids: list[str] = field(default_factory=list)
    voice_id: str | None = None
    voice_color: str = "#8fd3ff"


@dataclass
class BeatResult:
    mode: str
    conversation_mode: str
    beat_number: int
    narrator_text: str
    fragment_text: str
    intent: str
    transition_style: str
    search_mode: str
    search_query_used: str
    narrator_audio_id: str | None
    archive_audio_id: str | None
    bridge_audio_id: str | None
    narrator_provider: str
    degraded_mode: str
    music_active: bool
    prefetched_next_ready: bool
    candidate_count: int
    stage: str
    semantic_expansions: list[str]
    beat_type: str = "normal"
    silence_ms: int = 0
    montage_audio_ids: list[str] = field(default_factory=list)
    voice_id: str | None = None
    voice_color: str = "#8fd3ff"
    voice_name: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SceneState:
    session_id: str
    theme: str
    mode: str = "orchestrated"
    conversation_mode: str = "duet"
    ui_mode: str = "performance"
    planner_provider: str = "claude"
    tts_provider: str = "elevenlabs"
    voice_id: str | None = None
    format_id: str | None = None
    preset: str = "radio"
    beat_number: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)
    motifs: list[str] = field(default_factory=list)
    recent_fragment_ids: list[str] = field(default_factory=list)
    recent_queries: list[str] = field(default_factory=list)
    recent_gap_urls: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    interrupts: list[str] = field(default_factory=list)
    energy: float = 0.45
    stage_index: int = 0
    last_debug: dict[str, Any] = field(default_factory=dict)
    debug_events: list[dict[str, Any]] = field(default_factory=list)
    pending_interrupt: str = ""
    prefetched_turn: BeatResult | None = None
    prefetched_for_beat: int = -1
    prefetch_in_flight: bool = False
    last_accessed_at: float = field(default_factory=time.time)
    created_at: float = field(default_factory=time.time)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # Ensemble: meerdere TTS-stemmen
    ensemble: Any | None = None          # EnsembleConfig (vermijd circulaire import)
    active_voice_id: str | None = None   # welke stem momenteel actief is
    voice_history: list[str] = field(default_factory=list)  # laatste 6 stem-IDs
    # Arc pre-planning
    arc_plan: Any | None = None          # ArcPlan object
    arc_consumed: int = 0
    arc_building: bool = False           # achtergrondthread bezig met arc bouwen
    # Fragment palette
    fragment_palette: Any | None = None  # FragmentPalette object
    palette_building: bool = False       # achtergrondthread bezig met palette bouwen
    # Stijl
    script_style: str = "vrij"
    n_voices: int = 1
    s2s_mode: bool = False              # ElevenLabs Speech-to-Speech voor archief-fragmenten

    @property
    def stage(self) -> str:
        return ARC_STAGES[min(self.stage_index, len(ARC_STAGES) - 1)]

    def ensemble_voices_public(self) -> list[dict[str, Any]]:
        if not self.ensemble:
            return []
        return [
            {"id": v.id, "name": v.name, "color": v.color, "register": v.register,
             "elevenlabs_id": v.elevenlabs_id}
            for v in self.ensemble.voices
        ]

    def to_public_dict(self) -> dict[str, Any]:
        arc_total = len(self.arc_plan.beats) if self.arc_plan else 0
        arc_types = [b.beat_type for b in self.arc_plan.beats] if self.arc_plan else []
        return {
            "session_id": self.session_id,
            "theme": self.theme,
            "mode": self.mode,
            "conversation_mode": self.conversation_mode,
            "ui_mode": self.ui_mode,
            "planner_provider": self.planner_provider,
            "tts_provider": self.tts_provider,
            "preset": self.preset,
            "beat_number": self.beat_number,
            "stage": self.stage,
            "energy": round(self.energy, 3),
            "motifs": self.motifs[-8:],
            "recent_queries": self.recent_queries[-5:],
            "recent_fragment_ids": self.recent_fragment_ids[-5:],
            "open_questions": self.open_questions[-5:],
            "interrupts": self.interrupts[-5:],
            "prefetched_for_beat": self.prefetched_for_beat,
            "prefetch_in_flight": self.prefetch_in_flight,
            "last_debug": self.last_debug,
            "debug_events": self.debug_events[-10:],
            "ensemble_voices": self.ensemble_voices_public(),
            "script_style": self.script_style,
            "arc_total": arc_total,
            "arc_consumed": self.arc_consumed,
            "arc_beat_types": arc_types,
            "arc_building": self.arc_building,
            "palette_ready": self.fragment_palette is not None,
        }


def new_session_id() -> str:
    return uuid.uuid4().hex


class AudioRegistry:
    def __init__(self):
        self._entries: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def store(
        self,
        audio_bytes: bytes,
        target_dir: Path,
        beat_number: int,
        suffix: str = ".mp3",
        content_type: str = "audio/mpeg",
    ) -> str:
        audio_id = uuid.uuid4().hex
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{audio_id}{suffix}"
        path.write_bytes(audio_bytes)
        with self._lock:
            self._entries[audio_id] = {
                "path": path,
                "beat_number": beat_number,
                "content_type": content_type,
            }
        return audio_id

    def get(self, audio_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._entries.get(audio_id)

    def prune(self, current_beat: int, keep: int = 24) -> None:
        with self._lock:
            stale = [
                audio_id
                for audio_id, entry in self._entries.items()
                if entry["beat_number"] < current_beat - keep
            ]
            for audio_id in stale:
                self._entries.pop(audio_id, None)
