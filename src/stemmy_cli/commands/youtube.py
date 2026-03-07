"""YouTube utility commands."""

import typer

from stemmy_cli.adapters.http import HTTPAdapter
from stemmy_cli.output import output_result, print_error

app = typer.Typer(help="YouTube utilities")


@app.command()
def info(
    url: str = typer.Argument(..., help="YouTube video or channel URL"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Get info about a YouTube video or channel."""
    try:
        http = HTTPAdapter()
        result = http.get("/api/youtube/info", params={"url": url})

        output_result(result, json_output=json_output, title="YouTube info")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command()
def download(
    url: str = typer.Argument(..., help="YouTube video URL"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Download a YouTube video's audio."""
    try:
        http = HTTPAdapter()
        result = http.post("/api/youtube/download", json={"url": url})

        output_result(result, json_output=json_output, title="Download result")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)
