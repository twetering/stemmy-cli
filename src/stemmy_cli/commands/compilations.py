"""Compilation commands."""

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
from stemmy_cli.audio.local_extract import extract_segments_local

app = typer.Typer(help="Run compilation recipes")

# Buffer in milliseconds to add before/after each word for smoother extraction
WORD_BUFFER_MS = 50
# Pause threshold in ms to detect sentence boundaries
SENTENCE_PAUSE_MS = 750

# Search modes for word-level extraction
WORD_SEARCH_MODES = [
    "exact",           # Exact word match
    "starts_with",     # Words starting with prefix  
    "ends_with",       # Words ending with suffix
    "contains",        # Words containing substring
]

# Extraction modes - what to extract after finding match
EXTRACT_MODES = [
    "word",            # Just the matching word
    "sentence",        # Full sentence containing the word
    "word_plus",       # Word + N words before/after
    "time_plus",       # Word + X seconds before/after
]


def _extract_audio_segments_local(segments_payload: List[Dict[str, Any]], label: str) -> List[Dict[str, Any]]:
    """Extract audio locally and return extraction results."""
    extracted, failed = extract_segments_local(segments_payload)
    print_success(f"Extraction complete: {len(extracted)} {label}, {len(failed)} failed")
    return extracted


def _search_words_in_fragments(
    adapter: SQLiteAdapter,
    word_query: str,
    format_id: Optional[str] = None,
    item_id: Optional[str] = None,
    limit: int = 50,
    case_sensitive: bool = False,
) -> List[Dict[str, Any]]:
    """
    Search for individual word occurrences in fragment word arrays.
    Returns word-level timing information for audio extraction.
    Deduplicates by audio_url + timing to avoid extracting same audio twice.
    """
    conditions = ["words IS NOT NULL", "words != ''", "words != '[]'"]
    params = []
    
    if item_id:
        conditions.append("item_id = ?")
        params.append(item_id)
    
    if format_id:
        conditions.append("item_id IN (SELECT id FROM items WHERE format_id = ?)")
        params.append(format_id)
    
    where_clause = " AND ".join(conditions)
    
    fragments = adapter.execute_raw(
        f'''
        SELECT f.id, f.words, f.item_audio_url, f.item_id, i.title as item_title
        FROM fragments f
        LEFT JOIN items i ON f.item_id = i.id
        WHERE {where_clause}
        ''',
        params,
    )
    
    word_matches = []
    seen_timings = []
    search_term = word_query if case_sensitive else word_query.lower()
    
    def _is_duplicate_timing(audio_url: str, start_ms: int, end_ms: int, tolerance_ms: int = 100) -> bool:
        """Check if this timing is a duplicate within tolerance."""
        for seen_url, seen_start, seen_end in seen_timings:
            if seen_url != audio_url:
                continue
            if abs(seen_start - start_ms) < tolerance_ms and abs(seen_end - end_ms) < tolerance_ms:
                return True
        return False
    
    for frag in fragments:
        if not frag.get('words'):
            continue
        
        try:
            words = json.loads(frag['words'])
            audio_url = frag.get('item_audio_url')
            
            if not audio_url:
                continue
            
            for i, word in enumerate(words):
                word_text = word.get('text', '')
                word_clean = word_text.strip('.,!?:;"\'-')
                compare_text = word_clean if case_sensitive else word_clean.lower()
                
                if compare_text == search_term:
                    start_ms = word.get('start', 0)
                    end_ms = word.get('end', 0)
                    
                    if _is_duplicate_timing(audio_url, start_ms, end_ms):
                        continue
                    seen_timings.append((audio_url, start_ms, end_ms))
                    
                    word_matches.append({
                        'id': f"{frag['id']}_{start_ms}_{end_ms}",
                        'word': word_text,
                        'start_time': max(0, (start_ms - WORD_BUFFER_MS) / 1000),
                        'end_time': (end_ms + WORD_BUFFER_MS) / 1000,
                        'duration': (end_ms - start_ms + 2 * WORD_BUFFER_MS) / 1000,
                        'confidence': word.get('confidence', 0),
                        'speaker': word.get('speaker', ''),
                        'audio_url': audio_url,
                        'fragment_id': frag['id'],
                        'item_id': frag['item_id'],
                        'item_title': frag.get('item_title', ''),
                    })
                    
                    if len(word_matches) >= limit:
                        break
            
            if len(word_matches) >= limit:
                break
                
        except json.JSONDecodeError:
            continue
    
    return word_matches


def _find_sentence_boundaries(words: List[Dict], start_index: int) -> tuple:
    """
    Find sentence boundaries around a word based on punctuation and pauses.
    Mirrors ImportWordsDialog.findSentenceBoundaries logic.
    """
    sentence_start = start_index
    sentence_end = start_index
    
    # Search backwards for sentence start
    while sentence_start > 0:
        prev_word = words[sentence_start - 1]
        prev_text = prev_word.get('text', '')
        current_start = words[sentence_start].get('start', 0)
        prev_end = prev_word.get('end', 0)
        
        # Check for punctuation or pause
        if any(p in prev_text for p in ['.', '!', '?']):
            break
        if (current_start - prev_end) > SENTENCE_PAUSE_MS:
            break
        sentence_start -= 1
    
    # Search forwards for sentence end
    while sentence_end < len(words) - 1:
        current_word = words[sentence_end]
        current_text = current_word.get('text', '')
        current_end = current_word.get('end', 0)
        next_start = words[sentence_end + 1].get('start', 0)
        
        # Check for punctuation or pause
        if any(p in current_text for p in ['.', '!', '?']):
            break
        if (next_start - current_end) > SENTENCE_PAUSE_MS:
            break
        sentence_end += 1
    
    return sentence_start, sentence_end


def _word_matches_query(
    word_text: str,
    query: str,
    search_mode: str,
    case_sensitive: bool
) -> bool:
    """Check if a word matches the query based on search mode."""
    word_clean = word_text.strip('.,!?:;"\'-')
    compare_word = word_clean if case_sensitive else word_clean.lower()
    compare_query = query if case_sensitive else query.lower()
    
    if search_mode == "exact":
        return compare_word == compare_query
    elif search_mode == "starts_with":
        return compare_word.startswith(compare_query)
    elif search_mode == "ends_with":
        return compare_word.endswith(compare_query)
    elif search_mode == "contains":
        return compare_query in compare_word
    else:
        return compare_word == compare_query


def _search_words_advanced(
    adapter: SQLiteAdapter,
    query: str,
    search_mode: str = "exact",
    extract_mode: str = "word",
    format_id: Optional[str] = None,
    item_id: Optional[str] = None,
    limit: int = 50,
    case_sensitive: bool = False,
    words_before: int = 0,
    words_after: int = 0,
    time_before_ms: int = 0,
    time_after_ms: int = 0,
) -> List[Dict[str, Any]]:
    """
    Advanced word search with multiple search and extraction modes.
    
    Search modes:
        - exact: Word exactly matches query
        - starts_with: Word starts with query
        - ends_with: Word ends with query
        - contains: Word contains query
    
    Extract modes:
        - word: Just the matching word
        - sentence: Full sentence containing the word
        - word_plus: Word + N words before/after
        - time_plus: Word + X milliseconds before/after
    """
    conditions = ["words IS NOT NULL", "words != ''", "words != '[]'"]
    params = []
    
    if item_id:
        conditions.append("item_id = ?")
        params.append(item_id)
    
    if format_id:
        conditions.append("item_id IN (SELECT id FROM items WHERE format_id = ?)")
        params.append(format_id)
    
    where_clause = " AND ".join(conditions)
    
    fragments = adapter.execute_raw(
        f'''
        SELECT f.id, f.words, f.item_audio_url, f.item_id, i.title as item_title
        FROM fragments f
        LEFT JOIN items i ON f.item_id = i.id
        WHERE {where_clause}
        ''',
        params,
    )
    
    matches = []
    seen_timings = []
    
    def _is_duplicate_timing_adv(audio_url: str, start_ms: int, end_ms: int, tolerance_ms: int = 100) -> bool:
        """Check if this timing is a duplicate within tolerance."""
        for seen_url, seen_start, seen_end in seen_timings:
            if seen_url != audio_url:
                continue
            if abs(seen_start - start_ms) < tolerance_ms and abs(seen_end - end_ms) < tolerance_ms:
                return True
        return False
    
    for frag in fragments:
        if not frag.get('words'):
            continue
        
        try:
            words = json.loads(frag['words'])
            audio_url = frag.get('item_audio_url')
            
            if not audio_url:
                continue
            
            for i, word in enumerate(words):
                word_text = word.get('text', '')
                
                if not _word_matches_query(word_text, query, search_mode, case_sensitive):
                    continue
                
                # Calculate extraction range based on extract_mode
                if extract_mode == "sentence":
                    start_idx, end_idx = _find_sentence_boundaries(words, i)
                    extract_words = words[start_idx:end_idx + 1]
                    start_ms = extract_words[0].get('start', 0)
                    end_ms = extract_words[-1].get('end', 0)
                    extracted_text = ' '.join(w.get('text', '') for w in extract_words)
                    
                elif extract_mode == "word_plus":
                    start_idx = max(0, i - words_before)
                    end_idx = min(len(words) - 1, i + words_after)
                    extract_words = words[start_idx:end_idx + 1]
                    start_ms = extract_words[0].get('start', 0)
                    end_ms = extract_words[-1].get('end', 0)
                    extracted_text = ' '.join(w.get('text', '') for w in extract_words)
                    
                elif extract_mode == "time_plus":
                    start_ms = max(0, word.get('start', 0) - time_before_ms)
                    end_ms = word.get('end', 0) + time_after_ms
                    extracted_text = word_text
                    
                else:  # word mode
                    start_ms = word.get('start', 0)
                    end_ms = word.get('end', 0)
                    extracted_text = word_text
                
                # Deduplicate by audio_url + timing with tolerance
                if _is_duplicate_timing_adv(audio_url, start_ms, end_ms):
                    continue
                seen_timings.append((audio_url, start_ms, end_ms))
                
                # Apply buffer
                start_time_sec = max(0, (start_ms - WORD_BUFFER_MS) / 1000)
                end_time_sec = (end_ms + WORD_BUFFER_MS) / 1000
                
                matches.append({
                    'id': f"{frag['id']}_{start_ms}_{end_ms}",
                    'match_word': word_text,
                    'text': extracted_text,
                    'start_time': start_time_sec,
                    'end_time': end_time_sec,
                    'duration': end_time_sec - start_time_sec,
                    'confidence': word.get('confidence', 0),
                    'speaker': word.get('speaker', ''),
                    'audio_url': audio_url,
                    'fragment_id': frag['id'],
                    'item_id': frag['item_id'],
                    'item_title': frag.get('item_title', ''),
                    'search_mode': search_mode,
                    'extract_mode': extract_mode,
                })
                
                if len(matches) >= limit:
                    break
            
            if len(matches) >= limit:
                break
                
        except json.JSONDecodeError:
            continue
    
    return matches


@app.command("words")
def words_compilation(
    word_query: str = typer.Argument(..., help="Exact word to search for"),
    output: str = typer.Option(..., "--output", "-o", help="Output file path"),
    format_id: Optional[str] = typer.Option(None, "--format", "-f", help="Filter by format ID"),
    item_id: Optional[str] = typer.Option(None, "--item", "-i", help="Filter by item ID"),
    limit: int = typer.Option(20, "--limit", "-l", help="Maximum word occurrences"),
    case_sensitive: bool = typer.Option(False, "--case-sensitive", help="Case-sensitive search"),
    save_to_db: bool = typer.Option(True, "--save-db/--no-save-db", help="Save to database"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """
    Create compilation from individual word occurrences.
    
    Unlike extract-and-compile (which extracts full fragments), this command
    finds specific word occurrences using word-level timing data and extracts
    only those exact words.
    
    Perfect for creating "word montages" like "AI, AI, AI, AI, AI..."
    
    Examples:
      stemmy compilations words "AI" -o ./ai_words.mp3 --format <format_id> --limit 20
      stemmy compilations words "Rotterdam" -o ./rotterdam.mp3 --limit 10
    """
    try:
        adapter = SQLiteAdapter()
        
        print_info(f"Step 1: Searching for word '{word_query}' in transcript data...")
        
        word_matches = _search_words_in_fragments(
            adapter, word_query, format_id, item_id, limit, case_sensitive
        )
        
        if not word_matches:
            print_error(f"No occurrences of '{word_query}' found in word-level data")
            raise typer.Exit(1)
        
        print_success(f"Found {len(word_matches)} occurrences of '{word_query}'")
        
        for i, m in enumerate(word_matches[:5]):
            print_info(f"  {i+1}. [{m['start_time']:.2f}s-{m['end_time']:.2f}s] '{m['word']}' ({m['item_title'][:30]}...)")
        if len(word_matches) > 5:
            print_info(f"  ... and {len(word_matches) - 5} more")
        
        print_info("Step 2: Batch extracting word audio segments...")
        
        segments_payload = []
        for m in word_matches:
            segments_payload.append({
                "id": m["id"],
                "start_time": float(m["start_time"]),
                "end_time": float(m["end_time"]),
                "audio_url": m["audio_url"],
            })
        
        extraction_results = _extract_audio_segments_local(segments_payload, "words")
        
        if not extraction_results:
            print_error("Extraction timed out or returned no results")
            raise typer.Exit(1)
        
        extracted_urls = []
        result_map = {
            str(r.get("segment", {}).get("id")): r for r in extraction_results
        }
        for m in word_matches:
            r = result_map.get(str(m.get("id")))
            if not r:
                continue
            url = r.get("fragment_audio_url")
            if url:
                extracted_urls.append({
                    "url": url,
                    "segment_id": r.get("segment", {}).get("id"),
                    "duration": r.get("duration", 0),
                })
        
        print_success(f"Got {len(extracted_urls)} extracted word audio clips")
        
        print_info(f"Step 3: Concatenating {len(extracted_urls)} word clips...")
        
        import tempfile
        import subprocess
        
        temp_dir = Path(tempfile.mkdtemp())
        file_list_path = temp_dir / "files.txt"
        
        downloaded_files = []
        for i, ex in enumerate(extracted_urls):
            part_path = temp_dir / f"word_{i:03d}.mp3"
            url = ex["url"]
            if url.startswith("http"):
                response = httpx.get(url, follow_redirects=True, timeout=60.0)
                if response.status_code == 200:
                    part_path.write_bytes(response.content)
                    downloaded_files.append(part_path)
            else:
                local_path = Path(url)
                if local_path.exists():
                    downloaded_files.append(local_path)
        
        with open(file_list_path, "w") as f:
            for df in downloaded_files:
                f.write(f"file '{df}'\n")
        
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        cmd = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", str(file_list_path),
            "-c", "copy", str(output_path)
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        for df in downloaded_files:
            df.unlink()
        file_list_path.unlink()
        temp_dir.rmdir()
        
        if result.returncode != 0:
            print_error(f"FFmpeg concatenation failed: {result.stderr}")
            raise typer.Exit(1)
        
        print_success(f"Audio saved to: {output}")
        
        final_url = None
        print_info("Step 4: Uploading to S3...")
        try:
            with open(output_path, "rb") as audio_file:
                files = {"file": (output_path.name, audio_file, "audio/mpeg")}
                upload_response = httpx.post(
                    "http://localhost:5000/api/upload-audio",
                    files=files,
                    timeout=120.0
                )
                if upload_response.status_code == 200:
                    upload_result = upload_response.json()
                    final_url = (
                        upload_result.get("file_url") or 
                        upload_result.get("url") or 
                        upload_result.get("s3_url")
                    )
                    if final_url:
                        final_url = final_url.split("?")[0]
                        print_success(f"Uploaded to S3: {final_url}")
                else:
                    print_warning(f"S3 upload failed: HTTP {upload_response.status_code}")
        except Exception as upload_error:
            print_warning(f"S3 upload failed: {upload_error}")
        
        if save_to_db:
            print_info("Saving compilation to database...")
            compilation_id = str(uuid.uuid4())
            now = datetime.utcnow().isoformat()
            
            total_duration = sum(e.get("duration", 0) for e in extracted_urls)
            
            compilation_data = {
                "id": compilation_id,
                "title": f"Word Compilation: {word_query}",
                "audio_url": final_url or "",
                "s3_url": final_url or "",
                "local_path": str(output_path.resolve()),
                "query": word_query,
                "fragment_count": len(extracted_urls),
                "duration": total_duration,
                "source": "cli-words",
                "format_id": format_id or "",
                "created_at": now,
                "updated_at": now,
                "metadata": json.dumps({
                    "type": "word_compilation",
                    "word_query": word_query,
                    "item_id": item_id,
                    "format_id": format_id,
                    "word_count": len(word_matches),
                    "extracted_count": len(extracted_urls),
                    "case_sensitive": case_sensitive,
                    "created_via": "stemmy-cli-words",
                }),
            }
            
            try:
                adapter.insert("compilations", compilation_data)
                print_success(f"Saved to database: compilation_id={compilation_id}")
            except Exception as db_error:
                print_warning(f"Could not save to database: {db_error}")
        
        result_data = {
            "output_file": str(output),
            "s3_url": final_url,
            "word": word_query,
            "occurrences_found": len(word_matches),
            "words_extracted": len(extracted_urls),
            "total_duration": sum(e.get("duration", 0) for e in extracted_urls),
        }
        
        output_result(result_data, json_output=json_output, title="Word compilation complete")
        
    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("sentences")
def sentences_compilation(
    query: str = typer.Argument(..., help="Search query (word or phrase)"),
    output: str = typer.Option(..., "--output", "-o", help="Output file path"),
    search_mode: str = typer.Option(
        "exact", 
        "--search-mode", "-s",
        help="Search mode: exact, starts_with, ends_with, contains"
    ),
    extract_mode: str = typer.Option(
        "sentence",
        "--extract-mode", "-e",
        help="Extract mode: word, sentence, word_plus, time_plus"
    ),
    format_id: Optional[str] = typer.Option(None, "--format", "-f", help="Filter by format ID"),
    item_id: Optional[str] = typer.Option(None, "--item", "-i", help="Filter by item ID"),
    limit: int = typer.Option(20, "--limit", "-l", help="Maximum matches"),
    words_before: int = typer.Option(3, "--words-before", help="Words before match (word_plus mode)"),
    words_after: int = typer.Option(3, "--words-after", help="Words after match (word_plus mode)"),
    time_before: int = typer.Option(1000, "--time-before", help="Milliseconds before (time_plus mode)"),
    time_after: int = typer.Option(1000, "--time-after", help="Milliseconds after (time_plus mode)"),
    case_sensitive: bool = typer.Option(False, "--case-sensitive", help="Case-sensitive search"),
    save_to_db: bool = typer.Option(True, "--save-db/--no-save-db", help="Save to database"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """
    Create compilation using advanced word search with context extraction.
    
    SEARCH MODES (how to match the query):
      - exact: Word exactly matches query (default)
      - starts_with: Words starting with query (e.g. "tech" matches "technology")
      - ends_with: Words ending with query (e.g. "ing" matches "running")
      - contains: Words containing query (e.g. "art" matches "smart")
    
    EXTRACT MODES (what to include in audio):
      - word: Just the matching word
      - sentence: Full sentence containing the word (default)
      - word_plus: Match + N words before/after (use --words-before, --words-after)
      - time_plus: Match + X milliseconds before/after (use --time-before, --time-after)
    
    Examples:
      # Sentences starting with "Ik" (Dutch)
      stemmy compilations sentences "Ik" -o ./ik_zinnen.mp3 --extract-mode sentence --limit 20
      
      # Words ending in "heid" (Dutch suffix)
      stemmy compilations sentences "heid" -o ./heid.mp3 --search-mode ends_with --extract-mode word
      
      # Word + 5 words context before and after
      stemmy compilations sentences "technologie" -o ./tech.mp3 --extract-mode word_plus --words-before 5 --words-after 5
      
      # Word + 2 seconds context
      stemmy compilations sentences "AI" -o ./ai.mp3 --extract-mode time_plus --time-before 2000 --time-after 2000
    """
    try:
        adapter = SQLiteAdapter()
        
        mode_desc = f"search={search_mode}, extract={extract_mode}"
        print_info(f"Step 1: Searching for '{query}' ({mode_desc})...")
        
        matches = _search_words_advanced(
            adapter=adapter,
            query=query,
            search_mode=search_mode,
            extract_mode=extract_mode,
            format_id=format_id,
            item_id=item_id,
            limit=limit,
            case_sensitive=case_sensitive,
            words_before=words_before,
            words_after=words_after,
            time_before_ms=time_before,
            time_after_ms=time_after,
        )
        
        if not matches:
            print_error(f"No matches found for '{query}' with mode '{search_mode}'")
            raise typer.Exit(1)
        
        print_success(f"Found {len(matches)} matches")
        
        for i, m in enumerate(matches[:5]):
            text_preview = m['text'][:50] + "..." if len(m['text']) > 50 else m['text']
            print_info(f"  {i+1}. [{m['start_time']:.2f}s-{m['end_time']:.2f}s] '{text_preview}'")
        if len(matches) > 5:
            print_info(f"  ... and {len(matches) - 5} more")
        
        print_info(f"Step 2: Batch extracting {len(matches)} audio segments...")
        
        segments_payload = []
        for m in matches:
            segments_payload.append({
                "id": m["id"],
                "start_time": float(m["start_time"]),
                "end_time": float(m["end_time"]),
                "audio_url": m["audio_url"],
            })
        
        extraction_results = _extract_audio_segments_local(segments_payload, "segments")
        
        if not extraction_results:
            print_error("Extraction timed out or returned no results")
            raise typer.Exit(1)
        
        extracted_urls = []
        result_map = {
            str(r.get("segment", {}).get("id")): r for r in extraction_results
        }
        for m in matches:
            r = result_map.get(str(m.get("id")))
            if not r:
                continue
            url = r.get("fragment_audio_url")
            if url:
                extracted_urls.append({
                    "url": url,
                    "segment_id": r.get("segment", {}).get("id"),
                    "duration": r.get("duration", 0),
                })
        
        print_success(f"Got {len(extracted_urls)} extracted audio clips")
        
        print_info(f"Step 3: Concatenating {len(extracted_urls)} clips...")
        
        import tempfile
        import subprocess
        
        temp_dir = Path(tempfile.mkdtemp())
        file_list_path = temp_dir / "files.txt"
        
        downloaded_files = []
        for i, ex in enumerate(extracted_urls):
            part_path = temp_dir / f"segment_{i:03d}.mp3"
            url = ex["url"]
            if url.startswith("http"):
                response = httpx.get(url, follow_redirects=True, timeout=60.0)
                if response.status_code == 200:
                    part_path.write_bytes(response.content)
                    downloaded_files.append(part_path)
            else:
                local_path = Path(url)
                if local_path.exists():
                    downloaded_files.append(local_path)
        
        with open(file_list_path, "w") as f:
            for df in downloaded_files:
                f.write(f"file '{df}'\n")
        
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        cmd = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", str(file_list_path),
            "-c", "copy", str(output_path)
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        for df in downloaded_files:
            df.unlink()
        file_list_path.unlink()
        temp_dir.rmdir()
        
        if result.returncode != 0:
            print_error(f"FFmpeg failed: {result.stderr}")
            raise typer.Exit(1)
        
        print_success(f"Audio saved to: {output}")
        
        final_url = None
        print_info("Step 4: Uploading to S3...")
        try:
            with open(output_path, "rb") as audio_file:
                files = {"file": (output_path.name, audio_file, "audio/mpeg")}
                upload_response = httpx.post(
                    "http://localhost:5000/api/upload-audio",
                    files=files,
                    timeout=120.0
                )
                if upload_response.status_code == 200:
                    upload_result = upload_response.json()
                    final_url = (
                        upload_result.get("file_url") or 
                        upload_result.get("url") or 
                        upload_result.get("s3_url")
                    )
                    if final_url:
                        final_url = final_url.split("?")[0]
                        print_success(f"Uploaded to S3: {final_url}")
                else:
                    print_warning(f"S3 upload failed: HTTP {upload_response.status_code}")
        except Exception as upload_error:
            print_warning(f"S3 upload failed: {upload_error}")
        
        if save_to_db:
            print_info("Saving compilation to database...")
            compilation_id = str(uuid.uuid4())
            now = datetime.utcnow().isoformat()
            
            total_duration = sum(e.get("duration", 0) for e in extracted_urls)
            
            compilation_data = {
                "id": compilation_id,
                "title": f"Sentences: {query} ({extract_mode})",
                "audio_url": final_url or "",
                "s3_url": final_url or "",
                "local_path": str(output_path.resolve()),
                "query": query,
                "fragment_count": len(extracted_urls),
                "duration": total_duration,
                "source": f"cli-{extract_mode}",
                "format_id": format_id or "",
                "created_at": now,
                "updated_at": now,
                "metadata": json.dumps({
                    "type": "advanced_search_compilation",
                    "query": query,
                    "search_mode": search_mode,
                    "extract_mode": extract_mode,
                    "words_before": words_before,
                    "words_after": words_after,
                    "time_before_ms": time_before,
                    "time_after_ms": time_after,
                    "case_sensitive": case_sensitive,
                    "matches_found": len(matches),
                    "extracted_count": len(extracted_urls),
                    "created_via": "stemmy-cli-sentences",
                }),
            }
            
            try:
                adapter.insert("compilations", compilation_data)
                print_success(f"Saved to database: compilation_id={compilation_id}")
            except Exception as db_error:
                print_warning(f"Could not save to database: {db_error}")
        
        result_data = {
            "output_file": str(output),
            "s3_url": final_url,
            "query": query,
            "search_mode": search_mode,
            "extract_mode": extract_mode,
            "matches_found": len(matches),
            "segments_extracted": len(extracted_urls),
            "total_duration": sum(e.get("duration", 0) for e in extracted_urls),
        }
        
        output_result(result_data, json_output=json_output, title="Sentence compilation complete")
        
    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("entities")
def entities_compilation(
    query: str = typer.Argument(..., help="Search query for entity text"),
    output: str = typer.Option(..., "--output", "-o", help="Output file path"),
    entity_type: Optional[str] = typer.Option(
        None, "--type", "-t",
        help="Entity type(s), comma-separated: person_name, organization, location, etc."
    ),
    search_mode: str = typer.Option(
        "contains", "--search-mode", "-s",
        help="Search mode: exact, starts_with, ends_with, contains"
    ),
    format_id: Optional[str] = typer.Option(None, "--format", "-f", help="Filter by format ID"),
    item_id: Optional[str] = typer.Option(None, "--item", "-i", help="Filter by item ID"),
    limit: int = typer.Option(20, "--limit", "-l", help="Maximum entities"),
    min_duration: Optional[float] = typer.Option(None, "--min-duration", help="Minimum duration in seconds"),
    max_duration: Optional[float] = typer.Option(None, "--max-duration", help="Maximum duration in seconds"),
    unique: bool = typer.Option(False, "--unique", "-u", help="Only unique text values"),
    save_to_db: bool = typer.Option(True, "--save-db/--no-save-db", help="Save to database"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """
    Create compilation from entity occurrences.
    
    Entities are named entities detected in transcripts (persons, organizations,
    locations, dates, etc.) with their exact timing data.
    
    ENTITY TYPES (use --type):
      person_name, organization, location, date, time, duration,
      money_amount, person_age, occupation, nationality, language,
      event, medical_condition, medical_process, drug, and more.
    
    Examples:
      # All persons named "Jan"
      stemmy compilations entities "Jan" -o ./jan.mp3 --type person_name
      
      # Organizations containing "tech"
      stemmy compilations entities "tech" -o ./tech_orgs.mp3 --type organization --search-mode contains
      
      # Multiple entity types
      stemmy compilations entities "Amsterdam" -o ./adam.mp3 --type location,organization
      
      # All occurrences of a specific entity across a format
      stemmy compilations entities "Rotterdam" -o ./rotterdam.mp3 --format <id> --limit 50
    """
    try:
        adapter = SQLiteAdapter()
        
        mode_desc = f"search={search_mode}"
        if entity_type:
            mode_desc += f", types={entity_type}"
        print_info(f"Step 1: Searching entities for '{query}' ({mode_desc})...")
        
        query_lower = query.lower()
        
        if search_mode == "exact":
            text_cond = "LOWER(text) = ?"
            text_param = query_lower
        elif search_mode == "starts_with":
            text_cond = "LOWER(text) LIKE ?"
            text_param = f"{query_lower}%"
        elif search_mode == "ends_with":
            text_cond = "LOWER(text) LIKE ?"
            text_param = f"%{query_lower}"
        else:  # contains
            text_cond = "LOWER(text) LIKE ?"
            text_param = f"%{query_lower}%"
        
        conditions = [text_cond, "source_audio_url IS NOT NULL", "start_time IS NOT NULL", "end_time IS NOT NULL"]
        params = [text_param]
        
        if entity_type:
            types = [t.strip() for t in entity_type.split(",")]
            if len(types) == 1:
                conditions.append("entity_type = ?")
                params.append(types[0])
            else:
                placeholders = ",".join("?" * len(types))
                conditions.append(f"entity_type IN ({placeholders})")
                params.extend(types)
        
        if item_id:
            conditions.append("item_id = ?")
            params.append(item_id)
        
        if format_id:
            conditions.append("format_id = ?")
            params.append(format_id)
        
        where_clause = " AND ".join(conditions)
        
        # Fetch more for filtering
        fetch_limit = limit * 3 if (min_duration or max_duration or unique) else limit
        params.append(fetch_limit)
        
        entities = adapter.execute_raw(
            f'''
            SELECT id, text, entity_type, start_time, end_time, source_audio_url, item_id
            FROM entities
            WHERE {where_clause}
            ORDER BY CAST(start_time AS INTEGER)
            LIMIT ?
            ''',
            params,
        )
        
        # Filter by duration
        if min_duration or max_duration:
            filtered = []
            for e in entities:
                start = float(e.get("start_time") or 0)
                end = float(e.get("end_time") or 0)
                duration = (end - start) / 1000  # ms to seconds
                if min_duration and duration < min_duration:
                    continue
                if max_duration and duration > max_duration:
                    continue
                filtered.append(e)
            entities = filtered
        
        # Filter unique
        if unique:
            seen = set()
            filtered = []
            for e in entities:
                text_key = e.get("text", "").lower().strip()
                if text_key not in seen:
                    seen.add(text_key)
                    filtered.append(e)
            entities = filtered
        
        entities = entities[:limit]
        
        if not entities:
            print_error(f"No entities found for '{query}'")
            raise typer.Exit(1)
        
        print_success(f"Found {len(entities)} entities")
        
        for i, e in enumerate(entities[:5]):
            start_sec = float(e['start_time']) / 1000
            end_sec = float(e['end_time']) / 1000
            print_info(f"  {i+1}. [{start_sec:.2f}s-{end_sec:.2f}s] {e['entity_type']}: {e['text'][:40]}")
        if len(entities) > 5:
            print_info(f"  ... and {len(entities) - 5} more")
        
        print_info(f"Step 2: Batch extracting {len(entities)} entity audio segments...")
        
        segments_payload = []
        for e in entities:
            # Entity times are in milliseconds
            start_ms = float(e['start_time'])
            end_ms = float(e['end_time'])
            
            segments_payload.append({
                "id": e["id"],
                "start_time": max(0, (start_ms - WORD_BUFFER_MS) / 1000),
                "end_time": (end_ms + WORD_BUFFER_MS) / 1000,
                "audio_url": e["source_audio_url"],
            })
        
        extraction_results = _extract_audio_segments_local(segments_payload, "segments")
        
        if not extraction_results:
            print_error("Extraction timed out or returned no results")
            raise typer.Exit(1)
        
        extracted_urls = []
        result_map = {
            str(r.get("segment", {}).get("id")): r for r in extraction_results
        }
        for e in entities:
            r = result_map.get(str(e.get("id")))
            if not r:
                continue
            url = r.get("fragment_audio_url")
            if url:
                extracted_urls.append({
                    "url": url,
                    "segment_id": r.get("segment", {}).get("id"),
                    "duration": r.get("duration", 0),
                })
        
        print_success(f"Got {len(extracted_urls)} extracted audio clips")
        
        print_info(f"Step 3: Concatenating {len(extracted_urls)} clips...")
        
        import tempfile
        import subprocess
        
        temp_dir = Path(tempfile.mkdtemp())
        file_list_path = temp_dir / "files.txt"
        
        downloaded_files = []
        for i, ex in enumerate(extracted_urls):
            part_path = temp_dir / f"entity_{i:03d}.mp3"
            url = ex["url"]
            if url.startswith("http"):
                response = httpx.get(url, follow_redirects=True, timeout=60.0)
                if response.status_code == 200:
                    part_path.write_bytes(response.content)
                    downloaded_files.append(part_path)
            else:
                local_path = Path(url)
                if local_path.exists():
                    downloaded_files.append(local_path)
        
        with open(file_list_path, "w") as f:
            for df in downloaded_files:
                f.write(f"file '{df}'\n")
        
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        cmd = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", str(file_list_path),
            "-c", "copy", str(output_path)
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        for df in downloaded_files:
            df.unlink()
        file_list_path.unlink()
        temp_dir.rmdir()
        
        if result.returncode != 0:
            print_error(f"FFmpeg failed: {result.stderr}")
            raise typer.Exit(1)
        
        print_success(f"Audio saved to: {output}")
        
        final_url = None
        print_info("Step 4: Uploading to S3...")
        try:
            with open(output_path, "rb") as audio_file:
                files = {"file": (output_path.name, audio_file, "audio/mpeg")}
                upload_response = httpx.post(
                    "http://localhost:5000/api/upload-audio",
                    files=files,
                    timeout=120.0
                )
                if upload_response.status_code == 200:
                    upload_result = upload_response.json()
                    final_url = (
                        upload_result.get("file_url") or 
                        upload_result.get("url") or 
                        upload_result.get("s3_url")
                    )
                    if final_url:
                        final_url = final_url.split("?")[0]
                        print_success(f"Uploaded to S3: {final_url}")
                else:
                    print_warning(f"S3 upload failed: HTTP {upload_response.status_code}")
        except Exception as upload_error:
            print_warning(f"S3 upload failed: {upload_error}")
        
        if save_to_db:
            print_info("Saving compilation to database...")
            compilation_id = str(uuid.uuid4())
            now = datetime.utcnow().isoformat()
            
            total_duration = sum(e.get("duration", 0) for e in extracted_urls)
            
            compilation_data = {
                "id": compilation_id,
                "title": f"Entities: {query}" + (f" ({entity_type})" if entity_type else ""),
                "audio_url": final_url or "",
                "s3_url": final_url or "",
                "local_path": str(output_path.resolve()),
                "query": query,
                "fragment_count": len(extracted_urls),
                "duration": total_duration,
                "source": "cli-entities",
                "format_id": format_id or "",
                "created_at": now,
                "updated_at": now,
                "metadata": json.dumps({
                    "type": "entity_compilation",
                    "query": query,
                    "search_mode": search_mode,
                    "entity_type": entity_type,
                    "unique": unique,
                    "entities_found": len(entities),
                    "extracted_count": len(extracted_urls),
                    "created_via": "stemmy-cli-entities",
                }),
            }
            
            try:
                adapter.insert("compilations", compilation_data)
                print_success(f"Saved to database: compilation_id={compilation_id}")
            except Exception as db_error:
                print_warning(f"Could not save to database: {db_error}")
        
        result_data = {
            "output_file": str(output),
            "s3_url": final_url,
            "query": query,
            "entity_type": entity_type,
            "search_mode": search_mode,
            "entities_found": len(entities),
            "segments_extracted": len(extracted_urls),
            "total_duration": sum(e.get("duration", 0) for e in extracted_urls),
        }
        
        output_result(result_data, json_output=json_output, title="Entity compilation complete")
        
    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("list")
def list_compilations(
    limit: int = typer.Option(20, "--limit", "-l", help="Number of compilations"),
    source: Optional[str] = typer.Option(None, "--source", "-s", help="Filter by source (cli, web)"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """List saved compilations from database."""
    try:
        adapter = SQLiteAdapter()
        
        conditions = []
        params = []
        
        if source:
            conditions.append("source = ?")
            params.append(source)
        
        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)
        
        results = adapter.execute_raw(
            f'''
            SELECT id, title, s3_url, local_path, query, fragment_count, 
                   duration, source, created_at
            FROM compilations
            {where_clause}
            ORDER BY created_at DESC
            LIMIT ?
            ''',
            params,
        )
        
        if json_output:
            output_result(results, json_output=True, title="Compilations")
        else:
            print_info(f"Found {len(results)} compilations")
            for c in results:
                duration_str = f"{c.get('duration', 0):.1f}s" if c.get('duration') else "?"
                url_status = "✓ S3" if c.get("s3_url") else "local"
                print(f"  [{c.get('source', '?'):3}] {c['id'][:8]}.. {c.get('title', 'Untitled')[:40]} ({c.get('fragment_count', 0)} frags, {duration_str}) [{url_status}]")
    
    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("show")
def show_compilation(
    compilation_id: str = typer.Argument(..., help="Compilation ID"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Show details of a specific compilation."""
    try:
        adapter = SQLiteAdapter()
        
        result = adapter.get_by_id("compilations", compilation_id)
        
        if not result:
            print_error(f"Compilation not found: {compilation_id}")
            raise typer.Exit(1)
        
        output_result(result, json_output=json_output, title=f"Compilation: {result.get('title', 'Untitled')}")
    
    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


def _download_audio(url: str, output_path: str) -> None:
    """Download audio from URL to local file."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    
    with httpx.Client(timeout=120.0) as client:
        response = client.get(url)
        response.raise_for_status()
        path.write_bytes(response.content)


@app.command()
def run(
    query: str = typer.Argument(..., help="Search query"),
    source: str = typer.Option("fragments", "--source", "-s", help="Source: fragments or entities"),
    mode: str = typer.Option("text", "--mode", "-m", help="Search mode: text, semantic, hybrid, exact_word"),
    limit: int = typer.Option(10, "--limit", "-l", help="Maximum results"),
    item_ids: Optional[str] = typer.Option(None, "--items", "-i", help="Comma-separated item IDs"),
    format_id: Optional[str] = typer.Option(None, "--format", "-f", help="Filter by format ID"),
    speaker: Optional[str] = typer.Option(None, "--speaker", help="Filter by speaker"),
    min_duration: Optional[float] = typer.Option(None, "--min-duration", help="Minimum duration in seconds"),
    max_duration: Optional[float] = typer.Option(None, "--max-duration", help="Maximum duration in seconds"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """
    Run a compilation query.

    Example: stemmy compilations run "klimaat" --source fragments --limit 20
    """
    try:
        adapter = SQLiteAdapter()

        table = source if source in ("fragments", "entities") else "fragments"

        conditions = ["text LIKE ?"]
        params = [f"%{query}%"]

        if item_ids:
            ids = [i.strip() for i in item_ids.split(",")]
            placeholders = ",".join("?" * len(ids))
            conditions.append(f"segment_id IN ({placeholders})")
            params.extend(ids)

        if format_id:
            conditions.append('''
                segment_id IN (SELECT id FROM items WHERE format_id = ?)
            ''')
            params.append(format_id)

        if speaker:
            conditions.append("speaker_label = ?")
            params.append(speaker)

        if min_duration:
            conditions.append("(CAST(end_time_seconds AS REAL) - CAST(start_time_seconds AS REAL)) >= ?")
            params.append(min_duration)

        if max_duration:
            conditions.append("(CAST(end_time_seconds AS REAL) - CAST(start_time_seconds AS REAL)) <= ?")
            params.append(max_duration)

        where_clause = " AND ".join(conditions)
        params.append(limit)

        results = adapter.execute_raw(
            f'''
            SELECT id, text, 
                   start_time_seconds as start_time, 
                   end_time_seconds as end_time,
                   (CAST(end_time_seconds AS REAL) - CAST(start_time_seconds AS REAL)) as duration,
                   segment_id as item_id, 
                   speaker_label as speaker, 
                   item_audio_url as audio_url
            FROM {table}
            WHERE {where_clause}
            ORDER BY CAST(start_time_seconds AS REAL)
            LIMIT ?
            ''',
            params,
        )

        if json_output:
            output_result(
                {
                    "items": results,
                    "total_count": len(results),
                    "query": query,
                    "source": source,
                },
                json_output=True,
            )
        else:
            output_result(
                results,
                columns=["id", "text", "speaker", "duration", "item_id"],
                title=f"Compilation: '{query}' ({len(results)} results)",
            )

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("from-fragments")
def from_fragments(
    fragment_ids: str = typer.Argument(..., help="Comma-separated fragment IDs"),
    output_file: Optional[str] = typer.Option(None, "--output", "-o", help="Output file for playlist"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Create a compilation from specific fragment IDs."""
    try:
        adapter = SQLiteAdapter()

        ids = [i.strip() for i in fragment_ids.split(",")]
        placeholders = ",".join("?" * len(ids))

        results = adapter.execute_raw(
            f'''
            SELECT id, text, 
                   start_time_seconds as start_time, 
                   end_time_seconds as end_time,
                   (CAST(end_time_seconds AS REAL) - CAST(start_time_seconds AS REAL)) as duration,
                   segment_id as item_id, 
                   speaker_label as speaker, 
                   item_audio_url as audio_url
            FROM fragments
            WHERE id IN ({placeholders})
            ''',
            ids,
        )

        id_order = {id_: idx for idx, id_ in enumerate(ids)}
        results.sort(key=lambda x: id_order.get(x["id"], 999))

        if output_file:
            import json
            with open(output_file, "w") as f:
                json.dump(results, f, indent=2, default=str)
            print_success(f"Saved playlist to {output_file}")
        else:
            output_result(
                results,
                json_output=json_output,
                columns=["id", "text", "duration", "audio_url"],
                title=f"Compilation ({len(results)} fragments)",
            )

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("from-entities")
def from_entities(
    entity_type: str = typer.Argument(..., help="Entity type to compile"),
    item_id: Optional[str] = typer.Option(None, "--item", "-i", help="Filter by item ID"),
    limit: int = typer.Option(10, "--limit", "-l", help="Maximum results"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Create a compilation from entities of a specific type."""
    try:
        adapter = SQLiteAdapter()

        conditions = ["entity_type = ?"]
        params = [entity_type]

        if item_id:
            conditions.append("item_id = ?")
            params.append(item_id)

        where_clause = " AND ".join(conditions)
        params.append(limit)

        results = adapter.execute_raw(
            f'''
            SELECT id, text, entity_type, start_time, end_time,
                   (end_time - start_time) as duration,
                   item_id, speaker
            FROM entities
            WHERE {where_clause}
            ORDER BY start_time
            LIMIT ?
            ''',
            params,
        )

        output_result(
            results,
            json_output=json_output,
            columns=["id", "text", "entity_type", "duration", "speaker"],
            title=f"Compilation: {entity_type} entities ({len(results)} results)",
        )

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("year-review")
def year_review(
    format_id: str = typer.Argument(..., help="Format ID"),
    year: int = typer.Argument(..., help="Year to review"),
    categories: Optional[str] = typer.Option(None, "--categories", "-c", help="Comma-separated entity types"),
    limit_per_category: int = typer.Option(5, "--limit", "-l", help="Items per category"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Generate a year review compilation from entities."""
    try:
        adapter = SQLiteAdapter()

        default_categories = ["person_name", "location", "organization", "event"]
        cats = [c.strip() for c in categories.split(",")] if categories else default_categories

        items_in_format = adapter.execute_raw(
            '''
            SELECT id FROM items
            WHERE format_id = ?
            AND strftime('%Y', published_at) = ?
            ''',
            [format_id, str(year)],
        )
        item_ids = [i["id"] for i in items_in_format]

        if not item_ids:
            print_error(f"No items found for format {format_id} in year {year}")
            raise typer.Exit(1)

        placeholders = ",".join("?" * len(item_ids))

        results = {}
        for cat in cats:
            entities = adapter.execute_raw(
                f'''
                SELECT text, entity_type, COUNT(*) as count,
                       MIN(start_time) as first_mention,
                       MAX(end_time) as last_mention
                FROM entities
                WHERE item_id IN ({placeholders})
                AND entity_type = ?
                GROUP BY text
                ORDER BY count DESC
                LIMIT ?
                ''',
                item_ids + [cat, limit_per_category],
            )
            results[cat] = entities

        if json_output:
            output_result(
                {
                    "year": year,
                    "format_id": format_id,
                    "categories": results,
                },
                json_output=True,
            )
        else:
            from rich.console import Console
            console = Console()
            console.print(f"\n[bold]Year Review {year}[/bold] for format {format_id}\n")

            for cat, entities in results.items():
                console.print(f"[cyan]{cat}[/cyan]:")
                for e in entities:
                    console.print(f"  - {e['text']} ({e['count']} mentions)")
                console.print()

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command()
def generate(
    fragment_ids: str = typer.Argument(..., help="Comma-separated fragment IDs"),
    voice: str = typer.Option(..., "--voice", "-v", help="Voice ID for narration"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Save audio to local file"),
    title: Optional[str] = typer.Option(None, "--title", "-t", help="Compilation title"),
    intro_text: Optional[str] = typer.Option(None, "--intro-text", help="Intro narration text"),
    outro_text: Optional[str] = typer.Option(None, "--outro-text", help="Outro narration text"),
    with_transitions: bool = typer.Option(False, "--transitions", help="Add transition sounds"),
    provider: str = typer.Option("elevenlabs", "--provider", "-p", help="TTS provider"),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Wait for generation"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Generate audio compilation from fragments with TTS narration."""
    import json
    
    try:
        http = HTTPAdapter(timeout=120.0)
        adapter = SQLiteAdapter()
        idempotency_key = str(uuid.uuid4())

        ids = [i.strip() for i in fragment_ids.split(",")]
        
        fragments = []
        for fid in ids:
            frag = adapter.get_by_id("fragments", fid)
            if frag:
                frag_data = {
                    "id": fid,
                    "type": "transcript",
                    "text": frag.get("text", ""),
                    "start_time": frag.get("start_time"),
                    "end_time": frag.get("end_time"),
                    "audio_url": frag.get("audio_url"),
                    "item_id": frag.get("item_id"),
                }
                fragments.append(frag_data)

        if not fragments:
            print_error("No valid fragments found")
            raise typer.Exit(1)

        all_fragments = []
        
        if intro_text:
            all_fragments.append({
                "id": str(uuid.uuid4()),
                "type": "sentence",
                "text": intro_text,
                "voiceId": voice,
                "provider": provider,
                "ttsProvider": provider,
                "isGenerated": False,
            })

        all_fragments.extend(fragments)

        if outro_text:
            all_fragments.append({
                "id": str(uuid.uuid4()),
                "type": "sentence",
                "text": outro_text,
                "voiceId": voice,
                "provider": provider,
                "ttsProvider": provider,
                "isGenerated": False,
            })

        voicesettings = json.dumps({
            "stability": 0.3,
            "similarity_boost": 0.98,
            "style": 0.5,
            "use_speaker_boost": True,
        })

        payload = {
            "fragments": all_fragments,
            "showformat": "compilation",
            "title": title or f"Compilation ({len(ids)} fragments)",
            "voicesettings": voicesettings,
            "intro": {},
            "outro": {},
            "bgaudio": {},
        }

        if wait:
            print_info(f"Generating compilation with {len(all_fragments)} fragments...")
            result = http.start_and_wait(
                "/api/generate-multiple-voices",
                payload,
                "/api/audio-status/{task_id}",
                poll_interval=3.0,
                max_wait=600.0,
                verbose=True,
                headers={"Idempotency-Key": idempotency_key}
            )
        else:
            result = http.post(
                "/api/generate-multiple-voices", 
                json=payload,
                headers={"Idempotency-Key": idempotency_key}
            )
            print_success(f"Generation started: task_id={result.get('task_id')}")
            output_result(result, json_output=json_output, title="Compilation started")
            return

        inner_result = result.get("result", {})
        if isinstance(inner_result, str):
            inner_result = json.loads(inner_result)
        
        audio_url = (
            result.get("audio_url") or 
            result.get("audioUrl") or 
            inner_result.get("audio_file") or
            inner_result.get("audio_url")
        )
        
        if audio_url and output:
            print_info("Downloading audio...")
            _download_audio(audio_url, output)
            print_success(f"Audio saved to: {output}")

        output_result(result, json_output=json_output, title="Compilation generated")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("quick")
def quick_compilation(
    query: str = typer.Argument(..., help="Search query for fragments"),
    voice: str = typer.Option(..., "--voice", "-v", help="Voice ID"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Save audio to local file"),
    limit: int = typer.Option(5, "--limit", "-l", help="Number of fragments to include"),
    format_id: Optional[str] = typer.Option(None, "--format", "-f", help="Filter by format"),
    intro: Optional[str] = typer.Option(None, "--intro", help="Custom intro text"),
    provider: str = typer.Option("elevenlabs", "--provider", "-p", help="TTS provider"),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Wait for generation"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Quick compilation: search fragments and generate audio in one command."""
    import json
    
    try:
        adapter = SQLiteAdapter()
        http = HTTPAdapter(timeout=120.0)
        idempotency_key = str(uuid.uuid4())

        conditions = ["text LIKE ?"]
        params = [f"%{query}%"]

        if format_id:
            conditions.append("item_id IN (SELECT id FROM items WHERE format_id = ?)")
            params.append(format_id)

        where_clause = " AND ".join(conditions)
        params.append(limit)

        results = adapter.execute_raw(
            f'''
            SELECT id, text, 
                   start_time_seconds as start_time, 
                   end_time_seconds as end_time, 
                   segment_id as item_id, 
                   item_audio_url as audio_url
            FROM fragments
            WHERE {where_clause}
            ORDER BY CAST(start_time_seconds AS REAL)
            LIMIT ?
            ''',
            params,
        )

        if not results:
            print_error(f"No fragments found matching '{query}'")
            raise typer.Exit(1)

        print_info(f"Found {len(results)} fragments matching '{query}'")

        fragments = []
        
        intro_text = intro or f"Hier zijn {len(results)} fragmenten over {query}."
        fragments.append({
            "id": str(uuid.uuid4()),
            "type": "sentence",
            "text": intro_text,
            "voiceId": voice,
            "provider": provider,
            "ttsProvider": provider,
            "isGenerated": False,
        })

        for frag in results:
            fragments.append({
                "id": frag["id"],
                "type": "transcript",
                "text": frag.get("text", ""),
                "start_time": frag.get("start_time"),
                "end_time": frag.get("end_time"),
                "audio_url": frag.get("audio_url"),
                "item_id": frag.get("item_id"),
            })

        voicesettings = json.dumps({
            "stability": 0.3,
            "similarity_boost": 0.98,
            "style": 0.5,
            "use_speaker_boost": True,
        })

        payload = {
            "fragments": fragments,
            "showformat": "compilation",
            "title": f"Compilation: {query}",
            "voicesettings": voicesettings,
            "intro": {},
            "outro": {},
            "bgaudio": {},
        }

        if wait:
            print_info(f"Generating quick compilation...")
            result = http.start_and_wait(
                "/api/generate-multiple-voices",
                payload,
                "/api/audio-status/{task_id}",
                poll_interval=3.0,
                max_wait=600.0,
                verbose=True,
                headers={"Idempotency-Key": idempotency_key}
            )
        else:
            result = http.post(
                "/api/generate-multiple-voices", 
                json=payload,
                headers={"Idempotency-Key": idempotency_key}
            )
            print_success(f"Generation started: task_id={result.get('task_id')}")
            output_result(result, json_output=json_output, title="Quick compilation started")
            return

        inner_result = result.get("result", {})
        if isinstance(inner_result, str):
            inner_result = json.loads(inner_result)
        
        audio_url = (
            result.get("audio_url") or 
            result.get("audioUrl") or 
            inner_result.get("audio_file") or
            inner_result.get("audio_url")
        )
        
        if audio_url and output:
            print_info("Downloading audio...")
            _download_audio(audio_url, output)
            print_success(f"Audio saved to: {output}")

        output_result(result, json_output=json_output, title="Quick compilation generated")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("extract-and-compile")
def extract_and_compile(
    query: str = typer.Argument(..., help="Search query for fragments"),
    output: str = typer.Option(..., "--output", "-o", help="Output file path for final audio"),
    voice: Optional[str] = typer.Option(None, "--voice", "-v", help="Voice ID for TTS intro/outro"),
    item_id: Optional[str] = typer.Option(None, "--item", "-i", help="Filter by item ID"),
    format_id: Optional[str] = typer.Option(None, "--format", "-f", help="Filter by format ID"),
    limit: int = typer.Option(10, "--limit", "-l", help="Number of fragments"),
    intro_text: Optional[str] = typer.Option(None, "--intro", help="Intro text for TTS"),
    outro_text: Optional[str] = typer.Option(None, "--outro", help="Outro text for TTS"),
    unique: bool = typer.Option(False, "--unique", "-u", help="Only unique text values"),
    save_to_db: bool = typer.Option(True, "--save-db/--no-save-db", help="Save compilation to database"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """
    Extract audio from transcript fragments and compile into single file.
    
    This command:
      1. Searches fragments matching query
      2. Batch extracts audio segments using the fast extraction API
      3. Optionally generates TTS intro/outro
      4. Concatenates all audio into final output file
      5. Saves result to database (optional)
    
    Examples:
      stemmy compilations extract-and-compile "klimaat" -o ./klimaat.mp3 --limit 5
      stemmy compilations extract-and-compile "AI" -o ./ai.mp3 --item ITEM_ID --intro "Hier zijn fragmenten over AI"
    """
    try:
        adapter = SQLiteAdapter()
        http = HTTPAdapter(timeout=180.0)
        
        print_info(f"Step 1: Searching for fragments matching '{query}'...")
        
        conditions = ["LOWER(text) LIKE ?"]
        params = [f"%{query.lower()}%"]
        
        if item_id:
            conditions.append("item_id = ?")
            params.append(item_id)
        
        if format_id:
            conditions.append("item_id IN (SELECT id FROM items WHERE format_id = ?)")
            params.append(format_id)
        
        conditions.append("item_audio_url IS NOT NULL")
        conditions.append("item_audio_url != ''")
        
        where_clause = " AND ".join(conditions)
        fetch_limit = limit * 3 if unique else limit
        params.append(fetch_limit)
        
        fragments = adapter.execute_raw(
            f'''
            SELECT id, text, 
                   CAST(start_time_seconds AS REAL) as start_time, 
                   CAST(end_time_seconds AS REAL) as end_time,
                   item_audio_url as audio_url,
                   item_id,
                   speaker_label
            FROM fragments
            WHERE {where_clause}
            ORDER BY CAST(start_time_seconds AS REAL)
            LIMIT ?
            ''',
            params,
        )
        
        if unique:
            seen = set()
            unique_frags = []
            for f in fragments:
                text_key = f["text"].lower().strip()[:100]
                if text_key not in seen:
                    seen.add(text_key)
                    unique_frags.append(f)
            fragments = unique_frags[:limit]
        
        if not fragments:
            print_error(f"No fragments found matching '{query}' with valid audio URLs")
            raise typer.Exit(1)
        
        print_success(f"Found {len(fragments)} fragments to extract")
        
        for i, f in enumerate(fragments[:3]):
            print_info(f"  {i+1}. [{f['start_time']:.1f}s-{f['end_time']:.1f}s] {f['text'][:60]}...")
        if len(fragments) > 3:
            print_info(f"  ... and {len(fragments) - 3} more")
        
        print_info("Step 2: Batch extracting audio segments...")
        
        segments_payload = []
        for f in fragments:
            segments_payload.append({
                "id": f["id"],
                "start_time": float(f["start_time"]),
                "end_time": float(f["end_time"]),
                "audio_url": f["audio_url"],
            })
        
        extraction_results = _extract_audio_segments_local(segments_payload, "segments")
        
        if not extraction_results:
            print_error("Extraction timed out or returned no results")
            raise typer.Exit(1)
        
        extracted_urls = []
        result_map = {
            str(r.get("segment", {}).get("id")): r for r in extraction_results
        }
        for f in fragments:
            r = result_map.get(str(f.get("id")))
            if not r:
                continue
            url = r.get("fragment_audio_url")
            if url:
                extracted_urls.append({
                    "url": url,
                    "segment_id": r.get("segment", {}).get("id"),
                    "duration": r.get("duration", 0),
                })
        
        print_success(f"Got {len(extracted_urls)} extracted audio URLs")
        
        audio_parts = []
        
        def _try_generate_tts(text: str, voice_id: str, purpose: str) -> Optional[str]:
            """Try to generate TTS audio, return URL or None on failure."""
            try:
                tts_payload = {
                    "fragments": [{
                        "id": str(uuid.uuid4()),
                        "type": "sentence",
                        "text": text,
                        "voiceId": voice_id,
                        "provider": "elevenlabs",
                        "ttsProvider": "elevenlabs",
                        "isGenerated": False,
                    }],
                    "showformat": "cli",
                    "voicesettings": json.dumps({
                        "stability": 0.3,
                        "similarity_boost": 0.98,
                    }),
                    "intro": {},
                    "outro": {},
                    "bgaudio": {},
                }
                
                tts_result = http.start_and_wait(
                    "/api/generate-multiple-voices",
                    tts_payload,
                    "/api/audio-status/{task_id}",
                    poll_interval=2.0,
                    max_wait=120.0,
                    verbose=False,
                    headers={"Idempotency-Key": str(uuid.uuid4())}
                )
                
                inner = tts_result.get("result", {})
                if isinstance(inner, str):
                    inner = json.loads(inner)
                
                url = inner.get("audio_file") or tts_result.get("audio_file") or tts_result.get("audio_url")
                if url:
                    print_success(f"TTS {purpose} generated")
                    return url
                else:
                    print_warning(f"TTS {purpose} returned no audio URL")
                    return None
            except Exception as e:
                print_warning(f"TTS {purpose} failed: {e}")
                return None
        
        if intro_text and voice:
            print_info("Step 3a: Generating TTS intro...")
            intro_url = _try_generate_tts(intro_text, voice, "intro")
            if intro_url:
                audio_parts.append({"url": intro_url, "type": "intro"})
        
        for ex in extracted_urls:
            audio_parts.append({"url": ex["url"], "type": "fragment", "id": ex["segment_id"]})
        
        if outro_text and voice:
            print_info("Step 3b: Generating TTS outro...")
            outro_url = _try_generate_tts(outro_text, voice, "outro")
            if outro_url:
                audio_parts.append({"url": outro_url, "type": "outro"})
        
        print_info(f"Step 4: Concatenating {len(audio_parts)} audio parts...")
        
        concat_payload = {
            "audio_urls": [p["url"] for p in audio_parts],
            "output_format": "mp3",
        }
        
        try:
            concat_result = http.start_and_wait(
                "/api/concatenate-audio",
                concat_payload,
                "/api/task-status/{task_id}",
                poll_interval=2.0,
                max_wait=180.0,
                verbose=True,
            )
            
            final_url = concat_result.get("result", {}).get("audio_url") or concat_result.get("audio_url")
        except Exception as concat_error:
            print_warning(f"Concatenation API failed: {concat_error}")
            print_info("Falling back to local concatenation...")
            
            import tempfile
            import subprocess
            
            temp_dir = Path(tempfile.mkdtemp())
            file_list_path = temp_dir / "files.txt"
            
            downloaded_files = []
            for i, part in enumerate(audio_parts):
                part_path = temp_dir / f"part_{i:03d}.mp3"
                url = part["url"]
                if url.startswith("http"):
                    response = httpx.get(url, follow_redirects=True, timeout=60.0)
                    if response.status_code == 200:
                        part_path.write_bytes(response.content)
                        downloaded_files.append(part_path)
                else:
                    local_path = Path(url)
                    if local_path.exists():
                        downloaded_files.append(local_path)
            
            with open(file_list_path, "w") as f:
                for df in downloaded_files:
                    f.write(f"file '{df}'\n")
            
            output_path = Path(output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            
            cmd = [
                "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                "-i", str(file_list_path),
                "-c", "copy", str(output_path)
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True)
            
            for df in downloaded_files:
                df.unlink()
            file_list_path.unlink()
            temp_dir.rmdir()
            
            if result.returncode != 0:
                print_error(f"FFmpeg concatenation failed: {result.stderr}")
                raise typer.Exit(1)
            
            print_success(f"Audio saved to: {output}")
            
            print_info("Step 5: Uploading to S3...")
            try:
                with open(output_path, "rb") as audio_file:
                    files = {"file": (output_path.name, audio_file, "audio/mpeg")}
                    upload_response = httpx.post(
                        "http://localhost:5000/api/upload-audio",
                        files=files,
                        timeout=120.0
                    )
                    if upload_response.status_code == 200:
                        upload_result = upload_response.json()
                        final_url = (
                            upload_result.get("file_url") or 
                            upload_result.get("url") or 
                            upload_result.get("s3_url")
                        )
                        if final_url:
                            final_url = final_url.split("?")[0]
                            print_success(f"Uploaded to S3: {final_url}")
                    else:
                        print_warning(f"S3 upload failed: HTTP {upload_response.status_code}")
                        final_url = None
            except Exception as upload_error:
                print_warning(f"S3 upload failed: {upload_error}")
                final_url = None
        
        if final_url:
            output_path = Path(output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            
            print_info("Downloading final audio...")
            if final_url.startswith("http"):
                response = httpx.get(final_url, follow_redirects=True, timeout=120.0)
                if response.status_code == 200:
                    output_path.write_bytes(response.content)
                    print_success(f"Audio saved to: {output}")
                else:
                    print_error(f"Failed to download final audio: HTTP {response.status_code}")
            else:
                local_path = Path(final_url)
                if local_path.exists():
                    output_path.write_bytes(local_path.read_bytes())
                    print_success(f"Audio saved to: {output}")
                else:
                    print_error("Final audio path not found")
        
        if save_to_db:
            print_info("Saving compilation to database...")
            compilation_id = str(uuid.uuid4())
            now = datetime.utcnow().isoformat()
            
            total_duration = sum(e.get("duration", 0) for e in extracted_urls)
            
            compilation_data = {
                "id": compilation_id,
                "title": f"CLI Compilation: {query}",
                "audio_url": final_url or "",
                "s3_url": final_url or "",
                "local_path": str(Path(output).resolve()),
                "query": query,
                "fragment_count": len(extracted_urls),
                "duration": total_duration,
                "source": "cli",
                "format_id": format_id or "",
                "created_at": now,
                "updated_at": now,
                "metadata": json.dumps({
                    "query": query,
                    "item_id": item_id,
                    "format_id": format_id,
                    "fragment_ids": [f["id"] for f in fragments],
                    "extracted_urls": [e["url"] for e in extracted_urls],
                    "has_intro": bool(intro_text and voice),
                    "has_outro": bool(outro_text and voice),
                    "created_via": "stemmy-cli",
                    "cli_version": "1.0.0",
                }),
            }
            
            try:
                adapter.insert("compilations", compilation_data)
                print_success(f"Saved to database: compilation_id={compilation_id}")
            except Exception as db_error:
                print_warning(f"Could not save to database: {db_error}")
        
        result_data = {
            "output_file": str(output),
            "s3_url": final_url,
            "fragments_extracted": len(extracted_urls),
            "audio_parts": len(audio_parts),
            "fragment_ids": [f["id"] for f in fragments],
        }
        
        output_result(result_data, json_output=json_output, title="Compilation complete")
        
    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)
