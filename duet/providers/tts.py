from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import subprocess
import urllib.request
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any


GEMINI_TTS_MODEL = "gemini-2.5-flash-preview-tts"
GEMINI_TTS_VOICE = "Kore"
ELEVENLABS_MODEL = os.environ.get("DUET_ELEVEN_MODEL", "eleven_flash_v2_5")
ELEVENLABS_FALLBACK_MODEL = "eleven_multilingual_v2"
ELEVENLABS_VOICE = "PIpnKISoU0pJEeZu0rOq"


@dataclass
class TTSResult:
    audio_bytes: bytes | None
    provider: str
    voice: str
    model: str
    content_type: str = "audio/mpeg"
    error: str | None = None
    cache_hit: bool = False
    diagnostics: dict[str, Any] | None = None


def _ffmpeg_pcm_to_mp3(pcm_bytes: bytes) -> bytes | None:
    wav_buffer = io.BytesIO()
    with wave.open(wav_buffer, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(pcm_bytes)
    proc = subprocess.run(
        ["ffmpeg", "-y", "-i", "pipe:0", "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-f", "mp3", "pipe:1"],
        input=wav_buffer.getvalue(),
        capture_output=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        return None
    return proc.stdout


def _cache_key(provider: str, text: str, voice: str, model: str) -> str:
    return hashlib.md5(f"{provider}|{voice}|{model}|{text}".encode()).hexdigest()[:20]


def generate_tts_cached(
    text: str,
    provider: str,
    cache_dir: Path,
    voice_id: str | None = None,
) -> TTSResult:
    cache_dir.mkdir(parents=True, exist_ok=True)
    provider = (provider or "elevenlabs").lower()
    if provider == "browser":
        return TTSResult(None, "browser", "browser", "browser", error="browser fallback")
    if provider == "gemini-live":
        provider = "gemini"
    voice = voice_id or (ELEVENLABS_VOICE if provider == "elevenlabs" else GEMINI_TTS_VOICE)
    model = ELEVENLABS_MODEL if provider == "elevenlabs" else GEMINI_TTS_MODEL
    key = _cache_key(provider, text, voice, model)
    cache_path = cache_dir / f"{key}.mp3"
    if cache_path.exists() and cache_path.stat().st_size > 200:
        return TTSResult(
            cache_path.read_bytes(),
            provider,
            voice,
            model,
            cache_hit=True,
        )
    result = _generate_elevenlabs(text, voice, model) if provider == "elevenlabs" else _generate_gemini_tts(text, voice, model)
    if result.audio_bytes:
        cache_path.write_bytes(result.audio_bytes)
    return result


def _generate_elevenlabs(text: str, voice: str, model: str) -> TTSResult:
    try:
        from stemmy_cli.tts.elevenlabs import generate_tts
    except Exception as exc:
        return TTSResult(None, "elevenlabs", voice, model, error=f"import error: {exc}")
    try:
        audio = generate_tts(text=text, voice_id=voice, model_id=model)
        if not isinstance(audio, (bytes, bytearray)):
            audio = b"".join(audio)
        return TTSResult(bytes(audio), "elevenlabs", voice, model)
    except Exception as exc:
        if model != ELEVENLABS_FALLBACK_MODEL:
            try:
                audio = generate_tts(text=text, voice_id=voice, model_id=ELEVENLABS_FALLBACK_MODEL)
                if not isinstance(audio, (bytes, bytearray)):
                    audio = b"".join(audio)
                return TTSResult(
                    bytes(audio),
                    "elevenlabs",
                    voice,
                    ELEVENLABS_FALLBACK_MODEL,
                    diagnostics={"fallback_from": model},
                )
            except Exception as fallback_exc:
                return TTSResult(None, "elevenlabs", voice, model, error=f"{exc} | fallback: {fallback_exc}")
        return TTSResult(None, "elevenlabs", voice, model, error=str(exc))


def generate_s2s(
    audio_bytes: bytes,
    target_voice_id: str,
    model: str = "eleven_multilingual_sts_v2",
) -> TTSResult:
    """
    Speech-to-Speech: converteer bestaand audio naar de klank van target_voice_id
    via ElevenLabs /v1/speech-to-speech.
    Retourneert de MP3-bytes van het geconverteerde audio.
    """
    api_key = os.environ.get("ELEVENLABS_API_KEY", "")
    if not api_key:
        return TTSResult(None, "s2s", target_voice_id, model, error="missing ELEVENLABS_API_KEY")
    if not audio_bytes:
        return TTSResult(None, "s2s", target_voice_id, model, error="geen audio input")

    boundary = "DuetS2SBoundary7x3k9pQ"

    def _field(name: str, value: str) -> bytes:
        return (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        ).encode()

    body = b""
    body += _field("model_id", model)
    body += _field("voice_settings", '{"stability":0.45,"similarity_boost":0.82}')
    body += (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="audio"; filename="fragment.mp3"\r\n'
        f"Content-Type: audio/mpeg\r\n\r\n"
    ).encode()
    body += audio_bytes
    body += f"\r\n--{boundary}--\r\n".encode()

    url = f"https://api.elevenlabs.io/v1/speech-to-speech/{target_voice_id}"
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "xi-api-key": api_key,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "audio/mpeg",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result_bytes = resp.read()
        if not result_bytes or len(result_bytes) < 100:
            return TTSResult(None, "s2s", target_voice_id, model, error="leeg S2S antwoord")
        return TTSResult(result_bytes, "s2s", target_voice_id, model)
    except Exception as exc:
        return TTSResult(None, "s2s", target_voice_id, model, error=str(exc))


def _generate_gemini_tts(text: str, voice: str, model: str) -> TTSResult:
    api_key = os.environ.get("GOOGLE_API_KEY", "")
    if not api_key:
        return TTSResult(None, "gemini-tts", voice, model, error="missing GOOGLE_API_KEY")
    payload = json.dumps(
        {
            "contents": [{"parts": [{"text": text}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}
                },
            },
            "model": model,
        }
    ).encode()
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read())
        inline_data = data["candidates"][0]["content"]["parts"][0]["inlineData"]["data"]
        pcm_bytes = base64.b64decode(inline_data)
        mp3_bytes = _ffmpeg_pcm_to_mp3(pcm_bytes)
        if not mp3_bytes:
            return TTSResult(None, "gemini-tts", voice, model, error="ffmpeg pcm->mp3 conversion failed")
        return TTSResult(mp3_bytes, "gemini-tts", voice, model)
    except Exception as exc:
        return TTSResult(None, "gemini-tts", voice, model, error=str(exc))
