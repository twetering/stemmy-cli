"""Character management commands."""

from typing import Optional

import typer

from stemmy_cli.adapters.sqlite import SQLiteAdapter
from stemmy_cli.adapters.http import HTTPAdapter
from stemmy_cli.output import output_result, print_error, print_success, print_info

app = typer.Typer(help="Manage characters")


@app.command("list")
def list_characters(
    limit: int = typer.Option(50, "--limit", "-l", help="Maximum results"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """List all characters."""
    try:
        adapter = SQLiteAdapter()

        results = adapter.execute_raw(
            '''
            SELECT id, name, shortname, description, language, nationality, created_at
            FROM characters
            ORDER BY name
            LIMIT ?
            ''',
            [limit],
        )

        output_result(
            results,
            json_output=json_output,
            columns=["id", "name", "shortname", "description", "language"],
            title="Characters",
        )

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command()
def show(
    character_id: str = typer.Argument(..., help="Character ID"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Show details of a character."""
    try:
        adapter = SQLiteAdapter()
        result = adapter.get_by_id("characters", character_id)

        if not result:
            print_error(f"Character {character_id} not found")
            raise typer.Exit(1)

        formats = adapter.execute_raw(
            '''
            SELECT format_id, role FROM character_formats
            WHERE character_id = ?
            ''',
            [character_id],
        )
        result["formats"] = formats

        voices = adapter.execute_raw(
            '''
            SELECT voice_id, is_primary FROM character_voices
            WHERE character_id = ?
            ''',
            [character_id],
        )
        result["voices"] = voices

        output_result(result, json_output=json_output, title=f"Character {character_id}")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("generate-prompts")
def generate_prompts(
    character_id: str = typer.Argument(..., help="Character ID"),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Wait for generation to complete"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Generate prompts for a character based on their speech examples."""
    try:
        http = HTTPAdapter()

        payload = {"character_id": character_id}

        if wait:
            print_info("Generating character prompts...")
            result = http.start_and_wait(
                "/api/characters/generate-prompts",
                payload,
                "/api/characters/prompt-status/{task_id}",
                verbose=True,
            )
        else:
            result = http.post("/api/characters/generate-prompts", json=payload)
            print_success(f"Generation started: task_id={result.get('task_id')}")

        output_result(result, json_output=json_output, title="Prompt generation result")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("formats")
def character_formats(
    character_id: str = typer.Argument(..., help="Character ID"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """List formats associated with a character."""
    try:
        adapter = SQLiteAdapter()

        results = adapter.execute_raw(
            '''
            SELECT cf.format_id, cf.role, f.title as format_title
            FROM character_formats cf
            JOIN formats f ON cf.format_id = f.id
            WHERE cf.character_id = ?
            ''',
            [character_id],
        )

        output_result(
            results,
            json_output=json_output,
            columns=["format_id", "format_title", "role"],
            title=f"Formats for character {character_id}",
        )

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)
