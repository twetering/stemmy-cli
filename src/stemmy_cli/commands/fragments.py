"""Fragment management commands."""

import re
from typing import Optional, List

import typer

from stemmy_cli.adapters.sqlite import SQLiteAdapter
from stemmy_cli.output import output_result, print_error, print_info, print_success

app = typer.Typer(help="Manage audio fragments")


# Match modes for text search
MATCH_MODES = ["contains", "exact", "starts_with", "ends_with", "regex"]


def _build_text_condition(mode: str, query: str, column: str = "text") -> tuple:
    """Build SQL condition based on match mode."""
    query_lower = query.lower()
    
    if mode == "exact":
        return f"LOWER({column}) = ?", [query_lower]
    elif mode == "starts_with":
        return f"LOWER({column}) LIKE ?", [f"{query_lower}%"]
    elif mode == "ends_with":
        return f"LOWER({column}) LIKE ?", [f"%{query_lower}"]
    elif mode == "regex":
        return f"{column} REGEXP ?", [query]
    else:
        return f"LOWER({column}) LIKE ?", [f"%{query_lower}%"]


def _filter_by_duration(results: List[dict], min_dur: Optional[float], max_dur: Optional[float]) -> List[dict]:
    """Filter results by duration."""
    filtered = []
    for r in results:
        start = float(r.get("start_time") or r.get("start_time_seconds") or 0)
        end = float(r.get("end_time") or r.get("end_time_seconds") or 0)
        duration = end - start
        
        if min_dur is not None and duration < min_dur:
            continue
        if max_dur is not None and duration > max_dur:
            continue
        filtered.append(r)
    return filtered


def _filter_unique(results: List[dict], unique: bool) -> List[dict]:
    """Filter to unique text values only."""
    if not unique:
        return results
    
    seen = set()
    filtered = []
    for r in results:
        text = r.get("text", "").lower().strip()
        if text not in seen:
            seen.add(text)
            filtered.append(r)
    return filtered


@app.command()
def search(
    query: str = typer.Argument(..., help="Search query"),
    mode: str = typer.Option("contains", "--mode", "-m", help="Match mode: contains, exact, starts_with, ends_with, regex"),
    item_id: Optional[str] = typer.Option(None, "--item", "-i", help="Filter by item ID"),
    format_id: Optional[str] = typer.Option(None, "--format", "-f", help="Filter by format ID"),
    speaker: Optional[str] = typer.Option(None, "--speaker", "-s", help="Filter by speaker"),
    min_duration: Optional[float] = typer.Option(None, "--min-duration", help="Minimum duration in seconds"),
    max_duration: Optional[float] = typer.Option(None, "--max-duration", help="Maximum duration in seconds"),
    unique: bool = typer.Option(False, "--unique", "-u", help="Only return unique text values"),
    limit: int = typer.Option(50, "--limit", "-l", help="Maximum results"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """
    Search fragments by text content.
    
    Match modes:
      - contains: Text contains the query (default)
      - exact: Text exactly matches the query
      - starts_with: Text starts with the query
      - ends_with: Text ends with the query
      - regex: Text matches the regex pattern
    
    Examples:
      stemmy fragments search "klimaat"
      stemmy fragments search "de" --mode starts_with --limit 20
      stemmy fragments search "ing$" --mode regex
      stemmy fragments search "technologie" --min-duration 2 --max-duration 10
    """
    try:
        adapter = SQLiteAdapter()

        text_cond, text_params = _build_text_condition(mode, query)
        
        conditions = [text_cond]
        params = text_params.copy()

        if item_id:
            conditions.append("item_id = ?")
            params.append(item_id)

        if format_id:
            conditions.append("item_id IN (SELECT id FROM items WHERE format_id = ?)")
            params.append(format_id)

        if speaker:
            conditions.append("LOWER(speaker_label) = ?")
            params.append(speaker.lower())

        where_clause = " AND ".join(conditions)
        
        fetch_limit = limit * 5 if (min_duration or max_duration or unique) else limit
        params.append(fetch_limit)

        results = adapter.execute_raw(
            f'''
            SELECT id, text, type, 
                   start_time_seconds as start_time, 
                   end_time_seconds as end_time, 
                   item_id, 
                   speaker_label,
                   item_audio_url as audio_url
            FROM fragments
            WHERE {where_clause}
            ORDER BY CAST(start_time_seconds AS REAL)
            LIMIT ?
            ''',
            params,
        )

        if min_duration or max_duration:
            results = _filter_by_duration(results, min_duration, max_duration)

        if unique:
            results = _filter_unique(results, unique)

        results = results[:limit]

        mode_desc = f" ({mode})" if mode != "contains" else ""
        output_result(
            results,
            json_output=json_output,
            columns=["id", "text", "speaker_label", "start_time", "end_time", "item_id"],
            title=f"Fragments matching '{query}'{mode_desc}",
        )

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("advanced-search")
def advanced_search(
    starts_with: Optional[str] = typer.Option(None, "--starts-with", help="Text starts with this string"),
    ends_with: Optional[str] = typer.Option(None, "--ends-with", help="Text ends with this string"),
    contains: Optional[str] = typer.Option(None, "--contains", help="Text contains this string"),
    not_contains: Optional[str] = typer.Option(None, "--not-contains", help="Text does NOT contain this string"),
    min_words: Optional[int] = typer.Option(None, "--min-words", help="Minimum number of words"),
    max_words: Optional[int] = typer.Option(None, "--max-words", help="Maximum number of words"),
    min_duration: Optional[float] = typer.Option(None, "--min-duration", help="Minimum duration in seconds"),
    max_duration: Optional[float] = typer.Option(None, "--max-duration", help="Maximum duration in seconds"),
    speaker: Optional[str] = typer.Option(None, "--speaker", "-s", help="Filter by speaker"),
    format_id: Optional[str] = typer.Option(None, "--format", "-f", help="Filter by format ID"),
    item_id: Optional[str] = typer.Option(None, "--item", "-i", help="Filter by item ID"),
    unique: bool = typer.Option(False, "--unique", "-u", help="Only return unique text values"),
    random_sample: Optional[int] = typer.Option(None, "--random", "-r", help="Return N random results"),
    limit: int = typer.Option(50, "--limit", "-l", help="Maximum results"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """
    Advanced fragment search with multiple filters.
    
    Examples:
      stemmy fragments advanced-search --starts-with "De" --min-words 5
      stemmy fragments advanced-search --ends-with "?" --speaker A
      stemmy fragments advanced-search --contains "AI" --not-contains "niet"
      stemmy fragments advanced-search --min-duration 3 --max-duration 8 --random 10
    """
    try:
        adapter = SQLiteAdapter()

        conditions = []
        params = []

        if starts_with:
            conditions.append("LOWER(text) LIKE ?")
            params.append(f"{starts_with.lower()}%")

        if ends_with:
            conditions.append("LOWER(text) LIKE ?")
            params.append(f"%{ends_with.lower()}")

        if contains:
            conditions.append("LOWER(text) LIKE ?")
            params.append(f"%{contains.lower()}%")

        if not_contains:
            conditions.append("LOWER(text) NOT LIKE ?")
            params.append(f"%{not_contains.lower()}%")

        if speaker:
            conditions.append("LOWER(speaker_label) = ?")
            params.append(speaker.lower())

        if format_id:
            conditions.append("item_id IN (SELECT id FROM items WHERE format_id = ?)")
            params.append(format_id)

        if item_id:
            conditions.append("item_id = ?")
            params.append(item_id)

        if not conditions:
            conditions.append("1=1")

        where_clause = " AND ".join(conditions)
        
        fetch_limit = limit * 10
        params.append(fetch_limit)

        order_by = "RANDOM()" if random_sample else "CAST(start_time_seconds AS REAL)"

        results = adapter.execute_raw(
            f'''
            SELECT id, text, type, 
                   start_time_seconds as start_time, 
                   end_time_seconds as end_time, 
                   item_id, 
                   speaker_label,
                   item_audio_url as audio_url,
                   LENGTH(text) - LENGTH(REPLACE(text, ' ', '')) + 1 as word_count
            FROM fragments
            WHERE {where_clause}
            ORDER BY {order_by}
            LIMIT ?
            ''',
            params,
        )

        if min_words or max_words:
            filtered = []
            for r in results:
                wc = r.get("word_count", 1)
                if min_words and wc < min_words:
                    continue
                if max_words and wc > max_words:
                    continue
                filtered.append(r)
            results = filtered

        if min_duration or max_duration:
            results = _filter_by_duration(results, min_duration, max_duration)

        if unique:
            results = _filter_unique(results, unique)

        final_limit = random_sample or limit
        results = results[:final_limit]

        desc_parts = []
        if starts_with:
            desc_parts.append(f"starts with '{starts_with}'")
        if ends_with:
            desc_parts.append(f"ends with '{ends_with}'")
        if contains:
            desc_parts.append(f"contains '{contains}'")
        if not_contains:
            desc_parts.append(f"NOT contains '{not_contains}'")
        
        desc = " AND ".join(desc_parts) if desc_parts else "all fragments"

        output_result(
            results,
            json_output=json_output,
            columns=["id", "text", "speaker_label", "start_time", "end_time", "word_count"],
            title=f"Fragments: {desc} ({len(results)} results)",
        )

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("word-search")
def word_search(
    word: str = typer.Argument(..., help="Word to search for"),
    match_mode: str = typer.Option("exact", "--match", "-m", help="Match mode: exact, similar, phonetic"),
    context_words: int = typer.Option(0, "--context", "-c", help="Number of context words to include"),
    item_id: Optional[str] = typer.Option(None, "--item", "-i", help="Filter by item ID"),
    format_id: Optional[str] = typer.Option(None, "--format", "-f", help="Filter by format ID"),
    unique: bool = typer.Option(False, "--unique", "-u", help="Only return unique matches"),
    limit: int = typer.Option(50, "--limit", "-l", help="Maximum results"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """
    Search for specific words in fragments.
    
    Match modes:
      - exact: Word matches exactly (case-insensitive)
      - similar: Words with small spelling differences
      - phonetic: Words that sound similar
    
    Examples:
      stemmy fragments word-search "klimaat" --context 3
      stemmy fragments word-search "technologie" --match similar
    """
    try:
        adapter = SQLiteAdapter()

        conditions = []
        params = []

        word_lower = word.lower()
        
        conditions.append("LOWER(text) LIKE ?")
        params.append(f"%{word_lower}%")

        if item_id:
            conditions.append("item_id = ?")
            params.append(item_id)

        if format_id:
            conditions.append("item_id IN (SELECT id FROM items WHERE format_id = ?)")
            params.append(format_id)

        where_clause = " AND ".join(conditions)
        params.append(limit * 3)

        results = adapter.execute_raw(
            f'''
            SELECT id, text, 
                   start_time_seconds as start_time, 
                   end_time_seconds as end_time, 
                   item_id, 
                   speaker_label,
                   item_audio_url as audio_url
            FROM fragments
            WHERE {where_clause}
            ORDER BY CAST(start_time_seconds AS REAL)
            LIMIT ?
            ''',
            params,
        )

        if match_mode == "exact":
            word_pattern = re.compile(rf'\b{re.escape(word)}\b', re.IGNORECASE)
            results = [r for r in results if word_pattern.search(r.get("text", ""))]
        elif match_mode == "similar":
            def is_similar(text: str, target: str, threshold: int = 2) -> bool:
                words = text.lower().split()
                for w in words:
                    if abs(len(w) - len(target)) <= threshold:
                        diff = sum(a != b for a, b in zip(w, target))
                        if diff <= threshold:
                            return True
                return False
            results = [r for r in results if is_similar(r.get("text", ""), word_lower)]

        if unique:
            results = _filter_unique(results, unique)

        results = results[:limit]

        output_result(
            results,
            json_output=json_output,
            columns=["id", "text", "speaker_label", "start_time", "end_time", "item_id"],
            title=f"Word search: '{word}' ({match_mode} mode)",
        )

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("list")
def list_fragments(
    item_id: str = typer.Argument(..., help="Item ID to list fragments for"),
    type_filter: Optional[str] = typer.Option(None, "--type", "-t", help="Filter by type"),
    speaker: Optional[str] = typer.Option(None, "--speaker", "-s", help="Filter by speaker"),
    limit: int = typer.Option(100, "--limit", "-l", help="Maximum results"),
    offset: int = typer.Option(0, "--offset", "-o", help="Offset for pagination"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """List fragments for an item."""
    try:
        adapter = SQLiteAdapter()

        conditions = ["item_id = ?"]
        params = [item_id]

        if type_filter:
            conditions.append("type = ?")
            params.append(type_filter)

        if speaker:
            conditions.append("speaker_label = ?")
            params.append(speaker)

        where_clause = " AND ".join(conditions)
        params.extend([limit, offset])

        results = adapter.execute_raw(
            f'''
            SELECT id, text, type, 
                   start_time_seconds as start_time, 
                   end_time_seconds as end_time, 
                   speaker_label, 
                   item_audio_url as audio_url,
                   item_id
            FROM fragments
            WHERE {where_clause}
            ORDER BY CAST(start_time_seconds AS REAL)
            LIMIT ? OFFSET ?
            ''',
            params,
        )

        count = adapter.count("fragments", where={"item_id": item_id})

        if json_output:
            output_result({"items": results, "total": count, "limit": limit, "offset": offset}, json_output=True)
        else:
            output_result(
                results,
                columns=["id", "text", "type", "speaker_label", "start_time", "end_time"],
                title=f"Fragments for item {item_id} ({count} total)",
            )

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command()
def show(
    fragment_id: str = typer.Argument(..., help="Fragment ID"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Show details of a specific fragment."""
    try:
        adapter = SQLiteAdapter()
        result = adapter.get_by_id("fragments", fragment_id)

        if not result:
            print_error(f"Fragment {fragment_id} not found")
            raise typer.Exit(1)

        output_result(result, json_output=json_output, title=f"Fragment {fragment_id}")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command()
def update(
    fragment_id: str = typer.Argument(..., help="Fragment ID"),
    text: Optional[str] = typer.Option(None, "--text", "-t", help="Update text"),
    speaker: Optional[str] = typer.Option(None, "--speaker", "-s", help="Update speaker"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Update a fragment."""
    try:
        adapter = SQLiteAdapter()

        updates = {}
        if text is not None:
            updates["text"] = text
        if speaker is not None:
            updates["speaker_label"] = speaker

        if not updates:
            print_error("No updates provided")
            raise typer.Exit(1)

        success = adapter.update("fragments", fragment_id, updates)

        if success:
            result = adapter.get_by_id("fragments", fragment_id)
            output_result(result, json_output=json_output, title=f"Updated fragment {fragment_id}")
        else:
            print_error(f"Fragment {fragment_id} not found")
            raise typer.Exit(1)

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command()
def export(
    item_id: str = typer.Argument(..., help="Item ID to export fragments from"),
    output_file: Optional[str] = typer.Option(None, "--output", "-o", help="Output file path"),
    format: str = typer.Option("json", "--format", "-f", help="Export format: json, csv"),
):
    """Export fragments for an item."""
    import json as json_lib
    import csv
    import sys

    try:
        adapter = SQLiteAdapter()

        results = adapter.execute_raw(
            '''
            SELECT id, text, type, 
                   start_time_seconds as start_time, 
                   end_time_seconds as end_time, 
                   speaker_label, 
                   item_audio_url as audio_url,
                   item_id
            FROM fragments
            WHERE item_id = ?
            ORDER BY CAST(start_time_seconds AS REAL)
            ''',
            [item_id],
        )

        if format == "json":
            output = json_lib.dumps(results, indent=2, default=str)
        elif format == "csv":
            if not results:
                output = ""
            else:
                import io
                buf = io.StringIO()
                writer = csv.DictWriter(buf, fieldnames=results[0].keys())
                writer.writeheader()
                writer.writerows(results)
                output = buf.getvalue()
        else:
            print_error(f"Unknown format: {format}")
            raise typer.Exit(1)

        if output_file:
            with open(output_file, "w") as f:
                f.write(output)
            typer.echo(f"Exported {len(results)} fragments to {output_file}")
        else:
            typer.echo(output)

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)
