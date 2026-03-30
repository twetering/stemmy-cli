"""RunPod Serverless v2 HTTP API (enqueue + status)."""

from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional

import httpx

RUNPOD_API_BASE = os.getenv("RUNPOD_API_BASE", "https://api.runpod.ai/v2")


def _api_key() -> str:
    key = os.getenv("RUNPOD_API_KEY", "").strip()
    if not key:
        raise RuntimeError("RUNPOD_API_KEY is not set")
    return key


def _endpoint_id(explicit: Optional[str] = None) -> str:
    eid = (explicit or os.getenv("RUNPOD_ENDPOINT_ID", "")).strip()
    if not eid:
        raise RuntimeError("RUNPOD_ENDPOINT_ID is not set (or pass endpoint_id=…)")
    return eid


def run_job_async(
    input_payload: Dict[str, Any],
    *,
    endpoint_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    POST /run — returns at least {"id": "<job_id>"} on success.
    """
    eid = _endpoint_id(endpoint_id)
    url = f"{RUNPOD_API_BASE.rstrip('/')}/{eid}/run"
    headers = {"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"}
    body = {"input": input_payload}
    with httpx.Client(timeout=120.0) as client:
        r = client.post(url, json=body, headers=headers)
        r.raise_for_status()
        return r.json()


def get_job_status(
    job_id: str,
    *,
    endpoint_id: Optional[str] = None,
) -> Dict[str, Any]:
    eid = _endpoint_id(endpoint_id)
    url = f"{RUNPOD_API_BASE.rstrip('/')}/{eid}/status/{job_id}"
    headers = {"Authorization": f"Bearer {_api_key()}"}
    with httpx.Client(timeout=60.0) as client:
        r = client.get(url, headers=headers)
        r.raise_for_status()
        return r.json()


def wait_for_job(
    job_id: str,
    *,
    endpoint_id: Optional[str] = None,
    poll_interval_sec: float = 2.0,
    timeout_sec: float = 14400.0,
) -> Dict[str, Any]:
    deadline = time.monotonic() + timeout_sec
    last: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = get_job_status(job_id, endpoint_id=endpoint_id)
        st = (last.get("status") or "").upper()
        if st in ("COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"):
            return last
        time.sleep(poll_interval_sec)
    raise TimeoutError(f"RunPod job {job_id} did not finish within {timeout_sec}s; last={last!r}")


def build_worker_input(
    *,
    item_id: str,
    audio_url: str,
    language: str = "nl",
    model: str = "large-v2",
    diarize: bool = True,
    batch_size: int = 8,
    compute_type: str = "float16",
    s3_bucket: Optional[str] = None,
    s3_key: Optional[str] = None,
    min_speakers: Optional[int] = None,
    max_speakers: Optional[int] = None,
) -> Dict[str, Any]:
    bucket = (
        (s3_bucket or "").strip()
        or os.getenv("S3_OUTPUT_BUCKET", "").strip()
        or os.getenv("S3_BUCKET_NAME", "").strip()
        or os.getenv("AWS_S3_BUCKET", "").strip()
    )
    out: Dict[str, Any] = {
        "s3_bucket": bucket,
        "s3_key": (s3_key or f"transcripts/runpod/{item_id}.json").strip(),
        "aws_region": os.getenv("AWS_REGION"),
    }
    ak = os.getenv("RUNPOD_API_KEY_S3_ACCESS_KEY") or os.getenv("AWS_ACCESS_KEY_ID")
    sk = os.getenv("RUNPOD_API_KEY_S3_SECRET_KEY") or os.getenv("AWS_SECRET_ACCESS_KEY")
    if ak:
        out["aws_access_key_id"] = ak
    if sk:
        out["aws_secret_access_key"] = sk

    payload: Dict[str, Any] = {
        "item_id": item_id,
        "audio_url": audio_url,
        "language": language,
        "model": model,
        "diarize": diarize,
        "batch_size": batch_size,
        "compute_type": compute_type,
        "output": out,
    }
    if min_speakers is not None:
        payload["min_speakers"] = min_speakers
    if max_speakers is not None:
        payload["max_speakers"] = max_speakers
    return payload
