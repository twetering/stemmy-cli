"""RunPod vs Assembly benchmark helpers (storage + quality/timing eval)."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from stemmy_cli.adapters.sqlite import SQLiteAdapter
from stemmy_cli.benchmarks.whisperx_reference import compute_wer_cer
from stemmy_cli.storage.s3 import download_transcript_json_from_s3_uri


def ensure_benchmark_tables(adapter: SQLiteAdapter) -> None:
    """Create benchmark tables lazily (no schema migration required)."""
    adapter.execute_raw(
        """
        CREATE TABLE IF NOT EXISTS benchmark_transcripts (
            id TEXT PRIMARY KEY,
            item_id TEXT NOT NULL,
            source TEXT NOT NULL,
            engine TEXT NOT NULL,
            model_params TEXT,
            s3_uri TEXT,
            transcript_json TEXT,
            seconds_audio REAL,
            seconds_compute REAL,
            rtf REAL,
            wer REAL,
            cer REAL,
            timing_start_abs_err_sec REAL,
            timing_end_abs_err_sec REAL,
            timing_span_err_sec REAL,
            job_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    adapter.execute_raw(
        """
        CREATE INDEX IF NOT EXISTS idx_benchmark_transcripts_item_source
        ON benchmark_transcripts(item_id, source, created_at)
        """
    )
    adapter.execute_raw(
        """
        CREATE TABLE IF NOT EXISTS benchmark_utterance_pairs (
            id TEXT PRIMARY KEY,
            benchmark_id TEXT NOT NULL,
            item_id TEXT NOT NULL,
            pair_index INTEGER NOT NULL,
            assembly_text TEXT,
            runpod_text TEXT,
            assembly_start REAL,
            assembly_end REAL,
            runpod_start REAL,
            runpod_end REAL,
            overlap_ratio REAL,
            delta_start_ms INTEGER,
            delta_end_ms INTEGER,
            created_at TEXT NOT NULL
        )
        """
    )
    adapter.execute_raw(
        """
        CREATE INDEX IF NOT EXISTS idx_benchmark_utterance_pairs_bid
        ON benchmark_utterance_pairs(benchmark_id, pair_index)
        """
    )
    adapter.execute_raw(
        """
        CREATE TABLE IF NOT EXISTS benchmark_word_pairs (
            id TEXT PRIMARY KEY,
            benchmark_id TEXT NOT NULL,
            item_id TEXT NOT NULL,
            utterance_pair_id TEXT NOT NULL,
            pair_index INTEGER NOT NULL,
            assembly_text TEXT,
            runpod_text TEXT,
            assembly_start REAL,
            assembly_end REAL,
            runpod_start REAL,
            runpod_end REAL,
            delta_start_ms INTEGER,
            delta_end_ms INTEGER,
            created_at TEXT NOT NULL
        )
        """
    )
    adapter.execute_raw(
        """
        CREATE INDEX IF NOT EXISTS idx_benchmark_word_pairs_bid
        ON benchmark_word_pairs(benchmark_id, pair_index)
        """
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_ts(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    # Mixed providers may emit milliseconds; values above ~1000 are very likely ms.
    if x > 1000:
        return x / 1000.0
    return x


def latest_benchmark_row(
    adapter: SQLiteAdapter,
    item_id: str,
    source: str,
) -> Optional[Dict[str, Any]]:
    rows = adapter.execute_raw(
        """
        SELECT *
        FROM benchmark_transcripts
        WHERE item_id = ? AND source = ?
        ORDER BY datetime(created_at) DESC
        LIMIT 1
        """,
        [item_id, source],
    )
    return rows[0] if rows else None


def insert_benchmark_transcript(
    adapter: SQLiteAdapter,
    *,
    item_id: str,
    source: str,
    engine: str,
    model_params: Optional[Dict[str, Any]],
    transcript_payload: Dict[str, Any],
    s3_uri: Optional[str] = None,
    seconds_audio: Optional[float] = None,
    seconds_compute: Optional[float] = None,
    job_id: Optional[str] = None,
) -> str:
    ensure_benchmark_tables(adapter)
    now = _now_iso()
    td = transcript_payload.get("transcript_data") or transcript_payload
    rtf = None
    if seconds_audio and seconds_audio > 0 and seconds_compute is not None:
        rtf = float(seconds_compute) / float(seconds_audio)
    row_id = str(uuid.uuid4())
    adapter.insert(
        "benchmark_transcripts",
        {
            "id": row_id,
            "item_id": item_id,
            "source": source,
            "engine": engine,
            "model_params": json.dumps(model_params or {}),
            "s3_uri": s3_uri,
            "transcript_json": json.dumps(td, ensure_ascii=False),
            "seconds_audio": seconds_audio,
            "seconds_compute": seconds_compute,
            "rtf": rtf,
            "job_id": job_id,
            "created_at": now,
            "updated_at": now,
        },
    )
    return row_id


def _ref_text_and_bounds_from_fragments(
    adapter: SQLiteAdapter, item_id: str
) -> Tuple[str, Optional[float], Optional[float], List[Dict[str, Any]]]:
    rows = adapter.execute_raw(
        """
        SELECT text, start_time_seconds, end_time_seconds, words, order_index
        FROM fragments
        WHERE item_id = ?
        ORDER BY CAST(COALESCE(order_index, '0') AS INTEGER),
                 CAST(COALESCE(start_time_seconds, '0') AS REAL)
        """,
        [item_id],
    )
    text = " ".join((r.get("text") or "").strip() for r in rows if (r.get("text") or "").strip())
    starts = [_normalize_ts(r.get("start_time_seconds")) for r in rows]
    ends = [_normalize_ts(r.get("end_time_seconds")) for r in rows]
    starts = [x for x in starts if x is not None]
    ends = [x for x in ends if x is not None]
    return text, (min(starts) if starts else None), (max(ends) if ends else None), rows


def _assembly_utterances_from_fragments(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for i, r in enumerate(rows):
        st = _normalize_ts(r.get("start_time_seconds"))
        en = _normalize_ts(r.get("end_time_seconds"))
        txt = (r.get("text") or "").strip()
        if st is None or en is None:
            continue
        # Filter obvious non-speech/placeholder rows to stabilize alignment.
        txt_l = txt.lower()
        if (
            txt_l.startswith("johnny carson non-speech")
            or txt_l.startswith("non-speech")
            or txt_l.endswith("non-speech segment")
        ):
            continue
        if len(txt) < 2:
            continue
        out.append({"idx": i, "text": txt, "start": st, "end": en, "words": r.get("words")})
    return out


def _hyp_text_and_bounds_from_payload(
    transcript_payload: Dict[str, Any],
) -> Tuple[str, Optional[float], Optional[float], List[Dict[str, Any]]]:
    td = transcript_payload.get("transcript_data") or transcript_payload
    utt = td.get("utterances") or []
    text = " ".join((u.get("text") or "").strip() for u in utt if (u.get("text") or "").strip())
    starts = [_normalize_ts(u.get("start")) for u in utt]
    ends = [_normalize_ts(u.get("end")) for u in utt]
    starts = [x for x in starts if x is not None]
    ends = [x for x in ends if x is not None]
    return text, (min(starts) if starts else None), (max(ends) if ends else None), utt


def _runpod_utterances_from_payload(transcript_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    td = transcript_payload.get("transcript_data") or transcript_payload
    out: List[Dict[str, Any]] = []
    for i, u in enumerate(td.get("utterances") or []):
        st = _normalize_ts(u.get("start"))
        en = _normalize_ts(u.get("end"))
        txt = (u.get("text") or "").strip()
        if st is None or en is None:
            continue
        if len(txt) < 2:
            continue
        out.append({"idx": i, "text": txt, "start": st, "end": en, "words": u.get("words") or []})
    return out


def _interval_overlap_ratio(a0: float, a1: float, b0: float, b1: float) -> float:
    inter = max(0.0, min(a1, b1) - max(a0, b0))
    union = max(a1, b1) - min(a0, b0)
    if union <= 0:
        return 0.0
    return inter / union


def _build_utterance_pairs(
    assembly_utterances: List[Dict[str, Any]], runpod_utterances: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """
    Build monotonic utterance pairs.

    We only match in chronological order with a lookahead window and reject weak overlaps.
    This prevents global drift where one bad pair can shift all following pairings.
    """
    pairs: List[Dict[str, Any]] = []
    next_runpod_start = 0
    lookahead = 80
    min_overlap = 0.08
    for au in assembly_utterances:
        best_idx = None
        best_score = -1.0
        upper = min(len(runpod_utterances), next_runpod_start + lookahead)
        for ri in range(next_runpod_start, upper):
            ru = runpod_utterances[ri]
            score = _interval_overlap_ratio(au["start"], au["end"], ru["start"], ru["end"])
            # Slightly reward temporal proximity for short utterances.
            center_a = (au["start"] + au["end"]) / 2.0
            center_r = (ru["start"] + ru["end"]) / 2.0
            dist_penalty = min(1.0, abs(center_a - center_r) / 30.0)
            score = score - (0.15 * dist_penalty)
            if score > best_score:
                best_score = score
                best_idx = ri
        if best_idx is None or best_score < min_overlap:
            continue
        ru = runpod_utterances[best_idx]
        next_runpod_start = best_idx + 1
        pairs.append(
            {
                "assembly": au,
                "runpod": ru,
                "overlap_ratio": _interval_overlap_ratio(au["start"], au["end"], ru["start"], ru["end"]),
                "delta_start_ms": int(round((ru["start"] - au["start"]) * 1000)),
                "delta_end_ms": int(round((ru["end"] - au["end"]) * 1000)),
            }
        )
    return pairs


def evaluate_runpod_vs_assembly(
    adapter: SQLiteAdapter,
    *,
    item_id: str,
    runpod_payload: Dict[str, Any],
) -> Dict[str, Any]:
    ref_text, r0, r1, frag_rows = _ref_text_and_bounds_from_fragments(adapter, item_id)
    hyp_text, h0, h1, utt = _hyp_text_and_bounds_from_payload(runpod_payload)
    ass_utt = _assembly_utterances_from_fragments(frag_rows)
    run_utt = _runpod_utterances_from_payload(runpod_payload)
    utterance_pairs = _build_utterance_pairs(ass_utt, run_utt)
    if ref_text and hyp_text:
        wer, cer = compute_wer_cer(ref_text, hyp_text)
    else:
        wer, cer = None, None
    span_ref = (r1 - r0) if (r0 is not None and r1 is not None) else None
    span_hyp = (h1 - h0) if (h0 is not None and h1 is not None) else None
    mean_pair_start_abs = None
    mean_pair_end_abs = None
    if utterance_pairs:
        mean_pair_start_abs = sum(abs(p["delta_start_ms"]) for p in utterance_pairs) / (
            len(utterance_pairs) * 1000.0
        )
        mean_pair_end_abs = sum(abs(p["delta_end_ms"]) for p in utterance_pairs) / (
            len(utterance_pairs) * 1000.0
        )
    return {
        "item_id": item_id,
        "reference_chars": len(ref_text),
        "hypothesis_chars": len(hyp_text),
        "reference_fragments": len(frag_rows),
        "hypothesis_utterances": len(utt),
        "utterance_pairs": len(utterance_pairs),
        "wer": wer,
        "cer": cer,
        "reference_t0": r0,
        "reference_t1": r1,
        "hypothesis_t0": h0,
        "hypothesis_t1": h1,
        "timing_start_abs_err_sec": abs(h0 - r0) if (h0 is not None and r0 is not None) else None,
        "timing_end_abs_err_sec": abs(h1 - r1) if (h1 is not None and r1 is not None) else None,
        "timing_span_err_sec": abs(span_hyp - span_ref) if (span_hyp is not None and span_ref is not None) else None,
        "timing_pair_start_abs_err_sec": mean_pair_start_abs,
        "timing_pair_end_abs_err_sec": mean_pair_end_abs,
    }


def save_eval_metrics(adapter: SQLiteAdapter, benchmark_id: str, metrics: Dict[str, Any]) -> None:
    adapter.update(
        "benchmark_transcripts",
        benchmark_id,
        {
            "wer": metrics.get("wer"),
            "cer": metrics.get("cer"),
            "timing_start_abs_err_sec": metrics.get("timing_start_abs_err_sec"),
            "timing_end_abs_err_sec": metrics.get("timing_end_abs_err_sec"),
            "timing_span_err_sec": metrics.get("timing_span_err_sec"),
            "updated_at": _now_iso(),
        },
    )


def benchmark_s3_uri_for_item(item_id: str) -> str:
    bucket = (
        os.getenv("S3_OUTPUT_BUCKET")
        or os.getenv("S3_BUCKET_NAME")
        or os.getenv("AWS_S3_BUCKET")
        or ""
    ).strip()
    if not bucket:
        raise RuntimeError("Missing S3 output bucket (S3_OUTPUT_BUCKET/S3_BUCKET_NAME/AWS_S3_BUCKET)")
    return f"s3://{bucket}/transcripts/runpod/{item_id}.json"


def fetch_runpod_payload_for_item(item_id: str, s3_uri: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
    uri = s3_uri or benchmark_s3_uri_for_item(item_id)
    payload = download_transcript_json_from_s3_uri(uri)
    return uri, payload


def _flatten_assembly_words(fragment_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for r in fragment_rows:
        raw = r.get("words")
        if not raw:
            continue
        try:
            words = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            continue
        if not isinstance(words, list):
            continue
        for w in words:
            st = _normalize_ts(w.get("start"))
            en = _normalize_ts(w.get("end"))
            txt = (w.get("text") or w.get("word") or "").strip()
            if st is None or en is None or not txt:
                continue
            out.append({"text": txt, "start": st, "end": en})
    return out


def _flatten_runpod_words(transcript_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    td = transcript_payload.get("transcript_data") or transcript_payload
    words = td.get("words") or []
    out: List[Dict[str, Any]] = []
    for w in words:
        st = _normalize_ts(w.get("start"))
        en = _normalize_ts(w.get("end"))
        txt = (w.get("text") or w.get("word") or "").strip()
        if st is None or en is None or not txt:
            continue
        out.append({"text": txt, "start": st, "end": en})
    return out


def _flatten_utterance_words(utterance: Dict[str, Any], fallback_words_raw: Any = None) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    words_inline = utterance.get("words")
    if not isinstance(words_inline, list):
        words_inline = []
    for w in words_inline:
        st = _normalize_ts(w.get("start"))
        en = _normalize_ts(w.get("end"))
        txt = (w.get("text") or w.get("word") or "").strip()
        if st is None or en is None or not txt:
            continue
        out.append({"text": txt, "start": st, "end": en})
    if out:
        return out
    if not fallback_words_raw:
        return out
    try:
        words = json.loads(fallback_words_raw) if isinstance(fallback_words_raw, str) else fallback_words_raw
    except json.JSONDecodeError:
        return out
    if not isinstance(words, list):
        return out
    for w in words:
        st = _normalize_ts(w.get("start"))
        en = _normalize_ts(w.get("end"))
        txt = (w.get("text") or w.get("word") or "").strip()
        if st is None or en is None or not txt:
            continue
        out.append({"text": txt, "start": st, "end": en})
    return out


def materialize_alignment_rows(
    adapter: SQLiteAdapter,
    *,
    benchmark_id: str,
    item_id: str,
    runpod_payload: Dict[str, Any],
    max_word_pairs: int = 5000,
) -> Dict[str, int]:
    """Store stable utterance-first, word-second alignment rows for compare UI."""
    ensure_benchmark_tables(adapter)
    _, _, _, frag_rows = _ref_text_and_bounds_from_fragments(adapter, item_id)
    ass_utt = _assembly_utterances_from_fragments(frag_rows)
    run_utt = _runpod_utterances_from_payload(runpod_payload)
    utt_pairs = _build_utterance_pairs(ass_utt, run_utt)
    now = _now_iso()

    adapter.execute_raw("DELETE FROM benchmark_word_pairs WHERE benchmark_id = ?", [benchmark_id])
    adapter.execute_raw("DELETE FROM benchmark_utterance_pairs WHERE benchmark_id = ?", [benchmark_id])

    word_count = 0
    for i, p in enumerate(utt_pairs):
        up_id = str(uuid.uuid4())
        au = p["assembly"]
        ru = p["runpod"]
        adapter.insert(
            "benchmark_utterance_pairs",
            {
                "id": up_id,
                "benchmark_id": benchmark_id,
                "item_id": item_id,
                "pair_index": i,
                "assembly_text": au.get("text"),
                "runpod_text": ru.get("text"),
                "assembly_start": au.get("start"),
                "assembly_end": au.get("end"),
                "runpod_start": ru.get("start"),
                "runpod_end": ru.get("end"),
                "overlap_ratio": p.get("overlap_ratio"),
                "delta_start_ms": p.get("delta_start_ms"),
                "delta_end_ms": p.get("delta_end_ms"),
                "created_at": now,
            },
        )

        aw = _flatten_utterance_words(au, fallback_words_raw=au.get("words"))
        rw = _flatten_utterance_words(ru)
        n = min(len(aw), len(rw), max(0, max_word_pairs - word_count))
        for j in range(n):
            a = aw[j]
            r = rw[j]
            adapter.insert(
                "benchmark_word_pairs",
                {
                    "id": str(uuid.uuid4()),
                    "benchmark_id": benchmark_id,
                    "item_id": item_id,
                    "utterance_pair_id": up_id,
                    "pair_index": word_count,
                    "assembly_text": a["text"],
                    "runpod_text": r["text"],
                    "assembly_start": a["start"],
                    "assembly_end": a["end"],
                    "runpod_start": r["start"],
                    "runpod_end": r["end"],
                    "delta_start_ms": int(round((r["start"] - a["start"]) * 1000)),
                    "delta_end_ms": int(round((r["end"] - a["end"]) * 1000)),
                    "created_at": now,
                },
            )
            word_count += 1
            if word_count >= max_word_pairs:
                break
        if word_count >= max_word_pairs:
            break
    return {"utterance_pairs": len(utt_pairs), "word_pairs": word_count}


def build_word_alignment_preview(
    adapter: SQLiteAdapter,
    *,
    item_id: str,
    runpod_payload: Dict[str, Any],
    limit: int = 80,
) -> List[Dict[str, Any]]:
    # Fallback preview by global order only. Prefer DB-backed utterance-first pairs.
    _, _, _, frag_rows = _ref_text_and_bounds_from_fragments(adapter, item_id)
    aw = _flatten_assembly_words(frag_rows)
    rw = _flatten_runpod_words(runpod_payload)
    n = min(limit, len(aw), len(rw))
    rows: List[Dict[str, Any]] = []
    for i in range(n):
        a = aw[i]
        r = rw[i]
        rows.append(
            {
                "idx": i,
                "assembly_text": a["text"],
                "runpod_text": r["text"],
                "assembly_start": a["start"],
                "assembly_end": a["end"],
                "runpod_start": r["start"],
                "runpod_end": r["end"],
                "delta_start_ms": int(round((r["start"] - a["start"]) * 1000)),
                "delta_end_ms": int(round((r["end"] - a["end"]) * 1000)),
            }
        )
    return rows


def load_utterance_pairs_preview(
    adapter: SQLiteAdapter, benchmark_id: str, limit: int = 120
) -> List[Dict[str, Any]]:
    return adapter.execute_raw(
        """
        SELECT pair_index AS idx,
               assembly_text, runpod_text,
               assembly_start, assembly_end,
               runpod_start, runpod_end,
               overlap_ratio, delta_start_ms, delta_end_ms
        FROM benchmark_utterance_pairs
        WHERE benchmark_id = ?
        ORDER BY pair_index ASC
        LIMIT ?
        """,
        [benchmark_id, limit],
    )


def load_word_pairs_preview(
    adapter: SQLiteAdapter, benchmark_id: str, limit: int = 120
) -> List[Dict[str, Any]]:
    return adapter.execute_raw(
        """
        SELECT pair_index AS idx,
               assembly_text, runpod_text,
               assembly_start, assembly_end,
               runpod_start, runpod_end,
               delta_start_ms, delta_end_ms
        FROM benchmark_word_pairs
        WHERE benchmark_id = ?
        ORDER BY pair_index ASC
        LIMIT ?
        """,
        [benchmark_id, limit],
    )


def parse_transcript_json(raw: Any) -> Dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return {}


def save_benchmark_report(path: Path, report: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
