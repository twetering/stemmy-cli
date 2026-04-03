"""Main entry point for the Stemmy CLI."""

import os
import sys
import typer
from typing import Optional

from stemmy_cli import __version__


def _configure_terminal():
    """Configure terminal settings for best UX."""
    # Disable color when not a TTY or when NO_COLOR is set
    if not sys.stdout.isatty() or os.environ.get('NO_COLOR'):
        os.environ['NO_COLOR'] = '1'
    
    # Set TERM if not set (helps with some edge cases)
    if 'TERM' not in os.environ:
        os.environ['TERM'] = 'xterm-256color'


_configure_terminal()
from stemmy_cli.commands import (
    analyze,
    audio,
    benchmark,
    characters,
    chat,
    compilations,
    db,
    embeddings,
    entities,
    formats,
    fragments,
    items,
    knowledge,
    mix,
    recipes,
    runpod,
    search,
    sounds,
    stemmies,
    tasks,
    transcripts,
    voices,
    workflows,
    youtube,
)


def main_callback(
    ctx: typer.Context,
    rpg: bool = typer.Option(False, "--rpg", "-r", help="Start RPG mode (menu-driven, no AI)"),
    debug: bool = typer.Option(False, "--debug", "-d", help="Enable debug mode (show queries and diagnostics)"),
):
    """Start interactive mode when no command is provided."""
    if ctx.invoked_subcommand is None:
        if rpg:
            from stemmy_cli.rpg import start_rpg
            start_rpg(debug=debug)
        else:
            from stemmy_cli.interactive import start_interactive
            start_interactive()


app = typer.Typer(
    name="stemmy",
    help="Professional CLI for Stemmy audio content platform.",
    no_args_is_help=False,
    invoke_without_command=True,
    rich_markup_mode="rich",
    callback=main_callback,
)

# Register all subcommand groups
app.add_typer(analyze.app, name="analyze", help="Advanced analyzers (20+ modes)")
app.add_typer(fragments.app, name="fragments", help="Manage audio fragments")
app.add_typer(entities.app, name="entities", help="Manage entities from transcripts")
app.add_typer(formats.app, name="formats", help="Manage formats (podcasts/shows)")
app.add_typer(items.app, name="items", help="Manage items (episodes)")
app.add_typer(transcripts.app, name="transcripts", help="Manage transcripts")
app.add_typer(stemmies.app, name="stemmies", help="Manage stemmies (audio compositions)")
app.add_typer(voices.app, name="voices", help="Manage voices and cloning")
app.add_typer(sounds.app, name="sounds", help="Manage sounds and effects")
app.add_typer(compilations.app, name="compilations", help="Run compilation recipes")
app.add_typer(characters.app, name="characters", help="Manage characters")
app.add_typer(tasks.app, name="tasks", help="Monitor async tasks")
app.add_typer(search.app, name="search", help="Search embeddings")
app.add_typer(embeddings.app, name="embeddings", help="Generate embeddings")
app.add_typer(db.app, name="db", help="Database maintenance (migrate words, sync Turso)")
app.add_typer(audio.app, name="audio", help="Audio utilities")
app.add_typer(knowledge.app, name="knowledge", help="Knowledge lookups")
app.add_typer(youtube.app, name="youtube", help="YouTube utilities")
app.add_typer(chat.app, name="chat", help="Chat-based generation")
app.add_typer(recipes.app, name="recipes", help="Creative audio recipes")
app.add_typer(mix.app, name="mix", help="Audio mixing and blending")
app.add_typer(workflows.app, name="workflows", help="Automated workflows")
app.add_typer(benchmark.app, name="benchmark", help="Reference benchmarks (WhisperX vs DB)")
app.add_typer(runpod.app, name="runpod", help="RunPod GPU transcripts (S3 → SQLite)")


@app.command()
def version():
    """Show the CLI version."""
    from stemmy_cli.branding import print_banner
    print_banner(show_version=True, show_stats=True)


@app.command()
def welcome():
    """Show welcome screen with banner and quick help."""
    from stemmy_cli.branding import print_welcome
    print_welcome()


@app.command()
def interactive():
    """
    Start interactive shell mode.
    
    Provides a REPL-style interface with tab completion and history.
    """
    from stemmy_cli.interactive import start_interactive
    start_interactive()


@app.command()
def ask(
    prompt: str = typer.Argument(..., help="Natural language request"),
    provider: str = typer.Option("auto", "--provider", "-p", help="LLM provider: auto, openai, anthropic"),
    model: str = typer.Option(None, "--model", "-m", help="Model to use"),
    verbose: bool = typer.Option(True, "--verbose/--quiet", "-v/-q", help="Show execution details"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """
    Execute a natural language request using AI.

    Examples:
        stemmy ask "find all fragments mentioning climate change"
        stemmy ask "hoeveel formats zijn er?"
        stemmy ask "zoek entities van type person_name"
    """
    from rich.console import Console
    from stemmy_cli.agent import Orchestrator
    from stemmy_cli.output import output_result, print_error

    console = Console()
    
    try:
        if verbose:
            console.print()
            console.print("[bold cyan]🧠 Stemmy AI[/bold cyan]")
            console.print(f"[dim]Request:[/dim] {prompt}")
            console.print()
        
        orchestrator = Orchestrator(provider=provider, model=model)
        result = orchestrator.execute(prompt, verbose=verbose)
        
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


def _reset_terminal():
    """Reset terminal to sane state after exit."""
    import subprocess
    if sys.stdin.isatty():
        try:
            subprocess.run(['stty', 'sane'], stderr=subprocess.DEVNULL)
        except Exception:
            pass


def main():
    """Main entry point with proper signal handling."""
    import atexit
    import signal
    
    def handle_sigint(signum, frame):
        """Handle Ctrl+C at top level."""
        _reset_terminal()
        print("\nInterrupted")
        sys.exit(130)  # Standard exit code for SIGINT
    
    def handle_sigpipe(signum, frame):
        """Handle broken pipe (e.g., piping to head)."""
        sys.exit(0)
    
    # Reset terminal on exit
    atexit.register(_reset_terminal)
    
    # Set up signal handlers
    signal.signal(signal.SIGINT, handle_sigint)
    
    # Handle SIGPIPE on Unix (ignore broken pipe errors)
    if hasattr(signal, 'SIGPIPE'):
        signal.signal(signal.SIGPIPE, handle_sigpipe)
    
    try:
        app()
    except KeyboardInterrupt:
        _reset_terminal()
        print("\nInterrupted")
        sys.exit(130)
    except BrokenPipeError:
        # Gracefully handle broken pipe
        sys.exit(0)
    finally:
        _reset_terminal()


if __name__ == "__main__":
    main()
