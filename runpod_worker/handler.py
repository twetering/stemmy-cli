"""RunPod Serverless handler: fast stable profile (faster-whisper -> S3 JSON)."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from typing import Any, Dict, List, Optional

import httpx
import runpod
from faster_whisper import WhisperModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("runpod_asr")

_MODELS: Dict[str, WhisperModel] = {}


def _get_model(model_name: str, compute_type: str) -> WhisperModel:
    key = f"{model_name}:{compute_type}"
    if key not in _MODELS:
        logger.info("loading model=%s compute_type=%s", model_name, compute_type)
        _MODELS[key] = WhisperModel(model_name, device="cuda", compute_type=compute_type)
    return _MODELS[key]


def _s3_client(
    access_key: Optional[str],
    secret_key: Optional[str],
    region: Optional[str],
):
    import boto3

    a = access_key or os.getenv("AWS_ACCESS_KEY_ID")
    s = secret_key or os.getenv("AWS_SECRET_ACCESS_KEY")
    r = region or os.getenv("AWS_REGION", "eu-north-1")
    return boto3.client(
        "s3",
        aws_access_key_id=a,
        aws_secret_access_key=s,
        region_name=r,
    )


def _upload_json(
    bucket: str,
    key: str,
    payload: Dict[str, Any],
    *,
    access_key: Optional[str],
    secret_key: Optional[str],
    region: Optional[str],
) -> str:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    client = _s3_client(access_key, secret_key, region)
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType="application/json; charset=utf-8",
    )
    return f"s3://{bucket}/{key}"


def _download_audio_to_file(audio_url: str, out_path: str) -> None:
    with httpx.Client(timeout=600.0, follow_redirects=True) as client:
        with client.stream("GET", audio_url) as response:
            response.raise_for_status()
            with open(out_path, "wb") as f:
                for chunk in response.iter_bytes():
                    f.write(chunk)


def _segments_to_transcript_data(segments: List[Any]) -> Dict[str, Any]:
    utterances: List[Dict[str, Any]] = []
    flat_words: List[Dict[str, Any]] = []

    for seg in segments:
        seg_text = (getattr(seg, "text", "") or "").strip()
        seg_start = float(getattr(seg, "start", 0.0) or 0.0)
        seg_end = float(getattr(seg, "end", seg_start) or seg_start)

        words_out: List[Dict[str, Any]] = []
        for w in getattr(seg, "words", None) or []:
            word_txt = (getattr(w, "word", "") or "").strip()
            if not word_txt:
                continue
            st = int(round(float(getattr(w, "start", seg_start) or seg_start) * 1000))
            en = int(round(float(getattr(w, "end", seg_end) or seg_end) * 1000))
            conf = float(getattr(w, "probability", 0.0) or 0.0)
            wd = {"text": word_txt, "start": st, "end": en, "confidence": conf}
            words_out.append(wd)
            flat_words.append(wd)

        avg_conf = (
            sum(float(w.get("confidence", 0.0)) for w in words_out) / len(words_out)
            if words_out
            else 0.0
        )
        utterances.append(
            {
                "start": int(round(seg_start * 1000)),
                "end": int(round(seg_end * 1000)),
                "text": seg_text,
                "speaker": "A",
                "confidence": avg_conf,
                "words": words_out,
            }
        )

    return {
        "utterances": utterances,
        "words": flat_words,
        "entities": [],
        "text": " ".join(u["text"] for u in utterances if u.get("text")),
    }


def handler(job: Dict[str, Any]) -> Dict[str, Any]:
    t0 = time.perf_counter()
    inp = job.get("input") or {}
    item_id = str(inp.get("item_id") or "").strip()
    audio_url = str(inp.get("audio_url") or "").strip()
    if not item_id or not audio_url:
        return {
            "ok": False,
            "s3_uri": None,
            "item_id": item_id or None,
            "seconds_audio": None,
            "seconds_compute": time.perf_counter() - t0,
            "error": "item_id and audio_url are required",
        }

    language = str(inp.get("language") or "nl")
    model = str(inp.get("model") or "large-v3-turbo")
    diarize = bool(inp.get("diarize", False))
    beam_size = int(inp.get("beam_size") or 5)
    compute_type = str(inp.get("compute_type") or "float16")

    out_cfg = inp.get("output") or {}
    bucket = str(
        out_cfg.get("s3_bucket")
        or os.getenv("S3_OUTPUT_BUCKET")
        or os.getenv("S3_BUCKET_NAME")
        or os.getenv("AWS_S3_BUCKET")
        or ""
    ).strip()
    default_key = f"transcripts/runpod/{item_id}.json"
    s3_key = str(out_cfg.get("s3_key") or default_key).strip() or default_key

    ak = out_cfg.get("aws_access_key_id") or os.getenv("RUNPOD_API_KEY_S3_ACCESS_KEY")
    sk = out_cfg.get("aws_secret_access_key") or os.getenv("RUNPOD_API_KEY_S3_SECRET_KEY")
    region = out_cfg.get("aws_region") or os.getenv("AWS_REGION")

    if not bucket:
        return {
            "ok": False,
            "s3_uri": None,
            "item_id": item_id,
            "seconds_audio": None,
            "seconds_compute": time.perf_counter() - t0,
            "error": "output.s3_bucket or S3_OUTPUT_BUCKET / S3_BUCKET_NAME required",
        }

    try:
        asr = _get_model(model, compute_type)

        t_dl = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="stemmy_rp_") as tmp:
            audio_path = os.path.join(tmp, "audio_input.mp3")
            _download_audio_to_file(audio_url, audio_path)
            dl_sec = time.perf_counter() - t_dl
            logger.info("download done in %.1fs", dl_sec)

            t_tr = time.perf_counter()
            segments, _info = asr.transcribe(
                audio_path,
                language=language or None,
                beam_size=beam_size,
                word_timestamps=True,
            )
            segs = list(segments)
            tr_sec = time.perf_counter() - t_tr

        status = {"status": "completed", "transcript_data": _segments_to_transcript_data(segs)}

        s3_uri = _upload_json(bucket, s3_key, status, access_key=ak, secret_key=sk, region=region)

        return {
            "ok": True,
            "s3_uri": s3_uri,
            "item_id": item_id,
            "seconds_audio": dl_sec + tr_sec,
            "seconds_compute": time.perf_counter() - t0,
            "engine": "faster-whisper",
            "warning": (
                "diarize=true ignored in stable profile; enable WhisperX profile after baseline is stable"
                if diarize
                else None
            ),
            "error": None,
        }
    except Exception as e:
        logger.exception("handler failed")
        return {
            "ok": False,
            "s3_uri": None,
            "item_id": item_id,
            "seconds_audio": None,
            "seconds_compute": time.perf_counter() - t0,
            "error": str(e),
        }


runpod.serverless.start({"handler": handler})
