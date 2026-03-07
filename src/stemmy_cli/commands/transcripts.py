"""Transcript management commands."""

import json
import uuid
from datetime import datetime
from typing import Optional, Dict, Any, List

import typer

from stemmy_cli.adapters.sqlite import SQLiteAdapter
from stemmy_cli.adapters.http import HTTPAdapter
from stemmy_cli.output import output_result, print_error, print_success, print_info, print_warning

app = typer.Typer(help="Manage transcripts")


def _import_transcript_to_db(
    adapter: SQLiteAdapter,
    item_id: str,
    audio_url: str,
    transcript_data: Dict[str, Any],
) -> Dict[str, int]:
    """Import transcript utterances and entities to SQLite database."""
    utterances = transcript_data.get("utterances", [])
    entities = transcript_data.get("entities", [])
    
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
            adapter.insert("fragments", fragment_data)
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
            adapter.insert("entities", entity_data)
            entities_imported += 1
        except Exception as e:
            print_warning(f"Failed to insert entity: {e}")
    
    return {
        "fragments_imported": fragments_imported,
        "entities_imported": entities_imported,
    }


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

        payload = {
            "audio_url": audio_url,
            "item_id": item_id,
            "options": {
                "language_code": language,
                "speaker_labels": speaker_labels,
                "entity_detection": entity_detection,
            }
        }

        if wait:
            print_info("Waiting for transcription (this may take several minutes)...")
            result = http.start_and_wait(
                "/api/formats/transcribe",
                payload,
                "/api/formats/transcription-status/{task_id}",
                poll_interval=10.0,
                max_wait=1800.0,
                verbose=True,
            )
            
            adapter.update("items", item_id, {"transcript_status": "completed"})
        else:
            result = http.post("/api/formats/transcribe", json=payload)
            transcript_id = result.get("transcript_id") or result.get("id") or result.get("task_id")
            if transcript_id:
                print_success(f"Transcription started: {transcript_id}")
                print_info(f"Check status with: stemmy transcripts status {transcript_id}")
                
                adapter.update("items", item_id, {"transcript_status": "processing"})

        output_result(result, json_output=json_output, title="Transcription result")

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
        adapter = SQLiteAdapter()
        http = HTTPAdapter(timeout=60.0)
        
        item = adapter.get_by_id("items", item_id)
        if not item:
            print_error(f"Item {item_id} not found")
            raise typer.Exit(1)
        
        audio_url = item.get("audio_url", "")
        
        print_info(f"Fetching transcript {transcript_id}...")
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
