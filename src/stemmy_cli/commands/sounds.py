"""Sound management commands."""

from typing import Optional

import typer

from stemmy_cli.adapters.sqlite import SQLiteAdapter
from stemmy_cli.adapters.http import HTTPAdapter
from stemmy_cli.output import output_result, print_error, print_success, print_info

app = typer.Typer(help="Manage sounds and effects")


@app.command("list")
def list_sounds(
    limit: int = typer.Option(50, "--limit", "-l", help="Maximum results"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """List all sounds."""
    try:
        adapter = SQLiteAdapter()

        results = adapter.execute_raw(
            '''
            SELECT id, sound_name, sound_audio_url, description, duration_seconds, created_at
            FROM sounds
            ORDER BY created_at DESC
            LIMIT ?
            ''',
            [limit],
        )

        output_result(
            results,
            json_output=json_output,
            columns=["id", "sound_name", "description", "duration_seconds"],
            title="Sounds",
        )

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command()
def show(
    sound_id: str = typer.Argument(..., help="Sound ID"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Show details of a sound."""
    try:
        adapter = SQLiteAdapter()
        result = adapter.get_by_id("sounds", sound_id)

        if not result:
            print_error(f"Sound {sound_id} not found")
            raise typer.Exit(1)

        output_result(result, json_output=json_output, title=f"Sound {sound_id}")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command()
def download(
    url: str = typer.Argument(..., help="URL to download"),
    title: Optional[str] = typer.Option(None, "--title", "-t", help="Sound title"),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Wait for download to complete"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Download a sound from a URL."""
    try:
        http = HTTPAdapter()

        payload = {"url": url}
        if title:
            payload["title"] = title

        if wait:
            print_info("Downloading sound...")
            result = http.start_and_wait(
                "/api/sounds/download",
                payload,
                "/api/task-status/{task_id}",
                verbose=True,
            )
        else:
            result = http.post("/api/sounds/download", json=payload)
            print_success(f"Download started: task_id={result.get('task_id')}")

        output_result(result, json_output=json_output, title="Download result")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("generate-effect")
def generate_effect(
    prompt: str = typer.Argument(..., help="Sound effect description"),
    duration: float = typer.Option(5.0, "--duration", "-d", help="Duration in seconds"),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Wait for generation to complete"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Generate a sound effect from a text prompt."""
    try:
        http = HTTPAdapter()

        payload = {
            "prompt": prompt,
            "duration": duration,
        }

        if wait:
            print_info("Generating sound effect...")
            result = http.start_and_wait(
                "/api/generate-sound-effect",
                payload,
                "/api/sound-effect-status/{task_id}",
                poll_interval=2.0,
                verbose=True,
            )
        else:
            result = http.post("/api/generate-sound-effect", json=payload)
            print_success(f"Generation started: task_id={result.get('task_id')}")

        output_result(result, json_output=json_output, title="Sound effect result")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("save-effect")
def save_effect(
    audio_url: str = typer.Argument(..., help="Audio URL to save"),
    title: str = typer.Argument(..., help="Sound title"),
    stemmy_id: Optional[str] = typer.Option(None, "--stemmy", "-s", help="Associate with stemmy"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Save a generated sound effect."""
    try:
        http = HTTPAdapter()

        payload = {
            "audio_url": audio_url,
            "title": title,
        }
        if stemmy_id:
            payload["stemmy_id"] = stemmy_id

        result = http.post("/api/save-sound-effect", json=payload)

        print_success(f"Sound effect saved")
        output_result(result, json_output=json_output, title="Saved sound")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)
