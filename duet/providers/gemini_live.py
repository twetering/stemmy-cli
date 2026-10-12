from __future__ import annotations

import asyncio
import io
import os
import subprocess
import wave
from dataclasses import dataclass


LIVE_MODEL = "gemini-3.1-flash-live-preview"
LIVE_VOICE = "Kore"


@dataclass
class GeminiLiveResult:
    audio_bytes: bytes | None
    input_transcript: str
    output_transcript: str
    model: str
    voice: str
    error: str | None = None
    diagnostics: dict | None = None


def _ffmpeg_to_pcm16(audio_bytes: bytes, mime_type: str) -> bytes | None:
    fmt = "s16le"
    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            "pipe:0",
            "-f",
            fmt,
            "-ac",
            "1",
            "-ar",
            "16000",
            "pipe:1",
        ],
        input=audio_bytes,
        capture_output=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        return None
    return proc.stdout


def _pcm24k_to_mp3(pcm_bytes: bytes) -> bytes | None:
    wav_buffer = io.BytesIO()
    with wave.open(wav_buffer, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(pcm_bytes)
    proc = subprocess.run(
        ["ffmpeg", "-y", "-i", "pipe:0", "-af", "loudnorm=I=-16:TP=-1.5:LRA=10", "-f", "mp3", "pipe:1"],
        input=wav_buffer.getvalue(),
        capture_output=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        return None
    return proc.stdout


async def _run_live(audio_bytes: bytes, mime_type: str, prompt: str, voice: str, model: str) -> GeminiLiveResult:
    try:
        from google import genai
        from google.genai import types
    except Exception as exc:
        return GeminiLiveResult(None, "", "", model, voice, error=f"sdk import error: {exc}")
    api_key = os.environ.get("GOOGLE_API_KEY", "")
    if not api_key:
        return GeminiLiveResult(None, "", "", model, voice, error="missing GOOGLE_API_KEY")
    pcm_in = _ffmpeg_to_pcm16(audio_bytes, mime_type)
    if not pcm_in:
        return GeminiLiveResult(None, "", "", model, voice, error="ffmpeg input conversion failed")
    client = genai.Client(api_key=api_key)
    output_chunks: list[bytes] = []
    input_transcript = ""
    output_transcript = ""
    response_text = ""
    config = {
        "response_modalities": ["AUDIO"],
        "speech_config": {
            "voice_config": {
                "prebuilt_voice_config": {
                    "voice_name": voice,
                }
            }
        },
        "input_audio_transcription": {},
        "output_audio_transcription": {},
        "context_window_compression": {"trigger_tokens": 24000, "sliding_window": {}},
        "system_instruction": prompt,
    }
    try:
        async with client.aio.live.connect(model=model, config=config) as session:
            await session.send_realtime_input(
                audio=types.Blob(data=pcm_in, mime_type="audio/pcm;rate=16000")
            )
            await session.send(input="Reageer nu passend op de emotie, toon en inhoud van de audio.")
            async for response in session.receive():
                server_content = getattr(response, "server_content", None)
                if not server_content:
                    continue
                if getattr(server_content, "input_transcription", None):
                    input_transcript = getattr(server_content.input_transcription, "text", "") or input_transcript
                if getattr(server_content, "output_transcription", None):
                    output_transcript = getattr(server_content.output_transcription, "text", "") or output_transcript
                model_turn = getattr(server_content, "model_turn", None)
                parts = getattr(model_turn, "parts", None) or []
                for part in parts:
                    text = getattr(part, "text", None)
                    if text:
                        response_text += text
                    inline = getattr(part, "inline_data", None) or getattr(part, "inlineData", None)
                    data = getattr(inline, "data", None) if inline else None
                    if data:
                        output_chunks.append(bytes(data))
                if getattr(server_content, "turn_complete", False):
                    break
    except Exception as exc:
        return GeminiLiveResult(
            None,
            input_transcript,
            output_transcript,
            model,
            voice,
            error=str(exc),
            diagnostics={"response_text": response_text[:400]},
        )
    merged_audio = b"".join(output_chunks) if output_chunks else b""
    mp3_audio = _pcm24k_to_mp3(merged_audio) if merged_audio else None
    return GeminiLiveResult(
        mp3_audio,
        input_transcript,
        output_transcript or response_text.strip(),
        model,
        voice,
        diagnostics={"response_text": response_text[:800], "pcm_chunks": len(output_chunks)},
    )


def generate_live_audio_reply(audio_bytes: bytes, mime_type: str, prompt: str, voice: str | None = None, model: str | None = None) -> GeminiLiveResult:
    return asyncio.run(
        _run_live(
            audio_bytes=audio_bytes,
            mime_type=mime_type,
            prompt=prompt,
            voice=voice or os.environ.get("DUET_GEMINI_LIVE_VOICE", LIVE_VOICE),
            model=model or os.environ.get("DUET_GEMINI_LIVE_MODEL", LIVE_MODEL),
        )
    )
