# RunPod Benchmark Workflow

This document captures the production-ready benchmark flow for RunPod ASR in `stemmy_cli`.

## Recommended Endpoint Preset

- Active workers: `1` (or `2` during heavy batches)
- Max workers: `12-20` (start with `16`)
- Idle timeout: `30-60s`
- Execution timeout: `3600-7200s`
- GPU count per worker: `1`
- Flashboot: enabled

Model/runtime defaults for speed:

- `model=large-v3-turbo`
- `beam_size=1`
- `diarize=false`
- `compute_type=float16`

## Credential Policy

Use canonical AWS keys for S3:

- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`
- `AWS_REGION`
- `S3_BUCKET_NAME` (or `S3_OUTPUT_BUCKET`)

Avoid stale `RUNPOD_API_KEY_S3_*` overrides unless explicitly needed.

## Benchmark Data Model

`benchmark_transcripts` table (created lazily by benchmark commands) stores:

- source (`runpod` / `assembly`)
- model params
- s3 URI
- transcript JSON snapshot
- runtime metrics (`seconds_audio`, `seconds_compute`, `rtf`)
- quality/timing metrics (`wer`, `cer`, timing deltas)

No mutation of existing `fragments`/`entities` is needed.

## Commands

Fetch a RunPod transcript into benchmark storage:

```bash
stemmy benchmark fetch-runpod <ITEM_ID>
```

Evaluate timing/quality against Assembly fragments:

```bash
stemmy benchmark eval-timings <ITEM_ID>
```

Inspect side-by-side word timing deltas (optional snippet extraction):

```bash
stemmy benchmark inspect-item <ITEM_ID> --word-limit 120 --extract-snippets 20
```

Batch speed + quality evaluation:

```bash
stemmy benchmark eval-batch --limit 20 --concurrency 6 --model large-v3-turbo --beam-size 1
```

## Audio Snippet Compare Strategy

`inspect-item` reuses existing local audio extraction helper:

- `stemmy_cli.audio.local_extract.extract_segment_local`

No ad-hoc ffmpeg shell wrappers are introduced for the benchmark pathway.

Snippets are saved under:

- `results/benchmark_snippets/<item_id>/`

with paired files:

- `<idx>_assembly.mp3`
- `<idx>_runpod.mp3`

## Throughput Target

With the current stable worker and concurrency scaling, target throughput is measured as:

- `aggregate_realtime_factor = total_audio_seconds / batch_wall_seconds`

Use this aggregate metric (cluster throughput), not per-item RTF, for 1000-hour planning.
