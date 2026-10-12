"""
Local transcription with WhisperX: word-level alignment + optional diarization.

Designed for Apple Silicon (e.g. M3): use device=cpu and compute_type=int8 per
WhisperX docs — see https://github.com/m-bain/whisperx

Requires optional install: pip install 'stemmy-cli[whisperx]'  (or pip install whisperx)
Environment:
  HF_TOKEN or HUGGINGFACE_HUB_TOKEN — required for pyannote VAD/diarization models.
"""

from __future__ import annotations

import gc
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


def is_whisperx_available() -> bool:
    try:
        import whisperx  # noqa: F401
        return True
    except ImportError:
        return False


def whisperx_import_hint() -> str:
    return (
        "WhisperX is not installed in this environment. "
        "Run: pip install whisperx "
        "or: pip install stemmy-cli[whisperx] "
        "or from this repo: pip install -e '.[whisperx]'. "
        "Keep ffmpeg on PATH; set HF_TOKEN / HF_API_KEY for diarization (pyannote on Hugging Face)."
    )


def _pytorch_load_troubleshooting(original: str) -> str:
    return (
        "PyTorch could not load its native libraries. On macOS this often happens with "
        "Python 3.13 builds or Gatekeeper rejecting wheel binaries.\n\n"
        "Try in order:\n"
        "  1) Use Python 3.12 in a fresh venv (most reliable for torch+whisperx):\n"
        "     python3.12 -m venv .venv312 && source .venv312/bin/activate\n"
        "     pip install -U pip && pip install torch whisperx jiwer && pip install -e .\n"
        "  2) Reinstall PyTorch from PyPI:\n"
        "     pip uninstall -y torch torchvision torchaudio\n"
        "     pip install torch\n"
        "  3) Clear quarantine on the installed torch libs:\n"
        "     xattr -cr \"$VIRTUAL_ENV\"/lib/python3.*/site-packages/torch\n"
        "  4) See https://pytorch.org/get-started/locally/ for the official pip line for your OS.\n\n"
        f"Original error:\n{original}"
    )


def _import_torch():
    try:
        import torch
        return torch
    except (ImportError, OSError) as e:
        msg = str(e)
        if any(
            part in msg
            for part in (
                "libtorch",
                "Library not loaded",
                "disallowed by system policy",
                "not valid for use in process",
            )
        ):
            raise RuntimeError(_pytorch_load_troubleshooting(msg)) from e
        raise


def _hf_token() -> Optional[str]:
    return (
        os.getenv("HF_TOKEN")
        or os.getenv("HUGGINGFACE_HUB_TOKEN")
        or os.getenv("HF_API_KEY")
    )


def download_audio_to_file(
    audio_url: str,
    dest_dir: str,
    timeout: float = 600.0,
) -> Path:
    """Stream download to disk (avoids loading full episodes into RAM)."""
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    suffix = Path(audio_url.split("?")[0]).suffix.lower()
    if suffix not in (".mp3", ".m4a", ".wav", ".ogg", ".webm", ".flac"):
        suffix = ".mp3"
    out = dest / f"audio{suffix}"
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        with client.stream("GET", audio_url) as response:
            response.raise_for_status()
            with open(out, "wb") as f:
                for chunk in response.iter_bytes():
                    f.write(chunk)
    return out


def _avg_word_score(words: List[Dict[str, Any]]) -> float:
    scores = [float(w.get("confidence", 0) or 0) for w in words if w.get("confidence") is not None]
    if not scores:
        return 0.0
    return sum(scores) / len(scores)


def _segments_to_transcript_payload(aligned: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build utterances/words compatible with commands.transcripts._import_transcript_to_db.

    Utterance and word times are in milliseconds so long episodes do not trip the
    seconds/ms heuristic in _import_transcript_to_db (values > 1000 treated as ms).
    """
    utterances: List[Dict[str, Any]] = []
    flat_words: List[Dict[str, Any]] = []

    for seg in aligned.get("segments", []) or []:
        raw_words = seg.get("words") or []
        words_out: List[Dict[str, Any]] = []
        for w in raw_words:
            st = w.get("start")
            if st is None:
                continue
            st_f = float(st)
            en_f = float(w.get("end", st))
            text = (w.get("word") or "").strip()
            conf = float(w.get("score", 0) or 0)
            words_out.append(
                {
                    "text": text,
                    "start": int(round(st_f * 1000)),
                    "end": int(round(en_f * 1000)),
                    "confidence": conf,
                }
            )
            flat_words.append(
                {
                    "text": text,
                    "start": int(round(st_f * 1000)),
                    "end": int(round(en_f * 1000)),
                    "confidence": conf,
                }
            )

        st_sec = float(seg.get("start", 0))
        en_sec = float(seg.get("end", 0))
        spk = seg.get("speaker")
        utterances.append(
            {
                "start": int(round(st_sec * 1000)),
                "end": int(round(en_sec * 1000)),
                "text": (seg.get("text") or "").strip(),
                "speaker": spk if spk is not None else "A",
                "confidence": _avg_word_score(words_out),
                "words": words_out,
            }
        )

    full_text = " ".join(u["text"] for u in utterances if u["text"])
    return {
        "utterances": utterances,
        "words": flat_words,
        "entities": [],
        "text": full_text,
    }


def _status_dict(transcript_data: Dict[str, Any]) -> Dict[str, Any]:
    """Same shape as storage.assemblyai.get_transcription_status when completed."""
    return {
        "status": "completed",
        "transcript_data": transcript_data,
    }


@dataclass
class WhisperXRunnerConfig:
    model: str = "large-v2"
    language: str = "nl"
    device: str = "cpu"
    compute_type: str = "int8"
    batch_size: int = 8
    diarize: bool = True
    min_speakers: Optional[int] = None
    max_speakers: Optional[int] = None
    hf_token: Optional[str] = None
    threads: int = 4


class WhisperXRunner:
    """Load ASR + alignment (and optional diarization) once; run many files."""

    def __init__(self, cfg: WhisperXRunnerConfig):
        torch = _import_torch()
        import whisperx

        if not is_whisperx_available():
            raise RuntimeError(whisperx_import_hint())

        token = cfg.hf_token if cfg.hf_token is not None else _hf_token()

        self._cfg = cfg
        self._torch = torch
        self._whisperx = whisperx

        logger.info(
            "WhisperX loading model=%s device=%s compute_type=%s language=%s",
            cfg.model,
            cfg.device,
            cfg.compute_type,
            cfg.language,
        )
        self._asr = whisperx.load_model(
            cfg.model,
            cfg.device,
            compute_type=cfg.compute_type,
            language=cfg.language,
            threads=cfg.threads,
            use_auth_token=token,
        )
        self._align_model, self._align_metadata = whisperx.load_align_model(
            language_code=cfg.language,
            device=cfg.device,
        )
        self._diarize = None
        if cfg.diarize:
            if not token:
                raise ValueError(
                    "Diarization requires Hugging Face token. "
                    "Set HF_TOKEN and accept the pyannote model terms on huggingface.co."
                )
            from whisperx.diarize import DiarizationPipeline

            self._diarize = DiarizationPipeline(token=token, device=cfg.device)

    def transcribe_file(self, audio_path: str, print_progress: bool = False) -> Dict[str, Any]:
        audio = self._whisperx.load_audio(audio_path)
        result = self._asr.transcribe(
            audio,
            batch_size=self._cfg.batch_size,
            language=self._cfg.language,
            print_progress=print_progress,
        )
        aligned: Dict[str, Any] = self._whisperx.align(
            result["segments"],
            self._align_model,
            self._align_metadata,
            audio,
            self._cfg.device,
            return_char_alignments=False,
            print_progress=print_progress,
        )

        if self._diarize is not None:
            diarize_df = self._diarize(
                audio,
                min_speakers=self._cfg.min_speakers,
                max_speakers=self._cfg.max_speakers,
            )
            aligned = self._whisperx.assign_word_speakers(diarize_df, aligned)

        payload = _segments_to_transcript_payload(aligned)
        return _status_dict(payload)

    def release(self) -> None:
        self._asr = None
        self._align_model = None
        self._align_metadata = None
        self._diarize = None
        gc.collect()
        try:
            self._torch.mps.empty_cache()  # type: ignore[attr-defined]
        except Exception:
            pass


def transcribe_audio_file(
    audio_path: str,
    *,
    config: Optional[WhisperXRunnerConfig] = None,
    print_progress: bool = False,
) -> Dict[str, Any]:
    """
    One-shot: load models, transcribe one file, release. Prefer once WhisperXRunner for batches.
    """
    cfg = config or WhisperXRunnerConfig(hf_token=_hf_token())
    runner = WhisperXRunner(cfg)
    try:
        return runner.transcribe_file(audio_path, print_progress=print_progress)
    finally:
        runner.release()


def transcribe_from_url(
    audio_url: str,
    *,
    work_dir: str,
    config: Optional[WhisperXRunnerConfig] = None,
    print_progress: bool = False,
) -> Dict[str, Any]:
    path = download_audio_to_file(audio_url, work_dir)
    return transcribe_audio_file(str(path), config=config, print_progress=print_progress)
