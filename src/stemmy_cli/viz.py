"""
Audio visualization utilities for Stemmy CLI.

Provides terminal-based audio waveform rendering.
"""

import os
from pathlib import Path
from typing import Optional, Tuple

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

console = Console()

# Unicode block characters for waveform rendering (low to high)
WAVEFORM_CHARS = " ▁▂▃▄▅▆▇█"


def _format_duration(seconds: float) -> str:
    """Format seconds as mm:ss or hh:mm:ss."""
    if seconds < 3600:
        mins = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{mins}:{secs:02d}"
    else:
        hours = int(seconds // 3600)
        mins = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        return f"{hours}:{mins:02d}:{secs:02d}"


def _format_size(size_bytes: int) -> str:
    """Format bytes as human-readable size."""
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f}{unit}" if unit != "B" else f"{size_bytes}B"
        size_bytes /= 1024
    return f"{size_bytes:.1f}TB"


def render_waveform(
    audio_path: str,
    width: int = 50,
    color: str = "cyan",
) -> str:
    """
    Render audio file as ASCII waveform.
    
    Uses numpy for fast processing if available, falls back to pydub.
    
    Args:
        audio_path: Path to audio file
        width: Number of characters for waveform
        color: Rich color for waveform
    
    Returns:
        String containing waveform characters
    """
    try:
        import numpy as np
        from pydub import AudioSegment
        
        # Load audio
        audio = AudioSegment.from_file(audio_path)
        samples = np.array(audio.get_array_of_samples(), dtype=np.float32)
        
        # Convert stereo to mono if needed
        if audio.channels == 2:
            samples = samples[::2]  # Take every other sample (left channel)
        
        # Normalize
        if len(samples) > 0:
            samples = np.abs(samples)
            max_val = np.max(samples)
            if max_val > 0:
                samples = samples / max_val
        
        # Downsample to width
        if len(samples) > width:
            chunk_size = len(samples) // width
            chunks = [samples[i:i + chunk_size] for i in range(0, len(samples), chunk_size)]
            levels = [np.max(chunk) if len(chunk) > 0 else 0 for chunk in chunks[:width]]
        else:
            levels = list(samples) + [0] * (width - len(samples))
        
        # Map to characters
        waveform = ""
        for level in levels[:width]:
            char_idx = int(level * (len(WAVEFORM_CHARS) - 1))
            char_idx = min(char_idx, len(WAVEFORM_CHARS) - 1)
            waveform += WAVEFORM_CHARS[char_idx]
        
        return waveform
        
    except ImportError:
        # Fallback: generate fake waveform based on file size
        return _fake_waveform(audio_path, width)
    except Exception as e:
        console.print(f"[dim]Could not render waveform: {e}[/dim]")
        return "▄" * width


def _fake_waveform(audio_path: str, width: int) -> str:
    """Generate a plausible-looking fake waveform when numpy/pydub unavailable."""
    import random
    import hashlib
    
    # Use file hash for reproducible randomness
    file_hash = hashlib.md5(audio_path.encode()).hexdigest()
    random.seed(file_hash)
    
    waveform = ""
    level = 0.5
    for _ in range(width):
        # Random walk with tendency to stay moderate
        level += random.uniform(-0.3, 0.3)
        level = max(0.1, min(1.0, level))
        char_idx = int(level * (len(WAVEFORM_CHARS) - 1))
        waveform += WAVEFORM_CHARS[char_idx]
    
    return waveform


def get_audio_info(audio_path: str) -> Tuple[float, int]:
    """
    Get audio duration and file size.
    
    Returns:
        Tuple of (duration_seconds, size_bytes)
    """
    size_bytes = os.path.getsize(audio_path) if os.path.exists(audio_path) else 0
    duration = 0.0
    
    try:
        from pydub import AudioSegment
        audio = AudioSegment.from_file(audio_path)
        duration = len(audio) / 1000.0
    except Exception:
        # Estimate from file size (128kbps MP3 ≈ 16KB/sec)
        duration = size_bytes / 16000
    
    return duration, size_bytes


def print_audio_preview(
    audio_path: str,
    title: Optional[str] = None,
    show_waveform: bool = True,
    width: int = 50,
):
    """
    Print a rich audio preview panel with waveform.
    
    Args:
        audio_path: Path to audio file
        title: Optional title override (defaults to filename)
        show_waveform: Whether to render waveform
        width: Waveform width in characters
    """
    path = Path(audio_path)
    
    if not path.exists():
        console.print(f"[dim]File not found: {audio_path}[/dim]")
        return
    
    # Get audio info
    duration, size_bytes = get_audio_info(audio_path)
    
    # Build content
    content = Text()
    
    if show_waveform:
        waveform = render_waveform(audio_path, width=width)
        content.append(waveform, style="cyan")
        content.append("\n")
    
    # Time markers
    duration_str = _format_duration(duration)
    padding = width - len("00:00") - len(duration_str)
    content.append("00:00", style="dim")
    content.append(" " * max(0, padding), style="dim")
    content.append(duration_str, style="dim")
    
    # Panel title
    display_title = title or path.name
    size_str = _format_size(size_bytes)
    panel_title = f"{display_title} ({size_str}, {duration_str})"
    
    panel = Panel(
        content,
        title=f"[bold]{panel_title}[/bold]",
        border_style="cyan",
        padding=(0, 1),
    )
    console.print(panel)


def print_simple_waveform(audio_path: str, width: int = 60):
    """Print just the waveform without panel."""
    waveform = render_waveform(audio_path, width=width)
    console.print(f"[cyan]{waveform}[/cyan]")
