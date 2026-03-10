"""
Creative audio recipes for Stemmy CLI.

Pre-built AI-powered workflows for common audio compilation tasks.
"""

import json
import sqlite3
import tempfile
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Optional

import typer
import httpx
from rich.console import Console
from rich.panel import Panel

from stemmy_cli.output import (
    print_error,
    print_success,
    print_info,
    print_warning,
    status_spinner,
    WorkflowProgress,
    print_workflow_result,
)
from stemmy_cli.viz import print_audio_preview
from stemmy_cli.config import get_database_path
from stemmy_cli.audio.local_extract import extract_segments_local

app = typer.Typer(help="Creative audio recipes")
console = Console()

API_BASE = "http://localhost:5001"


def _get_db_path() -> Path:
    """Get database path from config."""
    return get_database_path()


def _get_fragments_for_pattern(
    pattern_type: str,
    pattern: str = "",
    format_id: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Get fragments matching a pattern."""
    from stemmy_cli.analyzers.text import search_text_pattern
    from stemmy_cli.analyzers.lexical import search_lexical_pattern
    
    conn = sqlite3.connect(str(_get_db_path()))
    conn.row_factory = sqlite3.Row
    
    sql = """
        SELECT f.id, f.text, f.words, f.item_audio_url, f.item_id, f.item_title
        FROM fragments f
        WHERE f.words IS NOT NULL AND f.words != '[]'
        AND f.item_audio_url IS NOT NULL AND f.item_audio_url != ''
    """
    params = []
    
    if format_id:
        sql += " AND f.format_id = ?"
        params.append(format_id)
    
    sql += " LIMIT 2000"
    
    cursor = conn.execute(sql, params)
    matches = []
    
    for row in cursor.fetchall():
        try:
            words_data = json.loads(row["words"]) if row["words"] else []
            
            # Try text patterns first
            if pattern_type in ["questions", "exclamations", "coherence"]:
                results = search_text_pattern(
                    words_data=words_data,
                    mode=pattern_type,
                    audio_url=row["item_audio_url"],
                    fragment_id=row["id"],
                    item_id=row["item_id"] or "",
                    item_title=row["item_title"] or "",
                )
            else:
                results = search_lexical_pattern(
                    words_data=words_data,
                    mode=pattern_type,
                    pattern=pattern,
                    audio_url=row["item_audio_url"],
                    fragment_id=row["id"],
                    item_id=row["item_id"] or "",
                    item_title=row["item_title"] or "",
                )
            
            matches.extend(results)
        except Exception:
            continue
    
    conn.close()
    return matches[:limit]


def _generate_tts(text: str, voice: str = "alloy") -> Optional[str]:
    """Generate TTS audio and return temporary file path."""
    try:
        with httpx.Client(timeout=60.0) as client:
            response = client.post(
                f"{API_BASE}/api/tts",
                json={"text": text, "voice": voice},
            )
            response.raise_for_status()
            data = response.json()
            return data.get("audio_url")
    except Exception as e:
        console.print(f"[dim]TTS error: {e}[/dim]")
        return None


def _extract_audio_segments(segments: List[Dict[str, Any]]) -> List[str]:
    """Extract audio segments locally and return paths."""
    if not segments:
        return []
    
    try:
        segments_payload = [
            {
                "id": str(i),
                "audio_url": s["audio_url"],
                "start_time": s["start_ms"] / 1000.0,
                "end_time": s["end_ms"] / 1000.0,
            }
            for i, s in enumerate(segments)
            if s.get("audio_url") and s.get("start_ms") is not None
        ]
        if not segments_payload:
            return []

        extracted, failed = extract_segments_local(segments_payload)
        if failed:
            console.print(f"[dim]Extract failed for {len(failed)} segments[/dim]")
        return [r.get("fragment_audio_url") for r in extracted if r.get("fragment_audio_url")]
    except Exception as e:
        console.print(f"[dim]Extract error: {e}[/dim]")
        return []


def _concatenate_audio(audio_urls: List[str], output_path: str) -> bool:
    """Concatenate audio files using ffmpeg."""
    if not audio_urls:
        return False
    
    try:
        # Download files
        temp_dir = tempfile.mkdtemp()
        local_files = []
        
        with httpx.Client(timeout=30.0) as client:
            for i, url in enumerate(audio_urls):
                if url.startswith("http"):
                    resp = client.get(url)
                    local_path = Path(temp_dir) / f"segment_{i:04d}.mp3"
                    local_path.write_bytes(resp.content)
                    local_files.append(str(local_path))
                elif Path(url).exists():
                    local_files.append(url)
        
        if not local_files:
            return False
        
        # Create concat file
        concat_file = Path(temp_dir) / "concat.txt"
        with open(concat_file, "w") as f:
            for lf in local_files:
                f.write(f"file '{lf}'\n")
        
        # Run ffmpeg
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
    
    except Exception as e:
        console.print(f"[dim]Concat error: {e}[/dim]")
        return False


@app.command("questions")
def questions_compilation(
    format_id: Optional[str] = typer.Option(None, "--format", "-f", help="Filter by format ID"),
    limit: int = typer.Option(20, "--limit", "-l", help="Max questions to include"),
    output: str = typer.Option("questions_compilation.mp3", "--output", "-o", help="Output file path"),
    with_intro: bool = typer.Option(True, "--intro/--no-intro", help="Add TTS intro"),
):
    """
    Create a compilation of all questions found in audio.
    
    Finds fragments containing questions and compiles them into a single audio file.
    """
    steps = ["Find questions", "Extract audio", "Generate intro", "Concatenate"]
    
    with WorkflowProgress("Questions Compilation", steps) as progress:
        # Step 1: Find questions
        progress.start_step(0)
        matches = _get_fragments_for_pattern("questions", format_id=format_id, limit=limit)
        if not matches:
            progress.fail_step(0)
            print_error("No questions found")
            raise typer.Exit(1)
        progress.complete_step(0)
        console.print(f"  [dim]Found {len(matches)} questions[/dim]")
        
        # Step 2: Extract audio
        progress.start_step(1)
        audio_urls = _extract_audio_segments(matches)
        if not audio_urls:
            progress.fail_step(1)
            print_error("Failed to extract audio")
            raise typer.Exit(1)
        progress.complete_step(1)
        
        # Step 3: Generate intro
        all_urls = []
        if with_intro:
            progress.start_step(2)
            intro_text = f"Hier zijn {len(matches)} vragen uit de audio."
            intro_url = _generate_tts(intro_text)
            if intro_url:
                all_urls.append(intro_url)
            progress.complete_step(2)
        else:
            progress.complete_step(2)
        
        all_urls.extend(audio_urls)
        
        # Step 4: Concatenate
        progress.start_step(3)
        if _concatenate_audio(all_urls, output):
            progress.complete_step(3)
        else:
            progress.fail_step(3)
            print_error("Failed to concatenate audio")
            raise typer.Exit(1)
    
    print_workflow_result(
        "Questions Compilation Complete",
        output,
        file_size=f"{Path(output).stat().st_size / 1024:.1f}KB" if Path(output).exists() else "",
    )
    print_audio_preview(output)


@app.command("highlights")
def highlights_compilation(
    query: str = typer.Argument(..., help="Search query for highlights"),
    format_id: Optional[str] = typer.Option(None, "--format", "-f", help="Filter by format"),
    limit: int = typer.Option(10, "--limit", "-l", help="Max highlights"),
    output: str = typer.Option("highlights.mp3", "--output", "-o", help="Output file"),
):
    """
    Create a highlights compilation based on a search query.
    
    Example: stemmy recipes highlights "AI technology" --limit 10
    """
    conn = sqlite3.connect(str(_get_db_path()))
    conn.row_factory = sqlite3.Row
    
    console.print(f"[cyan]Creating highlights for: {query}[/cyan]")
    
    # Search fragments
    sql = """
        SELECT f.id, f.text, f.start_time, f.end_time, f.item_audio_url, f.item_title
        FROM fragments f
        WHERE f.text LIKE ?
        AND f.item_audio_url IS NOT NULL
    """
    params = [f"%{query}%"]
    if format_id:
        sql += " AND f.format_id = ?"
        params.append(format_id)
    sql += f" LIMIT {limit}"
    
    cursor = conn.execute(sql, params)
    fragments = [dict(row) for row in cursor.fetchall()]
    conn.close()
    
    if not fragments:
        print_warning("No fragments found matching query")
        raise typer.Exit(1)
    
    console.print(f"[green]Found {len(fragments)} fragments[/green]")
    
    # Build segments
    segments = [
        {
            "audio_url": f["item_audio_url"],
            "start_ms": int(f["start_time"] * 1000) if f["start_time"] else 0,
            "end_ms": int(f["end_time"] * 1000) if f["end_time"] else 10000,
        }
        for f in fragments
    ]
    
    with status_spinner("Extracting audio segments...", "Segments extracted"):
        audio_urls = _extract_audio_segments(segments)
    
    if not audio_urls:
        print_error("Failed to extract audio")
        raise typer.Exit(1)
    
    with status_spinner("Creating compilation...", "Compilation created"):
        success = _concatenate_audio(audio_urls, output)
    
    if success:
        print_success(f"Created: {output}")
        print_audio_preview(output)
    else:
        print_error("Failed to create compilation")


@app.command("trailer")
def trailer_recipe(
    format_id: str = typer.Argument(..., help="Format ID to create trailer for"),
    duration: int = typer.Option(60, "--duration", "-d", help="Target duration in seconds"),
    output: str = typer.Option("trailer.mp3", "--output", "-o", help="Output file"),
):
    """
    Create a podcast trailer from highlights.
    
    Selects the most engaging moments from a podcast format.
    """
    conn = sqlite3.connect(str(_get_db_path()))
    conn.row_factory = sqlite3.Row
    
    # Get format info
    format_row = conn.execute(
        "SELECT title FROM formats WHERE id = ?", [format_id]
    ).fetchone()
    
    if not format_row:
        print_error(f"Format not found: {format_id}")
        raise typer.Exit(1)
    
    format_title = format_row["title"]
    console.print(f"[cyan]Creating trailer for: {format_title}[/cyan]")
    
    # Get diverse fragments (questions, exclamations, keywords)
    fragments = []
    
    # Questions
    questions = _get_fragments_for_pattern("questions", format_id=format_id, limit=5)
    fragments.extend(questions[:3])
    
    # Exclamations  
    exclamations = _get_fragments_for_pattern("exclamations", format_id=format_id, limit=5)
    fragments.extend(exclamations[:2])
    
    conn.close()
    
    if not fragments:
        print_warning("Not enough content for trailer")
        raise typer.Exit(1)
    
    console.print(f"[dim]Selected {len(fragments)} fragments for trailer[/dim]")
    
    # Generate intro TTS
    intro_text = f"Luister naar {format_title}. Een selectie van de beste momenten."
    intro_url = _generate_tts(intro_text)
    
    # Extract segments
    audio_urls = []
    if intro_url:
        audio_urls.append(intro_url)
    
    extracted = _extract_audio_segments(fragments)
    audio_urls.extend(extracted)
    
    # Generate outro
    outro_text = "Abonneer nu en mis geen aflevering."
    outro_url = _generate_tts(outro_text)
    if outro_url:
        audio_urls.append(outro_url)
    
    if _concatenate_audio(audio_urls, output):
        print_success(f"Created trailer: {output}")
        print_audio_preview(output)
    else:
        print_error("Failed to create trailer")


@app.command("narrated")
def narrated_compilation(
    topic: str = typer.Argument(..., help="Topic for compilation"),
    intro_text: str = typer.Option(None, "--intro", "-i", help="Custom intro text"),
    outro_text: str = typer.Option(None, "--outro", help="Custom outro text"),
    voice: str = typer.Option("alloy", "--voice", "-v", help="TTS voice"),
    limit: int = typer.Option(10, "--limit", "-l", help="Max fragments"),
    output: str = typer.Option("narrated.mp3", "--output", "-o", help="Output file"),
):
    """
    Create a narrated compilation with TTS intro/outro.
    
    Mixes AI-generated narration with real audio fragments.
    
    Example:
        stemmy recipes narrated "artificial intelligence" --intro "Welcome to our AI roundup"
    """
    console.print(f"[cyan]Creating narrated compilation: {topic}[/cyan]")
    
    # Search for fragments
    conn = sqlite3.connect(str(_get_db_path()))
    conn.row_factory = sqlite3.Row
    
    cursor = conn.execute("""
        SELECT f.id, f.text, f.start_time, f.end_time, f.item_audio_url
        FROM fragments f
        WHERE f.text LIKE ?
        AND f.item_audio_url IS NOT NULL
        LIMIT ?
    """, [f"%{topic}%", limit])
    
    fragments = [dict(row) for row in cursor.fetchall()]
    conn.close()
    
    if not fragments:
        print_warning(f"No fragments found for: {topic}")
        raise typer.Exit(1)
    
    console.print(f"[dim]Found {len(fragments)} fragments[/dim]")
    
    audio_parts = []
    
    # Generate intro
    if intro_text is None:
        intro_text = f"In deze compilatie hoor je fragmenten over {topic}."
    
    with status_spinner("Generating intro...", "Intro ready"):
        intro_url = _generate_tts(intro_text, voice=voice)
        if intro_url:
            audio_parts.append(intro_url)
    
    # Extract fragments
    segments = [
        {
            "audio_url": f["item_audio_url"],
            "start_ms": int(f["start_time"] * 1000) if f["start_time"] else 0,
            "end_ms": int(f["end_time"] * 1000) if f["end_time"] else 10000,
        }
        for f in fragments
    ]
    
    with status_spinner("Extracting fragments...", "Fragments ready"):
        extracted = _extract_audio_segments(segments)
        audio_parts.extend(extracted)
    
    # Generate outro
    if outro_text is None:
        outro_text = f"Dat was de compilatie over {topic}. Bedankt voor het luisteren."
    
    with status_spinner("Generating outro...", "Outro ready"):
        outro_url = _generate_tts(outro_text, voice=voice)
        if outro_url:
            audio_parts.append(outro_url)
    
    # Concatenate
    with status_spinner("Creating final audio...", "Complete"):
        success = _concatenate_audio(audio_parts, output)
    
    if success:
        print_success(f"Created: {output}")
        print_audio_preview(output)
    else:
        print_error("Failed to create narrated compilation")


@app.command("word-montage")
def word_montage(
    word: str = typer.Argument(..., help="Word to create montage of"),
    format_id: Optional[str] = typer.Option(None, "--format", "-f", help="Filter by format"),
    limit: int = typer.Option(30, "--limit", "-l", help="Max occurrences"),
    output: str = typer.Option(None, "--output", "-o", help="Output file"),
):
    """
    Create a montage of a single word spoken in different contexts.
    
    Example: stemmy recipes word-montage "AI" --limit 20
    """
    if output is None:
        output = f"{word.lower()}_montage.mp3"
    
    console.print(f"[cyan]Creating word montage: {word}[/cyan]")
    
    matches = _get_fragments_for_pattern("exact", pattern=word, format_id=format_id, limit=limit)
    
    if not matches:
        print_warning(f"No occurrences of '{word}' found")
        raise typer.Exit(1)
    
    console.print(f"[dim]Found {len(matches)} occurrences[/dim]")
    
    with status_spinner("Extracting word audio...", "Extracted"):
        audio_urls = _extract_audio_segments(matches)
    
    if not audio_urls:
        print_error("Failed to extract audio")
        raise typer.Exit(1)
    
    with status_spinner("Creating montage...", "Complete"):
        success = _concatenate_audio(audio_urls, output)
    
    if success:
        print_success(f"Created: {output}")
        print_audio_preview(output)
    else:
        print_error("Failed to create montage")


@app.command("list")
def list_recipes():
    """List all available recipes."""
    recipes = [
        ("questions", "Compile all questions from audio"),
        ("highlights", "Create highlights based on search query"),
        ("trailer", "Generate podcast trailer from best moments"),
        ("narrated", "Mix TTS narration with real fragments"),
        ("word-montage", "Create montage of a single word"),
    ]
    
    console.print()
    console.print("[bold]Available Recipes[/bold]")
    console.print()
    for name, desc in recipes:
        console.print(f"  [cyan]{name:15}[/cyan] {desc}")
    console.print()
    console.print("[dim]Use: stemmy recipes <recipe-name> --help for details[/dim]")
