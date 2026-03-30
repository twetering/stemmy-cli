"""RunPod GPU transcription: enqueue serverless jobs, poll, S3 JSON → SQLite import."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional

import typer

from stemmy_cli.adapters.sqlite import SQLiteAdapter
from stemmy_cli.commands.transcripts import _clear_item_transcript_rows, _import_transcript_to_db
from stemmy_cli.output import output_result, print_error, print_info, print_success, print_warning
from stemmy_cli.storage import runpod_api
from stemmy_cli.storage.s3 import download_transcript_json_from_s3_uri, is_s3_configured

app = typer.Typer(help="RunPod serverless GPU transcription (WhisperX → S3 → local DB)")


def _append_manifest(manifest: Path, record: Dict[str, Any]) -> None:
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with manifest.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _require_s3() -> None:
    if not is_s3_configured():
        print_error(
            "S3 not configured for download. Set AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, "
            "and S3_BUCKET_NAME (or AWS_S3_BUCKET)."
        )
        raise typer.Exit(1)


def import_transcript_from_s3(
    adapter: SQLiteAdapter,
    item_id: str,
    s3_uri: str,
    *,
    replace: bool = False,
) -> Dict[str, int]:
    """Single transaction: optional clear, fragments + entities, items.transcript_data."""
    item = adapter.get_by_id("items", item_id)
    if not item:
        raise ValueError(f"Item {item_id} not found")
    audio_url = str(item.get("audio_url") or "")
    payload = download_transcript_json_from_s3_uri(s3_uri)

    with adapter.write_transaction() as conn:
        if replace:
            _clear_item_transcript_rows(adapter, item_id, connection=conn)
        stats = _import_transcript_to_db(adapter, item_id, audio_url, payload, connection=conn)
        td = payload.get("transcript_data") or payload
        if isinstance(td, dict) and "utterances" not in td and "transcript_data" in td:
            td = td.get("transcript_data") or td
        adapter.update(
            "items",
            item_id,
            {
                "transcript_status": "completed",
                "transcript_data": json.dumps(td) if td else None,
            },
            connection=conn,
        )
    return stats


@app.command("submit")
def submit_one(
    item_id: str = typer.Argument(..., help="Item UUID"),
    language: str = typer.Option("nl", "--language"),
    model: str = typer.Option("large-v2", "--model", "-m"),
    diarize: bool = typer.Option(True, "--diarize/--no-diarize"),
    batch_size: int = typer.Option(8, "--batch-size", "-b"),
    compute_type: str = typer.Option("float16", "--compute-type"),
    manifest: Path = typer.Option(
        Path("results/runpod_jobs.jsonl"),
        "--manifest",
        help="Append one JSON line per enqueue (idempotent audit trail)",
    ),
    json_output: bool = typer.Option(False, "--json", "-j"),
):
    """Enqueue a single RunPod job from DB item (audio_url + S3 output contract)."""
    try:
        adapter = SQLiteAdapter()
        row = adapter.get_by_id("items", item_id)
        if not row:
            print_error(f"Item {item_id} not found")
            raise typer.Exit(1)
        audio_url = str(row.get("audio_url") or "").strip()
        if not audio_url:
            print_error("Item has no audio_url")
            raise typer.Exit(1)

        inp = runpod_api.build_worker_input(
            item_id=item_id,
            audio_url=audio_url,
            language=language,
            model=model,
            diarize=diarize,
            batch_size=batch_size,
            compute_type=compute_type,
        )
        if not str((inp.get("output") or {}).get("s3_bucket") or "").strip():
            print_error(
                "S3 output bucket missing. Set S3_OUTPUT_BUCKET, S3_BUCKET_NAME, or AWS_S3_BUCKET "
                "(worker needs PutObject on transcripts/runpod/{item_id}.json)."
            )
            raise typer.Exit(1)
        resp = runpod_api.run_job_async(inp)
        job_id = resp.get("id") or resp.get("jobId") or resp.get("job_id")
        rec = {
            "phase": "enqueued",
            "item_id": item_id,
            "job_id": str(job_id) if job_id else None,
            "response": resp,
        }
        _append_manifest(manifest, rec)
        print_success(f"Enqueued job id={job_id}")
        output_result(rec, json_output=json_output, title="RunPod submit")
    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("poll")
def poll_job(
    job_id: str = typer.Argument(..., help="RunPod job id"),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Block until terminal status"),
    poll_interval: float = typer.Option(3.0, "--poll-interval"),
    timeout: float = typer.Option(14400.0, "--timeout", help="Max seconds when waiting"),
    json_output: bool = typer.Option(False, "--json", "-j"),
):
    """Fetch job status (or exit after submit without --wait)."""
    try:
        if wait:
            status = runpod_api.wait_for_job(
                job_id, poll_interval_sec=poll_interval, timeout_sec=timeout
            )
        else:
            status = runpod_api.get_job_status(job_id)
        output_result(status, json_output=json_output, title=f"RunPod {job_id}")
    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("import-s3")
def import_s3(
    item_id: str = typer.Argument(..., help="Item UUID"),
    s3_uri: str = typer.Argument(..., help="s3://bucket/key from worker output"),
    replace: bool = typer.Option(False, "--replace", help="Delete existing fragments/entities first"),
    json_output: bool = typer.Option(False, "--json", "-j"),
):
    """Download completed transcript JSON from S3 and import into SQLite."""
    try:
        _require_s3()
        adapter = SQLiteAdapter()
        stats = import_transcript_from_s3(adapter, item_id, s3_uri, replace=replace)
        print_success(
            f"Imported {stats['fragments_imported']} fragments, {stats['entities_imported']} entities"
        )
        output_result({"item_id": item_id, **stats}, json_output=json_output, title="Import")
    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("transcribe-batch")
def transcribe_batch(
    item_id: Optional[str] = typer.Option(
        None,
        "--item-id",
        help="Process this item only (alternative to --format)",
    ),
    format_id: Optional[str] = typer.Option(
        None,
        "--format",
        "-f",
        help="Enqueue pending items for this format",
    ),
    limit: int = typer.Option(10, "--limit", help="Max items when using --format"),
    concurrency: int = typer.Option(
        2,
        "--concurrency",
        "-c",
        help="Max parallel RunPod submissions (not RunPod worker count)",
    ),
    wait: bool = typer.Option(
        True,
        "--wait/--no-wait",
        help="Poll until jobs finish, then download + import completed",
    ),
    replace: bool = typer.Option(False, "--replace", help="Replace transcript rows on import"),
    sync_turso: bool = typer.Option(
        False,
        "--sync-turso",
        help="After batch: run one stemmy db sync-turso (optional)",
    ),
    language: str = typer.Option("nl", "--language"),
    model: str = typer.Option("large-v2", "--model", "-m"),
    diarize: bool = typer.Option(True, "--diarize/--no-diarize"),
    batch_size: int = typer.Option(8, "--batch-size", "-b"),
    compute_type: str = typer.Option("float16", "--compute-type"),
    manifest: Path = typer.Option(Path("results/runpod_jobs.jsonl"), "--manifest"),
    poll_interval: float = typer.Option(3.0, "--poll-interval"),
    timeout: float = typer.Option(14400.0, "--timeout"),
    json_output: bool = typer.Option(False, "--json", "-j"),
):
    """
    Enqueue RunPod WhisperX jobs for items, optional wait, import transcripts from S3.

    Idempotent S3 key per item: transcripts/runpod/{item_id}.json (re-run overwrites object).
    """
    if (item_id is None) == (format_id is None):
        print_error("Provide exactly one of: --item-id or --format")
        raise typer.Exit(1)

    try:
        adapter = SQLiteAdapter()
        rows: List[Dict[str, Any]] = []
        if item_id:
            row = adapter.get_by_id("items", item_id)
            if not row:
                print_error(f"Item {item_id} not found")
                raise typer.Exit(1)
            rows = [row]
        else:
            rows = adapter.execute_raw(
                """
                SELECT id, title, audio_url, transcript_status
                FROM items
                WHERE format_id = ?
                  AND audio_url IS NOT NULL AND trim(audio_url) != ''
                  AND (
                    transcript_status IS NULL
                    OR transcript_status = ''
                    OR transcript_status = 'pending'
                  )
                ORDER BY datetime(COALESCE(published_at, created_at)) DESC
                LIMIT ?
                """,
                [format_id, limit],
            )

        if not rows:
            print_info("No matching items to process")
            raise typer.Exit(0)

        jobs: List[Dict[str, Any]] = []

        def _submit(r: Dict[str, Any]) -> Dict[str, Any]:
            iid = str(r["id"])
            au = str(r.get("audio_url") or "").strip()
            inp = runpod_api.build_worker_input(
                item_id=iid,
                audio_url=au,
                language=language,
                model=model,
                diarize=diarize,
                batch_size=batch_size,
                compute_type=compute_type,
            )
            if not str((inp.get("output") or {}).get("s3_bucket") or "").strip():
                raise RuntimeError(
                    "S3 output bucket missing (S3_OUTPUT_BUCKET / S3_BUCKET_NAME / AWS_S3_BUCKET)"
                )
            resp = runpod_api.run_job_async(inp)
            jid = resp.get("id") or resp.get("jobId") or resp.get("job_id")
            rec = {"phase": "enqueued", "item_id": iid, "job_id": str(jid) if jid else None, "response": resp}
            _append_manifest(manifest, rec)
            return {"item_id": iid, "job_id": str(jid) if jid else "", "audio_url": au, "submit": resp}

        conc = max(1, int(concurrency))
        with ThreadPoolExecutor(max_workers=conc) as ex:
            futs = [ex.submit(_submit, r) for r in rows]
            for fut in as_completed(futs):
                jobs.append(fut.result())

        print_success(f"Enqueued {len(jobs)} job(s)")

        if not wait:
            output_result(jobs, json_output=json_output, title="RunPod batch (no wait)")
            return

        results: List[Dict[str, Any]] = []
        for j in jobs:
            jid = j.get("job_id") or ""
            iid = j["item_id"]
            if not jid:
                results.append({"item_id": iid, "status": "error", "error": "no job id"})
                _append_manifest(manifest, {"phase": "failed", "item_id": iid, "error": "no job id"})
                continue
            try:
                st = runpod_api.wait_for_job(
                    jid, poll_interval_sec=poll_interval, timeout_sec=timeout
                )
            except Exception as e:
                results.append({"item_id": iid, "job_id": jid, "status": "error", "error": str(e)})
                _append_manifest(manifest, {"phase": "failed", "item_id": iid, "job_id": jid, "error": str(e)})
                continue

            terminal = (st.get("status") or "").upper()
            out = st.get("output") or {}
            if isinstance(out, str):
                try:
                    out = json.loads(out)
                except json.JSONDecodeError:
                    out = {}
            if terminal != "COMPLETED" or not out.get("ok", True):
                err = out.get("error") if isinstance(out, dict) else None
                results.append(
                    {
                        "item_id": iid,
                        "job_id": jid,
                        "status": terminal.lower(),
                        "error": err or st.get("error"),
                    }
                )
                _append_manifest(
                    manifest,
                    {
                        "phase": "failed",
                        "item_id": iid,
                        "job_id": jid,
                        "runpod": st,
                    },
                )
                continue

            s3_uri = out.get("s3_uri")
            if not s3_uri:
                results.append(
                    {"item_id": iid, "job_id": jid, "status": "error", "error": "missing s3_uri"}
                )
                continue

            try:
                _require_s3()
                stats = import_transcript_from_s3(adapter, iid, str(s3_uri), replace=replace)
                results.append(
                    {
                        "item_id": iid,
                        "job_id": jid,
                        "status": "imported",
                        "s3_uri": s3_uri,
                        **stats,
                    }
                )
                _append_manifest(
                    manifest,
                    {
                        "phase": "imported",
                        "item_id": iid,
                        "job_id": jid,
                        "s3_uri": s3_uri,
                        **stats,
                    },
                )
                print_success(f"Imported {iid} ← {s3_uri}")
            except Exception as e:
                print_warning(f"Import failed for {iid}: {e}")
                results.append({"item_id": iid, "job_id": jid, "status": "import_error", "error": str(e)})

        if sync_turso:
            try:
                from stemmy_cli.adapters.connection import sync_to_turso

                sync_to_turso(replace=False)
                print_success("Turso sync completed")
            except Exception as e:
                print_warning(f"Turso sync skipped/failed: {e}")

        output_result(results, json_output=json_output, title="RunPod batch")

    except typer.Exit:
        raise
    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("cuda-healthcheck")
def cuda_healthcheck():
    """Print torch CUDA/cuDNN availability (for local debugging only)."""
    try:
        import torch

        print("cuda_available", torch.cuda.is_available())
        if torch.cuda.is_available():
            print("device", torch.cuda.get_device_name(0))
            print("cudnn", torch.backends.cudnn.version())
        elif os.getenv("RUNPOD_POD_HOSTNAME"):
            print_warning("No CUDA in this shell — expected only inside GPU worker")
    except ImportError:
        print_error("torch not installed in this environment")
