"""Reference benchmarks (WhisperX vs database transcripts)."""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional

import typer

from stemmy_cli.adapters.sqlite import SQLiteAdapter
from stemmy_cli.audio.local_extract import extract_segment_local
from stemmy_cli.output import output_result, print_error, print_info, print_success
from stemmy_cli.storage import runpod_api

app = typer.Typer(help="Benchmark local transcription against DB reference data")


@app.command("whisperx")
def benchmark_whisperx(
    limit: int = typer.Option(100, "--limit", "-n", help="Number of random fragments with word data"),
    format_id: Optional[str] = typer.Option(
        None,
        "--format",
        "-f",
        help="Restrict to fragments from items in this format (optional)",
    ),
    models: str = typer.Option(
        "large-v2,medium",
        "--models",
        "-m",
        help="Comma-separated Whisper models to compare (loads each in turn)",
    ),
    min_duration: float = typer.Option(
        0.8,
        "--min-duration",
        help="Skip fragments shorter than this (seconds)",
    ),
    max_duration: float = typer.Option(
        90.0,
        "--max-duration",
        help="Skip fragments longer than this (seconds)",
    ),
    device: str = typer.Option("cpu", "--device", help="WhisperX device (cpu on Mac)"),
    compute_type: str = typer.Option("int8", "--compute-type"),
    batch_size: int = typer.Option(8, "--batch-size", "-b"),
    diarize: bool = typer.Option(
        True,
        "--diarize/--no-diarize",
        help="Run pyannote diarization (recommended; needs HF_TOKEN)",
    ),
    output: Optional[Path] = typer.Option(
        None,
        "--output",
        "-o",
        help="Write JSON report to this path",
    ),
    progress: bool = typer.Option(False, "--progress", "-p", help="WhisperX per-file progress"),
):
    """
    Compare WhisperX to existing fragment transcripts (text + words) from the database.

    Uses ffmpeg to cut each fragment's time range from item_audio_url (correct for long episodes).
    Reports mean WER/CER (jiwer) and realtime factor per model. HF_TOKEN / HF_API_KEY for diarization.
    """
    from stemmy_cli.benchmarks.whisperx_reference import (
        eval_clips_with_runner,
        fetch_fragment_benchmark_set,
        summarize_results,
    )
    from stemmy_cli.storage.whisperx_local import (
        WhisperXRunner,
        WhisperXRunnerConfig,
        is_whisperx_available,
        whisperx_import_hint,
    )

    try:
        import jiwer  # noqa: F401
    except ImportError:
        print_error("Install jiwer: pip install jiwer  (or pip install 'stemmy-cli[whisperx]')")
        raise typer.Exit(1)

    if not is_whisperx_available():
        print_error(whisperx_import_hint())
        raise typer.Exit(1)

    model_list = [x.strip() for x in models.split(",") if x.strip()]
    if not model_list:
        print_error("No models parsed from --models")
        raise typer.Exit(1)

    adapter = SQLiteAdapter()
    print_info(f"Sampling up to {limit} fragments (words + audio + {min_duration}s–{max_duration}s)…")
    rows = fetch_fragment_benchmark_set(
        adapter,
        limit=limit,
        format_id=format_id,
        min_duration_sec=min_duration,
        max_duration_sec=max_duration,
    )
    if not rows:
        print_error("No matching fragments. Check DB path (STEMMY_DB_PATH) and filters.")
        raise typer.Exit(1)

    print_success(f"Benchmark set: {len(rows)} fragments")

    report: dict = {
        "limit_requested": limit,
        "fragments_used": len(rows),
        "fragment_ids": [r.fragment_id for r in rows],
        "models": {},
    }

    for model_name in model_list:
        print_info(f"=== Model: {model_name} (diarize={diarize}) ===")
        cfg = WhisperXRunnerConfig(
            model=model_name,
            language="nl",
            device=device,
            compute_type=compute_type,
            batch_size=batch_size,
            diarize=diarize,
        )
        runner = WhisperXRunner(cfg)
        try:

            def _run(r, path: str, pr: bool):
                return r.transcribe_file(path, print_progress=pr)

            results = eval_clips_with_runner(rows, runner, _run)
        finally:
            runner.release()

        summary = summarize_results(results)
        report["models"][model_name] = {
            "summary": summary,
            "per_clip": [r.__dict__ for r in results],
        }

        print_success(
            f"{model_name}: mean WER={summary.get('mean_wer'):.4f}  "
            f"mean CER={summary.get('mean_cer'):.4f}  "
            f"wall RTF≈{summary.get('wall_rtf', summary.get('mean_rtf')):.2f}x  "
            f"(valid {summary.get('valid')}/{summary.get('clips')})"
        )

    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print_info(f"Wrote {output}")

    # Console summary table via output_result
    console_summary = {
        name: report["models"][name]["summary"]
        for name in model_list
        if name in report["models"]
    }
    output_result(console_summary, json_output=False, title="WhisperX benchmark (mean WER / wall RTF)")


@app.command("runpod-episode")
def benchmark_runpod_episode(
    item_id: Optional[str] = typer.Option(
        None,
        "--item-id",
        help="Episode UUID (omit to auto-pick longest completed + S3 audio)",
    ),
    min_minutes: float = typer.Option(
        30.0,
        "--min-minutes",
        help="When auto-picking: minimum item.duration_seconds (minutes)",
    ),
    submit: bool = typer.Option(
        False,
        "--submit",
        help="Submit RunPod job and wait (needs RUNPOD_API_KEY, RUNPOD_ENDPOINT_ID, S3 output env)",
    ),
    s3_uri: Optional[str] = typer.Option(
        None,
        "--s3-uri",
        help="Evaluate existing worker JSON at this s3:// URI (no GPU run)",
    ),
    language: str = typer.Option("nl", "--language"),
    model: str = typer.Option("large-v2", "--model", "-m"),
    diarize: bool = typer.Option(True, "--diarize/--no-diarize"),
    poll_interval: float = typer.Option(3.0, "--poll-interval"),
    timeout: float = typer.Option(14400.0, "--timeout"),
    json_output: bool = typer.Option(False, "--json", "-j"),
):
    """
    Compare full-episode RunPod WhisperX output to SQLite reference (completed transcript / fragments).

    Reference text prefers ordered fragments; falls back to items.transcript_data utterances.
    """
    try:
        import jiwer  # noqa: F401
    except ImportError:
        print_error("Install jiwer: pip install jiwer  (or pip install 'stemmy-cli[runpod]')")
        raise typer.Exit(1)

    from stemmy_cli.benchmarks.runpod_episode import (
        eval_hypothesis_vs_reference,
        pick_long_completed_item,
        reference_text_from_fragments,
        reference_text_from_item_column,
        run_runpod_and_eval,
    )
    from stemmy_cli.storage.s3 import download_transcript_json_from_s3_uri, is_s3_configured

    adapter = SQLiteAdapter()

    pick_id = item_id
    if not pick_id:
        cands = pick_long_completed_item(
            adapter, min_duration_seconds=min_minutes * 60.0, limit=1
        )
        if not cands:
            print_error(
                f"No items with transcript_status=completed, S3-like audio_url, "
                f"duration >= {min_minutes} min."
            )
            raise typer.Exit(1)
        pick_id = cands[0].item_id
        print_info(f"Auto-picked item {pick_id} ({cands[0].title[:60]}…)")

    ref = reference_text_from_fragments(adapter, pick_id)
    if not ref.strip():
        ref = reference_text_from_item_column(adapter, pick_id) or ""
    if not ref.strip():
        print_error("No reference text (fragments or items.transcript_data).")
        raise typer.Exit(1)

    row = adapter.get_by_id("items", pick_id)
    audio_url = str(row.get("audio_url") or "") if row else ""
    if not audio_url and submit:
        print_error("Item has no audio_url; cannot submit RunPod job.")
        raise typer.Exit(1)

    try:
        if s3_uri:
            if not is_s3_configured():
                print_error("S3 credentials required for --s3-uri download")
                raise typer.Exit(1)
            payload = download_transcript_json_from_s3_uri(s3_uri)
            wer_v, cer_v, hyp, n_seg, n_spk = eval_hypothesis_vs_reference(ref, payload)
            out = {
                "item_id": pick_id,
                "source": "s3_uri",
                "s3_uri": s3_uri,
                "reference_chars": len(ref),
                "hypothesis_chars": len(hyp),
                "wer": wer_v,
                "cer": cer_v,
                "num_segments": n_seg,
                "num_speakers": n_spk,
            }
            output_result(out, json_output=json_output, title="RunPod episode benchmark (--s3-uri)")
            return

        if submit:
            print_info("Submitting RunPod job (this may take a long time)…")
            ev = run_runpod_and_eval(
                item_id=pick_id,
                audio_url=audio_url,
                reference_text=ref,
                language=language,
                model=model,
                diarize=diarize,
                wait=True,
                poll_interval_sec=poll_interval,
                timeout_sec=timeout,
            )
            output_result(ev, json_output=json_output, title="RunPod episode benchmark (submit)")
            return

        print_info(
            "No action: use --submit to run GPU job or --s3-uri to score an existing JSON. "
            f"Reference length={len(ref)} chars for item {pick_id}."
        )
        output_result(
            {"item_id": pick_id, "reference_chars": len(ref), "audio_url": audio_url},
            json_output=json_output,
            title="RunPod episode benchmark (dry run)",
        )
    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("fetch-runpod")
def fetch_runpod(
    item_id: str = typer.Argument(..., help="Item UUID"),
    s3_uri: Optional[str] = typer.Option(
        None, "--s3-uri", help="Override s3://bucket/key (default transcripts/runpod/{item_id}.json)"
    ),
    model: str = typer.Option("large-v3-turbo", "--model"),
    beam_size: int = typer.Option(1, "--beam-size"),
    compute_type: str = typer.Option("float16", "--compute-type"),
    json_output: bool = typer.Option(False, "--json", "-j"),
):
    """Fetch RunPod transcript JSON from S3 and store in benchmark_transcripts table."""
    from stemmy_cli.benchmarks.runpod_compare import (
        fetch_runpod_payload_for_item,
        insert_benchmark_transcript,
    )

    try:
        adapter = SQLiteAdapter()
        uri, payload = fetch_runpod_payload_for_item(item_id, s3_uri=s3_uri)
        bid = insert_benchmark_transcript(
            adapter,
            item_id=item_id,
            source="runpod",
            engine="faster-whisper-large-v3-turbo",
            model_params={
                "model": model,
                "beam_size": beam_size,
                "compute_type": compute_type,
            },
            transcript_payload=payload,
            s3_uri=uri,
        )
        out = {"benchmark_id": bid, "item_id": item_id, "s3_uri": uri}
        print_success(f"Stored benchmark transcript {bid}")
        output_result(out, json_output=json_output, title="RunPod transcript fetched")
    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("eval-timings")
def eval_timings(
    item_id: str = typer.Argument(..., help="Item UUID"),
    benchmark_id: Optional[str] = typer.Option(
        None, "--benchmark-id", help="Specific benchmark_transcripts.id (default latest runpod row)"
    ),
    json_output: bool = typer.Option(False, "--json", "-j"),
):
    """Evaluate latest RunPod transcript for item vs Assembly fragments (WER/CER + timing deltas)."""
    from stemmy_cli.benchmarks.runpod_compare import (
        evaluate_runpod_vs_assembly,
        latest_benchmark_row,
        materialize_alignment_rows,
        parse_transcript_json,
        save_eval_metrics,
    )

    try:
        adapter = SQLiteAdapter()
        row = None
        if benchmark_id:
            rows = adapter.execute_raw(
                "SELECT * FROM benchmark_transcripts WHERE id = ? LIMIT 1", [benchmark_id]
            )
            row = rows[0] if rows else None
        else:
            row = latest_benchmark_row(adapter, item_id, "runpod")
        if not row:
            print_error("No runpod benchmark row found. Run `stemmy benchmark fetch-runpod` first.")
            raise typer.Exit(1)

        payload = parse_transcript_json(row.get("transcript_json"))
        metrics = evaluate_runpod_vs_assembly(adapter, item_id=item_id, runpod_payload=payload)
        save_eval_metrics(adapter, row["id"], metrics)
        materialized = materialize_alignment_rows(
            adapter, benchmark_id=row["id"], item_id=item_id, runpod_payload=payload
        )
        out = {"benchmark_id": row["id"], **metrics, **materialized}
        output_result(out, json_output=json_output, title="RunPod timing eval")
    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("inspect-item")
def inspect_item(
    item_id: str = typer.Argument(..., help="Item UUID"),
    benchmark_id: Optional[str] = typer.Option(
        None, "--benchmark-id", help="Specific benchmark_transcripts.id (default latest runpod row)"
    ),
    mode: str = typer.Option(
        "utterance",
        "--mode",
        help="Compare mode: utterance (recommended first) or word",
    ),
    row_limit: int = typer.Option(120, "--row-limit", help="Max rows to show"),
    extract_snippets: int = typer.Option(
        0,
        "--extract-snippets",
        help="Extract top-N worst word-delta snippets for listening (Assembly + RunPod windows)",
    ),
    snippet_context_ms: int = typer.Option(120, "--snippet-context-ms", help="Context padding for snippets"),
    out_dir: Path = typer.Option(
        Path("results/benchmark_snippets"),
        "--out-dir",
        help="Snippet output directory",
    ),
    json_output: bool = typer.Option(False, "--json", "-j"),
):
    """CLI-first compare view: Assembly vs RunPod word timings (+ optional snippet extraction)."""
    from stemmy_cli.benchmarks.runpod_compare import (
        build_word_alignment_preview,
        latest_benchmark_row,
        load_utterance_pairs_preview,
        load_word_pairs_preview,
        materialize_alignment_rows,
        parse_transcript_json,
    )

    try:
        adapter = SQLiteAdapter()
        if benchmark_id:
            rows = adapter.execute_raw(
                "SELECT * FROM benchmark_transcripts WHERE id = ? LIMIT 1", [benchmark_id]
            )
            row = rows[0] if rows else None
        else:
            row = latest_benchmark_row(adapter, item_id, "runpod")
        if not row:
            print_error("No runpod benchmark row found. Run `stemmy benchmark fetch-runpod` first.")
            raise typer.Exit(1)

        item = adapter.get_by_id("items", item_id)
        if not item or not item.get("audio_url"):
            print_error("Item/audio_url not found for snippet extraction.")
            raise typer.Exit(1)
        audio_url = str(item["audio_url"])

        payload = parse_transcript_json(row.get("transcript_json"))
        # Keep alignment data stable: always materialize utterance-first pairs before previewing.
        materialize_alignment_rows(adapter, benchmark_id=row["id"], item_id=item_id, runpod_payload=payload)
        mode_norm = mode.strip().lower()
        if mode_norm == "utterance":
            preview = load_utterance_pairs_preview(adapter, row["id"], limit=max(1, row_limit))
        elif mode_norm == "word":
            preview = load_word_pairs_preview(adapter, row["id"], limit=max(1, row_limit))
        else:
            preview = build_word_alignment_preview(
                adapter,
                item_id=item_id,
                runpod_payload=payload,
                limit=max(1, row_limit),
            )

        snippets: List[dict] = []
        if extract_snippets > 0 and preview:
            ranked = sorted(preview, key=lambda r: abs(r["delta_start_ms"]), reverse=True)[:extract_snippets]
            target_dir = out_dir / item_id
            target_dir.mkdir(parents=True, exist_ok=True)
            pad = max(0, snippet_context_ms) / 1000.0
            for r in ranked:
                idx = r["idx"]
                a_start = max(0.0, float(r["assembly_start"]) - pad)
                a_end = float(r["assembly_end"]) + pad
                rp_start = max(0.0, float(r["runpod_start"]) - pad)
                rp_end = float(r["runpod_end"]) + pad

                a_path = target_dir / f"{idx:04d}_assembly.mp3"
                r_path = target_dir / f"{idx:04d}_runpod.mp3"

                a_ex = extract_segment_local(audio_url, a_start, a_end, output_path=a_path)
                r_ex = extract_segment_local(audio_url, rp_start, rp_end, output_path=r_path)
                snippets.append(
                    {
                        "idx": idx,
                        "delta_start_ms": r["delta_start_ms"],
                        "assembly_file": a_ex.get("output_path") if a_ex.get("success") else None,
                        "runpod_file": r_ex.get("output_path") if r_ex.get("success") else None,
                        "assembly_error": a_ex.get("error") if not a_ex.get("success") else None,
                        "runpod_error": r_ex.get("error") if not r_ex.get("success") else None,
                    }
                )

        out = {
            "benchmark_id": row["id"],
            "item_id": item_id,
            "mode": mode_norm,
            "rows": preview,
            "snippets": snippets,
        }
        output_result(out, json_output=json_output, title="Assembly vs RunPod inspect")
    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("eval-batch")
def eval_batch(
    limit: int = typer.Option(20, "--limit"),
    format_id: Optional[str] = typer.Option(None, "--format", "-f"),
    min_duration: float = typer.Option(20.0, "--min-duration", help="Seconds"),
    max_duration: float = typer.Option(600.0, "--max-duration", help="Seconds"),
    concurrency: int = typer.Option(6, "--concurrency", "-c"),
    model: str = typer.Option("large-v3-turbo", "--model", "-m"),
    beam_size: int = typer.Option(1, "--beam-size"),
    language: str = typer.Option("nl", "--language"),
    compute_type: str = typer.Option("float16", "--compute-type"),
    report_path: Path = typer.Option(Path("results/runpod_scale_quality_eval_cli.json"), "--report"),
    json_output: bool = typer.Option(False, "--json", "-j"),
):
    """Submit/eval a batch: RunPod speed + quality (WER/CER + timing deltas) vs Assembly fragments."""
    from stemmy_cli.benchmarks.runpod_compare import (
        evaluate_runpod_vs_assembly,
        insert_benchmark_transcript,
        materialize_alignment_rows,
        save_benchmark_report,
        save_eval_metrics,
    )

    adapter = SQLiteAdapter()
    where_format = "AND i.format_id = ?" if format_id else ""
    params: List[object] = [min_duration, max_duration]
    if format_id:
        params.append(format_id)
    params.append(limit)
    rows = adapter.execute_raw(
        f"""
        SELECT i.id, i.title, i.audio_url, CAST(COALESCE(i.duration_seconds, '0') AS REAL) AS dur
        FROM items i
        WHERE i.audio_url IS NOT NULL AND trim(i.audio_url) != ''
          AND i.transcript_status = 'completed'
          AND CAST(COALESCE(i.duration_seconds, '0') AS REAL) BETWEEN ? AND ?
          {where_format}
        ORDER BY RANDOM()
        LIMIT ?
        """,
        params,
    )
    if not rows:
        print_error("No candidate items found for batch eval")
        raise typer.Exit(1)

    ak = os.getenv("AWS_ACCESS_KEY_ID")
    sk = os.getenv("AWS_SECRET_ACCESS_KEY")
    rg = os.getenv("AWS_REGION")

    def _submit_wait(row: dict) -> dict:
        item_id = str(row["id"])
        payload = runpod_api.build_worker_input(
            item_id=item_id,
            audio_url=str(row["audio_url"]),
            language=language,
            model=model,
            diarize=False,
            compute_type=compute_type,
        )
        payload["beam_size"] = beam_size
        if ak:
            payload["output"]["aws_access_key_id"] = ak
        if sk:
            payload["output"]["aws_secret_access_key"] = sk
        if rg:
            payload["output"]["aws_region"] = rg

        t0 = time.time()
        submit = runpod_api.run_job_async(payload)
        job_id = str(submit.get("id") or "")
        terminal = runpod_api.wait_for_job(job_id, poll_interval_sec=2.0, timeout_sec=14400.0)
        status = (terminal.get("status") or "").upper()
        out = terminal.get("output") or {}
        if isinstance(out, str):
            try:
                out = json.loads(out)
            except json.JSONDecodeError:
                out = {}
        res: dict = {
            "item_id": item_id,
            "title": (row.get("title") or "")[:80],
            "duration_seconds": float(row.get("dur") or 0),
            "job_id": job_id,
            "status": status,
            "wall_sec": time.time() - t0,
            "error": terminal.get("error") or out.get("error"),
        }
        if status == "COMPLETED" and out.get("ok") and out.get("s3_uri"):
            from stemmy_cli.storage.s3 import download_transcript_json_from_s3_uri

            payload_json = download_transcript_json_from_s3_uri(str(out["s3_uri"]))
            bid = insert_benchmark_transcript(
                adapter,
                item_id=item_id,
                source="runpod",
                engine=f"faster-whisper-{model}",
                model_params={"beam_size": beam_size, "compute_type": compute_type, "language": language},
                transcript_payload=payload_json,
                s3_uri=str(out["s3_uri"]),
                seconds_audio=out.get("seconds_audio"),
                seconds_compute=out.get("seconds_compute"),
                job_id=job_id,
            )
            metrics = evaluate_runpod_vs_assembly(adapter, item_id=item_id, runpod_payload=payload_json)
            save_eval_metrics(adapter, bid, metrics)
            align_stats = materialize_alignment_rows(
                adapter, benchmark_id=bid, item_id=item_id, runpod_payload=payload_json
            )
            res.update(
                {
                    "benchmark_id": bid,
                    "s3_uri": out.get("s3_uri"),
                    "seconds_audio": out.get("seconds_audio"),
                    "seconds_compute": out.get("seconds_compute"),
                    "rtf": (
                        float(out["seconds_compute"]) / float(out["seconds_audio"])
                        if out.get("seconds_audio")
                        else None
                    ),
                    **align_stats,
                    **metrics,
                }
            )
        return res

    print_info(f"Submitting {len(rows)} items with concurrency={max(1, concurrency)} …")
    start = time.time()
    results: List[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        futs = [ex.submit(_submit_wait, r) for r in rows]
        for fut in as_completed(futs):
            res = fut.result()
            results.append(res)
            print_success(f"{res['item_id']} → {res['status']}")

    completed = [r for r in results if r.get("status") == "COMPLETED" and r.get("wer") is not None]
    total_audio = sum(float(r.get("duration_seconds") or 0) for r in results)
    wall = time.time() - start
    summary = {
        "total_items": len(results),
        "completed": len([r for r in results if r.get("status") == "COMPLETED"]),
        "failed": len([r for r in results if r.get("status") != "COMPLETED"]),
        "total_audio_sec": total_audio,
        "batch_wall_sec": wall,
        "aggregate_realtime_factor": (total_audio / wall) if wall > 0 else None,
        "quality_eval_count": len(completed),
        "mean_wer": (sum(float(r["wer"]) for r in completed) / len(completed)) if completed else None,
        "mean_cer": (sum(float(r["cer"]) for r in completed) / len(completed)) if completed else None,
        "mean_timing_end_abs_err_sec": (
            sum(float(r["timing_end_abs_err_sec"]) for r in completed if r.get("timing_end_abs_err_sec") is not None)
            / max(1, len([r for r in completed if r.get("timing_end_abs_err_sec") is not None]))
        )
        if completed
        else None,
    }
    report = {"summary": summary, "results": results}
    save_benchmark_report(report_path, report)
    print_info(f"Wrote {report_path}")
    output_result(report, json_output=json_output, title="RunPod batch eval")
