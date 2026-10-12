from __future__ import annotations

import hashlib
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from duet.state import AudioRegistry, BeatPlan, Candidate, RenderedTurn, SceneState
from duet.providers.tts import TTSResult, generate_tts_cached, generate_s2s


def extract_fragment_cached(candidate: Candidate, cache_dir: Path) -> bytes | None:
    if not candidate.item_audio_url:
        return None
    url_hash = hashlib.md5(candidate.item_audio_url.encode()).hexdigest()[:10]
    start_ms = int(candidate.start_time_seconds * 1000)
    end_ms = int(candidate.end_time_seconds * 1000)
    cache_path = cache_dir / f"{url_hash}_{start_ms}_{end_ms}.mp3"
    if cache_path.exists() and cache_path.stat().st_size > 300:
        return cache_path.read_bytes()
    try:
        from stemmy_cli.audio.parallel_extractor import ParallelExtractor
    except Exception:
        return None
    segment = [
        {
            "id": "duet_fragment",
            "audio_url": candidate.item_audio_url,
            "start_time": candidate.start_time_seconds,
            "end_time": candidate.end_time_seconds,
        }
    ]
    with tempfile.TemporaryDirectory(prefix="duet_frag_") as tmp:
        extractor = ParallelExtractor(max_workers=2, output_dir=Path(tmp))
        successes, _ = extractor.extract_segments(segment)
        if not successes:
            return None
        src = Path(successes[0].output_path)
        fade_out_st = max(0.0, candidate.duration_seconds - 0.22)
        proc = subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(src),
                "-af",
                f"afade=t=in:st=0:d=0.08,afade=t=out:st={fade_out_st:.2f}:d=0.18,loudnorm=I=-16:TP=-1.5:LRA=11",
                "-c:a", "libmp3lame", "-q:a", "2",
                str(cache_path),
            ],
            capture_output=True,
        )
        if proc.returncode == 0 and cache_path.exists():
            return cache_path.read_bytes()
    return None


def extract_fragment_s2s_cached(
    candidate: Candidate,
    target_voice_id: str,
    cache_dir: Path,
) -> bytes | None:
    """
    Extraheer archief-fragment en converteer het naar de klank van target_voice_id
    via ElevenLabs Speech-to-Speech. Cache het resultaat separaat per voice.
    Fallback: retourneert None zodat de aanroeper terug kan vallen op het origineel.
    """
    if not candidate.item_audio_url or not target_voice_id:
        return None
    url_hash = hashlib.md5(candidate.item_audio_url.encode()).hexdigest()[:10]
    start_ms = int(candidate.start_time_seconds * 1000)
    end_ms = int(candidate.end_time_seconds * 1000)
    voice_hash = hashlib.md5(target_voice_id.encode()).hexdigest()[:6]
    cache_path = cache_dir / f"s2s_{url_hash}_{start_ms}_{end_ms}_{voice_hash}.mp3"

    if cache_path.exists() and cache_path.stat().st_size > 300:
        return cache_path.read_bytes()

    # Stap 1: haal het originele fragment op
    frag_bytes = extract_fragment_cached(candidate, cache_dir)
    if not frag_bytes:
        return None

    # Stap 2: S2S conversie
    result = generate_s2s(frag_bytes, target_voice_id)
    if result.audio_bytes:
        cache_path.write_bytes(result.audio_bytes)
        return result.audio_bytes
    return None


def extract_gap_cached(gap: dict, cache_dir: Path) -> bytes | None:
    audio_url = gap.get("audio_url")
    if not audio_url:
        return None
    url_hash = hashlib.md5(audio_url.encode()).hexdigest()[:10]
    start_ms = int(gap["start_ms"])
    end_ms = int(gap["end_ms"])
    cache_path = cache_dir / f"gap_{url_hash}_{start_ms}_{end_ms}.mp3"
    if cache_path.exists() and cache_path.stat().st_size > 200:
        return cache_path.read_bytes()
    try:
        from stemmy_cli.audio.parallel_extractor import ParallelExtractor
    except Exception:
        return None
    segment = [
        {
            "id": "duet_gap",
            "audio_url": audio_url,
            "start_time": start_ms / 1000,
            "end_time": end_ms / 1000,
        }
    ]
    with tempfile.TemporaryDirectory(prefix="duet_gap_") as tmp:
        extractor = ParallelExtractor(max_workers=2, output_dir=Path(tmp))
        successes, _ = extractor.extract_segments(segment)
        if not successes:
            return None
        src = Path(successes[0].output_path)
        proc = subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(src),
                "-af",
                "afade=t=in:st=0:d=0.03,afade=t=out:st=0.75:d=0.12,volume=1.15,loudnorm=I=-18:TP=-2:LRA=9",
                "-c:a", "libmp3lame", "-q:a", "4",
                str(cache_path),
            ],
            capture_output=True,
        )
        if proc.returncode == 0 and cache_path.exists():
            return cache_path.read_bytes()
    return None


def extract_word_sequence_cached(
    candidate: Candidate,
    target_words: list[str],
    cache_dir: Path,
    words_data: list[dict] | None = None,
) -> bytes | None:
    """
    Extraheer 1-5 specifieke woorden uit een fragment (Echo-stijl micro-extract).
    Zoekt de target_words in de word-level timing data en extraheert alleen dat stuk.
    """
    if not candidate.item_audio_url:
        return None
    if not words_data:
        return None

    target_lower = [w.lower().strip(".,!?;:\"'") for w in target_words if w.strip()]
    if not target_lower:
        return None

    # Vind het best-passende woord-bereik in de words_data
    best_start_ms: int | None = None
    best_end_ms: int | None = None
    for i, w in enumerate(words_data):
        word_text = str(w.get("text") or "").lower().strip(".,!?;:\"'")
        if word_text in target_lower:
            ws = int(w.get("start") or 0)
            we = int(w.get("end") or 0)
            if best_start_ms is None:
                best_start_ms = ws
                best_end_ms = we
            else:
                best_end_ms = max(best_end_ms or 0, we)
            # Neem mee tot target volledig matched
            if len(target_lower) > 1:
                end_idx = min(i + len(target_lower), len(words_data))
                for j in range(i + 1, end_idx):
                    best_end_ms = max(best_end_ms or 0, int(words_data[j].get("end") or 0))

    if best_start_ms is None or not best_end_ms:
        return None

    # Kleine padding (0.1s voor/na)
    pad_pre_ms = 100
    pad_post_ms = 120
    audio_start = max(0.0, (best_start_ms - pad_pre_ms) / 1000)
    audio_end = (best_end_ms + pad_post_ms) / 1000

    key = hashlib.md5(
        f"{candidate.item_audio_url}|{best_start_ms}|{best_end_ms}".encode()
    ).hexdigest()[:14]
    cache_path = cache_dir / f"micro_{key}.mp3"
    if cache_path.exists() and cache_path.stat().st_size > 200:
        return cache_path.read_bytes()

    try:
        from stemmy_cli.audio.parallel_extractor import ParallelExtractor
    except Exception:
        return None

    segment = [{"id": "duet_micro", "audio_url": candidate.item_audio_url,
                "start_time": audio_start, "end_time": audio_end}]
    with tempfile.TemporaryDirectory(prefix="duet_micro_") as tmp:
        extractor = ParallelExtractor(max_workers=2, output_dir=Path(tmp))
        successes, _ = extractor.extract_segments(segment)
        if not successes:
            return None
        src = Path(successes[0].output_path)
        dur = audio_end - audio_start
        fade_out_st = max(0.0, dur - 0.08)
        proc = subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(src),
                "-af",
                f"afade=t=in:st=0:d=0.04,afade=t=out:st={fade_out_st:.2f}:d=0.08,loudnorm=I=-14:TP=-1:LRA=9",
                "-c:a", "libmp3lame", "-q:a", "2",
                str(cache_path),
            ],
            capture_output=True,
        )
        if proc.returncode == 0 and cache_path.exists():
            return cache_path.read_bytes()
    return None


def _trim_narrator_audio(audio_bytes: bytes, trim_ratio: float = 0.60) -> bytes:
    """
    Trim narrator-audio tot trim_ratio van de originele duur (voor INTERRUPT beats).
    Gebruikt ffmpeg met een tijdslimiet op basis van geschatte duur.
    """
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as src_f:
        src_f.write(audio_bytes)
        src_path = src_f.name
    out_path = src_path.replace(".mp3", "_trimmed.mp3")
    try:
        # Haal duur op
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", src_path],
            capture_output=True, text=True,
        )
        duration = float(probe.stdout.strip() or "3.0")
        trim_end = max(0.5, duration * trim_ratio)
        proc = subprocess.run(
            [
                "ffmpeg", "-y", "-i", src_path,
                "-t", f"{trim_end:.2f}",
                "-af", f"afade=t=out:st={max(0.0, trim_end-0.15):.2f}:d=0.15",
                "-c:a", "libmp3lame", "-q:a", "2", out_path,
            ],
            capture_output=True,
        )
        if proc.returncode == 0 and Path(out_path).exists():
            result = Path(out_path).read_bytes()
            Path(out_path).unlink(missing_ok=True)
            Path(src_path).unlink(missing_ok=True)
            return result
    except Exception:
        pass
    finally:
        Path(src_path).unlink(missing_ok=True)
        Path(out_path).unlink(missing_ok=True)
    return audio_bytes  # fallback: ongetrimd


def render_turn(
    scene: SceneState,
    narrator_text: str,
    narrator_provider: str,
    voice_id: str | None,
    candidate: Candidate | None,
    gap: dict | None,
    registry: AudioRegistry,
    tts_dir: Path,
    frag_dir: Path,
    bridge_dir: Path,
    plan: BeatPlan | None = None,
    extra_candidates: list[Candidate] | None = None,
) -> tuple[RenderedTurn, dict]:
    beat_type = (plan.beat_type if plan else "normal") or "normal"
    silence_ms = 0
    montage_audio_ids: list[str] = []

    # ── Stilte beat ────────────────────────────────────────────────────────────
    if beat_type == "silence":
        import random as _random
        silence_ms = _random.randint(1800, 3800)
        return (
            RenderedTurn(
                narrator_audio_id=None,
                archive_audio_id=None,
                bridge_audio_id=None,
                narrator_provider="silence",
                degraded_mode="full",
                beat_type="silence",
                silence_ms=silence_ms,
                voice_id=voice_id,
                voice_color=_resolve_voice_color(scene, voice_id),
            ),
            {"beat_type": "silence", "silence_ms": silence_ms},
        )

    # ── TTS narrator ────────────────────────────────────────────────────────────
    tts_result: TTSResult = generate_tts_cached(narrator_text, narrator_provider, tts_dir, voice_id=voice_id)
    narrator_audio_id = None
    degraded_mode = "full"

    if tts_result.audio_bytes:
        audio_to_store = tts_result.audio_bytes
        # INTERRUPT: trim narrator audio
        if beat_type == "interrupt":
            audio_to_store = _trim_narrator_audio(tts_result.audio_bytes, trim_ratio=0.60)
        narrator_audio_id = registry.store(audio_to_store, tts_dir, scene.beat_number)
    else:
        degraded_mode = "browser-fallback"

    # ── Archief fragment(en) ────────────────────────────────────────────────────
    archive_audio_id = None

    if beat_type == "montage" and extra_candidates:
        # Extraheer 2-3 fragmenten snel achter elkaar
        for mc in (extra_candidates[:3]):
            mb = extract_fragment_cached(mc, frag_dir)
            if mb:
                mid = registry.store(mb, frag_dir, scene.beat_number)
                montage_audio_ids.append(mid)

    elif beat_type == "micro" and candidate and plan:
        # Micro-extract: haal words_data op uit de palette of direct
        words_data = _get_words_data(candidate, scene)
        micro_bytes = extract_word_sequence_cached(
            candidate, plan.micro_target_words or [], frag_dir, words_data=words_data
        )
        if micro_bytes:
            archive_audio_id = registry.store(micro_bytes, frag_dir, scene.beat_number)
        elif degraded_mode == "full":
            degraded_mode = "narrator-only-bridge"

    elif candidate:
        # S2S: converteer fragment naar de stem van de actieve voice (indien ingeschakeld)
        s2s_voice = voice_id if (scene.s2s_mode and voice_id and scene.tts_provider == "elevenlabs") else None
        if s2s_voice:
            frag_bytes = extract_fragment_s2s_cached(candidate, s2s_voice, frag_dir)
            if not frag_bytes:
                # S2S mislukt → fallback naar origineel
                frag_bytes = extract_fragment_cached(candidate, frag_dir)
        else:
            frag_bytes = extract_fragment_cached(candidate, frag_dir)
        if frag_bytes:
            archive_audio_id = registry.store(frag_bytes, frag_dir, scene.beat_number)
        elif degraded_mode == "full":
            degraded_mode = "narrator-only-bridge"

    # ── Bridge / gap ────────────────────────────────────────────────────────────
    bridge_audio_id = None
    if gap and scene.preset != "droog" and beat_type not in {"silence", "montage"}:
        gap_bytes = extract_gap_cached(gap, bridge_dir)
        if gap_bytes:
            bridge_audio_id = registry.store(gap_bytes, bridge_dir, scene.beat_number)

    music_active = scene.preset in {"radio", "filmisch"} and scene.stage in {"opening", "wending", "landing"}
    if not narrator_audio_id and archive_audio_id:
        degraded_mode = "archive-only-bridge"
    elif not archive_audio_id and narrator_audio_id and degraded_mode == "full" and beat_type not in {"silence", "montage"}:
        degraded_mode = "narrator-only-bridge"

    # Montage heeft geen narrator nodig
    if beat_type == "montage" and montage_audio_ids:
        degraded_mode = "full"

    diagnostics: dict[str, Any] = {
        "tts_provider": tts_result.provider,
        "tts_voice": tts_result.voice,
        "tts_model": tts_result.model,
        "tts_error": tts_result.error,
        "tts_cache_hit": tts_result.cache_hit,
        "beat_type": beat_type,
        "s2s_active": bool(scene.s2s_mode and voice_id and scene.tts_provider == "elevenlabs"),
    }
    return (
        RenderedTurn(
            narrator_audio_id=narrator_audio_id,
            archive_audio_id=archive_audio_id,
            bridge_audio_id=bridge_audio_id,
            narrator_provider=tts_result.provider,
            degraded_mode=degraded_mode,
            music_active=music_active,
            atmosphere={"bridge_kind": "tussenruimte" if bridge_audio_id else "", "preset": scene.preset},
            beat_type=beat_type,
            silence_ms=silence_ms,
            montage_audio_ids=montage_audio_ids,
            voice_id=voice_id,
            voice_color=_resolve_voice_color(scene, voice_id),
        ),
        diagnostics,
    )


def _resolve_voice_color(scene: SceneState, voice_id: str | None) -> str:
    if not voice_id or not scene.ensemble:
        return "#8fd3ff"
    for v in scene.ensemble.voices:
        if v.elevenlabs_id == voice_id or v.id == voice_id:
            return v.color
    return "#8fd3ff"


def _get_words_data(candidate: Candidate, scene: SceneState) -> list[dict]:
    """Probeer word-level timing data op te halen voor micro-extracts."""
    palette = getattr(scene, "fragment_palette", None)
    if not palette:
        return []
    for role_frags in palette.by_role.values():
        for fr in role_frags:
            if fr.fragment_id == candidate.id and fr.words_data:
                return fr.words_data
    for chain_frags in palette.entity_chains.values():
        for fr in chain_frags:
            if fr.fragment_id == candidate.id and fr.words_data:
                return fr.words_data
    return []
