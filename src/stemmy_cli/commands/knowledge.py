"""Knowledge lookup commands."""

import typer

from stemmy_cli.adapters.http import HTTPAdapter
from stemmy_cli.output import output_result, print_error

app = typer.Typer(help="Knowledge lookups")


@app.command()
def wikipedia(
    query: str = typer.Argument(..., help="Wikipedia search query"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Search Wikipedia."""
    try:
        http = HTTPAdapter()
        result = http.get("/api/wikipedia", params={"query": query})

        output_result(result, json_output=json_output, title=f"Wikipedia: {query}")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command()
def news(
    query: str = typer.Argument(..., help="News search query"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Search news articles."""
    try:
        http = HTTPAdapter()
        result = http.get("/api/news", params={"query": query})

        articles = result.get("articles", result) if isinstance(result, dict) else result

        if json_output:
            output_result(result, json_output=True)
        else:
            display = []
            for a in articles:
                display.append({
                    "title": a.get("title", "")[:60],
                    "source": a.get("source", {}).get("name", ""),
                    "published": a.get("publishedAt", ""),
                })
            output_result(
                display,
                columns=["title", "source", "published"],
                title=f"News: {query}",
            )

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("news-content")
def news_content(
    url: str = typer.Argument(..., help="Article URL"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Get the full content of a news article."""
    try:
        http = HTTPAdapter()
        result = http.get("/api/news/content", params={"url": url})

        output_result(result, json_output=json_output, title="Article content")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)
