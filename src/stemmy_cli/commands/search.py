"""Search and embedding query commands."""

from typing import Optional

import typer

from stemmy_cli.adapters.http import HTTPAdapter
from stemmy_cli.output import output_result, print_error

app = typer.Typer(help="Search embeddings")


@app.command()
def pinecone(
    query: str = typer.Argument(..., help="Search query"),
    item_id: Optional[str] = typer.Option(None, "--item", "-i", help="Filter by item ID"),
    top_k: int = typer.Option(10, "--top-k", "-k", help="Number of results"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Search using Pinecone embeddings."""
    try:
        http = HTTPAdapter()

        payload = {
            "query": query,
            "top_k": top_k,
        }
        if item_id:
            payload["item_id"] = item_id

        result = http.post("/api/pinecone/query", json=payload)

        matches = result.get("matches", result) if isinstance(result, dict) else result

        if json_output:
            output_result(result, json_output=True)
        else:
            display = []
            for m in matches:
                display.append({
                    "id": m.get("id"),
                    "score": round(m.get("score", 0), 4),
                    "text": m.get("metadata", {}).get("text", "")[:80],
                })
            output_result(
                display,
                columns=["id", "score", "text"],
                title=f"Pinecone results for '{query}'",
            )

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command()
def similar(
    fragment_id: str = typer.Argument(..., help="Fragment ID to find similar to"),
    top_k: int = typer.Option(10, "--top-k", "-k", help="Number of results"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Find fragments similar to a given fragment."""
    try:
        http = HTTPAdapter()

        payload = {
            "fragment_id": fragment_id,
            "top_k": top_k,
        }

        result = http.post("/api/pinecone/find-similar", json=payload)

        matches = result.get("matches", result) if isinstance(result, dict) else result

        if json_output:
            output_result(result, json_output=True)
        else:
            display = []
            for m in matches:
                display.append({
                    "id": m.get("id"),
                    "score": round(m.get("score", 0), 4),
                    "text": m.get("metadata", {}).get("text", "")[:80],
                })
            output_result(
                display,
                columns=["id", "score", "text"],
                title=f"Fragments similar to {fragment_id}",
            )

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)
