"""Stemmy management commands."""

import uuid
from pathlib import Path
from typing import Optional

import httpx
import typer

from stemmy_cli.adapters.sqlite import SQLiteAdapter
from stemmy_cli.adapters.http import HTTPAdapter
from stemmy_cli.audio.local_extract import extract_segments_local
from stemmy_cli.output import output_result, print_error, print_success, print_info

app = typer.Typer(help="Manage stemmies (audio compositions)")


def _download_audio(url: str, output_path: str) -> None:
    """Download audio from URL to local file."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    
    with httpx.Client(timeout=120.0) as client:
        response = client.get(url)
        response.raise_for_status()
        path.write_bytes(response.content)


@app.command("list")
def list_stemmies(
    limit: int = typer.Option(50, "--limit", "-l", help="Maximum results"),
    offset: int = typer.Option(0, "--offset", "-o", help="Offset for pagination"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """List all stemmies."""
    try:
        adapter = SQLiteAdapter()

        results = adapter.execute_raw(
            '''
            SELECT id, title, format, audio_url, duration, created_at
            FROM stemmies
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
            ''',
            [limit, offset],
        )

        output_result(
            results,
            json_output=json_output,
            columns=["id", "title", "format", "duration", "created_at"],
            title="Stemmies",
        )

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command()
def show(
    stemmy_id: str = typer.Argument(..., help="Stemmy ID"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Show details of a stemmy."""
    try:
        adapter = SQLiteAdapter()
        result = adapter.get_by_id("stemmies", stemmy_id)

        if not result:
            print_error(f"Stemmy {stemmy_id} not found")
            raise typer.Exit(1)

        output_result(result, json_output=json_output, title=f"Stemmy {stemmy_id}")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command()
def url(
    stemmy_id: str = typer.Argument(..., help="Stemmy ID"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Get the audio URL for a stemmy."""
    try:
        http = HTTPAdapter()
        result = http.get("/api/stemmie-url", params={"id": stemmy_id})

        if json_output:
            output_result(result, json_output=True)
        else:
            url = result.get("url") or result.get("audio_url")
            if url:
                typer.echo(url)
            else:
                print_error("No URL found")
                raise typer.Exit(1)

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("generate-sentence")
def generate_sentence(
    text: str = typer.Argument(..., help="Text to synthesize"),
    voice_id: str = typer.Option(..., "--voice", "-v", help="Voice ID"),
    stability: float = typer.Option(0.3, "--stability", help="Voice stability (0-1)"),
    similarity_boost: float = typer.Option(0.98, "--similarity", help="Similarity boost (0-1)"),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Wait for generation to complete"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Generate a single sentence with TTS."""
    try:
        payload = {
            "text": text,
            "voice_id": voice_id,
            "stability": stability,
            "similarity_boost": similarity_boost,
        }

        if wait:
            print_info("Generating sentence...")
            result = http.start_and_wait(
                "/api/generate-sentence",
                payload,
                "/api/sentence-status/{task_id}",
                verbose=True,
            )
        else:
            result = http.post("/api/generate-sentence", json=payload)
            print_success(f"Generation started: task_id={result.get('task_id')}")

        output_result(result, json_output=json_output, title="Generation result")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("generate-complete")
def generate_complete(
    stemmy_id: str = typer.Argument(..., help="Stemmy ID"),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Wait for generation to complete"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Generate complete audio for a stemmy."""
    try:
        http = HTTPAdapter()

        payload = {"stemmy_id": stemmy_id}

        if wait:
            print_info("Generating complete stemmy...")
            result = http.start_and_wait(
                "/api/generate-complete",
                payload,
                "/api/generate-complete/{task_id}",
                poll_interval=3.0,
                max_wait=600.0,
                verbose=True,
            )
        else:
            result = http.post("/api/generate-complete", json=payload)
            print_success(f"Generation started: task_id={result.get('task_id')}")

        output_result(result, json_output=json_output, title="Generation result")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("generate-multi")
def generate_multi(
    stemmy_id: str = typer.Argument(..., help="Stemmy ID"),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Wait for generation to complete"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Generate multiple voices for a stemmy."""
    try:
        http = HTTPAdapter()

        payload = {"stemmy_id": stemmy_id}

        if wait:
            print_info("Generating multiple voices...")
            result = http.start_and_wait(
                "/api/generate-multiple-voices",
                payload,
                "/api/task-status/{task_id}",
                poll_interval=3.0,
                max_wait=600.0,
                verbose=True,
            )
        else:
            result = http.post("/api/generate-multiple-voices", json=payload)
            print_success(f"Generation started: task_id={result.get('task_id')}")

        output_result(result, json_output=json_output, title="Generation result")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("import-fragments")
def import_fragments(
    stemmy_id: str = typer.Argument(..., help="Target stemmy ID"),
    fragment_ids: str = typer.Argument(..., help="Comma-separated fragment IDs"),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Wait for extraction to complete"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Import fragments into a stemmy by extracting their audio."""
    try:
        http = HTTPAdapter()

        ids = [fid.strip() for fid in fragment_ids.split(",")]

        adapter = SQLiteAdapter()
        segments = []
        for fid in ids:
            frag = adapter.get_by_id("fragments", fid)
            if frag:
                segments.append({
                    "id": fid,
                    "item_id": frag.get("item_id"),
                    "start_time": frag.get("start_time"),
                    "end_time": frag.get("end_time"),
                    "text": frag.get("text"),
                    "audio_url": frag.get("item_audio_url") or frag.get("source_audio_url") or "",
                })

        if not segments:
            print_error("No valid fragments found")
            raise typer.Exit(1)

        print_info(f"Extracting audio for {len(segments)} fragments locally...")
        extracted, failed = extract_segments_local(segments)
        result = {
            "stemmy_id": stemmy_id,
            "segments": extracted,
            "failed": failed,
            "stats": {"extracted": len(extracted), "failed": len(failed)},
        }

        output_result(result, json_output=json_output, title="Import result")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("magic-fragments")
def magic_fragments(
    stemmy_id: str = typer.Argument(..., help="Stemmy ID"),
    prompt: str = typer.Argument(..., help="Prompt describing what fragments to generate"),
    count: int = typer.Option(5, "--count", "-c", help="Number of fragments to generate"),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Wait for generation to complete"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Generate magic fragments for a stemmy using AI."""
    try:
        http = HTTPAdapter()

        payload = {
            "stemmy_id": stemmy_id,
            "prompt": prompt,
            "count": count,
        }

        if wait:
            print_info("Generating magic fragments...")
            result = http.start_and_wait(
                "/api/generate-magic-fragments",
                payload,
                "/api/magic-fragments-status/{task_id}",
                poll_interval=3.0,
                max_wait=300.0,
                verbose=True,
            )
        else:
            result = http.post("/api/generate-magic-fragments", json=payload)
            print_success(f"Generation started: task_id={result.get('task_id')}")

        output_result(result, json_output=json_output, title="Magic fragments result")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command()
def generate(
    text: str = typer.Argument(..., help="Text to convert to audio (or use --fragments)"),
    voice: str = typer.Option(..., "--voice", "-v", help="Voice ID"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Save audio to local file"),
    title: Optional[str] = typer.Option(None, "--title", "-t", help="Stemmy title"),
    format_id: Optional[str] = typer.Option(None, "--format", "-f", help="Format/show ID"),
    provider: str = typer.Option("elevenlabs", "--provider", "-p", help="TTS provider"),
    stability: float = typer.Option(0.3, "--stability", help="Voice stability (0-1)"),
    similarity: float = typer.Option(0.98, "--similarity", help="Voice similarity (0-1)"),
    with_intro: Optional[str] = typer.Option(None, "--intro", help="Intro sound URL"),
    with_outro: Optional[str] = typer.Option(None, "--outro", help="Outro sound URL"),
    with_background: Optional[str] = typer.Option(None, "--background", help="Background music URL"),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Wait for generation"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Generate a complete stemmy from text with TTS."""
    import json
    
    try:
        http = HTTPAdapter(timeout=120.0)
        idempotency_key = str(uuid.uuid4())

        fragment = {
            "id": str(uuid.uuid4()),
            "type": "sentence",
            "text": text,
            "voiceId": voice,
            "provider": provider,
            "ttsProvider": provider,
            "isGenerated": False,
        }

        voicesettings = json.dumps({
            "stability": stability,
            "similarity_boost": similarity,
            "style": 0.5,
            "use_speaker_boost": True,
        })

        payload = {
            "fragments": [fragment],
            "showformat": format_id or "cli",
            "voicesettings": voicesettings,
            "intro": {"audio_url": with_intro} if with_intro else {},
            "outro": {"audio_url": with_outro} if with_outro else {},
            "bgaudio": {"audio_url": with_background} if with_background else {},
        }

        if title:
            payload["title"] = title

        if wait:
            print_info(f"Generating stemmy...")
            result = http.start_and_wait(
                "/api/generate-multiple-voices",
                payload,
                "/api/audio-status/{task_id}",
                poll_interval=2.0,
                max_wait=300.0,
                verbose=True,
                headers={"Idempotency-Key": idempotency_key}
            )
        else:
            result = http.post(
                "/api/generate-multiple-voices", 
                json=payload,
                headers={"Idempotency-Key": idempotency_key}
            )
            print_success(f"Generation started: task_id={result.get('task_id')}")
            output_result(result, json_output=json_output, title="Generation started")
            return

        inner_result = result.get("result", {})
        if isinstance(inner_result, str):
            inner_result = json.loads(inner_result)
        
        audio_url = (
            result.get("audio_url") or 
            result.get("audioUrl") or 
            inner_result.get("audio_file") or
            inner_result.get("audio_url")
        )
        
        if audio_url and output:
            print_info("Downloading audio...")
            _download_audio(audio_url, output)
            print_success(f"Audio saved to: {output}")

        output_result(result, json_output=json_output, title="Stemmy generated")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command()
def download(
    stemmy_id: str = typer.Argument(..., help="Stemmy ID"),
    output: str = typer.Option(..., "--output", "-o", help="Output file path"),
):
    """Download a stemmy's audio to a local file."""
    try:
        adapter = SQLiteAdapter()
        stemmy = adapter.get_by_id("stemmies", stemmy_id)

        if not stemmy:
            print_error(f"Stemmy {stemmy_id} not found")
            raise typer.Exit(1)

        audio_url = stemmy.get("audio_url")
        if not audio_url:
            print_error(f"Stemmy {stemmy_id} has no audio URL")
            raise typer.Exit(1)

        print_info(f"Downloading audio from: {audio_url[:60]}...")
        _download_audio(audio_url, output)
        print_success(f"Audio saved to: {output}")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)
