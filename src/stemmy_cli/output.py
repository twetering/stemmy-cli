"""Output formatting utilities for Stemmy CLI."""

import json
from typing import Any, Dict, List, Optional
from contextlib import contextmanager

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text
from rich.progress import (
    Progress, 
    SpinnerColumn, 
    TextColumn, 
    BarColumn, 
    TaskProgressColumn,
    TimeElapsedColumn,
    MofNCompleteColumn,
)

console = Console()
error_console = Console(stderr=True)


def print_json(data: Any) -> None:
    """Print data as formatted JSON."""
    console.print_json(json.dumps(data, indent=2, default=str))


def print_table(
    data: List[Dict[str, Any]],
    columns: Optional[List[str]] = None,
    title: Optional[str] = None,
) -> None:
    """Print data as a rich table."""
    if not data:
        console.print("[dim]No results found.[/dim]")
        return

    if columns is None:
        columns = list(data[0].keys())

    table = Table(title=title, show_header=True, header_style="bold cyan")

    for col in columns:
        table.add_column(col)

    for row in data:
        table.add_row(*[str(row.get(col, "")) for col in columns])

    console.print(table)


def print_detail(data: Dict[str, Any], title: Optional[str] = None) -> None:
    """Print a single record as key-value pairs."""
    if title:
        console.print(f"\n[bold]{title}[/bold]")

    for key, value in data.items():
        if isinstance(value, (dict, list)):
            value = json.dumps(value, indent=2, default=str)
        console.print(f"  [cyan]{key}:[/cyan] {value}")


def print_success(message: str) -> None:
    """Print a success message."""
    console.print(f"[green]✓[/green] {message}")


def print_error(message: str) -> None:
    """Print an error message."""
    error_console.print(f"[red]✗[/red] {message}")


def print_warning(message: str) -> None:
    """Print a warning message."""
    console.print(f"[yellow]![/yellow] {message}")


def print_info(message: str) -> None:
    """Print an info message."""
    console.print(f"[blue]ℹ[/blue] {message}")


def create_progress() -> Progress:
    """Create a progress bar for long-running operations."""
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    )


def create_detailed_progress() -> Progress:
    """Create a detailed progress bar with time elapsed and item count."""
    return Progress(
        SpinnerColumn(),
        TextColumn("[bold cyan]{task.description}"),
        BarColumn(bar_width=30),
        MofNCompleteColumn(),
        TextColumn("[dim]•[/dim]"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    )


@contextmanager
def status_spinner(message: str, success_message: str = "Done"):
    """Context manager for showing a spinner during an operation."""
    with console.status(f"[cyan]{message}[/cyan]", spinner="dots") as status:
        try:
            yield status
            console.print(f"[green]✓[/green] {success_message}")
        except Exception as e:
            console.print(f"[red]✗[/red] Failed: {e}")
            raise


class WorkflowProgress:
    """Track multi-step workflow progress with rich visuals."""
    
    def __init__(self, title: str, steps: List[str]):
        self.title = title
        self.steps = steps
        self.current_step = 0
        self.step_status = ["pending"] * len(steps)
    
    def _render(self) -> Panel:
        """Render current workflow state."""
        lines = []
        for i, step in enumerate(self.steps):
            if self.step_status[i] == "completed":
                icon = "[green]✓[/green]"
            elif self.step_status[i] == "in_progress":
                icon = "[yellow]●[/yellow]"
            elif self.step_status[i] == "failed":
                icon = "[red]✗[/red]"
            else:
                icon = "[dim]○[/dim]"
            
            style = "bold" if self.step_status[i] == "in_progress" else ""
            lines.append(f"  {icon} [{style}]{step}[/{style}]")
        
        content = "\n".join(lines)
        return Panel(content, title=f"[bold cyan]{self.title}[/bold cyan]", border_style="cyan")
    
    def __enter__(self):
        self.live = Live(self._render(), console=console, refresh_per_second=4)
        self.live.__enter__()
        return self
    
    def __exit__(self, *args):
        self.live.__exit__(*args)
    
    def start_step(self, index: int):
        """Mark a step as in progress."""
        self.current_step = index
        self.step_status[index] = "in_progress"
        self.live.update(self._render())
    
    def complete_step(self, index: int):
        """Mark a step as completed."""
        self.step_status[index] = "completed"
        self.live.update(self._render())
    
    def fail_step(self, index: int):
        """Mark a step as failed."""
        self.step_status[index] = "failed"
        self.live.update(self._render())


def print_workflow_result(
    title: str,
    output_path: str,
    file_size: str = "",
    duration: str = "",
):
    """Print a styled result box for workflow completion."""
    info_parts = []
    if file_size:
        info_parts.append(f"[dim]Size:[/dim] {file_size}")
    if duration:
        info_parts.append(f"[dim]Duration:[/dim] {duration}")
    
    content = Text()
    content.append("📁 ", style="")
    content.append(output_path, style="bold green")
    
    if info_parts:
        content.append("\n" + " • ".join(info_parts))
    
    panel = Panel(
        content,
        title=f"[bold green]✓ {title}[/bold green]",
        border_style="green",
        padding=(0, 1),
    )
    console.print(panel)


def output_result(
    data: Any,
    json_output: bool = False,
    columns: Optional[List[str]] = None,
    title: Optional[str] = None,
) -> None:
    """Output data in the appropriate format based on flags."""
    # Handle string output formats like "json" or "table"
    if isinstance(json_output, str):
        json_output = json_output.lower() == "json"
    
    if json_output:
        print_json(data)
    elif isinstance(data, list):
        print_table(data, columns=columns, title=title)
    elif isinstance(data, dict):
        print_detail(data, title=title)
    else:
        console.print(data)
