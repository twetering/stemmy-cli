"""
Audio mixing commands for Stemmy CLI.

Seamlessly mix AI-generated speech with real audio fragments.
"""

import json
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional

import typer
import httpx
from rich.console import Console

from stemmy_cli.output import (
    print_error,
    print_success,
    print_info,
    print_warning,
    status_spinner,
    create_detailed_progress,
)
from stemmy_cli.viz import print_audio_preview

app = typer.Typer(help="Audio mixing and blending")
console = Console()

API_BASE = "http://localhost:5001"


def _download_file(url: str, local_path: Path) -> bool:
    """Download a file from URL."""
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(url)
            resp.raise_for_status()
            local_path.write_bytes(resp.content)
            return True
    except Exception as e:
        console.print(f"[dim]Download error: {e}[/dim]")
        return False


def _generate_tts(text: str, voice: str = "alloy") -> Optional[str]:
    """Generate TTS and return audio URL."""
    try:
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(
                f"{API_BASE}/api/tts",
                json={"text": text, "voice": voice},
            )
            resp.raise_for_status()
            return resp.json().get("audio_url")
    except Exception as e:
        console.print(f"[dim]TTS error: {e}[/dim]")
        return None


def _normalize_audio(input_path: str, output_path: str, target_db: float = -16.0) -> bool:
    """Normalize audio to target dB level."""
    result = subprocess.run(
        [
            "ffmpeg", "-y", "-i", input_path,
            "-af", f"loudnorm=I={target_db}:TP=-1.5:LRA=11",
            "-ar", "44100", "-ac", "2",
            output_path,
        ],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _apply_crossfade(
    input_files: List[str],
    output_path: str,
    crossfade_ms: int = 500,
) -> bool:
    """Concatenate audio files with crossfade."""
    if len(input_files) < 2:
        # No crossfade needed for single file
        if input_files:
            import shutil
            shutil.copy(input_files[0], output_path)
            return True
        return False
    
    # Build complex filter for crossfade
    filter_parts = []
    for i in range(len(input_files)):
        filter_parts.append(f"[{i}:a]")
    
    # Simple concat for now (ffmpeg crossfade is complex)
    concat_file = Path(tempfile.mkdtemp()) / "concat.txt"
    with open(concat_file, "w") as f:
        for fp in input_files:
            f.write(f"file '{fp}'\n")
    
    result = subprocess.run(
        [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", str(concat_file),
            "-c:a", "libmp3lame", "-q:a", "2",
            output_path,
        ],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _add_background_music(
    voice_path: str,
    music_path: str,
    output_path: str,
    music_volume: float = 0.15,
) -> bool:
    """Mix voice with background music."""
    result = subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", voice_path,
            "-i", music_path,
            "-filter_complex",
            f"[1:a]volume={music_volume}[music];[0:a][music]amix=inputs=2:duration=first",
            "-c:a", "libmp3lame", "-q:a", "2",
            output_path,
        ],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


@app.command("create")
def create_mix(
    output: str = typer.Option("mix_output.mp3", "--output", "-o", help="Output file path"),
    tts: List[str] = typer.Option([], "--tts", "-t", help="TTS text segments (in order)"),
    fragment: List[str] = typer.Option([], "--fragment", "-f", help="Fragment URLs (in order)"),
    voice: str = typer.Option("alloy", "--voice", "-v", help="TTS voice"),
    normalize: bool = typer.Option(True, "--normalize/--no-normalize", help="Normalize audio levels"),
):
    """
    Create a custom audio mix from TTS and fragments.
    
    Interleave TTS narration with real audio fragments.
    
    Example:
        stemmy mix create \\
            --tts "Welcome to this compilation" \\
            --fragment "https://example.com/clip1.mp3" \\
            --tts "And here's another clip" \\
            --fragment "https://example.com/clip2.mp3" \\
            --output my_mix.mp3
    """
    if not tts and not fragment:
        print_error("Provide at least one --tts or --fragment")
        raise typer.Exit(1)
    
    # Process items in order they appear
    # Note: typer collects options separately, so we need a different approach
    # For now, alternate between tts and fragments
    
    console.print("[cyan]Creating audio mix...[/cyan]")
    
    temp_dir = Path(tempfile.mkdtemp())
    audio_files = []
    
    # Generate TTS segments
    if tts:
        console.print(f"[dim]Generating {len(tts)} TTS segments...[/dim]")
        for i, text in enumerate(tts):
            url = _generate_tts(text, voice=voice)
            if url:
                local_path = temp_dir / f"tts_{i:03d}.mp3"
                if _download_file(url, local_path):
                    audio_files.append(("tts", str(local_path), i))
    
    # Download fragments
    if fragment:
        console.print(f"[dim]Downloading {len(fragment)} fragments...[/dim]")
        for i, url in enumerate(fragment):
            local_path = temp_dir / f"frag_{i:03d}.mp3"
            if _download_file(url, local_path):
                audio_files.append(("fragment", str(local_path), i))
    
    if not audio_files:
        print_error("No audio files to mix")
        raise typer.Exit(1)
    
    # Sort by type then index to interleave
    # (tts_0, frag_0, tts_1, frag_1, ...)
    sorted_files = sorted(audio_files, key=lambda x: (x[2], 0 if x[0] == "tts" else 1))
    file_paths = [f[1] for f in sorted_files]
    
    # Normalize if requested
    if normalize:
        console.print("[dim]Normalizing audio levels...[/dim]")
        normalized_files = []
        for fp in file_paths:
            norm_path = fp.replace(".mp3", "_norm.mp3")
            if _normalize_audio(fp, norm_path):
                normalized_files.append(norm_path)
            else:
                normalized_files.append(fp)
        file_paths = normalized_files
    
    # Concatenate
    with status_spinner("Creating final mix...", "Mix complete"):
        success = _apply_crossfade(file_paths, output)
    
    if success:
        print_success(f"Created: {output}")
        print_audio_preview(output)
    else:
        print_error("Failed to create mix")


@app.command("tts")
def generate_tts_command(
    text: str = typer.Argument(..., help="Text to convert to speech"),
    voice: str = typer.Option("alloy", "--voice", "-v", help="Voice: alloy, echo, fable, onyx, nova, shimmer"),
    output: str = typer.Option(None, "--output", "-o", help="Output file (default: auto-generated)"),
):
    """
    Generate TTS audio from text.
    
    Example: stemmy mix tts "Hello world" --voice nova --output greeting.mp3
    """
    if output is None:
        safe_name = "".join(c if c.isalnum() else "_" for c in text[:20])
        output = f"tts_{safe_name}.mp3"
    
    console.print(f"[cyan]Generating TTS...[/cyan]")
    console.print(f"[dim]Text: {text[:50]}{'...' if len(text) > 50 else ''}[/dim]")
    
    with status_spinner(f"Using voice: {voice}", "TTS generated"):
        url = _generate_tts(text, voice=voice)
    
    if url:
        with status_spinner("Downloading audio...", "Downloaded"):
            if _download_file(url, Path(output)):
                print_success(f"Created: {output}")
                print_audio_preview(output)
            else:
                print_error("Failed to download audio")
    else:
        print_error("TTS generation failed")


@app.command("concat")
def concat_audio(
    files: List[str] = typer.Argument(..., help="Audio files to concatenate"),
    output: str = typer.Option("concatenated.mp3", "--output", "-o", help="Output file"),
    normalize: bool = typer.Option(False, "--normalize", "-n", help="Normalize audio levels"),
):
    """
    Concatenate multiple audio files.
    
    Example: stemmy mix concat file1.mp3 file2.mp3 file3.mp3 --output combined.mp3
    """
    if len(files) < 2:
        print_warning("Need at least 2 files to concatenate")
        raise typer.Exit(1)
    
    # Check files exist
    missing = [f for f in files if not Path(f).exists()]
    if missing:
        print_error(f"Files not found: {', '.join(missing)}")
        raise typer.Exit(1)
    
    console.print(f"[cyan]Concatenating {len(files)} files...[/cyan]")
    
    file_list = list(files)
    
    if normalize:
        temp_dir = Path(tempfile.mkdtemp())
        normalized = []
        with create_detailed_progress() as progress:
            task = progress.add_task("Normalizing", total=len(files))
            for i, f in enumerate(files):
                norm_path = temp_dir / f"norm_{i:03d}.mp3"
                _normalize_audio(f, str(norm_path))
                normalized.append(str(norm_path))
                progress.update(task, advance=1)
        file_list = normalized
    
    with status_spinner("Concatenating...", "Complete"):
        success = _apply_crossfade(file_list, output)
    
    if success:
        print_success(f"Created: {output}")
        print_audio_preview(output)
    else:
        print_error("Concatenation failed")


@app.command("with-music")
def add_music(
    voice_file: str = typer.Argument(..., help="Voice/speech audio file"),
    music_file: str = typer.Argument(..., help="Background music file"),
    output: str = typer.Option("with_music.mp3", "--output", "-o", help="Output file"),
    music_volume: float = typer.Option(0.15, "--volume", "-v", help="Music volume (0.0-1.0)"),
):
    """
    Add background music to voice audio.
    
    Example: stemmy mix with-music narration.mp3 ambient.mp3 --volume 0.2
    """
    if not Path(voice_file).exists():
        print_error(f"Voice file not found: {voice_file}")
        raise typer.Exit(1)
    
    if not Path(music_file).exists():
        print_error(f"Music file not found: {music_file}")
        raise typer.Exit(1)
    
    console.print(f"[cyan]Adding background music...[/cyan]")
    console.print(f"[dim]Music volume: {music_volume * 100:.0f}%[/dim]")
    
    with status_spinner("Mixing audio...", "Complete"):
        success = _add_background_music(voice_file, music_file, output, music_volume)
    
    if success:
        print_success(f"Created: {output}")
        print_audio_preview(output)
    else:
        print_error("Failed to add music")


@app.command("voices")
def list_voices():
    """List available TTS voices."""
    voices = [
        ("alloy", "Neutral, balanced voice"),
        ("echo", "Warm, conversational male"),
        ("fable", "Expressive, British accent"),
        ("onyx", "Deep, authoritative male"),
        ("nova", "Friendly, energetic female"),
        ("shimmer", "Soft, gentle female"),
    ]
    
    console.print()
    console.print("[bold]Available TTS Voices[/bold]")
    console.print()
    for name, desc in voices:
        console.print(f"  [cyan]{name:10}[/cyan] {desc}")
    console.print()
    console.print("[dim]Use: stemmy mix tts \"text\" --voice <name>[/dim]")
