"""Chat-based generation commands."""

import typer
from rich.console import Console
from rich.panel import Panel
from typing import Optional

from stemmy_cli.adapters.http import HTTPAdapter
from stemmy_cli.output import output_result, print_error, print_info, print_success

console = Console()


def _run_ai_chat(
    request: str,
    provider: str = "anthropic",
    model: Optional[str] = None,
    verbose: bool = True,
    json_output: bool = False,
):
    """Core AI chat execution logic."""
    try:
        from stemmy_cli.agent import Orchestrator
        
        if verbose:
            console.print()
            console.print("[bold cyan]🧠 Stemmy AI[/bold cyan]")
            console.print(f"[dim]Request:[/dim] {request}")
            console.print()
        
        orchestrator = Orchestrator(provider=provider, model=model)
        result = orchestrator.execute(request, verbose=verbose)
        
        if json_output:
            output_result(result, json_output=True)
        else:
            orchestrator.print_result(result)
    
    except ImportError as e:
        print_error(f"Missing dependency: {e}")
        console.print("[dim]Install with: pip install httpx[/dim]")
        raise typer.Exit(1)
    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


def chat_callback(ctx: typer.Context, request: Optional[str] = None):
    """Handle direct invocation with a request string."""
    if ctx.invoked_subcommand is None and request:
        _run_ai_chat(request)


app = typer.Typer(
    help="Chat-based generation - use 'stemmy chat \"your request\"' for AI mode",
    invoke_without_command=True,
    no_args_is_help=False,
)


@app.command("ai")
def ai_command(
    request: str = typer.Argument(..., help="Natural language request for AI"),
    provider: str = typer.Option("auto", "--provider", "-p", help="LLM provider: auto, openai, anthropic"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Model to use"),
    verbose: bool = typer.Option(True, "--verbose/--quiet", "-v/-q", help="Show details"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """
    Execute a natural language request using AI.
    
    Examples:
        stemmy chat ai "find all mentions of AI"
        stemmy chat ai "maak een compilatie van vragen"
        stemmy chat ai "how many formats are there?"
    """
    _run_ai_chat(request, provider, model, verbose, json_output)


@app.command("send")
def send_message(
    message: str = typer.Argument(..., help="Message to send"),
    system_prompt: str = typer.Option(None, "--system", "-s", help="System prompt"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Send a chat message to the API and get a response."""
    try:
        http = HTTPAdapter()

        payload = {"message": message}
        if system_prompt:
            payload["system_prompt"] = system_prompt

        result = http.post("/api/chat", json=payload)

        if json_output:
            output_result(result, json_output=True)
        else:
            response = result.get("response") or result.get("message") or result.get("content")
            if response:
                typer.echo(response)
            else:
                output_result(result, title="Chat response")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("tools")
def list_ai_tools(
    category: str = typer.Option(None, "--category", "-c", help="Filter by category"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """
    List available AI tools that can be used in natural language requests.
    """
    from stemmy_cli.agent.tools import list_tools
    
    tools = list_tools(category=category)
    
    if json_output:
        output_result([{"name": t["name"], "description": t["description"], "category": t["category"]} for t in tools], json_output=True)
    else:
        console.print()
        console.print("[bold]Available AI Tools[/bold]")
        console.print()
        
        current_category = None
        for tool in sorted(tools, key=lambda t: (t["category"], t["name"])):
            if tool["category"] != current_category:
                current_category = tool["category"]
                console.print(f"[bold cyan]{current_category.upper()}[/bold cyan]")
            
            console.print(f"  [green]{tool['name']}[/green]")
            console.print(f"    [dim]{tool['description']}[/dim]")
        
        console.print()
        console.print(f"[dim]Total: {len(tools)} tools[/dim]")


@app.command("generate-script")
def generate_script(
    topic: str = typer.Argument(..., help="Topic for the script"),
    style: str = typer.Option("podcast", "--style", "-s", help="Script style: podcast, news, story"),
    length: str = typer.Option("medium", "--length", "-l", help="Script length: short, medium, long"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Generate a script using chat."""
    try:
        http = HTTPAdapter()

        length_map = {
            "short": "around 100 words",
            "medium": "around 300 words",
            "long": "around 600 words",
        }

        prompt = f"Write a {style} script about: {topic}. Length: {length_map.get(length, 'medium')}."

        payload = {"message": prompt}
        result = http.post("/api/chat", json=payload)

        if json_output:
            output_result(result, json_output=True)
        else:
            response = result.get("response") or result.get("message") or result.get("content")
            if response:
                typer.echo(response)
            else:
                output_result(result, title="Generated script")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)
