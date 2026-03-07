"""Embedding generation commands."""

import typer
from typing import Optional
from pathlib import Path
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn

from stemmy_cli.output import output_result, print_error

app = typer.Typer(help="Generate and manage embeddings for semantic search")
console = Console()


@app.command()
def status():
    """Check embedding status in database."""
    from stemmy_cli.embeddings.service import check_embedding_status
    from stemmy_cli.agent.tools import _get_db_path
    
    db_path = _get_db_path()
    stats = check_embedding_status(db_path)
    
    console.print()
    console.print("[bold]Embedding Status[/bold]")
    console.print()
    console.print(f"  Total fragments:    {stats['total_fragments']:,}")
    console.print(f"  Embedded:          {stats['embedded_count']:,} ({stats['percentage']}%)")
    console.print(f"  Needs embedding:   {stats['needs_embedding']:,}")
    
    if stats['model']:
        console.print()
        console.print(f"  Model:             {stats['model']}")
        console.print(f"  Dimensions:        {stats['dimensions']}")
    
    if stats['needs_embedding'] > 0:
        console.print()
        console.print("[yellow]Run 'stemmy embeddings build' to generate embeddings[/yellow]")


@app.command()
def build(
    provider: str = typer.Option("local", "--provider", "-p", help="Embedding provider: local (HuggingFace) or openai"),
    batch_size: int = typer.Option(32, "--batch-size", "-b", help="Batch size for embedding"),
    limit: Optional[int] = typer.Option(None, "--limit", "-l", help="Limit number of fragments to embed"),
):
    """Build embeddings for all fragments.
    
    Uses local HuggingFace model by default (free, no API calls).
    Use --provider openai for higher quality (requires OPENAI_API_KEY).
    """
    from stemmy_cli.embeddings.service import FragmentEmbedder, check_embedding_status
    from stemmy_cli.agent.tools import _get_db_path, _get_db_connection
    import json
    
    db_path = _get_db_path()
    
    # Get fragments that need embedding
    conn = _get_db_connection()
    
    sql = """
        SELECT f.id, f.text 
        FROM fragments f
        LEFT JOIN fragment_embeddings e ON f.id = e.fragment_id
        WHERE f.text IS NOT NULL AND f.text != '' AND e.fragment_id IS NULL
    """
    if limit:
        sql += f" LIMIT {limit}"
    
    try:
        cursor = conn.execute(sql)
        fragments = [{"id": row[0], "text": row[1]} for row in cursor.fetchall()]
    except Exception:
        # Table might not exist yet
        cursor = conn.execute("""
            SELECT id, text FROM fragments 
            WHERE text IS NOT NULL AND text != ''
        """ + (f" LIMIT {limit}" if limit else ""))
        fragments = [{"id": row[0], "text": row[1]} for row in cursor.fetchall()]
    
    conn.close()
    
    if not fragments:
        console.print("[green]All fragments already have embeddings![/green]")
        return
    
    console.print(f"\nWill embed {len(fragments)} fragments using [cyan]{provider}[/cyan] provider\n")
    
    embedder = FragmentEmbedder(provider=provider, db_path=db_path)
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[cyan]{task.description}[/cyan]"),
        BarColumn(bar_width=30),
        TextColumn("{task.percentage:>3.0f}%"),
        console=console,
    ) as progress:
        task = progress.add_task("Generating embeddings...", total=len(fragments))
        
        def update_progress(current, total, msg):
            progress.update(task, completed=current)
        
        results = embedder.embed_fragments(
            fragments,
            batch_size=batch_size,
            progress_callback=update_progress,
        )
        
        progress.update(task, description="Saving to database...")
        embedder.save_embeddings_to_db(results)
    
    console.print(f"\n[green]✓ Successfully embedded {len(results)} fragments[/green]")
    
    # Show updated status
    stats = check_embedding_status(db_path)
    console.print(f"  Total coverage: {stats['percentage']}%")


@app.command()
def search(
    query: str = typer.Argument(..., help="Search query"),
    top_k: int = typer.Option(10, "--top", "-k", help="Number of results"),
    provider: str = typer.Option("openai", "--provider", "-p", help="Embedding provider (openai matches DB embeddings)"),
):
    """Semantic search across fragments."""
    from stemmy_cli.embeddings.service import FragmentEmbedder
    from stemmy_cli.agent.tools import _get_db_path, _get_db_connection
    from rich.table import Table
    
    db_path = _get_db_path()
    embedder = FragmentEmbedder(provider=provider, db_path=db_path)
    
    try:
        results = embedder.similarity_search(query, top_k=top_k)
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        console.print("[yellow]Make sure embeddings exist. Run 'stemmy embeddings build' first.[/yellow]")
        raise typer.Exit(1)
    
    if not results:
        console.print("[yellow]No results found[/yellow]")
        return
    
    # Get fragment texts
    conn = _get_db_connection()
    
    table = Table(title=f"Semantic Search: '{query}'", show_lines=True)
    table.add_column("#", style="cyan", width=3)
    table.add_column("Score", style="green", width=6)
    table.add_column("Fragment", width=70)
    
    for i, result in enumerate(results, 1):
        cursor = conn.execute(
            "SELECT text FROM fragments WHERE id = ?",
            (result["fragment_id"],)
        )
        row = cursor.fetchone()
        text = row[0][:100] + "..." if row and len(row[0]) > 100 else (row[0] if row else "?")
        
        table.add_row(
            str(i),
            f"{result['similarity']:.3f}",
            text,
        )
    
    conn.close()
    console.print(table)


@app.command()
def generate(
    text: str = typer.Argument(..., help="Text to generate embedding for"),
    provider: str = typer.Option("local", "--provider", "-p", help="Embedding provider"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Generate an embedding for text."""
    from stemmy_cli.embeddings.service import EmbeddingService
    import json
    
    try:
        service = EmbeddingService(provider=provider)
        result = service.embed_text(text)
        
        if json_output:
            output_result({
                "text": result.text,
                "embedding": result.embedding[:10] + ["..."],  # Truncate for display
                "dimensions": result.dimensions,
                "model": result.model,
            }, json_output=True)
        else:
            console.print(f"Generated embedding: {result.dimensions} dimensions using {result.model}")
            console.print(f"First 5 values: {result.embedding[:5]}")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)
