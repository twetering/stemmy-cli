"""
Unified transcription: direct AssemblyAI or surrounded API.

Uses ASSEMBLY_API_KEY when set for standalone; otherwise falls back to STEMMY_API_URL.
"""

import time
from typing import Any, Dict, Optional

from stemmy_cli.adapters.http import HTTPAdapter
from stemmy_cli.output import print_error, print_info, print_warning


def _poll_transcript_via_api(
    http: HTTPAdapter,
    transcript_id: str,
    item_title: str,
    max_wait: int = 3600,
) -> Optional[Dict[str, Any]]:
    """Poll transcript status via surrounded API."""
    waited = 0
    last_log = 0
    poll_interval = 5
    while waited < max_wait:
        try:
            status = http.get(f"/api/formats/transcription-status/{transcript_id}")
        except Exception as e:
            print_warning(f"Status check failed: {e}")
            time.sleep(poll_interval)
            waited += poll_interval
            continue

        status_val = status.get("status", "").lower()
        if status_val == "completed":
            return status
        if status_val in ("error", "failed"):
            print_error(f"Transcription failed: {status.get('error', status.get('message', 'Unknown'))}")
            return None

        interval = 5 if waited < 120 else 15
        time.sleep(interval)
        waited += interval

        if waited - last_log >= 60:
            mins = waited // 60
            print_info(f"AssemblyAI still processing '{item_title[:40]}...' ({mins} min elapsed)")
            last_log = waited

    print_error(f"Transcription timed out after {max_wait}s")
    return None


def transcribe_item(
    audio_url: str,
    item_id: str,
    item_title: str,
    options: Dict[str, Any],
    wait: bool,
    http: Optional[HTTPAdapter] = None,
) -> tuple[Optional[str], Optional[Dict[str, Any]]]:
    """
    Start transcription and optionally wait. Returns (transcript_id, status).
    Uses direct AssemblyAI when ASSEMBLY_API_KEY is set, else surrounded API.
    """
    from stemmy_cli.storage.assemblyai import (
        is_assemblyai_configured,
        start_transcription,
        poll_until_complete,
    )

    opts = {
        "language_code": options.get("language_code", "nl"),
        "speaker_labels": options.get("speaker_labels", True),
        "entity_detection": options.get("entity_detection", True),
    }

    if is_assemblyai_configured():
        transcript_id = start_transcription(audio_url, opts)
        if not wait:
            return transcript_id, None

        def on_progress(waited_secs: int) -> None:
            mins = waited_secs // 60
            print_info(f"AssemblyAI still processing '{item_title[:40]}...' ({mins} min elapsed)")

        status = poll_until_complete(
            transcript_id,
            max_wait=3600,
            poll_interval=5,
            on_progress=on_progress,
        )
        return transcript_id, status

    # Fallback to surrounded API
    if not http:
        http = HTTPAdapter(timeout=60.0)
    payload = {"audio_url": audio_url, "item_id": item_id, "options": opts}
    result = http.post("/api/formats/transcribe", json=payload)
    transcript_id = result.get("transcript_id") or result.get("id")

    if not wait or not transcript_id:
        return transcript_id, None

    status = _poll_transcript_via_api(http, transcript_id, item_title, max_wait=3600)
    return transcript_id, status
