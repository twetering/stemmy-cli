"""Reference benchmarks (WhisperX vs database transcripts)."""

import json
from pathlib import Path
from typing import List, Optional

import typer

from stemmy_cli.adapters.sqlite import SQLiteAdapter
from stemmy_cli.output import output_result, print_error, print_info, print_success

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
