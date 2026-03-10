"""Local audio extraction helpers (standalone, no API)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Callable

from stemmy_cli.audio.parallel_extractor import ParallelExtractor, ExtractionResult


def _get_max_workers(default: int = 4) -> int:
    """Get max workers from env or default."""
    env = os.getenv("STEMMY_EXTRACT_WORKERS")
    if not env:
        return default
    try:
        value = int(env)
        return value if value > 0 else default
    except ValueError:
        return default


def _normalize_segment(seg: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Normalize a segment dict into extractor-friendly format."""
    audio_url = seg.get("audio_url") or seg.get("item_audio_url") or seg.get("source_audio_url")
    if not audio_url:
        return None, "Missing audio_url"

    start = seg.get("start_time", seg.get("start", seg.get("start_ms")))
    end = seg.get("end_time", seg.get("end", seg.get("end_ms")))

    if start is None or end is None:
        return None, "Missing start/end time"

    try:
        start_f = float(start)
        end_f = float(end)
    except (TypeError, ValueError):
        return None, "Invalid start/end time"

    # Convert ms to seconds if values look like ms
    if start_f > 10000 or end_f > 10000:
        start_f /= 1000.0
        end_f /= 1000.0

    normalized = {
        "id": str(seg.get("id", f"seg_{id(seg)}")),
        "audio_url": audio_url,
        "start_time": start_f,
        "end_time": end_f,
        "original": seg,
    }
    return normalized, None


def extract_segments_local(
    segments: Iterable[Dict[str, Any]],
    output_dir: Optional[Path] = None,
    max_workers: Optional[int] = None,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Extract segments locally using ParallelExtractor.

    Returns:
        (successes, failures)
        successes: list of dicts with keys: segment, fragment_audio_url, duration, output_path
        failures: list of dicts with keys: segment, error
    """
    normalized_segments: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []

    for seg in segments:
        normalized, error = _normalize_segment(seg)
        if not normalized:
            failures.append({"segment": seg, "error": error or "Invalid segment"})
            continue
        normalized_segments.append(normalized)

    if not normalized_segments:
        return [], failures

    extractor = ParallelExtractor(
        max_workers=max_workers or _get_max_workers(),
        output_dir=output_dir,
    )
    successes_raw, failures_raw = extractor.extract_segments(
        normalized_segments, progress_callback=progress_callback
    )

    # Map id -> original segment
    id_to_segment = {s["id"]: s.get("original", s) for s in normalized_segments}

    successes: List[Dict[str, Any]] = []
    for r in successes_raw:
        seg = id_to_segment.get(r.segment_id)
        if not seg:
            continue
        successes.append(
            {
                "segment": seg,
                "fragment_audio_url": r.output_path,
                "audio_url": r.output_path,
                "output_path": r.output_path,
                "duration": r.duration,
            }
        )

    for r in failures_raw:
        seg = id_to_segment.get(r.segment_id)
        failures.append(
            {
                "segment": seg or {"id": r.segment_id},
                "error": r.error or "Unknown error",
            }
        )

    return successes, failures


def extract_segment_local(
    audio_url: str,
    start_time: float,
    end_time: float,
    output_path: Optional[Path] = None,
    max_workers: Optional[int] = None,
) -> Dict[str, Any]:
    """Convenience wrapper for a single segment extraction."""
    segments = [
        {
            "id": "single",
            "audio_url": audio_url,
            "start_time": start_time,
            "end_time": end_time,
        }
    ]
    output_dir = output_path.parent if output_path else None
    successes, failures = extract_segments_local(
        segments,
        output_dir=output_dir,
        max_workers=max_workers,
    )
    if successes:
        result = successes[0]
        if output_path and Path(result["output_path"]) != output_path:
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(result["output_path"]).replace(output_path)
            result["output_path"] = str(output_path)
            result["fragment_audio_url"] = str(output_path)
            result["audio_url"] = str(output_path)
        return {"success": True, **result}
    error = failures[0]["error"] if failures else "Extraction failed"
    return {"success": False, "error": error}
