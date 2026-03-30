"""
RunPod Serverless handler: WhisperX on GPU → transcript JSON → S3.

Input schema (job["input"]):
  item_id (str, required)
  audio_url (str, required HTTPS URL to audio)
  language (str, default "nl")
  model (str, default "large-v2")
  diarize (bool, default True)
  batch_size (int, default 8)
  compute_type (str, default "float16"; GPU)
  min_speakers / max_speakers (optional int)
  output (dict):
    s3_bucket (str, required unless env S3_OUTPUT_BUCKET)
    s3_key (str, optional; default transcripts/runpod/{item_id}.json)
    aws_access_key_id, aws_secret_access_key, aws_region (optional; else env AWS_*)

Output (return value):
  ok (bool)
  s3_uri (str | null)
  item_id (str)
  seconds_audio (float | null)
  seconds_compute (float)
  error (str | null)
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from typing import Any, Dict, Optional

import runpod

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("runpod_whisperx")


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
    reg = region or os.getenv("AWS_REGION", "eu-north-1")
    return f"s3://{bucket}/{key}"


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
    model = str(inp.get("model") or "large-v2")
    diarize = bool(inp.get("diarize", True))
    batch_size = int(inp.get("batch_size") or 8)
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

    hf_token = (
        os.getenv("HF_TOKEN")
        or os.getenv("HUGGINGFACE_HUB_TOKEN")
        or os.getenv("HF_API_KEY")
    )

    try:
        from stemmy_cli.storage.whisperx_local import (
            WhisperXRunner,
            WhisperXRunnerConfig,
            download_audio_to_file,
        )

        cfg = WhisperXRunnerConfig(
            model=model,
            language=language,
            device="cuda",
            compute_type=compute_type,
            batch_size=batch_size,
            diarize=diarize,
            min_speakers=inp.get("min_speakers"),
            max_speakers=inp.get("max_speakers"),
            hf_token=hf_token,
            threads=int(os.getenv("WHISPERX_THREADS", "4")),
        )

        t_dl = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="stemmy_rp_") as tmp:
            audio_path = str(download_audio_to_file(audio_url, tmp))
            dl_sec = time.perf_counter() - t_dl
            logger.info("download done in %.1fs", dl_sec)

            t_tr = time.perf_counter()
            runner = WhisperXRunner(cfg)
            try:
                status = runner.transcribe_file(audio_path, print_progress=False)
            finally:
                runner.release()
            tr_sec = time.perf_counter() - t_tr

        if status.get("status") != "completed":
            return {
                "ok": False,
                "s3_uri": None,
                "item_id": item_id,
                "seconds_audio": None,
                "seconds_compute": time.perf_counter() - t0,
                "error": "WhisperX did not produce completed status",
            }

        s3_uri = _upload_json(bucket, s3_key, status, access_key=ak, secret_key=sk, region=region)
        try:
            import torch

            del torch
        except Exception:
            pass

        return {
            "ok": True,
            "s3_uri": s3_uri,
            "item_id": item_id,
            "seconds_audio": dl_sec + tr_sec,
            "seconds_compute": time.perf_counter() - t0,
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
