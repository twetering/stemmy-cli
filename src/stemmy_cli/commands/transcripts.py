"""Transcript management commands."""

import json
import sqlite3
import tempfile
import uuid
from datetime import datetime
from typing import Optional, Dict, Any, List

import typer

from stemmy_cli.adapters.sqlite import SQLiteAdapter
from stemmy_cli.adapters.http import HTTPAdapter
from stemmy_cli.transcribe import transcribe_item
from stemmy_cli.output import output_result, print_error, print_success, print_info, print_warning

app = typer.Typer(help="Manage transcripts")


def _import_transcript_to_db(
    adapter: SQLiteAdapter,
    item_id: str,
    audio_url: str,
    transcript_data: Dict[str, Any],
    connection: Optional[sqlite3.Connection] = None,
) -> Dict[str, int]:
    """Import transcript utterances (fragments) and entities to SQLite database."""
    # Surrounded returns nested transcript_data when completed
    data = transcript_data.get("transcript_data", transcript_data)
    utterances = data.get("utterances", [])
    entities = data.get("entities", [])
    
    fragments_imported = 0
    entities_imported = 0
    
    now = datetime.utcnow().isoformat()
    
    for idx, utterance in enumerate(utterances):
        fragment_id = str(uuid.uuid4())
        
        start_time = utterance.get("start", 0)
        end_time = utterance.get("end", 0)
        if start_time > 1000:
            start_seconds = start_time / 1000.0
            end_seconds = end_time / 1000.0
        else:
            start_seconds = start_time
            end_seconds = end_time
        
        fragment_data = {
            "id": fragment_id,
            "item_id": item_id,
            "text": utterance.get("text", ""),
            "start_time_seconds": str(start_seconds),
            "end_time_seconds": str(end_seconds),
            "start_time": start_time,
            "end_time": end_time,
            "speaker_label": utterance.get("speaker", "A"),
            "confidence": str(utterance.get("confidence", 0)),
            "words": json.dumps(utterance.get("words", [])),
            "type": "transcript",
            "source_type": "utterances",
            "item_audio_url": audio_url,
            "order_index": str(idx),
            "created_at": now,
            "updated_at": now,
            "metadata": json.dumps({
                "source_info": {
                    "type": "utterances",
                    "imported_at": now,
                    "total_fragments": len(utterances)
                }
            }),
        }
        
        try:
            adapter.insert("fragments", fragment_data, connection=connection)
            fragments_imported += 1
        except Exception as e:
            print_warning(f"Failed to insert fragment: {e}")
    
    for entity in entities:
        entity_id = str(uuid.uuid4())
        
        start_time = entity.get("start", 0)
        end_time = entity.get("end", 0)
        
        entity_data = {
            "id": entity_id,
            "item_id": item_id,
            "text": entity.get("text", ""),
            "entity_type": entity.get("entity_type", "unknown"),
            "start_time": start_time,
            "end_time": end_time,
            "confidence": str(entity.get("confidence", 0)),
            "source_audio_url": audio_url,
            "created_at": now,
            "updated_at": now,
        }
        
        try:
            adapter.insert("entities", entity_data, connection=connection)
            entities_imported += 1
        except Exception as e:
            print_warning(f"Failed to insert entity: {e}")
    
    return {
        "fragments_imported": fragments_imported,
        "entities_imported": entities_imported,
    }


def _clear_item_transcript_rows(
    adapter: SQLiteAdapter,
    item_id: str,
    connection: Optional[sqlite3.Connection] = None,
) -> None:
    adapter.execute_raw('DELETE FROM "fragments" WHERE "item_id" = ?', [item_id], connection=connection)
    adapter.execute_raw('DELETE FROM "entities" WHERE "item_id" = ?', [item_id], connection=connection)


@app.command("list")
def list_transcripts(
    format_id: Optional[str] = typer.Option(None, "--format", "-f", help="Filter by format ID"),
    limit: int = typer.Option(50, "--limit", "-l", help="Maximum results"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """List transcripts."""
    try:
        http = HTTPAdapter()

        params = {"limit": limit}
        if format_id:
            params["format_id"] = format_id

        result = http.get("/api/formats/transcripts", params=params)

        transcripts = result.get("transcripts", result) if isinstance(result, dict) else result

        output_result(
            transcripts,
            json_output=json_output,
            columns=["id", "item_id", "status", "created_at"],
            title="Transcripts",
        )

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command()
def create(
    item_id: str = typer.Argument(..., help="Item ID to transcribe"),
    language: str = typer.Option("nl", "--language", "-l", help="Language code"),
    speaker_labels: bool = typer.Option(True, "--speakers/--no-speakers", help="Enable speaker diarization"),
    entity_detection: bool = typer.Option(True, "--entities/--no-entities", help="Enable entity detection"),
    wait: bool = typer.Option(False, "--wait/--no-wait", help="Wait for transcription to complete"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Create a new transcript for an item."""
    try:
        adapter = SQLiteAdapter()
        item = adapter.get_by_id("items", item_id)
        
        if not item:
            print_error(f"Item {item_id} not found")
            raise typer.Exit(1)
        
        audio_url = item.get("audio_url")
        if not audio_url:
            print_error(f"Item {item_id} has no audio URL")
            raise typer.Exit(1)
        
        print_info(f"Starting transcription for: {item.get('title', item_id)}")
        print_info(f"Audio URL: {audio_url}")

        http = HTTPAdapter(timeout=60.0)
        options = {
            "language_code": language,
            "speaker_labels": speaker_labels,
            "entity_detection": entity_detection,
        }

        if wait:
            print_info("Waiting for transcription (this may take several minutes)...")

        transcript_id, status = transcribe_item(
            audio_url,
            item_id,
            item.get("title", item_id),
            options,
            wait=wait,
            http=http,
        )

        if wait and status:
            import_result = _import_transcript_to_db(adapter, item_id, audio_url, status)
            adapter.update("items", item_id, {"transcript_status": "completed"})
            result = {
                "transcript_id": transcript_id,
                "status": "completed",
                "fragments_imported": import_result["fragments_imported"],
                "entities_imported": import_result["entities_imported"],
            }
            print_success(f"Imported {import_result['fragments_imported']} fragments and {import_result['entities_imported']} entities")
        elif transcript_id:
            adapter.update("items", item_id, {"transcript_status": "processing"})
            result = {"transcript_id": transcript_id, "status": "started"}
            print_success(f"Transcription started: {transcript_id}")
            print_info(f"Check status with: stemmy transcripts status {transcript_id}")
        else:
            result = {"status": "error"}

        output_result(result, json_output=json_output, title="Transcription result")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("transcribe-local")
def transcribe_local(
    item_id: Optional[str] = typer.Argument(
        None,
        help="Single item id (episode UUID). Omit when using --format.",
    ),
    format_id: Optional[str] = typer.Option(
        None,
        "--format",
        "-f",
        help="Transcribe pending items for this podcast format id",
    ),
    limit: int = typer.Option(
        10,
        "--limit",
        help="With --format: max episodes to process (default 10)",
    ),
    model: str = typer.Option("large-v2", "--model", "-m", help="Whisper model name (e.g. large-v2, large-v3, medium)"),
    language: str = typer.Option("nl", "--language", help="ISO language code for ASR + alignment"),
    device: str = typer.Option(
        "cpu",
        "--device",
        help="Inference device: cpu (default on Mac), cuda, or mps (align/diarize only; ASR via faster-whisper)",
    ),
    compute_type: str = typer.Option(
        "int8",
        "--compute-type",
        help="faster-whisper compute type: int8 on Mac CPU; float16 on GPU",
    ),
    batch_size: int = typer.Option(8, "--batch-size", "-b", help="WhisperX ASR batch size"),
    diarize: bool = typer.Option(
        True,
        "--diarize/--no-diarize",
        help="Speaker diarization (requires HF_TOKEN and Hugging Face model access)",
    ),
    min_speakers: Optional[int] = typer.Option(None, "--min-speakers"),
    max_speakers: Optional[int] = typer.Option(None, "--max-speakers"),
    replace: bool = typer.Option(
        False,
        "--replace",
        help="Remove existing fragments and entities for the item(s) before import",
    ),
    progress: bool = typer.Option(False, "--progress", "-p", help="Print WhisperX progress"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """
    Transcribe using WhisperX locally (word timings + optional diarization), then import fragments.

    On Apple Silicon, use --device cpu --compute-type int8 (see WhisperX README).

    Install: pip install 'stemmy-cli[whisperx]'. Set HF_TOKEN (pyannote VAD + diarization; accept model terms on HF).
    """
    from stemmy_cli.storage.whisperx_local import (
        WhisperXRunner,
        WhisperXRunnerConfig,
        download_audio_to_file,
        is_whisperx_available,
        whisperx_import_hint,
    )

    if not is_whisperx_available():
        print_error(whisperx_import_hint())
        raise typer.Exit(1)

    if (item_id is None) == (format_id is None):
        print_error("Provide exactly one of: ITEM_ID (argument) or --format FORMAT_ID")
        raise typer.Exit(1)

    try:
        adapter = SQLiteAdapter()
        items_queue: List[Dict[str, Any]] = []

        if item_id:
            row = adapter.get_by_id("items", item_id)
            if not row:
                print_error(f"Item {item_id} not found")
                raise typer.Exit(1)
            items_queue = [row]
        else:
            rows = adapter.execute_raw(
                """
                SELECT id, title, audio_url, transcript_status
                FROM items
                WHERE format_id = ?
                  AND (transcript_status IS NULL OR transcript_status = 'pending' OR transcript_status = '')
                ORDER BY datetime(published_at) DESC
                LIMIT ?
                """,
                [format_id, limit],
            )
            if not rows:
                print_info("No pending items for this format (or invalid format id)")
                raise typer.Exit(0)
            items_queue = rows

        cfg = WhisperXRunnerConfig(
            model=model,
            language=language,
            device=device,
            compute_type=compute_type,
            batch_size=batch_size,
            diarize=diarize,
            min_speakers=min_speakers,
            max_speakers=max_speakers,
        )

        print_info(
            f"Loading WhisperX ({model}, device={device}, diarize={diarize}) — first load downloads weights…"
        )
        runner = WhisperXRunner(cfg)
        results: List[Dict[str, Any]] = []

        try:
            for row in items_queue:
                iid = row["id"]
                audio_url = row.get("audio_url") or ""
                title = row.get("title", iid)

                if not audio_url:
                    print_warning(f"Skip (no audio_url): {title}")
                    results.append({"item_id": iid, "status": "skipped", "reason": "no audio_url"})
                    continue

                if not replace:
                    frag_n = adapter.count("fragments", where={"item_id": iid})
                    if frag_n > 0:
                        print_info(f"Skip (already {frag_n} fragments): {title[:60]}…")
                        results.append({"item_id": iid, "status": "skipped", "reason": "has_fragments"})
                        continue

                if replace:
                    _clear_item_transcript_rows(adapter, iid)
                    adapter.update(
                        "items",
                        iid,
                        {
                            "transcript_status": "pending",
                            "transcript_data": None,
                        },
                    )

                print_info(f"Transcribing: {title[:70]}…")
                with tempfile.TemporaryDirectory(prefix="stemmy_whisperx_") as tmp:
                    path = download_audio_to_file(audio_url, tmp)
                    status = runner.transcribe_file(str(path), print_progress=progress)

                if status.get("status") != "completed":
                    print_error(f"WhisperX did not complete for {title}")
                    results.append({"item_id": iid, "status": "error"})
                    continue

                audio_url = row.get("audio_url") or ""
                import_result = _import_transcript_to_db(adapter, iid, audio_url, status)
                td = status.get("transcript_data", {})
                adapter.update(
                    "items",
                    iid,
                    {
                        "transcript_status": "completed",
                        "transcript_data": json.dumps(td) if td else None,
                    },
                )
                print_success(
                    f"Imported {import_result['fragments_imported']} fragments "
                    f"({import_result['entities_imported']} entities) for {title[:50]}…"
                )
                results.append({"item_id": iid, "status": "completed", **import_result})
        finally:
            runner.release()

        output_result(results, json_output=json_output, title="Local transcription results")

    except ValueError as e:
        print_error(str(e))
        raise typer.Exit(1)
    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command()
def status(
    transcript_id: str = typer.Argument(..., help="Transcript ID"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Check the status of a transcript."""
    try:
        from stemmy_cli.storage.assemblyai import is_assemblyai_configured, get_transcription_status

        if is_assemblyai_configured():
            result = get_transcription_status(transcript_id)
        else:
            http = HTTPAdapter()
            result = http.get(f"/api/formats/transcription-status/{transcript_id}")

        output_result(result, json_output=json_output, title=f"Transcript {transcript_id} status")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command()
def get(
    transcript_id: str = typer.Argument(..., help="Transcript ID"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Get a transcript's full content."""
    try:
        http = HTTPAdapter()
        result = http.get(f"/api/formats/transcripts/{transcript_id}")

        output_result(result, json_output=json_output, title=f"Transcript {transcript_id}")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command()
def delete(
    transcript_id: str = typer.Argument(..., help="Transcript ID"),
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Delete a transcript."""
    try:
        if not force:
            confirm = typer.confirm(f"Delete transcript {transcript_id}?")
            if not confirm:
                raise typer.Abort()

        http = HTTPAdapter()
        result = http.delete(f"/api/formats/transcripts/{transcript_id}")

        print_success(f"Transcript {transcript_id} deleted")
        output_result(result, json_output=json_output)

    except typer.Abort:
        print_info("Cancelled")
        raise typer.Exit(0)
    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command()
def refresh(
    transcript_id: str = typer.Argument(..., help="Transcript ID"),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Wait for refresh to complete"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Refresh a transcript from AssemblyAI."""
    try:
        http = HTTPAdapter()

        if wait:
            print_info("Refreshing transcript...")
            result = http.start_and_wait(
                f"/api/formats/transcription-refresh/{transcript_id}",
                {},
                "/api/task-status/{task_id}",
                verbose=True,
            )
        else:
            result = http.post(f"/api/formats/transcription-refresh/{transcript_id}", json={})
            print_success(f"Refresh started")

        output_result(result, json_output=json_output, title="Refresh result")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command()
def lemur(
    format_id: str = typer.Argument(..., help="Format ID"),
    item_id: str = typer.Argument(..., help="Item ID"),
    prompt: str = typer.Argument(..., help="Question/prompt for Lemur"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Query a transcript using AssemblyAI Lemur."""
    try:
        http = HTTPAdapter()

        result = http.post(
            f"/api/formats/{format_id}/items/{item_id}/lemur",
            json={"prompt": prompt},
        )

        output_result(result, json_output=json_output, title="Lemur response")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("import")
def import_transcript(
    transcript_id: str = typer.Argument(..., help="Transcript ID from AssemblyAI"),
    item_id: str = typer.Argument(..., help="Item ID to associate fragments with"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Import a completed transcript's fragments and entities into the database."""
    try:
        from stemmy_cli.storage.assemblyai import is_assemblyai_configured, get_transcription_status

        adapter = SQLiteAdapter()
        item = adapter.get_by_id("items", item_id)
        if not item:
            print_error(f"Item {item_id} not found")
            raise typer.Exit(1)

        audio_url = item.get("audio_url", "")

        print_info(f"Fetching transcript {transcript_id}...")
        if is_assemblyai_configured():
            status = get_transcription_status(transcript_id)
        else:
            http = HTTPAdapter(timeout=60.0)
            status = http.get(f"/api/formats/transcription-status/{transcript_id}")
        
        if status.get("status") != "completed":
            print_error(f"Transcript not complete. Status: {status.get('status')}")
            output_result(status, json_output=json_output, title="Transcript status")
            raise typer.Exit(1)
        
        transcript_data = status
        
        if "utterances" not in transcript_data and "words" not in transcript_data:
            print_info("Fetching full transcript content...")
            try:
                full_data = http.get(f"/api/formats/transcripts/{transcript_id}")
                transcript_data = full_data
            except Exception:
                pass
        
        print_info(f"Importing utterances and entities for item {item_id}...")
        result = _import_transcript_to_db(adapter, item_id, audio_url, transcript_data)
        
        adapter.update("items", item_id, {"transcript_status": "completed"})
        
        print_success(f"Imported {result['fragments_imported']} fragments and {result['entities_imported']} entities")
        output_result(result, json_output=json_output, title="Import result")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)
