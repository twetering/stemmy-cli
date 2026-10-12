# RunPod Worker Notes (Stemmy)

Operational notes for `runpod_worker/` after iterative debugging.

## Current Stable Profile

- Engine: `faster-whisper` (no diarization)
- Base image: `nvidia/cuda:12.3.2-cudnn9-runtime-ubuntu22.04`
- Handler output stays import-compatible:
  - `status: "completed"`
  - `transcript_data.utterances`
  - `transcript_data.words`
  - `transcript_data.entities` (empty)

This profile is intentionally minimal to maximize build reliability.

## Why We Disabled Diarization First

WhisperX + pyannote caused repeated dependency resolver conflicts in serverless builds
(notably torch/whisperx pin incompatibilities). The stable path is:

1. First ship ASR-only worker (this profile).
2. Validate queue, S3 upload, end-to-end import.
3. Reintroduce diarization in a separate profile/version only if needed.

## Credential Precedence (Important)

The worker uses S3 credentials in this order:

1. `input.output.aws_access_key_id` / `aws_secret_access_key`
2. `RUNPOD_API_KEY_S3_ACCESS_KEY` / `RUNPOD_API_KEY_S3_SECRET_KEY`
3. `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`

Region:

1. `input.output.aws_region`
2. `AWS_REGION` (default `eu-north-1`)

### Common Failure

`InvalidAccessKeyId` at `PutObject` means the worker used a key that AWS does not
recognize. In practice, this was often caused by stale/incorrect
`RUNPOD_API_KEY_S3_*` values overriding good `AWS_*` keys.

## Fast Smoke Test Strategy

Use a short clip/item first (sub-1 minute) to reduce debug loop times.

Suggested workflow:

1. Submit one short item.
2. Poll until `COMPLETED` or `FAILED`.
3. Verify `s3_uri` exists and JSON can be fetched.
4. Only then run full-episode benchmark.

## Local Credential Probe (before submitting jobs)

Run an STS + S3 put/get/delete probe with local `.env` values:

```bash
python - <<'PY'
from dotenv import load_dotenv
import os, json, time, boto3
load_dotenv('.env', override=True)
s3 = boto3.client('s3',
    aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID'),
    aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY'),
    region_name=os.getenv('AWS_REGION'))
key=f"transcripts/runpod/_probe_{int(time.time())}.json"
s3.put_object(Bucket=os.getenv('S3_BUCKET_NAME'), Key=key, Body=b'{}')
s3.get_object(Bucket=os.getenv('S3_BUCKET_NAME'), Key=key)
s3.delete_object(Bucket=os.getenv('S3_BUCKET_NAME'), Key=key)
print("S3 probe ok")
PY
```

## RunPod Endpoint Checklist

- Endpoint version is green and active
- Min workers >= 1 during debugging
- Worker env has valid keys (prefer Secrets)
- Bucket/region aligned with credentials
- No stale secret values after key rotation
