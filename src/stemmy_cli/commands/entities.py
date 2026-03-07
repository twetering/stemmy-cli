"""Entity management commands."""

from typing import Optional, List
from collections import Counter

import typer

from stemmy_cli.adapters.sqlite import SQLiteAdapter
from stemmy_cli.output import output_result, print_error, print_info, print_success

app = typer.Typer(help="Manage entities from transcripts")


ENTITY_TYPES = [
    "person_name", "location", "organization", "date", "nationality",
    "occupation", "event", "product", "money_amount", "language",
    "duration", "time", "email", "phone_number", "url", "age"
]


def _filter_by_duration(results: List[dict], min_dur: Optional[float], max_dur: Optional[float]) -> List[dict]:
    """Filter results by duration."""
    filtered = []
    for r in results:
        start = float(r.get("start_time") or 0)
        end = float(r.get("end_time") or 0)
        duration = (end - start) / 1000 if end > 1000 else (end - start)
        
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


@app.command("list")
def list_entities(
    item_id: str = typer.Argument(..., help="Item ID to list entities for"),
    entity_type: Optional[str] = typer.Option(None, "--type", "-t", help="Filter by entity type"),
    min_duration: Optional[float] = typer.Option(None, "--min-duration", help="Minimum duration in seconds"),
    max_duration: Optional[float] = typer.Option(None, "--max-duration", help="Maximum duration in seconds"),
    unique: bool = typer.Option(False, "--unique", "-u", help="Only return unique text values"),
    limit: int = typer.Option(100, "--limit", "-l", help="Maximum results"),
    offset: int = typer.Option(0, "--offset", "-o", help="Offset for pagination"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """List entities for an item."""
    try:
        adapter = SQLiteAdapter()

        conditions = ["item_id = ?"]
        params = [item_id]

        if entity_type:
            conditions.append("entity_type = ?")
            params.append(entity_type)

        where_clause = " AND ".join(conditions)
        
        fetch_limit = limit * 5 if (min_duration or max_duration or unique) else limit
        params.extend([fetch_limit, offset])

        results = adapter.execute_raw(
            f'''
            SELECT id, text, entity_type, start_time, end_time, confidence, source_audio_url
            FROM entities
            WHERE {where_clause}
            ORDER BY start_time
            LIMIT ? OFFSET ?
            ''',
            params,
        )

        if min_duration or max_duration:
            results = _filter_by_duration(results, min_duration, max_duration)

        if unique:
            results = _filter_unique(results, unique)

        results = results[:limit]

        output_result(
            results,
            json_output=json_output,
            columns=["id", "text", "entity_type", "confidence", "start_time", "end_time"],
            title=f"Entities for item {item_id}",
        )

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command()
def search(
    query: str = typer.Argument(..., help="Search query"),
    mode: str = typer.Option("contains", "--mode", "-m", help="Match mode: contains, exact, starts_with, ends_with"),
    item_id: Optional[str] = typer.Option(None, "--item", "-i", help="Filter by item ID"),
    format_id: Optional[str] = typer.Option(None, "--format", "-f", help="Filter by format ID"),
    entity_type: Optional[str] = typer.Option(None, "--type", "-t", help="Filter by entity type (comma-separated for multiple)"),
    min_duration: Optional[float] = typer.Option(None, "--min-duration", help="Minimum duration in seconds"),
    max_duration: Optional[float] = typer.Option(None, "--max-duration", help="Maximum duration in seconds"),
    min_confidence: Optional[float] = typer.Option(None, "--min-confidence", help="Minimum confidence score (0-1)"),
    unique: bool = typer.Option(False, "--unique", "-u", help="Only return unique text values"),
    limit: int = typer.Option(50, "--limit", "-l", help="Maximum results"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """
    Search entities by text with various match modes.
    
    Match modes:
      - contains: Text contains the query (default)
      - exact: Text exactly matches the query
      - starts_with: Text starts with the query
      - ends_with: Text ends with the query
    
    Examples:
      stemmy entities search "Amsterdam"
      stemmy entities search "Jan" --mode starts_with --type person_name
      stemmy entities search "tech" --type organization,product
    """
    try:
        adapter = SQLiteAdapter()

        query_lower = query.lower()
        
        if mode == "exact":
            text_cond = "LOWER(text) = ?"
            text_param = query_lower
        elif mode == "starts_with":
            text_cond = "LOWER(text) LIKE ?"
            text_param = f"{query_lower}%"
        elif mode == "ends_with":
            text_cond = "LOWER(text) LIKE ?"
            text_param = f"%{query_lower}"
        else:
            text_cond = "LOWER(text) LIKE ?"
            text_param = f"%{query_lower}%"

        conditions = [text_cond]
        params = [text_param]

        if item_id:
            conditions.append("item_id = ?")
            params.append(item_id)

        if format_id:
            conditions.append("format_id = ?")
            params.append(format_id)

        if entity_type:
            types = [t.strip() for t in entity_type.split(",")]
            if len(types) == 1:
                conditions.append("entity_type = ?")
                params.append(types[0])
            else:
                placeholders = ",".join("?" * len(types))
                conditions.append(f"entity_type IN ({placeholders})")
                params.extend(types)

        if min_confidence:
            conditions.append("CAST(confidence AS REAL) >= ?")
            params.append(min_confidence)

        where_clause = " AND ".join(conditions)
        
        fetch_limit = limit * 5 if (min_duration or max_duration or unique) else limit
        params.append(fetch_limit)

        results = adapter.execute_raw(
            f'''
            SELECT id, text, entity_type, start_time, end_time, item_id, confidence, source_audio_url
            FROM entities
            WHERE {where_clause}
            ORDER BY start_time
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
            columns=["id", "text", "entity_type", "confidence", "item_id"],
            title=f"Entities matching '{query}'{mode_desc}",
        )

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("by-category")
def by_category(
    categories: str = typer.Argument(..., help="Entity categories to search (comma-separated)"),
    item_id: Optional[str] = typer.Option(None, "--item", "-i", help="Filter by item ID"),
    format_id: Optional[str] = typer.Option(None, "--format", "-f", help="Filter by format ID"),
    min_duration: Optional[float] = typer.Option(None, "--min-duration", help="Minimum duration in seconds"),
    max_duration: Optional[float] = typer.Option(None, "--max-duration", help="Maximum duration in seconds"),
    unique: bool = typer.Option(False, "--unique", "-u", help="Only return unique text values"),
    limit: int = typer.Option(50, "--limit", "-l", help="Maximum results"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """
    Find entities by category/type.
    
    Available categories:
      person_name, location, organization, date, nationality,
      occupation, event, product, money_amount, language,
      duration, time, email, phone_number, url, age
    
    Examples:
      stemmy entities by-category "person_name"
      stemmy entities by-category "person_name,location" --unique
      stemmy entities by-category "organization" --format abc123
    """
    try:
        adapter = SQLiteAdapter()

        cat_list = [c.strip() for c in categories.split(",")]
        
        conditions = []
        params = []

        if len(cat_list) == 1:
            conditions.append("entity_type = ?")
            params.append(cat_list[0])
        else:
            placeholders = ",".join("?" * len(cat_list))
            conditions.append(f"entity_type IN ({placeholders})")
            params.extend(cat_list)

        if item_id:
            conditions.append("item_id = ?")
            params.append(item_id)

        if format_id:
            conditions.append("format_id = ?")
            params.append(format_id)

        conditions.append("source_audio_url IS NOT NULL")

        where_clause = " AND ".join(conditions)
        
        fetch_limit = limit * 5 if (min_duration or max_duration or unique) else limit
        params.append(fetch_limit)

        results = adapter.execute_raw(
            f'''
            SELECT id, text, entity_type, start_time, end_time, item_id, confidence, source_audio_url
            FROM entities
            WHERE {where_clause}
            ORDER BY start_time
            LIMIT ?
            ''',
            params,
        )

        if min_duration or max_duration:
            results = _filter_by_duration(results, min_duration, max_duration)

        if unique:
            results = _filter_unique(results, unique)

        results = results[:limit]

        output_result(
            results,
            json_output=json_output,
            columns=["id", "text", "entity_type", "confidence", "start_time", "end_time"],
            title=f"Entities in categories: {categories}",
        )

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("top")
def top_entities(
    entity_type: Optional[str] = typer.Option(None, "--type", "-t", help="Filter by entity type"),
    item_id: Optional[str] = typer.Option(None, "--item", "-i", help="Filter by item ID"),
    format_id: Optional[str] = typer.Option(None, "--format", "-f", help="Filter by format ID"),
    top_n: int = typer.Option(10, "--top", "-n", help="Number of top entities per category"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """
    Find top/most frequently mentioned entities.
    
    Examples:
      stemmy entities top --type person_name --top 20
      stemmy entities top --format abc123
    """
    try:
        adapter = SQLiteAdapter()

        conditions = []
        params = []

        if entity_type:
            conditions.append("entity_type = ?")
            params.append(entity_type)

        if item_id:
            conditions.append("item_id = ?")
            params.append(item_id)

        if format_id:
            conditions.append("format_id = ?")
            params.append(format_id)

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        results = adapter.execute_raw(
            f'''
            SELECT text, entity_type, COUNT(*) as mention_count,
                   AVG(CAST(confidence AS REAL)) as avg_confidence
            FROM entities
            WHERE {where_clause}
            GROUP BY LOWER(text), entity_type
            ORDER BY mention_count DESC
            LIMIT ?
            ''',
            params + [top_n * 5],
        )

        if entity_type:
            results = results[:top_n]
        else:
            by_type = {}
            for r in results:
                t = r["entity_type"]
                if t not in by_type:
                    by_type[t] = []
                if len(by_type[t]) < top_n:
                    by_type[t].append(r)
            
            results = []
            for t in sorted(by_type.keys()):
                results.extend(by_type[t])

        output_result(
            results,
            json_output=json_output,
            columns=["text", "entity_type", "mention_count", "avg_confidence"],
            title=f"Top entities (grouped by frequency)",
        )

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("stats")
def entity_stats(
    item_id: Optional[str] = typer.Option(None, "--item", "-i", help="Filter by item ID"),
    format_id: Optional[str] = typer.Option(None, "--format", "-f", help="Filter by format ID"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """
    Show entity statistics and distribution.
    
    Examples:
      stemmy entities stats
      stemmy entities stats --format abc123
    """
    try:
        adapter = SQLiteAdapter()

        conditions = []
        params = []

        if item_id:
            conditions.append("item_id = ?")
            params.append(item_id)

        if format_id:
            conditions.append("format_id = ?")
            params.append(format_id)

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        type_counts = adapter.execute_raw(
            f'''
            SELECT entity_type, COUNT(*) as count,
                   COUNT(DISTINCT LOWER(text)) as unique_count,
                   AVG(CAST(confidence AS REAL)) as avg_confidence
            FROM entities
            WHERE {where_clause}
            GROUP BY entity_type
            ORDER BY count DESC
            ''',
            params,
        )

        total = sum(r["count"] for r in type_counts)
        unique_total = sum(r["unique_count"] for r in type_counts)

        for r in type_counts:
            r["percentage"] = round(r["count"] / total * 100, 1) if total > 0 else 0

        if json_output:
            output_result({
                "types": type_counts,
                "total_entities": total,
                "total_unique": unique_total
            }, json_output=True)
        else:
            print_info(f"Total entities: {total} ({unique_total} unique)")
            output_result(
                type_counts,
                columns=["entity_type", "count", "unique_count", "percentage", "avg_confidence"],
                title="Entity distribution",
            )

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command()
def types(
    item_id: Optional[str] = typer.Option(None, "--item", "-i", help="Filter by item ID"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """List available entity types and their counts."""
    try:
        adapter = SQLiteAdapter()

        if item_id:
            results = adapter.execute_raw(
                '''
                SELECT entity_type, COUNT(*) as count
                FROM entities
                WHERE item_id = ?
                GROUP BY entity_type
                ORDER BY count DESC
                ''',
                [item_id],
            )
        else:
            results = adapter.execute_raw(
                '''
                SELECT entity_type, COUNT(*) as count
                FROM entities
                GROUP BY entity_type
                ORDER BY count DESC
                ''',
            )

        output_result(
            results,
            json_output=json_output,
            columns=["entity_type", "count"],
            title="Entity types",
        )

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("import")
def import_entities(
    item_id: str = typer.Argument(..., help="Item ID to import entities from"),
    stemmy_id: str = typer.Argument(..., help="Target stemmy ID"),
    entity_type: Optional[str] = typer.Option(None, "--type", "-t", help="Filter by entity type"),
    limit: int = typer.Option(10, "--limit", "-l", help="Maximum entities to import"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Import entities into a stemmy as fragments."""
    print_error("Import not yet implemented - use extract-audio-segments endpoint")
    raise typer.Exit(1)


@app.command()
def patterns(
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """List common entity patterns for compilation."""
    patterns = [
        {"category": "person_name", "description": "Names of people mentioned"},
        {"category": "location", "description": "Places and locations"},
        {"category": "organization", "description": "Companies, institutions"},
        {"category": "date", "description": "Dates and time references"},
        {"category": "nationality", "description": "Nationalities mentioned"},
        {"category": "occupation", "description": "Jobs and professions"},
        {"category": "event", "description": "Named events"},
        {"category": "product", "description": "Product names"},
        {"category": "money_amount", "description": "Monetary values"},
        {"category": "language", "description": "Languages mentioned"},
    ]

    output_result(
        patterns,
        json_output=json_output,
        columns=["category", "description"],
        title="Entity patterns for compilation",
    )
