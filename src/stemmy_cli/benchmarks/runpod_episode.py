"""Full-episode reference text + RunPod / S3 transcript eval (WER/CER vs Assembly ground truth)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from stemmy_cli.adapters.sqlite import SQLiteAdapter
from stemmy_cli.benchmarks.whisperx_reference import (
    _whisperx_hypothesis_text,
    compute_wer_cer,
)
from stemmy_cli.storage import runpod_api
@dataclass
class EpisodeBenchmarkPick:
    item_id: str
    audio_url: str
    title: str
    duration_seconds: Optional[float]


def reference_text_from_fragments(adapter: SQLiteAdapter, item_id: str) -> str:
    """Concat fragment text in order (episodes imported as utterances)."""
    rows = adapter.execute_raw(
        """
        SELECT text FROM fragments
        WHERE item_id = ?
        ORDER BY CAST(COALESCE(order_index, '0') AS INTEGER), start_time
        """,
        [item_id],
    )
    parts = [str(r.get("text") or "").strip() for r in rows]
    return " ".join(p for p in parts if p)


def reference_text_from_item_column(adapter: SQLiteAdapter, item_id: str) -> Optional[str]:
    row = adapter.get_by_id("items", item_id)
    if not row:
        return None
    raw = row.get("transcript_data")
    if not raw:
        return None
    if isinstance(raw, dict):
        data = raw
    else:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
    td = data.get("transcript_data") or data
    utt = td.get("utterances") if isinstance(td, dict) else None
    if isinstance(utt, list) and utt:
        return " ".join(
            (u.get("text") or "").strip() for u in utt if (u.get("text") or "").strip()
        )
    if isinstance(td, dict) and td.get("text"):
        return str(td["text"]).strip()
    return None


def pick_long_completed_item(
    adapter: SQLiteAdapter,
    *,
    min_duration_seconds: float = 1800.0,
    limit: int = 5,
) -> List[EpisodeBenchmarkPick]:
    rows = adapter.execute_raw(
        """
        SELECT i.id, i.audio_url, i.title,
               CAST(COALESCE(i.duration_seconds, '0') AS REAL) AS dur
        FROM items i
        WHERE i.transcript_status = 'completed'
          AND i.audio_url IS NOT NULL AND trim(i.audio_url) != ''
          AND (
            i.audio_url LIKE '%amazonaws.com%'
            OR i.audio_url LIKE '%s3.%'
            OR i.audio_url LIKE 'https://%'
          )
          AND CAST(COALESCE(i.duration_seconds, '0') AS REAL) >= ?
        ORDER BY dur DESC
        LIMIT ?
        """,
        [min_duration_seconds, limit],
    )
    out: List[EpisodeBenchmarkPick] = []
    for r in rows:
        out.append(
            EpisodeBenchmarkPick(
                item_id=str(r["id"]),
                audio_url=str(r["audio_url"]),
                title=str(r.get("title") or ""),
                duration_seconds=float(r["dur"]) if r.get("dur") is not None else None,
            )
        )
    return out


def eval_hypothesis_vs_reference(
    reference_text: str,
    status_like_payload: Dict[str, Any],
) -> Tuple[float, float, str, int, int]:
    """Returns WER, CER, hypothesis_text, num_segments, num_speakers."""
    hyp_text, n_seg, n_spk = _whisperx_hypothesis_text(status_like_payload)
    wer_v, cer_v = compute_wer_cer(reference_text, hyp_text)
    return wer_v, cer_v, hyp_text, n_seg, n_spk


def run_runpod_and_eval(
    *,
    item_id: str,
    audio_url: str,
    reference_text: str,
    language: str = "nl",
    model: str = "large-v2",
    diarize: bool = True,
    wait: bool = True,
    poll_interval_sec: float = 3.0,
    timeout_sec: float = 14400.0,
) -> Dict[str, Any]:
    """
    Submit async RunPod job; optionally wait; download transcript JSON; WER/CER vs reference.
    """
    from stemmy_cli.storage.s3 import download_transcript_json_from_s3_uri

    inp = runpod_api.build_worker_input(
        item_id=item_id,
        audio_url=audio_url,
        language=language,
        model=model,
        diarize=diarize,
    )
    if not str((inp.get("output") or {}).get("s3_bucket") or "").strip():
        return {
            "ok": False,
            "error": "S3 output bucket not set (S3_OUTPUT_BUCKET / S3_BUCKET_NAME / AWS_S3_BUCKET)",
        }
    t0 = time.perf_counter()
    submit = runpod_api.run_job_async(inp)
    job_id = submit.get("id") or submit.get("jobId") or submit.get("job_id")
    if not job_id:
        return {"ok": False, "error": f"unexpected submit response: {submit!r}"}

    out: Dict[str, Any] = {
        "ok": True,
        "item_id": item_id,
        "job_id": job_id,
        "wall_submit_sec": time.perf_counter() - t0,
    }

    if not wait:
        out["waited"] = False
        return out

    status = runpod_api.wait_for_job(
        str(job_id),
        poll_interval_sec=poll_interval_sec,
        timeout_sec=timeout_sec,
    )
    out["runpod_status"] = status
    st = (status.get("status") or "").upper()
    if st != "COMPLETED":
        out["ok"] = False
        out["error"] = status.get("error") or f"job not completed: {st}"
        return out

    handler_out = status.get("output") or {}
    if isinstance(handler_out, str):
        try:
            handler_out = json.loads(handler_out)
        except json.JSONDecodeError:
            handler_out = {}

    s3_uri = handler_out.get("s3_uri")
    if not s3_uri:
        out["ok"] = False
        out["error"] = handler_out.get("error") or "missing s3_uri in output"
        return out

    payload = download_transcript_json_from_s3_uri(str(s3_uri))
    wer_v, cer_v, hyp_text, n_seg, n_spk = eval_hypothesis_vs_reference(reference_text, payload)
    audio_sec = float(handler_out.get("seconds_audio") or 0.0) or None
    compute_sec = float(handler_out.get("seconds_compute") or 0.0)
    rtf = (compute_sec / audio_sec) if audio_sec and audio_sec > 0 else None

    out.update(
        {
            "s3_uri": s3_uri,
            "wer": wer_v,
            "cer": cer_v,
            "hypothesis_chars": len(hyp_text),
            "reference_chars": len(reference_text),
            "num_segments": n_seg,
            "num_speakers": n_spk,
            "seconds_audio": audio_sec,
            "seconds_compute": compute_sec,
            "rtf": rtf,
            "waited": True,
        }
    )
    return out
</think>

I introduced a syntax error: `title str` should be `title=`.

<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>
StrReplace