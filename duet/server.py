#!/usr/bin/env python3
"""
HET DUET 3.0 — meerstemmige postmoderne audio-installatie.

Start:
    python3 duet/server.py --db data/stemmy-replica.db --browser
"""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = Path(__file__).resolve().parent / "cache"
TTS_DIR = CACHE_DIR / "tts"
FRAG_DIR = CACHE_DIR / "frags"
BRIDGE_DIR = CACHE_DIR / "bridges"
MIX_DIR = CACHE_DIR / "mixes"
PLAN_DIR = CACHE_DIR / "plans"
MUSIC_DIR = ROOT / "src" / "stemmy_cli" / "static" / "music"

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
os.environ["PATH"] = os.environ.get("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin"

try:
    from dotenv import load_dotenv as _load_dotenv

    _load_dotenv(ROOT / ".env", override=True)
    _load_dotenv(ROOT / ".env.local", override=True)
except Exception:
    pass

from duet.audio_engine import render_turn
from duet.arc_planner import plan_full_arc
from duet.ensemble import select_ensemble_voices, pick_next_voice
from duet.fragment_palette import build_palette
from duet.providers.gemini_live import generate_live_audio_reply
from duet.providers.llm import plan_next_turn
from duet.retrieval import load_formats, pick_candidate, pick_gap, retrieve_candidates
from duet.state import AudioRegistry, BeatPlan, BeatResult, SceneState, new_session_id

DEFAULT_PORT = 7844
SESSION_TTL = 60 * 45
DEFAULT_MODE = "orchestrated"
DEFAULT_PRESET = "radio"
DEFAULT_UI_MODE = "performance"
DEFAULT_TTS_PROVIDER = "elevenlabs"
DEFAULT_PLANNER_PROVIDER = "claude"
DEFAULT_CONVERSATION_MODE = "duet"

_db_path_global = ""
_session_lock = threading.Lock()
_sessions: dict[str, SceneState] = {}
_audio_registry = AudioRegistry()
_voices_cache: list[dict] = []
_voices_cache_lock = threading.Lock()


class _RateLimiter:
    def __init__(self, max_per_window: int = 6, window_sec: float = 10.0):
        self._max = max_per_window
        self._window = window_sec
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check(self, ip: str) -> bool:
        now = time.time()
        with self._lock:
            recent = [stamp for stamp in self._hits.get(ip, []) if now - stamp < self._window]
            if len(recent) >= self._max:
                return False
            recent.append(now)
            self._hits[ip] = recent
            return True


_rate_limiter = _RateLimiter()


def _clean_session_payload(payload: dict) -> dict:
    mode = str(payload.get("mode") or DEFAULT_MODE).strip().lower()
    ui_mode = str(payload.get("ui_mode") or DEFAULT_UI_MODE).strip().lower()
    planner_provider = str(payload.get("planner_provider") or DEFAULT_PLANNER_PROVIDER).strip().lower()
    tts_provider = str(payload.get("tts_provider") or DEFAULT_TTS_PROVIDER).strip().lower()
    preset = str(payload.get("preset") or DEFAULT_PRESET).strip().lower()
    conversation_mode = str(payload.get("conversation_mode") or DEFAULT_CONVERSATION_MODE).strip().lower()
    if mode not in {"orchestrated", "live", "script"}:
        mode = DEFAULT_MODE
    if ui_mode not in {"performance", "studio"}:
        ui_mode = DEFAULT_UI_MODE
    if planner_provider not in {"claude", "gemini"}:
        planner_provider = DEFAULT_PLANNER_PROVIDER
    if tts_provider not in {"elevenlabs", "gemini", "browser", "gemini-live"}:
        tts_provider = DEFAULT_TTS_PROVIDER
    if preset not in {"droog", "radio", "filmisch"}:
        preset = DEFAULT_PRESET
    if conversation_mode not in {"duet", "interview"}:
        conversation_mode = DEFAULT_CONVERSATION_MODE
    if mode == "live":
        planner_provider = "gemini"
        if tts_provider in {"elevenlabs", "gemini"}:
            tts_provider = "gemini-live"
    script_style = str(payload.get("script_style") or "vrij").strip().lower()
    if script_style not in {"vrij", "kakofonie", "verhoor", "requiem"}:
        script_style = "vrij"
    try:
        n_voices = int(payload.get("n_voices") or 1)
        n_voices = max(1, min(4, n_voices))
    except (TypeError, ValueError):
        n_voices = 1
    return {
        "theme": str(payload.get("theme") or "").strip()[:120],
        "format_id": (str(payload.get("format_id")).strip()[:64] or None) if payload.get("format_id") else None,
        "mode": mode,
        "conversation_mode": conversation_mode,
        "ui_mode": ui_mode,
        "planner_provider": planner_provider,
        "tts_provider": tts_provider,
        "preset": preset,
        "voice_id": (str(payload.get("voice_id")).strip()[:64] or None) if payload.get("voice_id") else None,
        "script_style": script_style,
        "n_voices": n_voices,
        "voice_assignments": {str(k): str(v) for k, v in (payload.get("voice_assignments") or {}).items()},
        "s2s_mode": bool(payload.get("s2s_mode", False)),
    }


def _create_session(payload: dict) -> SceneState:
    clean = _clean_session_payload(payload)
    n_voices = clean["n_voices"]
    script_style = clean["script_style"]

    # Bouw ensemble (synchrone DB-query, snel)
    ensemble = None
    voice_assignments = clean.get("voice_assignments") or {}
    if n_voices > 1 and _db_path_global:
        try:
            ensemble = select_ensemble_voices(
                _db_path_global,
                n=n_voices,
                script_style=script_style,
                voice_assignments=voice_assignments if voice_assignments else None,
            )
        except Exception:
            ensemble = None

    session = SceneState(
        session_id=new_session_id(),
        theme=clean["theme"],
        mode=clean["mode"],
        conversation_mode=clean["conversation_mode"],
        ui_mode=clean["ui_mode"],
        planner_provider=clean["planner_provider"],
        tts_provider=clean["tts_provider"],
        voice_id=clean["voice_id"],
        format_id=clean["format_id"],
        preset=clean["preset"],
        script_style=script_style,
        n_voices=n_voices,
        ensemble=ensemble,
        s2s_mode=clean.get("s2s_mode", False),
    )
    with _session_lock:
        _sessions[session.session_id] = session

    # Start achtergrondthreads voor palette en arc
    if _db_path_global and clean["theme"]:
        if clean.get("mode") == "script":
            # Script-modus: bouw pool + script in achtergrond
            threading.Thread(
                target=_build_script_worker,
                args=(session, _db_path_global),
                daemon=True,
            ).start()
        else:
            threading.Thread(
                target=_build_palette_worker,
                args=(session.session_id,),
                daemon=True,
            ).start()
    return session


def _build_palette_worker(session_id: str) -> None:
    """Bouwt het fragment-palette in de achtergrond."""
    session = _get_session(session_id)
    if not session:
        return
    with session.lock:
        if session.palette_building:
            return
        session.palette_building = True
        theme = session.theme
        format_id = session.format_id
    try:
        palette = build_palette(theme, _db_path_global, format_id)
        with session.lock:
            session.fragment_palette = palette
            session.palette_building = False
        # Start arc-planning zodra palette klaar is
        threading.Thread(
            target=_build_arc_worker,
            args=(session_id,),
            daemon=True,
        ).start()
    except Exception:
        with session.lock:
            session.palette_building = False


def _build_arc_worker(session_id: str) -> None:
    """Bouwt de volledige arc in de achtergrond."""
    session = _get_session(session_id)
    if not session:
        return
    with session.lock:
        if session.arc_building or session.arc_plan:
            return
        session.arc_building = True
    try:
        arc = plan_full_arc(session)
        with session.lock:
            session.arc_building = False
            if arc:
                session.arc_plan = arc
                # Voeg arc-motieven toe aan session
                for m in arc.motifs:
                    if m not in session.motifs:
                        session.motifs.append(m)
                session.motifs = session.motifs[-16:]
    except Exception:
        with session.lock:
            session.arc_building = False


def _build_script_worker(session: SceneState, db_path: str) -> None:
    """Bouwt een Monteur-script in de achtergrond."""
    try:
        from duet.script_builder import build_script
        from duet.retrieval import retrieve_pool

        # 1. Haal fragment pool op
        pool = retrieve_pool(session.theme, db_path, session.format_id, n=60)
        if not pool:
            return

        # 2. Lees sessie-state onder lock
        with session.lock:
            ensemble = session.ensemble
            style = session.script_style
            provider = session.planner_provider
            session.arc_building = True

        # 3. Bouw script
        arc = build_script(
            theme=session.theme,
            fragment_pool=pool,
            ensemble_config=ensemble,
            script_style=style,
            n_beats=22,
            planner_provider=provider,
        )
        if arc:
            with session.lock:
                session.arc_plan = arc
                session.arc_building = False
                session.motifs = arc.motifs[:8]
    except Exception:
        import traceback
        traceback.print_exc()
    finally:
        with session.lock:
            session.arc_building = False


def _get_session(session_id: str) -> SceneState | None:
    if not session_id:
        return None
    with _session_lock:
        session = _sessions.get(session_id)
    if session:
        session.last_accessed_at = time.time()
    return session


def _prune_sessions() -> None:
    now = time.time()
    with _session_lock:
        stale = [sid for sid, session in _sessions.items() if now - session.last_accessed_at > SESSION_TTL]
        for sid in stale:
            _sessions.pop(sid, None)


def _derive_stage_index(beat_number: int) -> int:
    if beat_number <= 1:
        return 0
    if beat_number <= 3:
        return 1
    if beat_number <= 6:
        return 2
    if beat_number <= 8:
        return 3
    return 4


def _extract_motifs(plan) -> list[str]:
    motifs = []
    motifs.extend(plan.semantic_expansions[:4])
    motifs.extend(re.findall(r"[\w']+", plan.spoken_line.lower())[:4])
    deduped = []
    for motif in motifs:
        motif = motif.strip().lower()
        if len(motif) < 4 or motif in deduped:
            continue
        deduped.append(motif)
    return deduped[:6]


def _clone_scene(scene: SceneState) -> SceneState:
    clone = SceneState(
        session_id=scene.session_id,
        theme=scene.theme,
        mode=scene.mode,
        conversation_mode=scene.conversation_mode,
        ui_mode=scene.ui_mode,
        planner_provider=scene.planner_provider,
        tts_provider=scene.tts_provider,
        voice_id=scene.voice_id,
        format_id=scene.format_id,
        preset=scene.preset,
        beat_number=scene.beat_number,
        history=list(scene.history),
        motifs=list(scene.motifs),
        recent_fragment_ids=list(scene.recent_fragment_ids),
        recent_queries=list(scene.recent_queries),
        recent_gap_urls=list(scene.recent_gap_urls),
        open_questions=list(scene.open_questions),
        interrupts=list(scene.interrupts),
        energy=scene.energy,
        stage_index=scene.stage_index,
        last_debug=dict(scene.last_debug),
        debug_events=list(scene.debug_events),
        pending_interrupt=scene.pending_interrupt,
        ensemble=scene.ensemble,
        active_voice_id=scene.active_voice_id,
        voice_history=list(scene.voice_history),
        fragment_palette=scene.fragment_palette,
        script_style=scene.script_style,
        n_voices=scene.n_voices,
    )
    return clone


def _compute_turn(scene: SceneState, user_input: str = "") -> BeatResult:
    timings = {"started_at": round(time.time(), 3)}

    # ── Arc consumptie: gebruik pre-geplande beat als beschikbaar ────────────
    plan: BeatPlan | None = None
    planner_debug: dict = {}
    using_arc = False
    if scene.arc_plan and scene.arc_consumed < len(scene.arc_plan.beats) and not user_input:
        plan = scene.arc_plan.beats[scene.arc_consumed]
        planner_debug = {
            "planner_provider": f"arc:{scene.arc_plan.provider}",
            "planner_model": scene.arc_plan.model,
            "planner_error": None,
            "raw_plan": "(arc pre-planned)",
            "plan_parse_mode": "arc",
            "plan_missing_fields": {"spoken_line": not bool(plan.spoken_line), "search_brief": not bool(plan.search_brief)},
        }
        using_arc = True

    if not plan:
        plan, planner_debug = plan_next_turn(scene, user_input)
    timings["planned_at"] = round(time.time(), 3)

    # ── Stem-selectie voor ensemble ─────────────────────────────────────────
    active_voice_id = scene.voice_id  # default: enkelvoudige stem
    active_voice_name = ""
    active_voice_color = "#8fd3ff"
    active_voice_stem_id = ""
    if scene.ensemble and scene.ensemble.voices:
        suggested = plan.voice_id  # LLM/arc suggestie
        chosen_voice = pick_next_voice(scene.ensemble, scene.voice_history, suggested_id=suggested)
        active_voice_id = chosen_voice.elevenlabs_id
        active_voice_stem_id = chosen_voice.id  # "stem_0", "stem_1" etc.
        active_voice_name = chosen_voice.name
        active_voice_color = chosen_voice.color

    beat_type = (plan.beat_type or "normal").lower()
    search_query = plan.search_brief or plan.fallback_brief or scene.theme
    if scene.conversation_mode == "interview":
        search_query = f"{search_query} waarom hoe wat vraag"

    # ── Candidate retrieval ─────────────────────────────────────────────────
    # Monteur: gebruik vooraf geselecteerd fragment als beschikbaar
    preselected_chosen = None
    if plan.preselected_fragment_id:
        import sqlite3 as _sqlite3
        try:
            _conn = _sqlite3.connect(_db_path_global)
            _conn.row_factory = _sqlite3.Row
            _row = _conn.execute(
                "SELECT id, text, item_audio_url, start_time_seconds, end_time_seconds FROM fragments WHERE id = ?",
                [plan.preselected_fragment_id],
            ).fetchone()
            _conn.close()
            if _row and _row["item_audio_url"]:
                from duet.state import Candidate
                preselected_chosen = Candidate(
                    id=str(_row["id"]),
                    text=str(_row["text"] or ""),
                    item_audio_url=str(_row["item_audio_url"]),
                    start_time_seconds=float(_row["start_time_seconds"] or 0),
                    end_time_seconds=float(_row["end_time_seconds"] or 0),
                    source="monteur",
                    score=1.0,
                )
        except Exception:
            pass

    if preselected_chosen is not None:
        candidates = [preselected_chosen]
        chosen = preselected_chosen
        timings["retrieved_at"] = round(time.time(), 3)
    else:
        candidates = retrieve_candidates(plan, scene, _db_path_global)
        timings["retrieved_at"] = round(time.time(), 3)
        chosen = pick_candidate(candidates, scene)

    # ── Connector: fragment-aware spoken_line genereren ────────────────────
    if chosen and scene.conversation_mode != "interview" and not using_arc:
        # Pas de spoken_line aan zodat hij het fragment direct inleidt
        from duet.providers.llm import generate_connector
        adapted = generate_connector(
            fragment_text=chosen.text,
            scene=scene,
            intent=plan.intent,
            spoken_hint=plan.spoken_line,
        )
        if adapted and adapted != plan.spoken_line:
            plan = BeatPlan(
                spoken_line=adapted,
                search_brief=plan.search_brief,
                fallback_brief=plan.fallback_brief,
                semantic_expansions=plan.semantic_expansions,
                intent=plan.intent,
                transition_style=plan.transition_style,
                search_mode=plan.search_mode,
                energy_level=plan.energy_level,
                stage=plan.stage,
                notes=plan.notes + ["connector_adapted"],
                voice_id=plan.voice_id,
                beat_type=plan.beat_type,
            )

    # ── Interview modus (ongewijzigd) ───────────────────────────────────────
    if scene.conversation_mode == "interview" and chosen:
        answer_scene = _clone_scene(scene)
        answer_scene.history.append({"fragment_text": chosen.text, "intent": "vraag", "stage": answer_scene.stage})
        plan, answer_debug = plan_next_turn(answer_scene, user_input)
        planner_debug["answer_plan"] = answer_debug
        timings["answered_at"] = round(time.time(), 3)

    # ── Montage: haal extra candidates op ──────────────────────────────────
    extra_candidates: list[Candidate] = []
    if beat_type == "montage" and plan.montage_queries:
        from duet.state import BeatPlan as _BP
        for mq in plan.montage_queries[:2]:
            mplan = _BP(spoken_line="", search_brief=mq, fallback_brief=mq, search_mode="hybrid")
            mc_list = retrieve_candidates(mplan, scene, _db_path_global, limit=4)
            mc = pick_candidate(mc_list, scene)
            if mc and mc.id != (chosen.id if chosen else ""):
                extra_candidates.append(mc)

    # ── Callback: haal eerder fragment op ──────────────────────────────────
    if beat_type == "callback" and scene.arc_plan and using_arc:
        src_idx = scene.arc_plan.callback_map.get(scene.arc_consumed)
        if src_idx is not None and src_idx < len(scene.arc_plan.beats):
            # Zoek het fragment dat bij beat src_idx gespeeld werd
            if src_idx < len(scene.history):
                hist_frag_text = scene.history[src_idx].get("fragment_text", "")
                if hist_frag_text:
                    cb_plan = scene.arc_plan.beats[src_idx]
                    cb_cands = retrieve_candidates(cb_plan, scene, _db_path_global, limit=8)
                    # Zoek op tekstmatch
                    for cb_c in cb_cands:
                        if cb_c.text[:40] == hist_frag_text[:40]:
                            chosen = cb_c
                            break

    # ── Gap ────────────────────────────────────────────────────────────────
    gap = None
    if scene.preset != "droog" and beat_type not in {"silence", "montage"} and (
        scene.beat_number > 0 or plan.transition_style in {"breath", "dissolve"}
    ):
        gap = pick_gap(scene, _db_path_global)

    # ── Rendering ──────────────────────────────────────────────────────────
    rendered, render_debug = render_turn(
        scene,
        narrator_text=plan.spoken_line,
        narrator_provider=scene.tts_provider,
        voice_id=active_voice_id,
        candidate=chosen,
        gap=gap,
        registry=_audio_registry,
        tts_dir=TTS_DIR,
        frag_dir=FRAG_DIR,
        bridge_dir=BRIDGE_DIR,
        plan=plan,
        extra_candidates=extra_candidates,
    )
    fragment_text = chosen.text if chosen else ""
    timings["rendered_at"] = round(time.time(), 3)
    timings["plan_ms"] = int((timings["planned_at"] - timings["started_at"]) * 1000)
    timings["retrieve_ms"] = int((timings["retrieved_at"] - timings["planned_at"]) * 1000)
    timings["render_ms"] = int((timings["rendered_at"] - timings["retrieved_at"]) * 1000)
    timings["total_ms"] = int((timings["rendered_at"] - timings["started_at"]) * 1000)
    diagnostics = {
        **planner_debug,
        **render_debug,
        "stage": scene.stage,
        "question_plan": plan.to_dict(),
        "question_plan_notes": plan.notes[:],
        "answer_line": plan.spoken_line if scene.conversation_mode == "interview" else "",
        "top_candidates": [c.to_dict() for c in candidates[:5]],
        "chosen_candidate": chosen.to_dict() if chosen else None,
        "gap": gap,
        "timings": timings,
        "using_arc": using_arc,
        "beat_type": beat_type,
        "active_voice_id": active_voice_id,
        "active_voice_stem_id": active_voice_stem_id if scene.ensemble and scene.ensemble.voices else "",
        "active_voice_name": active_voice_name,
        "live_preview_note": (
            "Gemini Live native transport is nog experimenteel; deze mode gebruikt nu de snellere Gemini-orchestratie."
            if scene.mode == "live" and scene.tts_provider != "gemini-live"
            else ""
        ),
    }
    return BeatResult(
        mode=scene.mode,
        conversation_mode=scene.conversation_mode,
        beat_number=scene.beat_number,
        narrator_text=plan.spoken_line,
        fragment_text=fragment_text,
        intent=plan.intent,
        transition_style=plan.transition_style,
        search_mode=plan.search_mode,
        search_query_used=search_query,
        narrator_audio_id=rendered.narrator_audio_id,
        archive_audio_id=rendered.archive_audio_id,
        bridge_audio_id=rendered.bridge_audio_id,
        narrator_provider=rendered.narrator_provider,
        degraded_mode=rendered.degraded_mode,
        music_active=rendered.music_active,
        prefetched_next_ready=False,
        candidate_count=len(candidates),
        stage=scene.stage,
        semantic_expansions=plan.semantic_expansions,
        beat_type=rendered.beat_type,
        silence_ms=rendered.silence_ms,
        montage_audio_ids=rendered.montage_audio_ids,
        voice_id=active_voice_id,
        voice_color=active_voice_color,
        voice_name=active_voice_name,
        diagnostics=diagnostics,
    )


def _apply_turn(scene: SceneState, result: BeatResult, user_input: str = "") -> None:
    scene.history.append(
        {
            "narrator_text": result.narrator_text,
            "fragment_text": result.fragment_text,
            "search_query": result.search_query_used,
            "intent": result.intent,
            "stage": result.stage,
            "beat_type": result.beat_type,
            "voice_id": result.voice_id,
            "voice_name": result.voice_name,
        }
    )
    if len(scene.history) > 12:
        scene.history = scene.history[-12:]
    if user_input:
        scene.interrupts.append(user_input)
        scene.interrupts = scene.interrupts[-8:]
    if result.diagnostics.get("chosen_candidate"):
        candidate = result.diagnostics["chosen_candidate"]
        scene.recent_fragment_ids.append(candidate["id"])
        scene.recent_fragment_ids = scene.recent_fragment_ids[-10:]
    gap_info = result.diagnostics.get("gap") or {}
    if gap_info.get("audio_url"):
        scene.recent_gap_urls.append(gap_info["audio_url"])
        scene.recent_gap_urls = scene.recent_gap_urls[-10:]
    scene.recent_queries.append(result.search_query_used)
    scene.recent_queries = scene.recent_queries[-10:]
    plan_motifs = _extract_motifs(type("P", (), {"semantic_expansions": result.semantic_expansions, "spoken_line": result.narrator_text}))
    for motif in plan_motifs:
        if motif not in scene.motifs:
            scene.motifs.append(motif)
    scene.motifs = scene.motifs[-12:]
    if "?" in result.narrator_text:
        scene.open_questions.append(result.narrator_text)
        scene.open_questions = scene.open_questions[-8:]
    scene.energy = max(0.15, min(0.95, scene.energy * 0.65 + 0.35 * (0.45 if result.stage == "opening" else 0.68)))
    scene.beat_number += 1
    # Voice-history bijwerken voor ensemble-rotatie
    stem_id = result.diagnostics.get("active_voice_stem_id")
    if stem_id:
        scene.voice_history.append(stem_id)
        scene.voice_history = scene.voice_history[-8:]
    scene.stage_index = _derive_stage_index(scene.beat_number)
    scene.last_debug = result.diagnostics
    scene.debug_events.append(
        {
            "beat_number": result.beat_number,
            "conversation_mode": result.conversation_mode,
            "search_mode": result.search_mode,
            "intent": result.intent,
            "search_query_used": result.search_query_used,
            "degraded_mode": result.degraded_mode,
            "timings": result.diagnostics.get("timings", {}),
            "chosen_candidate": result.diagnostics.get("chosen_candidate"),
            "tts_provider": result.diagnostics.get("tts_provider"),
            "tts_voice": result.diagnostics.get("tts_voice"),
            "planner_provider": result.diagnostics.get("planner_provider"),
            "planner_error": result.diagnostics.get("planner_error"),
            "raw_plan": result.diagnostics.get("raw_plan"),
            "plan_parse_mode": result.diagnostics.get("plan_parse_mode"),
            "question_plan_notes": result.diagnostics.get("question_plan_notes"),
            "answer_plan": result.diagnostics.get("answer_plan"),
        }
    )
    scene.debug_events = scene.debug_events[-20:]
    # Arc consumptie verhogen
    if result.diagnostics.get("using_arc") and scene.arc_plan:
        scene.arc_consumed += 1
        # Als arc uitgeput: reset (valt terug op live planning)
        if scene.arc_consumed >= len(scene.arc_plan.beats):
            scene.arc_plan = None
            scene.arc_consumed = 0
    # Bij interrupt: arc invalide maken
    if user_input and scene.arc_plan:
        scene.arc_plan = None
        scene.arc_consumed = 0
    # Voice history bijhouden
    if result.voice_id:
        # Sla intern stem-ID op (stem_0, stem_1, …)
        voice_internal_id = result.diagnostics.get("active_voice_id", "") or ""
        # Zoek intern id via ensemble
        if scene.ensemble:
            for v in scene.ensemble.voices:
                if v.elevenlabs_id == result.voice_id:
                    voice_internal_id = v.id
                    break
        if voice_internal_id:
            scene.voice_history.append(voice_internal_id)
            scene.voice_history = scene.voice_history[-8:]
        scene.active_voice_id = result.voice_id
    scene.pending_interrupt = ""
    scene.prefetched_turn = None
    scene.prefetched_for_beat = -1
    scene.last_accessed_at = time.time()
    _audio_registry.prune(scene.beat_number)


def _prefetch_worker(session_id: str) -> None:
    session = _get_session(session_id)
    if not session:
        return
    with session.lock:
        if session.prefetch_in_flight or session.pending_interrupt:
            return
        expected_beat = session.beat_number
        session.prefetch_in_flight = True
        prefetch_started_at = round(time.time(), 3)
    try:
        prefetched = _compute_turn(session, "")
    except Exception as exc:
        prefetched = None
        err = str(exc)
    else:
        err = ""
    with session.lock:
        session.prefetch_in_flight = False
        if session.beat_number == expected_beat and not session.pending_interrupt and prefetched:
            prefetched.prefetched_next_ready = True
            session.prefetched_turn = prefetched
            session.prefetched_for_beat = expected_beat
            session.last_debug["prefetch"] = {
                "status": "ready",
                "for_beat": expected_beat,
                "started_at": prefetch_started_at,
                "finished_at": round(time.time(), 3),
            }
        elif err:
            session.last_debug["prefetch"] = {
                "status": "error",
                "for_beat": expected_beat,
                "started_at": prefetch_started_at,
                "finished_at": round(time.time(), 3),
                "error": err,
            }
        else:
            session.last_debug["prefetch"] = {
                "status": "discarded",
                "for_beat": expected_beat,
                "started_at": prefetch_started_at,
                "finished_at": round(time.time(), 3),
            }


def _trigger_prefetch(session: SceneState) -> None:
    if session.mode == "live":
        return
    threading.Thread(target=_prefetch_worker, args=(session.session_id,), daemon=True).start()


def _consume_turn(session: SceneState, user_input: str) -> BeatResult:
    with session.lock:
        user_input = (user_input or session.pending_interrupt or "").strip()
        if (
            not user_input
            and session.prefetched_turn is not None
            and session.prefetched_for_beat == session.beat_number
        ):
            result = session.prefetched_turn
            result.prefetched_next_ready = True
            session.prefetched_turn = None
            session.prefetched_for_beat = -1
        else:
            result = _compute_turn(session, user_input)
        _apply_turn(session, result, user_input)
    _trigger_prefetch(session)
    return result


def _collect_health() -> dict:
    health = {
        "dotenv": {
            "installed": importlib.util.find_spec("dotenv") is not None,
            "env_loaded": bool(os.environ.get("ELEVENLABS_API_KEY") or os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("GOOGLE_API_KEY")),
        },
        "ffmpeg": {
            "installed": shutil.which("ffmpeg") is not None,
        },
        "elevenlabs": {
            "package": importlib.util.find_spec("elevenlabs") is not None,
            "key_present": bool(os.environ.get("ELEVENLABS_API_KEY")),
            "voice_id": os.environ.get("DUET_VOICE_ID", "PIpnKISoU0pJEeZu0rOq"),
            "available_voice_count": len(_load_duet_voices(_db_path_global)),
            "default_voice_name": next((v["name"] for v in _load_duet_voices(_db_path_global) if v["voice_id"] == os.environ.get("DUET_VOICE_ID", "PIpnKISoU0pJEeZu0rOq")), "Theo"),
        },
        "anthropic": {
            "key_present": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "model": os.environ.get("DUET_MODEL", "claude-sonnet-4-6"),
        },
        "google": {
            "key_present": bool(os.environ.get("GOOGLE_API_KEY")),
            "tts_model": os.environ.get("DUET_GEMINI_TTS_MODEL", "gemini-2.5-flash-preview-tts"),
            "live_model": os.environ.get("DUET_GEMINI_LIVE_MODEL", "gemini-3.1-flash-live-preview"),
            "genai_sdk": importlib.util.find_spec("google.genai") is not None if importlib.util.find_spec("google") else False,
            "websockets": importlib.util.find_spec("websockets") is not None,
        },
        "db": {
            "path": _db_path_global,
            "exists": bool(_db_path_global and Path(_db_path_global).exists()),
        },
        "embeddings": {
            "fragment_embeddings": 0,
            "fragment_gemini_embeddings": 0,
        },
    }
    if health["db"]["exists"]:
        try:
            conn = sqlite3.connect(_db_path_global)
            for table in ("fragment_embeddings", "fragment_gemini_embeddings"):
                try:
                    health["embeddings"][table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                except Exception:
                    health["embeddings"][table] = 0
            conn.close()
        except Exception as exc:
            health["db"]["error"] = str(exc)
    health["live_mode"] = {
        "status": "experimental",
        "native_transport_ready": bool(health["google"]["key_present"] and health["google"]["genai_sdk"]),
        "note": "Gemini Live audio-to-audio is aangesloten voor microfoon-interrupts; de volledige always-on streaming agent blijft experimenteel.",
    }
    return health


def _load_duet_voices(db_path: str) -> list[dict]:
    with _voices_cache_lock:
        if _voices_cache:
            return list(_voices_cache)
        voices: list[dict] = []
        if db_path and Path(db_path).exists():
            try:
                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT id, name, voice_id, elevenlabs_id, category, description, language
                    FROM voices
                    WHERE COALESCE(elevenlabs_id, voice_id) IS NOT NULL
                    ORDER BY
                      CASE WHEN lower(name) = 'theo' THEN 0 ELSE 1 END,
                      CASE WHEN category = 'cloned' THEN 0 ELSE 1 END,
                      name
                    """
                ).fetchall()
                conn.close()
                for row in rows:
                    actual_voice_id = row["elevenlabs_id"] or row["voice_id"]
                    if not actual_voice_id:
                        continue
                    voices.append(
                        {
                            "id": row["id"],
                            "name": row["name"] or actual_voice_id,
                            "voice_id": actual_voice_id,
                            "category": row["category"] or "",
                            "description": row["description"] or "",
                            "language": row["language"] or "",
                            "source": "db",
                        }
                    )
            except Exception:
                voices = []
        deduped: dict[str, dict] = {}
        for voice in voices:
            deduped.setdefault(voice["voice_id"], voice)
        ordered = list(deduped.values())
        default_voice = os.environ.get("DUET_VOICE_ID", "PIpnKISoU0pJEeZu0rOq")
        ordered.sort(key=lambda item: (0 if item["voice_id"] == default_voice else 1, 0 if item["category"] == "cloned" else 1, item["name"].lower()))
        _voices_cache.extend(ordered)
        return list(_voices_cache)


def _gemini_transcribe(audio_bytes: bytes, mime_type: str = "audio/webm") -> str:
    import urllib.request as _req

    api_key = os.environ.get("GOOGLE_API_KEY", "")
    if not api_key or not audio_bytes:
        return ""
    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "text": (
                            "Transcribeer exact wat er gezegd wordt in het Nederlands. "
                            "Geef alleen de gesproken woorden terug."
                        )
                    },
                    {"inlineData": {"mimeType": mime_type, "data": base64.b64encode(audio_bytes).decode()}},
                ]
            }
        ]
    }
    req = _req.Request(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-lite:generateContent?key="
        + api_key,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _req.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception:
        return ""


STATIC_DIR = Path(__file__).resolve().parent / "static"


def _load_html() -> bytes:
    p = STATIC_DIR / "app.html"
    if p.exists():
        return p.read_bytes()
    return b"<html><body>app.html not found</body></html>"



class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class DuetHandler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        _prune_sessions()
        parsed = urlparse(self.path)
        path = parsed.path
        if path in {"/duet", "/duet/"}:
            self._send_html(_load_html())
            return
        if path == "/duet/ready":
            self._send_json(
                200,
                {
                    "ready": True,
                    "formats": load_formats(_db_path_global),
                    "voices": _load_duet_voices(_db_path_global),
                    "health": _collect_health(),
                },
            )
            return
        if path == "/duet/voices":
            self._send_json(200, {"voices": _load_duet_voices(_db_path_global)})
            return
        if path == "/duet/health":
            self._send_json(200, _collect_health())
            return
        if path.startswith("/duet/audio/"):
            audio_id = path.split("/duet/audio/", 1)[1].strip("/")
            if not re.fullmatch(r"[0-9a-f]{32}", audio_id):
                self._send_error(400, "ongeldig audio-id")
                return
            entry = _audio_registry.get(audio_id)
            if not entry or not entry["path"].exists():
                self._send_error(404, "audio niet gevonden")
                return
            data = entry["path"].read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", entry.get("content_type", "audio/mpeg"))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self._cors()
            self.end_headers()
            self.wfile.write(data)
            return
        if path.startswith("/duet/music/"):
            name = path.split("/duet/music/", 1)[1].strip("/")
            if "/" in name or ".." in name:
                self._send_error(400, "ongeldige music path")
                return
            target = MUSIC_DIR / name
            if not target.exists():
                self._send_error(404, "music not found")
                return
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(data)))
            self._cors()
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/duet/debug/state":
            session_id = parse_qs(parsed.query).get("session_id", [""])[0]
            session = _get_session(session_id)
            if not session:
                self._send_error(404, "sessie niet gevonden")
                return
            self._send_json(200, session.to_public_dict())
            return
        if path == "/duet/debug/events":
            session_id = parse_qs(parsed.query).get("session_id", [""])[0]
            session = _get_session(session_id)
            if not session:
                self._send_error(404, "sessie niet gevonden")
                return
            self._send_json(200, {"session_id": session.session_id, "events": session.debug_events})
            return
        self._send_error(404, "niet gevonden")

    def do_POST(self):
        _prune_sessions()
        parsed = urlparse(self.path)
        path = parsed.path
        if path != "/duet/audiomatch" and not _rate_limiter.check(self.client_address[0]):
            self._send_error(429, "te veel verzoeken")
            return
        if path == "/duet/session/start":
            self._handle_session_start()
            return
        if path == "/duet/session/beat":
            self._handle_session_beat()
            return
        if path == "/duet/session/interrupt":
            self._handle_session_interrupt()
            return
        if path == "/duet/audiomatch":
            self._handle_audiomatch()
            return
        if path == "/duet/live/respond":
            self._handle_live_respond()
            return
        self._send_error(404, "niet gevonden")

    def _read_json(self) -> dict | None:
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            content_length = 0
        if content_length > 262_144:
            self._send_error(413, "payload te groot")
            return None
        try:
            raw = self.rfile.read(content_length) if content_length > 0 else b"{}"
            return json.loads(raw.decode("utf-8"))
        except Exception:
            self._send_error(400, "ongeldige json")
            return None

    def _handle_session_start(self):
        payload = self._read_json()
        if payload is None:
            return
        clean = _clean_session_payload(payload)
        if not clean["theme"]:
            self._send_error(400, "thema ontbreekt")
            return
        session = _create_session(payload)
        self._send_json(
            200,
            {
                "session": session.to_public_dict(),
                "formats": load_formats(_db_path_global),
                "health": _collect_health(),
            },
        )

    def _handle_session_beat(self):
        payload = self._read_json()
        if payload is None:
            return
        session = _get_session(str(payload.get("session_id") or ""))
        if not session:
            self._send_error(404, "sessie niet gevonden")
            return
        user_input = str(payload.get("user_input") or "").strip()[:200]
        try:
            result = _consume_turn(session, user_input)
        except Exception as exc:
            self._send_error(500, f"beat fout: {exc}")
            return
        self._send_json(200, result.to_dict())

    def _handle_session_interrupt(self):
        payload = self._read_json()
        if payload is None:
            return
        session = _get_session(str(payload.get("session_id") or ""))
        if not session:
            self._send_error(404, "sessie niet gevonden")
            return
        user_input = str(payload.get("user_input") or "").strip()[:200]
        with session.lock:
            session.pending_interrupt = user_input
            session.prefetched_turn = None
            session.prefetched_for_beat = -1
        self._send_json(200, {"status": "interrupt geregistreerd", "session_id": session.session_id})

    def _handle_audiomatch(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            content_length = 0
        if content_length > 12_000_000:
            self._send_error(413, "audio te groot")
            return
        audio_in = self.rfile.read(content_length) if content_length > 0 else b""
        mime_type = self.headers.get("Content-Type", "audio/webm").split(";")[0].strip()
        self._send_json(200, {"transcript": _gemini_transcribe(audio_in, mime_type)})

    def _handle_live_respond(self):
        session_id = self.headers.get("X-Duet-Session", "").strip()
        session = _get_session(session_id)
        if not session:
            self._send_error(404, "sessie niet gevonden")
            return
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            content_length = 0
        if content_length > 12_000_000:
            self._send_error(413, "audio te groot")
            return
        audio_in = self.rfile.read(content_length) if content_length > 0 else b""
        mime_type = self.headers.get("Content-Type", "audio/webm").split(";")[0].strip()
        if not audio_in:
            self._send_error(400, "geen audio ontvangen")
            return
        with session.lock:
            prompt = (
                f"Je bent {'een podcastgast' if session.conversation_mode == 'interview' else 'de AI-stem in een duet'} "
                f"binnen het thema '{session.theme}'. Reageer emotioneel passend, compact, in het Nederlands, "
                f"en houd rekening met deze recente context: {json.dumps(session.history[-4:], ensure_ascii=False)}"
            )
        live_result = generate_live_audio_reply(audio_in, mime_type, prompt)
        transcript = live_result.input_transcript or _gemini_transcribe(audio_in, mime_type)
        if transcript:
            with session.lock:
                session.pending_interrupt = transcript
        audio_id = None
        if live_result.audio_bytes:
            audio_id = _audio_registry.store(live_result.audio_bytes, TTS_DIR, session.beat_number)
        self._send_json(
            200,
            {
                "audio_id": audio_id,
                "input_transcript": transcript,
                "output_transcript": live_result.output_transcript,
                "provider": "gemini-live",
                "model": live_result.model,
                "voice": live_result.voice,
                "error": live_result.error,
                "diagnostics": live_result.diagnostics or {},
            },
        )

    def _send_json(self, code: int, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, data: bytes):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _send_error(self, code: int, message: str):
        self._send_json(code, {"error": message})

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def log_message(self, fmt, *args):
        if args and "/duet/" in str(args[0]):
            print(f"  → {self.client_address[0]} {args[0]}", flush=True)


def main():
    global _db_path_global
    parser = argparse.ArgumentParser(description="Het Duet 2.0 — geregisseerde audio-installatie")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"poort (default: {DEFAULT_PORT})")
    parser.add_argument("--db", default="data/stemmy-replica.db", help="pad naar SQLite database")
    parser.add_argument("--browser", action="store_true", help="open browser automatisch")
    args = parser.parse_args()

    _db_path_global = str(ROOT / args.db) if not Path(args.db).is_absolute() else args.db
    for directory in (CACHE_DIR, TTS_DIR, FRAG_DIR, BRIDGE_DIR, MIX_DIR, PLAN_DIR):
        directory.mkdir(parents=True, exist_ok=True)

    url = f"http://localhost:{args.port}/duet"
    server = ThreadedHTTPServer(("", args.port), DuetHandler)
    print()
    print("  ◈ HET DUET 3.0")
    print("  ─────────────────────────────────────────")
    print(f"  {url}")
    print(f"  DB: {_db_path_global}")
    print(f"  Formats: {len(load_formats(_db_path_global))}")
    print(f"  Health: {_collect_health()}")
    print()
    if args.browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Het Duet gesloten.", flush=True)


if __name__ == "__main__":
    main()
