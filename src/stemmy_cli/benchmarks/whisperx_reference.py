"""
Benchmark WhisperX against existing fragment transcripts + word data.

- Samples fragments with non-empty words + item_audio_url + text
- Extracts each clip with ffmpeg (seconds from DB; avoids ms/heuristic bugs on long episodes)
- Runs WhisperX with diarization; compares WER/CER and RTF to reference text
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from stemmy_cli.adapters.sqlite import SQLiteAdapter


@dataclass
class FragmentBenchmarkRow:
    fragment_id: str
    item_id: str
    item_title: str
    reference_text: str
    speaker_label: str
    words_json: str
    item_audio_url: str
    start_sec: float
    end_sec: float


@dataclass
class ClipEvalResult:
    fragment_id: str
    item_id: str
    reference_wer: float
    reference_cer: float
    audio_duration_sec: float
    transcribe_sec: float
    rtf: float  # realtime factor = transcribe_sec / audio_duration
    ref_chars: int
    hyp_chars: int
    num_hyp_segments: int
    num_hyp_speakers: int
    reference_speaker: str
    hypothesis_text: str  # truncated in summary optional


def _parse_json_words(words_col: Optional[str]) -> List[Dict[str, Any]]:
    if not words_col or words_col == "[]":
        return []
    try:
        w = json.loads(words_col)
        return w if isinstance(w, list) else []
    except json.JSONDecodeError:
        return []


def fetch_fragment_benchmark_set(
    adapter: SQLiteAdapter,
    *,
    limit: int = 100,
    format_id: Optional[str] = None,
    min_duration_sec: float = 0.8,
    max_duration_sec: float = 90.0,
) -> List[FragmentBenchmarkRow]:
    """
    Random sample of fragments suitable for eval (has words, url, text, sane duration).
    """
    conditions = [
        "f.words IS NOT NULL",
        "trim(f.words) != ''",
        "f.words != '[]'",
        "f.text IS NOT NULL",
        "trim(f.text) != ''",
        "f.item_audio_url IS NOT NULL",
        "trim(f.item_audio_url) != ''",
        """(
            CAST(f.end_time_seconds AS REAL) - CAST(f.start_time_seconds AS REAL)
        ) BETWEEN ? AND ?""",
    ]
    params: List[Any] = [min_duration_sec, max_duration_sec]
    if format_id:
        conditions.append("f.item_id IN (SELECT id FROM items WHERE format_id = ?)")
        params.append(format_id)

    where_sql = " AND ".join(conditions)
    params.append(limit)

    rows = adapter.execute_raw(
        f"""
        SELECT f.id, f.item_id, f.text, f.words, f.item_audio_url,
               f.start_time_seconds, f.end_time_seconds, f.speaker_label,
               i.title AS item_title
        FROM fragments f
        LEFT JOIN items i ON i.id = f.item_id
        WHERE {where_sql}
        ORDER BY RANDOM()
        LIMIT ?
        """,
        params,
    )

    out: List[FragmentBenchmarkRow] = []
    for r in rows:
        try:
            start = float(r["start_time_seconds"] or 0)
            end = float(r["end_time_seconds"] or 0)
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        words = _parse_json_words(r.get("words"))
        if len(words) < 2:
            continue
        out.append(
            FragmentBenchmarkRow(
                fragment_id=str(r["id"]),
                item_id=str(r["item_id"]),
                item_title=str(r.get("item_title") or ""),
                reference_text=str(r.get("text") or "").strip(),
                speaker_label=str(r.get("speaker_label") or "A"),
                words_json=str(r.get("words") or "[]"),
                item_audio_url=str(r["item_audio_url"]),
                start_sec=start,
                end_sec=end,
            )
        )
    return out


def extract_clip_wav_ffmpeg(
    audio_url: str,
    start_sec: float,
    end_sec: float,
    out_path: Path,
) -> None:
    """Extract [start_sec, end_sec] from URL or path into 16 kHz mono WAV for Whisper."""
    dur = max(0.05, end_sec - start_sec)
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-loglevel",
        "error",
        "-i",
        audio_url,
        "-ss",
        str(max(0.0, start_sec)),
        "-t",
        str(dur),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(out_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout or "ffmpeg failed")


def _whisperx_hypothesis_text(status: Dict[str, Any]) -> Tuple[str, int, int]:
    """Full hypothesis text, segment count, unique speaker count."""
    data = status.get("transcript_data") or status
    utterances = data.get("utterances") or []
    if not utterances:
        segs = data.get("segments") or []
        parts = [s.get("text", "").strip() for s in segs]
        return " ".join(p for p in parts if p), len(segs), 0
    parts = []
    speakers: set = set()
    for u in utterances:
        t = (u.get("text") or "").strip()
        if t:
            parts.append(t)
        sp = u.get("speaker")
        if sp is not None and str(sp).strip():
            speakers.add(str(sp).strip())
    return " ".join(parts), len(utterances), len(speakers)


def _jiwer_transforms():
    try:
        import jiwer
    except ImportError as e:
        raise ImportError("Install jiwer: pip install jiwer") from e
    return jiwer.Compose(
        [
            jiwer.ToLowerCase(),
            jiwer.SubstituteRegexes({r"\s+": " "}),
            jiwer.Strip(),
            jiwer.RemovePunctuation(),
        ]
    )


def compute_wer_cer(reference: str, hypothesis: str) -> Tuple[float, float]:
    import jiwer

    transform = _jiwer_transforms()
    ref_t = transform(reference)
    hyp_t = transform(hypothesis)
    if not ref_t:
        return (1.0 if hyp_t else 0.0, 1.0 if hyp_t else 0.0)
    wer_v = jiwer.wer(ref_t, hyp_t)
    cer_v = jiwer.cer(ref_t, hyp_t)
    return float(wer_v), float(cer_v)


def eval_clips_with_runner(
    rows: Sequence[FragmentBenchmarkRow],
    runner: Any,
    transcribe_file_fn: Any,
) -> List[ClipEvalResult]:
    """run transcribe_file_fn(runner, wav_path, progress) -> status dict."""
    results: List[ClipEvalResult] = []
    for row in rows:
        with tempfile.TemporaryDirectory(prefix="stemmy_wxbench_") as tmp:
            wav = Path(tmp) / "clip.wav"
            try:
                extract_clip_wav_ffmpeg(row.item_audio_url, row.start_sec, row.end_sec, wav)
            except Exception as e:
                results.append(
                    ClipEvalResult(
                        fragment_id=row.fragment_id,
                        item_id=row.item_id,
                        reference_wer=1.0,
                        reference_cer=1.0,
                        audio_duration_sec=row.end_sec - row.start_sec,
                        transcribe_sec=0.0,
                        rtf=999.0,
                        ref_chars=len(row.reference_text),
                        hyp_chars=0,
                        num_hyp_segments=0,
                        num_hyp_speakers=0,
                        reference_speaker=row.speaker_label,
                        hypothesis_text=f"[extract_error] {e}",
                    )
                )
                continue

            audio_dur = row.end_sec - row.start_sec
            t0 = time.perf_counter()
            try:
                status = transcribe_file_fn(runner, str(wav), False)
            except Exception as e:
                dt = time.perf_counter() - t0
                results.append(
                    ClipEvalResult(
                        fragment_id=row.fragment_id,
                        item_id=row.item_id,
                        reference_wer=1.0,
                        reference_cer=1.0,
                        audio_duration_sec=audio_dur,
                        transcribe_sec=dt,
                        rtf=dt / audio_dur if audio_dur > 0 else 999.0,
                        ref_chars=len(row.reference_text),
                        hyp_chars=0,
                        num_hyp_segments=0,
                        num_hyp_speakers=0,
                        reference_speaker=row.speaker_label,
                        hypothesis_text=f"[transcribe_error] {e}",
                    )
                )
                continue
            dt = time.perf_counter() - t0

            hyp_text, n_seg, n_spk = _whisperx_hypothesis_text(status)
            try:
                wer_v, cer_v = compute_wer_cer(row.reference_text, hyp_text)
            except Exception:
                wer_v, cer_v = 1.0, 1.0

            results.append(
                ClipEvalResult(
                    fragment_id=row.fragment_id,
                    item_id=row.item_id,
                    reference_wer=wer_v,
                    reference_cer=cer_v,
                    audio_duration_sec=audio_dur,
                    transcribe_sec=dt,
                    rtf=dt / audio_dur if audio_dur > 0 else 0.0,
                    ref_chars=len(row.reference_text),
                    hyp_chars=len(hyp_text),
                    num_hyp_segments=n_seg,
                    num_hyp_speakers=n_spk,
                    reference_speaker=row.speaker_label,
                    hypothesis_text=hyp_text[:500],
                )
            )
    return results


def summarize_results(results: Sequence[ClipEvalResult]) -> Dict[str, Any]:
    ok = [r for r in results if not r.hypothesis_text.startswith("[")]
    if not ok:
        return {
            "clips": len(results),
            "valid": 0,
            "mean_wer": None,
            "mean_cer": None,
            "mean_rtf": None,
            "total_audio_sec": 0.0,
            "total_transcribe_sec": 0.0,
        }
    return {
        "clips": len(results),
        "valid": len(ok),
        "mean_wer": sum(r.reference_wer for r in ok) / len(ok),
        "mean_cer": sum(r.reference_cer for r in ok) / len(ok),
        "mean_rtf": sum(r.rtf for r in ok) / len(ok),
        "total_audio_sec": sum(r.audio_duration_sec for r in ok),
        "total_transcribe_sec": sum(r.transcribe_sec for r in ok),
        "wall_rtf": sum(r.transcribe_sec for r in ok) / sum(r.audio_duration_sec for r in ok)
        if sum(r.audio_duration_sec for r in ok) > 0
        else 0.0,
    }
