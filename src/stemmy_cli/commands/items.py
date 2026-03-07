"""Item (episode) management commands."""

from typing import Optional

import typer

from stemmy_cli.adapters.sqlite import SQLiteAdapter
from stemmy_cli.adapters.http import HTTPAdapter
from stemmy_cli.output import output_result, print_error, print_success, print_info

app = typer.Typer(help="Manage items (episodes)")


@app.command("list")
def list_items(
    format_id: Optional[str] = typer.Option(None, "--format", "-f", help="Filter by format ID"),
    limit: int = typer.Option(50, "--limit", "-l", help="Maximum results"),
    offset: int = typer.Option(0, "--offset", "-o", help="Offset for pagination"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """List items (episodes)."""
    try:
        adapter = SQLiteAdapter()

        if format_id:
            results = adapter.execute_raw(
                '''
                SELECT id, title, format_id, published_at, duration_seconds, audio_url
                FROM items
                WHERE format_id = ?
                ORDER BY published_at DESC
                LIMIT ? OFFSET ?
                ''',
                [format_id, limit, offset],
            )
        else:
            results = adapter.execute_raw(
                '''
                SELECT id, title, format_id, published_at, duration_seconds, audio_url
                FROM items
                ORDER BY published_at DESC
                LIMIT ? OFFSET ?
                ''',
                [limit, offset],
            )

        output_result(
            results,
            json_output=json_output,
            columns=["id", "title", "format_id", "published_at", "duration_seconds"],
            title="Items",
        )

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command()
def show(
    item_id: str = typer.Argument(..., help="Item ID"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Show details of an item."""
    try:
        adapter = SQLiteAdapter()
        result = adapter.get_by_id("items", item_id)

        if not result:
            print_error(f"Item {item_id} not found")
            raise typer.Exit(1)

        fragment_count = adapter.count("fragments", where={"item_id": item_id})
        entity_count = adapter.count("entities", where={"item_id": item_id})

        result["fragment_count"] = fragment_count
        result["entity_count"] = entity_count

        output_result(result, json_output=json_output, title=f"Item {item_id}")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command()
def stats(
    item_id: str = typer.Argument(..., help="Item ID"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Show statistics for an item."""
    try:
        adapter = SQLiteAdapter()

        item = adapter.get_by_id("items", item_id)
        if not item:
            print_error(f"Item {item_id} not found")
            raise typer.Exit(1)

        fragment_count = adapter.count("fragments", where={"item_id": item_id})
        entity_count = adapter.count("entities", where={"item_id": item_id})

        fragment_with_audio = adapter.execute_raw(
            '''
            SELECT COUNT(*) as count FROM fragments
            WHERE item_id = ? AND audio_url IS NOT NULL AND audio_url != ''
            ''',
            [item_id],
        )[0]["count"]

        speakers = adapter.execute_raw(
            '''
            SELECT speaker, COUNT(*) as count FROM fragments
            WHERE item_id = ? AND speaker IS NOT NULL
            GROUP BY speaker
            ORDER BY count DESC
            ''',
            [item_id],
        )

        entity_types = adapter.execute_raw(
            '''
            SELECT entity_type, COUNT(*) as count FROM entities
            WHERE item_id = ?
            GROUP BY entity_type
            ORDER BY count DESC
            ''',
            [item_id],
        )

        stats = {
            "item_id": item_id,
            "title": item.get("title"),
            "fragment_count": fragment_count,
            "fragments_with_audio": fragment_with_audio,
            "entity_count": entity_count,
            "speakers": speakers,
            "entity_types": entity_types,
        }

        output_result(stats, json_output=json_output, title=f"Stats for item {item_id}")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command()
def trim(
    item_id: str = typer.Argument(..., help="Item ID"),
    start_time: float = typer.Argument(..., help="Start time in seconds"),
    end_time: float = typer.Argument(..., help="End time in seconds"),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Wait for task to complete"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Trim the audio of an item."""
    try:
        http = HTTPAdapter()

        payload = {
            "item_id": item_id,
            "start_time": start_time,
            "end_time": end_time,
        }

        if wait:
            print_info("Starting audio trim...")
            result = http.start_and_wait(
                "/api/items/trim-audio",
                payload,
                "/api/task-status/{task_id}",
                verbose=True,
            )
        else:
            result = http.post("/api/items/trim-audio", json=payload)
            print_success(f"Trim started: task_id={result.get('task_id')}")

        output_result(result, json_output=json_output, title="Trim result")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("upsert-embeddings")
def upsert_embeddings(
    item_id: str = typer.Argument(..., help="Item ID"),
    backend: str = typer.Option("pinecone", "--backend", "-b", help="Backend: pinecone or supabase"),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Wait for task to complete"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Upsert embeddings for an item's fragments."""
    try:
        http = HTTPAdapter()

        if backend == "pinecone":
            path = f"/api/pinecone/episodes/{item_id}/upsert"
        elif backend == "supabase":
            path = f"/api/supabase/episodes/{item_id}/upsert"
        else:
            print_error(f"Unknown backend: {backend}")
            raise typer.Exit(1)

        if wait:
            print_info(f"Starting embedding upsert to {backend}...")
            result = http.start_and_wait(
                path,
                {},
                "/api/task-status/{task_id}",
                verbose=True,
            )
        else:
            result = http.post(path, json={})
            task_id = result.get("task_id")
            if task_id:
                print_success(f"Upsert started: task_id={task_id}")
            else:
                print_success("Upsert completed")

        output_result(result, json_output=json_output, title="Embedding upsert result")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)
