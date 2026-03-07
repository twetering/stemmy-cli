"""Voice management commands."""

from typing import Optional

import typer

from stemmy_cli.adapters.http import HTTPAdapter
from stemmy_cli.adapters.sqlite import SQLiteAdapter
from stemmy_cli.output import output_result, print_error, print_success, print_info

app = typer.Typer(help="Manage voices and cloning")


@app.command("list")
def list_voices(
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """List available voices."""
    try:
        http = HTTPAdapter()
        result = http.get("/api/voices")

        voices = result.get("voices", result) if isinstance(result, dict) else result

        if json_output:
            output_result(voices, json_output=True)
        else:
            voice_list = []
            for v in voices:
                voice_list.append({
                    "voice_id": v.get("voice_id"),
                    "name": v.get("name"),
                    "category": v.get("category", ""),
                    "labels": str(v.get("labels", {})),
                })
            output_result(
                voice_list,
                columns=["voice_id", "name", "category"],
                title="Available voices",
            )

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command()
def clone(
    name: str = typer.Argument(..., help="Name for the cloned voice"),
    audio_url: str = typer.Argument(..., help="URL to audio sample"),
    description: Optional[str] = typer.Option(None, "--description", "-d", help="Voice description"),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Wait for cloning to complete"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Clone a voice from an audio sample."""
    try:
        http = HTTPAdapter()

        payload = {
            "name": name,
            "audio_url": audio_url,
        }
        if description:
            payload["description"] = description

        if wait:
            print_info("Starting voice cloning...")
            result = http.start_and_wait(
                "/api/voices/clone",
                payload,
                "/api/voices/clone-status/{task_id}",
                poll_interval=3.0,
                max_wait=300.0,
                verbose=True,
            )
        else:
            result = http.post("/api/voices/clone", json=payload)
            print_success(f"Cloning started: task_id={result.get('task_id')}")

        output_result(result, json_output=json_output, title="Clone result")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command()
def preview(
    voice_id: str = typer.Argument(..., help="Voice ID to preview"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Get a preview URL for a voice."""
    try:
        http = HTTPAdapter()
        result = http.get(f"/api/play-voice-sample/{voice_id}")

        if json_output:
            output_result(result, json_output=True)
        else:
            url = result.get("url") or result.get("audio_url") or result.get("preview_url")
            if url:
                typer.echo(url)
            else:
                output_result(result, title=f"Voice {voice_id} preview")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command()
def mappings(
    item_id: str = typer.Argument(..., help="Item ID"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """List voice mappings for an item."""
    try:
        adapter = SQLiteAdapter()

        results = adapter.execute_raw(
            '''
            SELECT id, item_id, speaker, voice_id, created_at
            FROM voice_mappings
            WHERE item_id = ?
            ORDER BY speaker
            ''',
            [item_id],
        )

        output_result(
            results,
            json_output=json_output,
            columns=["speaker", "voice_id", "created_at"],
            title=f"Voice mappings for item {item_id}",
        )

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)
