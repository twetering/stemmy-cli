"""Standalone TTS (Text-to-Speech) module."""

from stemmy_cli.tts.elevenlabs import generate_tts, is_tts_configured

__all__ = ["generate_tts", "is_tts_configured"]
