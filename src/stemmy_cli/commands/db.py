"""Database maintenance commands."""

from typing import Optional

import typer

from stemmy_cli.output import print_error, print_info, print_success

app = typer.Typer(help="Database maintenance (migrate words, sync to Turso)")


@app.command("migrate-words")
def migrate_words_cmd(
    execute: bool = typer.Option(False, "--execute", "-e", help="Apply changes (default: dry run)"),
    limit: Optional[int] = typer.Option(None, "--limit", "-l", help="Limit items to process (for testing)"),
):
    """
    Migrate words data from items.transcript_data to fragments.words.

    Extracts word-level timing from AssemblyAI transcripts and assigns to fragments.
    Run without --execute first to see what would be updated.
    """
    import sys
    from pathlib import Path
    from stemmy_cli.paths import get_project_root
    project_root = get_project_root()
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    from scripts.migrate_words_data import migrate_words

    dry_run = not execute
    if dry_run:
        print_info("DRY RUN - use --execute to apply changes")
    updated = migrate_words(dry_run=dry_run, limit=limit)
    if execute and updated > 0:
        print_success(f"Updated {updated:,} fragments with words data")


@app.command("sync-turso")
def sync_turso_cmd(
    replace: bool = typer.Option(False, "--replace", "-r", help="Delete existing Turso DB first (for re-import)"),
):
    """
    Sync full SQLite database to Turso cloud.

    Uses Turso CLI `db import` when available (recommended). Requires TURSO_DB_URL and TURSO_API_KEY.
    Use --replace to delete existing stemmy DB before import (for full refresh).
    """
    try:
        from stemmy_cli.adapters.connection import sync_to_turso
        result = sync_to_turso(replace=replace)
        msg = result.get("method", "sync") == "turso_cli_import" and "Imported to Turso via CLI" or f"Synced {result.get('rows_inserted', 0):,} rows to Turso"
        if result.get("rows_failed"):
            msg += f" ({result['rows_failed']} failed)"
        print_success(msg)
    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)
