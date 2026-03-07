"""
Branding and visual identity for Stemmy CLI.

Provides ASCII banners, styled output, and visual consistency.
"""

import pyfiglet
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.style import Style

from stemmy_cli import __version__

console = Console()

# Brand colors
BRAND_GRADIENT = ["#00d4ff", "#00b4d8", "#0096c7", "#0077b6", "#023e8a"]
ACCENT_COLOR = "cyan"
SUCCESS_COLOR = "green"
ERROR_COLOR = "red"
WARNING_COLOR = "yellow"


def get_banner(font: str = "slant", include_tagline: bool = True) -> str:
    """Generate ASCII art banner for STEMMY."""
    try:
        banner = pyfiglet.figlet_format("STEMMY", font=font)
    except pyfiglet.FontNotFound:
        banner = pyfiglet.figlet_format("STEMMY", font="standard")
    return banner


def print_banner(
    show_version: bool = True,
    show_stats: bool = False,
    analyzer_count: int = 47,
):
    """Print the branded CLI banner."""
    banner_text = get_banner()
    
    # Create gradient-colored banner
    styled_banner = Text()
    lines = banner_text.strip().split('\n')
    
    for i, line in enumerate(lines):
        color_idx = min(i, len(BRAND_GRADIENT) - 1)
        styled_banner.append(line + "\n", style=Style(color=BRAND_GRADIENT[color_idx], bold=True))
    
    console.print(styled_banner, end="")
    
    # Tagline
    tagline_parts = [f"[bold]Audio Intelligence CLI[/bold]"]
    if show_version:
        tagline_parts.append(f"[dim]v{__version__}[/dim]")
    if show_stats:
        tagline_parts.append(f"[cyan]{analyzer_count} analyzers[/cyan]")
        tagline_parts.append("[magenta]AI-powered[/magenta]")
    
    console.print(" | ".join(tagline_parts))
    console.print()


def print_quick_help():
    """Print quick command reference."""
    console.print("[bold]Quick commands:[/bold]")
    console.print("  [cyan]stemmy interactive[/cyan]             [bold]Recommended:[/bold] Interactive shell (stays open)")
    console.print("  [cyan]stemmy ask[/cyan] [dim]\"request\"[/dim]           Single AI request (then exits)")
    console.print("  [cyan]stemmy analyze modes[/cyan]           List all analyzers")
    console.print("  [cyan]stemmy --help[/cyan]                  Full documentation")
    console.print()


def print_welcome():
    """Print full welcome screen with banner and help."""
    print_banner(show_version=True, show_stats=True)
    print_quick_help()


def print_section_header(title: str, subtitle: str = ""):
    """Print a styled section header."""
    header = Text()
    header.append(f"━━━ ", style="dim")
    header.append(title, style=f"bold {ACCENT_COLOR}")
    if subtitle:
        header.append(f" ", style="dim")
        header.append(subtitle, style="dim italic")
    header.append(" ━━━", style="dim")
    console.print(header)


def print_step(step_num: int, total: int, description: str, status: str = "pending"):
    """Print a workflow step with status."""
    status_icons = {
        "pending": "[dim]○[/dim]",
        "in_progress": "[yellow]◐[/yellow]",
        "completed": "[green]●[/green]",
        "failed": "[red]✗[/red]",
    }
    icon = status_icons.get(status, status_icons["pending"])
    console.print(f"  {icon} [dim]{step_num}/{total}[/dim] {description}")


def print_result_box(
    title: str,
    content: str,
    file_info: str = "",
    style: str = "cyan",
):
    """Print a result in a styled box."""
    box_content = Text()
    box_content.append(content)
    
    if file_info:
        box_content.append(f"\n\n[dim]{file_info}[/dim]")
    
    panel = Panel(
        box_content,
        title=f"[bold]{title}[/bold]",
        border_style=style,
        padding=(0, 1),
    )
    console.print(panel)
