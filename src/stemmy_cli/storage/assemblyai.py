"""
Direct AssemblyAI transcription for standalone operation.

Uses ASSEMBLY_API_KEY. Compatible with surrounded format_service options.
"""

import logging
import time
from typing import Any, Callable, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://api.assemblyai.com/v2"


def _get_api_key() -> Optional[str]:
    import os
    return os.getenv("ASSEMBLY_API_KEY") or os.getenv("ASSEMBLYAI_API_KEY")


def is_assemblyai_configured() -> bool:
    """Check if AssemblyAI API key is set."""
    return bool(_get_api_key())


def _headers() -> Dict[str, str]:
    key = _get_api_key()
    if not key:
        raise ValueError("ASSEMBLY_API_KEY not set")
    return {
        "authorization": key,
        "content-type": "application/json",
    }


def start_transcription(audio_url: str, options: Optional[Dict[str, Any]] = None) -> str:
    """
    Start transcription with AssemblyAI. Returns transcript_id.

    Args:
        audio_url: Public URL of audio (S3, etc.)
        options: language_code, speaker_labels, entity_detection, etc.

    Returns:
        transcript_id
    """
    opts = options or {}
    payload = {
        "audio_url": audio_url,
        "speaker_labels": opts.get("speaker_labels", True),
        "sentiment_analysis": opts.get("sentiment_analysis", False),
    }
    if opts.get("language_code") and opts["language_code"] != "auto":
        payload["language_code"] = opts["language_code"]
    else:
        payload["language_detection"] = opts.get("language_detection", True)
    if opts.get("entity_detection") is not None:
        payload["entity_detection"] = opts["entity_detection"]

    with httpx.Client(timeout=30.0) as client:
        resp = client.post(
            f"{BASE_URL}/transcript",
            headers=_headers(),
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
    return data["id"]


def get_transcription_status(transcript_id: str) -> Dict[str, Any]:
    """
    Get transcript status. When completed, includes transcript_data with
    utterances and entities (compatible with _import_transcript_to_db).
    """
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(
            f"{BASE_URL}/transcript/{transcript_id}",
            headers=_headers(),
        )
        resp.raise_for_status()
        data = resp.json()

    status_val = data.get("status", "").lower()
    result = {
        "status": status_val,
        "percent_complete": data.get("percent_complete"),
        "error": data.get("error"),
    }
    if status_val == "completed":
        result["transcript_data"] = {
            "text": data.get("text"),
            "words": data.get("words", []),
            "utterances": data.get("utterances", []),
            "entities": data.get("entities", []),
            "chapters": data.get("chapters", []),
        }
    return result


def poll_until_complete(
    transcript_id: str,
    max_wait: int = 3600,
    poll_interval: int = 5,
    on_progress: Optional[Callable[[int], None]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Poll until transcription completes or errors. Returns status dict with
    transcript_data when completed, None on error/timeout.
    """
    waited = 0
    last_log = 0
    while waited < max_wait:
        status = get_transcription_status(transcript_id)
        status_val = status.get("status", "").lower()

        if status_val == "completed":
            return status
        if status_val in ("error", "failed"):
            logger.error("Transcription failed: %s", status.get("error", "Unknown"))
            return None

        interval = poll_interval if waited < 120 else 15
        time.sleep(interval)
        waited += interval

        if on_progress and waited - last_log >= 60:
            on_progress(waited)
            last_log = waited

    logger.error("Transcription timed out after %ds", max_wait)
    return None
