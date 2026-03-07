"""
Advanced analyzers CLI commands.

Provides 20+ search modes organized by category:
- lexical: Word patterns (starts_with, ends_with, alliteration, palindrome, etc.)
- syntactic: Syntactic patterns (comparison, enumeration, causation, temporal, etc.)
- speaker: Speaker analysis (filler_words, speaker_change, long_pauses, etc.)
- style: Style analysis (formality, rhetoric, tone, dialog, argumentation)
- text: Text analysis (complexity, questions, exclamations, etc.)
"""

import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

import httpx
import typer

from stemmy_cli.adapters.sqlite import SQLiteAdapter
from stemmy_cli.adapters.http import HTTPAdapter
from stemmy_cli.output import output_result, print_error, print_success, print_info, print_warning

# Import all analyzers
from stemmy_cli.analyzers.lexical import LEXICAL_MODES, search_lexical_pattern
from stemmy_cli.analyzers.syntactic import SYNTACTIC_PATTERNS, search_syntactic_pattern
from stemmy_cli.analyzers.speaker import SPEAKER_MODES, search_speaker_pattern
from stemmy_cli.analyzers.style import STYLE_MODES, search_style_pattern
from stemmy_cli.analyzers.text import TEXT_MODES, search_text_pattern

app = typer.Typer(help="Advanced word and text analyzers (20+ modes)")

# Constants
WORD_BUFFER_MS = 50
API_BASE_URL = "http://localhost:5000"


def _get_fragments_with_words(
    adapter: SQLiteAdapter,
    format_id: Optional[str] = None,
    item_id: Optional[str] = None,
    limit: int = 1000,
) -> List[Dict]:
    """Get fragments with word-level timing data."""
    conditions = [
        "words IS NOT NULL", 
        "words != ''", 
        "words != '[]'",
        "f.item_audio_url IS NOT NULL",
        "f.item_audio_url != ''",
    ]
    params = []
    
    if item_id:
        conditions.append("item_id = ?")
        params.append(item_id)
    
    if format_id:
        conditions.append("item_id IN (SELECT id FROM items WHERE format_id = ?)")
        params.append(format_id)
    
    where_clause = " AND ".join(conditions)
    
    return adapter.execute_raw(
        f'''
        SELECT f.id, f.text, f.words, f.item_audio_url, f.item_id, i.title as item_title
        FROM fragments f
        LEFT JOIN items i ON f.item_id = i.id
        WHERE {where_clause}
        LIMIT {limit}
        ''',
        params,
    )


def _deduplicate_matches(matches: List[Dict], tolerance_ms: int = 100) -> List[Dict]:
    """Remove duplicate matches based on audio_url and timing."""
    seen = []
    unique = []
    
    for match in matches:
        audio_url = match.get("audio_url", "")
        start_ms = int(match.get("start_time", 0) * 1000)
        end_ms = int(match.get("end_time", 0) * 1000)
        
        is_dup = False
        for seen_url, seen_start, seen_end in seen:
            if seen_url == audio_url:
                if abs(seen_start - start_ms) < tolerance_ms and abs(seen_end - end_ms) < tolerance_ms:
                    is_dup = True
                    break
        
        if not is_dup:
            seen.append((audio_url, start_ms, end_ms))
            unique.append(match)
    
    return unique


def _batch_extract_audio(matches: List[Dict], http: HTTPAdapter) -> List[Dict]:
    """Extract audio for all matches using batch API (async with polling)."""
    if not matches:
        return []
    
    segments = []
    for m in matches:
        start_time = m.get("start_time", 0)
        end_time = m.get("end_time", 0)
        
        # Add buffer (in seconds)
        start_time = max(0, start_time - WORD_BUFFER_MS / 1000)
        end_time = end_time + WORD_BUFFER_MS / 1000
        
        segments.append({
            "id": m.get("id", str(uuid.uuid4())),
            "audio_url": m.get("audio_url", ""),
            "start_time": start_time,
            "end_time": end_time,
        })
    
    print_info(f"Extracting {len(segments)} audio segments...")
    
    try:
        # Start async extraction task
        response = httpx.post(
            f"{API_BASE_URL}/api/extract-audio-segments",
            json={"segments": segments},
            timeout=60.0,
        )
        
        if response.status_code != 200:
            print_error(f"Batch extract failed: {response.status_code}")
            return []
        
        result = response.json()
        task_id = result.get("task_id")
        
        if not task_id:
            print_error("No task_id received from extraction API")
            return []
        
        print_info(f"Extraction task started: {task_id}")
        
        # Poll for completion
        max_wait = 300
        poll_interval = 2
        waited = 0
        extraction_results = []
        
        while waited < max_wait:
            status_resp = httpx.get(
                f"{API_BASE_URL}/api/extract-audio-segments-status/{task_id}",
                timeout=30.0,
            )
            
            if status_resp.status_code != 200:
                time.sleep(poll_interval)
                waited += poll_interval
                continue
            
            status = status_resp.json()
            state = status.get("state")
            
            if state == "SUCCESS":
                result_data = status.get("result", {})
                extraction_results = result_data.get("segments", []) or result_data.get("results", [])
                failed_count = result_data.get("stats", {}).get("failed", 0)
                print_success(f"Extraction complete: {len(extraction_results)} segments, {failed_count} failed")
                break
            elif state == "FAILURE":
                print_error(f"Extraction failed: {status.get('error')}")
                return []
            elif state == "PENDING" or state == "STARTED":
                progress = status.get("progress", 0)
                print_info(f"Extraction in progress... {progress}%")
            
            time.sleep(poll_interval)
            waited += poll_interval
        
        # Map extraction results back to matches
        for ext in extraction_results:
            # The segment id is nested in ext["segment"]["id"]
            segment_data = ext.get("segment", {})
            seg_id = segment_data.get("id") or ext.get("id")
            audio_url = ext.get("fragment_audio_url") or ext.get("audio_url") or ext.get("url")
            
            for m in matches:
                if m.get("id") == seg_id:
                    m["extracted_audio_url"] = audio_url
                    break
        
        return matches
        
    except Exception as e:
        print_error(f"Batch extract error: {e}")
        return []


def _concatenate_and_upload(
    matches: List[Dict],
    output_name: str,
    source_tag: str = "cli-analyze",
) -> Optional[str]:
    """Concatenate extracted audio files and upload to S3."""
    urls = [m.get("extracted_audio_url") for m in matches if m.get("extracted_audio_url")]
    
    if not urls:
        print_error("No audio URLs to concatenate")
        return None
    
    output_dir = Path("cli_test_results/analyze")
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{output_name}.mp3"
    
    import subprocess
    import tempfile
    
    print_info(f"Concatenating {len(urls)} audio files...")
    
    with tempfile.TemporaryDirectory() as tmpdir:
        temp_files = []
        
        for i, url in enumerate(urls):
            try:
                resp = httpx.get(url, timeout=30.0)
                if resp.status_code == 200:
                    temp_path = Path(tmpdir) / f"seg_{i:04d}.mp3"
                    temp_path.write_bytes(resp.content)
                    temp_files.append(str(temp_path))
            except Exception as e:
                print_warning(f"Failed to download segment {i}: {e}")
        
        if not temp_files:
            print_error("No segments downloaded")
            return None
        
        # Create concat file
        concat_file = Path(tmpdir) / "concat.txt"
        with open(concat_file, "w") as f:
            for tf in temp_files:
                f.write(f"file '{tf}'\n")
        
        # Run ffmpeg
        result = subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
             "-i", str(concat_file), "-c", "copy", str(output_path)],
            capture_output=True,
        )
        
        if result.returncode != 0:
            print_error(f"FFmpeg failed: {result.stderr.decode()}")
            return None
    
    print_success(f"Created: {output_path}")
    
    # Upload to S3
    try:
        with open(output_path, "rb") as f:
            files = {"audio": (output_path.name, f, "audio/mpeg")}
            response = httpx.post(
                f"{API_BASE_URL}/api/upload-audio",
                files=files,
                timeout=60.0,
            )
            
            if response.status_code == 200:
                s3_url = response.json().get("url")
                print_success(f"Uploaded to S3: {s3_url}")
                return s3_url
    except Exception as e:
        print_warning(f"S3 upload failed: {e}")
    
    return str(output_path)


# ============================================================================
# LIST MODES COMMANDS
# ============================================================================

@app.command("modes")
def list_modes():
    """List all available analyzer modes (20+ total)."""
    typer.echo("\n=== LEXICAL MODES (11) ===")
    for mode, info in LEXICAL_MODES.items():
        typer.echo(f"  lexical:{mode:<20} - {info['help']}")
    
    typer.echo("\n=== SYNTACTIC PATTERNS (8) ===")
    for pattern, info in SYNTACTIC_PATTERNS.items():
        typer.echo(f"  syntactic:{pattern:<18} - {info['help']}")
    
    typer.echo("\n=== SPEAKER MODES (8) ===")
    for mode, info in SPEAKER_MODES.items():
        typer.echo(f"  speaker:{mode:<20} - {info['help']}")
    
    typer.echo("\n=== STYLE MODES (5 categories, 20+ subtypes) ===")
    for mode, info in STYLE_MODES.items():
        subtypes = list(info.get("subtypes", {}).keys())
        typer.echo(f"  style:{mode:<22} - {info['help']}")
        if subtypes:
            typer.echo(f"      subtypes: {', '.join(subtypes)}")
    
    typer.echo("\n=== TEXT MODES (5) ===")
    for mode, info in TEXT_MODES.items():
        metrics = list(info.get("metrics", {}).keys())
        typer.echo(f"  text:{mode:<22} - {info['help']}")
        if metrics:
            typer.echo(f"      metrics: {', '.join(metrics)}")
    
    typer.echo("\n")


# ============================================================================
# LEXICAL ANALYZER
# ============================================================================

@app.command("lexical")
def lexical_search(
    mode: str = typer.Argument(..., help="Lexical mode: starts_with, ends_with, alliteration, word_length, repeating_letters, palindrome, rhyme, syllable_count, compound, contains, regex"),
    pattern: str = typer.Option("", "--pattern", "-p", help="Search pattern"),
    min_length: int = typer.Option(0, "--min-length", help="Min word length (for word_length mode)"),
    max_length: int = typer.Option(0, "--max-length", help="Max word length (for word_length mode)"),
    syllables: int = typer.Option(0, "--syllables", help="Syllable count (for syllable_count mode)"),
    case_sensitive: bool = typer.Option(False, "--case-sensitive", help="Case sensitive matching"),
    format_id: Optional[str] = typer.Option(None, "--format", "-f", help="Filter by format ID"),
    item_id: Optional[str] = typer.Option(None, "--item", "-i", help="Filter by item ID"),
    limit: int = typer.Option(50, "--limit", "-l", help="Max results"),
    compile: bool = typer.Option(False, "--compile", "-c", help="Create audio compilation"),
    output: str = typer.Option("", "--output", "-o", help="Output filename (for compile)"),
    output_format: str = typer.Option("table", "--format-output", help="Output format: table, json"),
):
    """
    Search for lexical patterns in words.
    
    Examples:
      stemmy analyze lexical starts_with -p "tech"
      stemmy analyze lexical palindrome
      stemmy analyze lexical word_length --min-length 10
      stemmy analyze lexical rhyme -p "huis"
      stemmy analyze lexical alliteration --compile
    """
    if mode not in LEXICAL_MODES:
        print_error(f"Unknown mode: {mode}. Available: {', '.join(LEXICAL_MODES.keys())}")
        raise typer.Exit(1)
    
    adapter = SQLiteAdapter()
    http = HTTPAdapter(API_BASE_URL)
    
    print_info(f"Searching lexical pattern: {mode}" + (f" pattern='{pattern}'" if pattern else ""))
    
    fragments = _get_fragments_with_words(adapter, format_id, item_id)
    print_info(f"Scanning {len(fragments)} fragments...")
    
    all_matches = []
    for frag in fragments:
        try:
            words = json.loads(frag.get("words", "[]"))
            if not words:
                continue
            
            matches = search_lexical_pattern(
                words_data=words,
                mode=mode,
                pattern=pattern,
                min_length=min_length,
                max_length=max_length,
                syllables=syllables,
                case_sensitive=case_sensitive,
                audio_url=frag.get("item_audio_url", ""),
                fragment_id=frag.get("id", ""),
                item_id=frag.get("item_id", ""),
                item_title=frag.get("item_title", ""),
            )
            all_matches.extend(matches)
        except json.JSONDecodeError:
            continue
    
    # Deduplicate
    all_matches = _deduplicate_matches(all_matches)
    
    if limit:
        all_matches = all_matches[:limit]
    
    print_success(f"Found {len(all_matches)} matches")
    
    if compile and all_matches:
        # Extract audio and create compilation
        all_matches = _batch_extract_audio(all_matches, http)
        output_name = output or f"lexical_{mode}_{pattern or 'all'}"
        _concatenate_and_upload(all_matches, output_name, f"cli-lexical-{mode}")
    else:
        output_result(all_matches, output_format)


# ============================================================================
# SYNTACTIC ANALYZER
# ============================================================================

@app.command("syntactic")
def syntactic_search(
    pattern_type: str = typer.Argument(..., help="Pattern type: comparison, enumeration, causation, temporal, contrast, condition, conclusion, example"),
    custom_patterns: str = typer.Option("", "--patterns", help="Custom patterns (comma-separated)"),
    extract_mode: str = typer.Option("sentence", "--extract", "-e", help="Extract mode: sentence, context, word"),
    context_words: int = typer.Option(5, "--context", help="Context words (for context mode)"),
    format_id: Optional[str] = typer.Option(None, "--format", "-f", help="Filter by format ID"),
    item_id: Optional[str] = typer.Option(None, "--item", "-i", help="Filter by item ID"),
    limit: int = typer.Option(50, "--limit", "-l", help="Max results"),
    compile: bool = typer.Option(False, "--compile", "-c", help="Create audio compilation"),
    output: str = typer.Option("", "--output", "-o", help="Output filename"),
    output_format: str = typer.Option("table", "--format-output", help="Output format: table, json"),
):
    """
    Search for syntactic patterns (comparisons, enumerations, etc.).
    
    Examples:
      stemmy analyze syntactic comparison
      stemmy analyze syntactic causation --extract sentence
      stemmy analyze syntactic temporal --patterns "gisteren,morgen" --compile
    """
    if pattern_type not in SYNTACTIC_PATTERNS:
        print_error(f"Unknown pattern: {pattern_type}. Available: {', '.join(SYNTACTIC_PATTERNS.keys())}")
        raise typer.Exit(1)
    
    adapter = SQLiteAdapter()
    http = HTTPAdapter(API_BASE_URL)
    
    print_info(f"Searching syntactic pattern: {pattern_type}")
    
    fragments = _get_fragments_with_words(adapter, format_id, item_id)
    print_info(f"Scanning {len(fragments)} fragments...")
    
    custom = [p.strip() for p in custom_patterns.split(",") if p.strip()] if custom_patterns else None
    
    all_matches = []
    for frag in fragments:
        try:
            words = json.loads(frag.get("words", "[]"))
            if not words:
                continue
            
            matches = search_syntactic_pattern(
                words_data=words,
                pattern_type=pattern_type,
                custom_patterns=custom,
                context_words=context_words,
                audio_url=frag.get("item_audio_url", ""),
                fragment_id=frag.get("id", ""),
                item_id=frag.get("item_id", ""),
                item_title=frag.get("item_title", ""),
                extract_mode=extract_mode,
            )
            all_matches.extend(matches)
        except json.JSONDecodeError:
            continue
    
    all_matches = _deduplicate_matches(all_matches)
    
    if limit:
        all_matches = all_matches[:limit]
    
    print_success(f"Found {len(all_matches)} matches")
    
    if compile and all_matches:
        all_matches = _batch_extract_audio(all_matches, http)
        output_name = output or f"syntactic_{pattern_type}"
        _concatenate_and_upload(all_matches, output_name, f"cli-syntactic-{pattern_type}")
    else:
        output_result(all_matches, output_format)


# ============================================================================
# SPEAKER ANALYZER
# ============================================================================

@app.command("speaker")
def speaker_search(
    mode: str = typer.Argument(..., help="Speaker mode: filler_words, speaker_change, long_pauses, speaking_pace, monologue"),
    threshold: float = typer.Option(0.5, "--threshold", "-t", help="Sensitivity (0-1)"),
    min_pause: int = typer.Option(500, "--min-pause", help="Min pause duration in ms (for long_pauses)"),
    min_words: int = typer.Option(20, "--min-words", help="Min words for monologue detection"),
    format_id: Optional[str] = typer.Option(None, "--format", "-f", help="Filter by format ID"),
    item_id: Optional[str] = typer.Option(None, "--item", "-i", help="Filter by item ID"),
    limit: int = typer.Option(50, "--limit", "-l", help="Max results"),
    compile: bool = typer.Option(False, "--compile", "-c", help="Create audio compilation"),
    output: str = typer.Option("", "--output", "-o", help="Output filename"),
    output_format: str = typer.Option("table", "--format-output", help="Output format: table, json"),
):
    """
    Search for speaker-related patterns.
    
    Examples:
      stemmy analyze speaker filler_words
      stemmy analyze speaker speaker_change --compile
      stemmy analyze speaker long_pauses --min-pause 1000
      stemmy analyze speaker speaking_pace --threshold 0.7
    """
    if mode not in SPEAKER_MODES:
        print_error(f"Unknown mode: {mode}. Available: {', '.join(SPEAKER_MODES.keys())}")
        raise typer.Exit(1)
    
    adapter = SQLiteAdapter()
    http = HTTPAdapter(API_BASE_URL)
    
    print_info(f"Searching speaker pattern: {mode}")
    
    fragments = _get_fragments_with_words(adapter, format_id, item_id)
    print_info(f"Scanning {len(fragments)} fragments...")
    
    all_matches = []
    for frag in fragments:
        try:
            words = json.loads(frag.get("words", "[]"))
            if not words:
                continue
            
            matches = search_speaker_pattern(
                words_data=words,
                mode=mode,
                threshold=threshold,
                min_pause_ms=min_pause,
                min_segment_words=min_words,
                audio_url=frag.get("item_audio_url", ""),
                fragment_id=frag.get("id", ""),
                item_id=frag.get("item_id", ""),
                item_title=frag.get("item_title", ""),
            )
            all_matches.extend(matches)
        except json.JSONDecodeError:
            continue
    
    all_matches = _deduplicate_matches(all_matches)
    
    if limit:
        all_matches = all_matches[:limit]
    
    print_success(f"Found {len(all_matches)} matches")
    
    if compile and all_matches:
        all_matches = _batch_extract_audio(all_matches, http)
        output_name = output or f"speaker_{mode}"
        _concatenate_and_upload(all_matches, output_name, f"cli-speaker-{mode}")
    else:
        output_result(all_matches, output_format)


# ============================================================================
# STYLE ANALYZER
# ============================================================================

@app.command("style")
def style_search(
    mode: str = typer.Argument(..., help="Style mode: formality, rhetoric, tone, dialog, argumentation"),
    subtype: str = typer.Option("", "--subtype", "-s", help="Subtype within mode"),
    custom_patterns: str = typer.Option("", "--patterns", help="Custom patterns (comma-separated)"),
    extract_mode: str = typer.Option("sentence", "--extract", "-e", help="Extract mode: sentence, context, word"),
    context_words: int = typer.Option(5, "--context", help="Context words"),
    format_id: Optional[str] = typer.Option(None, "--format", "-f", help="Filter by format ID"),
    item_id: Optional[str] = typer.Option(None, "--item", "-i", help="Filter by item ID"),
    limit: int = typer.Option(50, "--limit", "-l", help="Max results"),
    compile: bool = typer.Option(False, "--compile", "-c", help="Create audio compilation"),
    output: str = typer.Option("", "--output", "-o", help="Output filename"),
    output_format: str = typer.Option("table", "--format-output", help="Output format: table, json"),
):
    """
    Search for style patterns (formality, rhetoric, tone, etc.).
    
    Examples:
      stemmy analyze style formality --subtype formal
      stemmy analyze style rhetoric --subtype repetition
      stemmy analyze style tone --subtype humorous --compile
      stemmy analyze style argumentation --subtype conclusion
    """
    if mode not in STYLE_MODES:
        print_error(f"Unknown mode: {mode}. Available: {', '.join(STYLE_MODES.keys())}")
        raise typer.Exit(1)
    
    adapter = SQLiteAdapter()
    http = HTTPAdapter(API_BASE_URL)
    
    print_info(f"Searching style pattern: {mode}" + (f"/{subtype}" if subtype else ""))
    
    fragments = _get_fragments_with_words(adapter, format_id, item_id)
    print_info(f"Scanning {len(fragments)} fragments...")
    
    custom = [p.strip() for p in custom_patterns.split(",") if p.strip()] if custom_patterns else None
    
    all_matches = []
    for frag in fragments:
        try:
            words = json.loads(frag.get("words", "[]"))
            if not words:
                continue
            
            matches = search_style_pattern(
                words_data=words,
                mode=mode,
                subtype=subtype or None,
                custom_patterns=custom,
                extract_mode=extract_mode,
                context_words=context_words,
                audio_url=frag.get("item_audio_url", ""),
                fragment_id=frag.get("id", ""),
                item_id=frag.get("item_id", ""),
                item_title=frag.get("item_title", ""),
            )
            all_matches.extend(matches)
        except json.JSONDecodeError:
            continue
    
    all_matches = _deduplicate_matches(all_matches)
    
    if limit:
        all_matches = all_matches[:limit]
    
    print_success(f"Found {len(all_matches)} matches")
    
    if compile and all_matches:
        all_matches = _batch_extract_audio(all_matches, http)
        output_name = output or f"style_{mode}_{subtype or 'all'}"
        _concatenate_and_upload(all_matches, output_name, f"cli-style-{mode}")
    else:
        output_result(all_matches, output_format)


# ============================================================================
# TEXT ANALYZER
# ============================================================================

@app.command("text")
def text_search(
    mode: str = typer.Argument(..., help="Text mode: complexity, coherence, variation, questions, exclamations"),
    metric: str = typer.Option("", "--metric", "-m", help="Specific metric within mode"),
    threshold: float = typer.Option(0.5, "--threshold", "-t", help="Sensitivity (0-1)"),
    format_id: Optional[str] = typer.Option(None, "--format", "-f", help="Filter by format ID"),
    item_id: Optional[str] = typer.Option(None, "--item", "-i", help="Filter by item ID"),
    limit: int = typer.Option(50, "--limit", "-l", help="Max results"),
    compile: bool = typer.Option(False, "--compile", "-c", help="Create audio compilation"),
    output: str = typer.Option("", "--output", "-o", help="Output filename"),
    output_format: str = typer.Option("table", "--format-output", help="Output format: table, json"),
):
    """
    Search for text patterns (complexity, questions, etc.).
    
    Examples:
      stemmy analyze text questions --metric all_questions
      stemmy analyze text complexity --metric long_sentences
      stemmy analyze text exclamations --compile
      stemmy analyze text coherence --metric transitions
    """
    if mode not in TEXT_MODES:
        print_error(f"Unknown mode: {mode}. Available: {', '.join(TEXT_MODES.keys())}")
        raise typer.Exit(1)
    
    adapter = SQLiteAdapter()
    http = HTTPAdapter(API_BASE_URL)
    
    print_info(f"Searching text pattern: {mode}" + (f"/{metric}" if metric else ""))
    
    fragments = _get_fragments_with_words(adapter, format_id, item_id)
    print_info(f"Scanning {len(fragments)} fragments...")
    
    all_matches = []
    for frag in fragments:
        try:
            words = json.loads(frag.get("words", "[]"))
            if not words:
                continue
            
            matches = search_text_pattern(
                words_data=words,
                mode=mode,
                metric=metric or "",
                threshold=threshold,
                audio_url=frag.get("item_audio_url", ""),
                fragment_id=frag.get("id", ""),
                item_id=frag.get("item_id", ""),
                item_title=frag.get("item_title", ""),
            )
            all_matches.extend(matches)
        except json.JSONDecodeError:
            continue
    
    all_matches = _deduplicate_matches(all_matches)
    
    if limit:
        all_matches = all_matches[:limit]
    
    print_success(f"Found {len(all_matches)} matches")
    
    if compile and all_matches:
        all_matches = _batch_extract_audio(all_matches, http)
        output_name = output or f"text_{mode}_{metric or 'all'}"
        _concatenate_and_upload(all_matches, output_name, f"cli-text-{mode}")
    else:
        output_result(all_matches, output_format)


# ============================================================================
# QUICK COMPILATION SHORTCUTS
# ============================================================================

@app.command("filler-words")
def filler_words_compilation(
    format_id: Optional[str] = typer.Option(None, "--format", "-f", help="Filter by format ID"),
    item_id: Optional[str] = typer.Option(None, "--item", "-i", help="Filter by item ID"),
    limit: int = typer.Option(100, "--limit", "-l", help="Max results"),
    compile: bool = typer.Option(True, "--compile", "-c", help="Create audio compilation"),
    output: str = typer.Option("filler_words", "--output", "-o", help="Output filename"),
):
    """Quick compilation of filler words (eh, uhm, nou, etc.)."""
    speaker_search(
        mode="filler_words",
        threshold=0.5,
        min_pause=500,
        min_words=20,
        format_id=format_id,
        item_id=item_id,
        limit=limit,
        compile=compile,
        output=output,
        output_format="table",
    )


@app.command("questions")
def questions_compilation(
    format_id: Optional[str] = typer.Option(None, "--format", "-f", help="Filter by format ID"),
    item_id: Optional[str] = typer.Option(None, "--item", "-i", help="Filter by item ID"),
    limit: int = typer.Option(50, "--limit", "-l", help="Max results"),
    compile: bool = typer.Option(True, "--compile", "-c", help="Create audio compilation"),
    output: str = typer.Option("questions", "--output", "-o", help="Output filename"),
):
    """Quick compilation of all questions."""
    text_search(
        mode="questions",
        metric="all_questions",
        threshold=0.5,
        format_id=format_id,
        item_id=item_id,
        limit=limit,
        compile=compile,
        output=output,
        output_format="table",
    )


@app.command("conclusions")
def conclusions_compilation(
    format_id: Optional[str] = typer.Option(None, "--format", "-f", help="Filter by format ID"),
    item_id: Optional[str] = typer.Option(None, "--item", "-i", help="Filter by item ID"),
    limit: int = typer.Option(50, "--limit", "-l", help="Max results"),
    compile: bool = typer.Option(True, "--compile", "-c", help="Create audio compilation"),
    output: str = typer.Option("conclusions", "--output", "-o", help="Output filename"),
):
    """Quick compilation of conclusions (dus, daarom, kortom, etc.)."""
    syntactic_search(
        pattern_type="conclusion",
        custom_patterns="",
        extract_mode="sentence",
        context_words=5,
        format_id=format_id,
        item_id=item_id,
        limit=limit,
        compile=compile,
        output=output,
        output_format="table",
    )


@app.command("alliterations")
def alliterations_compilation(
    format_id: Optional[str] = typer.Option(None, "--format", "-f", help="Filter by format ID"),
    item_id: Optional[str] = typer.Option(None, "--item", "-i", help="Filter by item ID"),
    limit: int = typer.Option(50, "--limit", "-l", help="Max results"),
    compile: bool = typer.Option(True, "--compile", "-c", help="Create audio compilation"),
    output: str = typer.Option("alliterations", "--output", "-o", help="Output filename"),
):
    """Quick compilation of alliterations."""
    lexical_search(
        mode="alliteration",
        pattern="",
        min_length=0,
        max_length=0,
        syllables=0,
        case_sensitive=False,
        format_id=format_id,
        item_id=item_id,
        limit=limit,
        compile=compile,
        output=output,
        output_format="table",
    )


@app.command("palindromes")
def palindromes_compilation(
    format_id: Optional[str] = typer.Option(None, "--format", "-f", help="Filter by format ID"),
    item_id: Optional[str] = typer.Option(None, "--item", "-i", help="Filter by item ID"),
    limit: int = typer.Option(50, "--limit", "-l", help="Max results"),
    compile: bool = typer.Option(True, "--compile", "-c", help="Create audio compilation"),
    output: str = typer.Option("palindromes", "--output", "-o", help="Output filename"),
):
    """Quick compilation of palindromic words."""
    lexical_search(
        mode="palindrome",
        pattern="",
        min_length=0,
        max_length=0,
        syllables=0,
        case_sensitive=False,
        format_id=format_id,
        item_id=item_id,
        limit=limit,
        compile=compile,
        output=output,
        output_format="table",
    )
