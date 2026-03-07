"""
Audio normalization for RSS import.

Mirrors surrounded/services/format_service.py process_audio_for_import:
- 1ms trim from end (force re-encoding, fix timing for extraction accuracy)
- 192k CBR bitrate (uniform across all items for consistent extraction)
- ID3v2.3, write_xing 0 (consistent headers)
"""

import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Match legacy format_service.py exactly for bitrate uniformity
NORMALIZE_BITRATE = "192k"
NORMALIZE_PARAMS = [
    "-write_xing", "0",
    "-write_id3v1", "1",
    "-id3v2_version", "3",
]


def download_audio(audio_url: str, temp_dir: str) -> str:
    """Download RSS/HTTP audio to temp file. Mirrors _download_rss_audio."""
    response = httpx.get(audio_url, follow_redirects=True, timeout=60.0)
    response.raise_for_status()

    temp_file = os.path.join(temp_dir, "audio.mp3")
    with open(temp_file, "wb") as f:
        f.write(response.content)
    return temp_file


def normalize_audio_file(input_path: str, output_path: Optional[str] = None) -> str:
    """
    Normalize audio: 1ms trim, 192k bitrate. Matches legacy format_service.

    Args:
        input_path: Path to input MP3
        output_path: Optional output path (default: same dir, normalized.mp3)

    Returns:
        Path to normalized file
    """
    from pydub import AudioSegment

    audio = AudioSegment.from_file(input_path)
    # Trim 1ms from end to force re-encoding and fix timing (legacy line 691)
    audio = audio[:-1]

    out = output_path or str(Path(input_path).parent / "normalized.mp3")
    audio.export(
        out,
        format="mp3",
        bitrate=NORMALIZE_BITRATE,
        parameters=NORMALIZE_PARAMS,
    )
    return out


def process_audio_for_import(
    source_url: str,
    item_id: str,
    subfolder: str = "items",
    upload_to_api: Optional[str] = None,
) -> str:
    """
    Download, normalize, and upload audio. Standalone equivalent of
    format_service.process_audio_for_import.

    Args:
        source_url: Original mp3 URL (RSS enclosure)
        item_id: Item UUID for naming
        subfolder: S3 subfolder (e.g. formats/{format_id})
        upload_to_api: Base URL for /api/upload-audio (fallback when S3 not configured)

    Returns:
        URL of normalized audio (S3 or API upload)
    """
    from stemmy_cli.storage.s3 import upload_audio_to_s3, is_s3_configured

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_file = download_audio(source_url, temp_dir)
        normalized_path = os.path.join(temp_dir, "normalized.mp3")
        normalize_audio_file(temp_file, normalized_path)

        if is_s3_configured():
            s3_key = f"audio/{subfolder}/{item_id}.mp3"
            return upload_audio_to_s3(normalized_path, s3_key)

        if upload_to_api:
            base = upload_to_api.rstrip("/")
            api_url = f"{base}/api/upload-audio"
            with open(normalized_path, "rb") as f:
                files = {"file": (f"{item_id}.mp3", f, "audio/mpeg")}
                resp = httpx.post(api_url, files=files, timeout=120.0)
                resp.raise_for_status()
            data = resp.json()
            result_url = (
                data.get("file_url")
                or data.get("url")
                or data.get("s3_url")
                or ""
            )
            if not result_url:
                raise RuntimeError("Upload succeeded but no URL returned")
            return result_url

        raise ValueError(
            "Upload required: AssemblyAI needs a public URL. "
            "Set AWS_* and S3_BUCKET_NAME for direct S3, or STEMMY_API_URL for surrounded."
        )
