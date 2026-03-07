"""Standalone ElevenLabs TTS - no surrounded dependency."""

import os
from pathlib import Path
from typing import Optional, Union


def is_tts_configured() -> bool:
    """Check if ElevenLabs TTS is available (ELEVENLABS_API_KEY set)."""
    return bool(os.environ.get("ELEVENLABS_API_KEY"))


def generate_tts(
    text: str,
    voice_id: str,
    output_path: Optional[Union[str, Path]] = None,
    model_id: str = "eleven_multilingual_v2",
    output_format: str = "mp3_44100_128",
) -> bytes:
    """
    Generate speech from text using ElevenLabs API.

    Args:
        text: Text to convert to speech
        voice_id: ElevenLabs voice ID (e.g. JBFqnCBsd6RMkjVDRZzb)
        output_path: Optional path to save audio file
        model_id: Model to use (default: eleven_multilingual_v2 for Dutch)
        output_format: Audio format (default: mp3_44100_128)

    Returns:
        Audio bytes (MP3)

    Raises:
        ValueError: If ELEVENLABS_API_KEY is not set
    """
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        raise ValueError(
            "ELEVENLABS_API_KEY not set. Add to .env for standalone TTS."
        )

    from elevenlabs.client import ElevenLabs

    client = ElevenLabs(api_key=api_key)
    audio: bytes = client.text_to_speech.convert(
        voice_id=voice_id,
        text=text,
        model_id=model_id,
        output_format=output_format,
    )

    if output_path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "wb") as f:
            f.write(audio)

    return audio
