"""Format (podcast/show) management commands."""

import uuid
from typing import Optional, List, Dict, Any
from datetime import datetime

import typer
import feedparser

from stemmy_cli.adapters.sqlite import SQLiteAdapter
from stemmy_cli.adapters.http import HTTPAdapter
from stemmy_cli.output import output_result, print_error, print_success, print_info, print_warning

app = typer.Typer(help="Manage formats (podcasts/shows)")


def _parse_rss_feed(url: str) -> Dict[str, Any]:
    """Parse an RSS feed and return structured data."""
    feed = feedparser.parse(url)
    
    if feed.bozo and not feed.entries:
        raise ValueError(f"Failed to parse RSS feed: {feed.bozo_exception}")
    
    channel = feed.feed
    
    return {
        "title": getattr(channel, "title", "Unknown"),
        "description": getattr(channel, "description", ""),
        "link": getattr(channel, "link", url),
        "image": getattr(channel, "image", {}).get("href", "") if hasattr(channel, "image") else "",
        "author": getattr(channel, "author", getattr(channel, "itunes_author", "")),
        "language": getattr(channel, "language", ""),
        "categories": [tag.term for tag in getattr(channel, "tags", [])],
        "episode_count": len(feed.entries),
        "source_url": url,
        "source_type": "rss",
    }


def _parse_rss_episodes(url: str, limit: int = 20) -> List[Dict[str, Any]]:
    """Parse episodes from an RSS feed."""
    feed = feedparser.parse(url)
    
    if feed.bozo and not feed.entries:
        raise ValueError(f"Failed to parse RSS feed: {feed.bozo_exception}")
    
    episodes = []
    for entry in feed.entries[:limit]:
        audio_url = ""
        duration = 0
        
        for link in getattr(entry, "links", []):
            if link.get("type", "").startswith("audio/"):
                audio_url = link.get("href", "")
                break
        
        for enclosure in getattr(entry, "enclosures", []):
            if enclosure.get("type", "").startswith("audio/"):
                audio_url = enclosure.get("href", "")
                break
        
        if hasattr(entry, "itunes_duration"):
            dur = entry.itunes_duration
            if isinstance(dur, str):
                parts = dur.split(":")
                if len(parts) == 3:
                    duration = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                elif len(parts) == 2:
                    duration = int(parts[0]) * 60 + int(parts[1])
                else:
                    try:
                        duration = int(dur)
                    except ValueError:
                        pass
            else:
                duration = int(dur) if dur else 0
        
        published = ""
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            try:
                published = datetime(*entry.published_parsed[:6]).isoformat()
            except Exception:
                published = getattr(entry, "published", "")
        
        episodes.append({
            "title": getattr(entry, "title", "Untitled"),
            "description": getattr(entry, "summary", getattr(entry, "description", "")),
            "audio_url": audio_url,
            "published": published,
            "duration": duration,
            "link": getattr(entry, "link", ""),
            "guid": getattr(entry, "id", getattr(entry, "guid", "")),
        })
    
    return episodes


@app.command("list")
def list_formats(
    limit: int = typer.Option(50, "--limit", "-l", help="Maximum results"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """List all formats."""
    try:
        adapter = SQLiteAdapter()

        results = adapter.execute_raw(
            '''
            SELECT id, title, source_type, source_url, created_at
            FROM formats
            ORDER BY created_at DESC
            LIMIT ?
            ''',
            [limit],
        )

        output_result(
            results,
            json_output=json_output,
            columns=["id", "title", "source_type", "source_url"],
            title="Formats",
        )

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command()
def show(
    format_id: str = typer.Argument(..., help="Format ID"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Show details of a format."""
    try:
        adapter = SQLiteAdapter()
        result = adapter.get_by_id("formats", format_id)

        if not result:
            print_error(f"Format {format_id} not found")
            raise typer.Exit(1)

        item_count = adapter.count("items", where={"format_id": format_id})
        result["item_count"] = item_count

        output_result(result, json_output=json_output, title=f"Format {format_id}")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command()
def create(
    url: str = typer.Argument(..., help="RSS feed URL"),
    title: Optional[str] = typer.Option(None, "--title", "-t", help="Override title"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Create a new format from an RSS feed."""
    try:
        print_info(f"Fetching feed info from {url}...")
        feed_info = _parse_rss_feed(url)
        
        adapter = SQLiteAdapter()
        
        existing = adapter.execute_raw(
            "SELECT id, title FROM formats WHERE source_url = ?",
            [url]
        )
        if existing:
            print_warning(f"Format already exists: {existing[0]['title']} (ID: {existing[0]['id']})")
            output_result(existing[0], json_output=json_output, title="Existing format")
            return
        
        format_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()
        
        format_data = {
            "id": format_id,
            "title": title or feed_info["title"],
            "description": feed_info.get("description", ""),
            "source_url": url,
            "source_type": "rss",
            "image_url": feed_info.get("image", ""),
            "author": feed_info.get("author", ""),
            "language": feed_info.get("language", ""),
            "created_at": now,
            "updated_at": now,
        }
        
        adapter.insert("formats", format_data)
        print_success(f"Created format: {format_data['title']} (ID: {format_id})")
        
        output_result(format_data, json_output=json_output, title="New format")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("import-episodes")
def import_episodes(
    format_id: str = typer.Argument(..., help="Format ID"),
    url: Optional[str] = typer.Option(None, "--url", "-u", help="RSS URL (uses format's source_url if not provided)"),
    limit: int = typer.Option(10, "--limit", "-l", help="Maximum episodes to import"),
    transcribe: bool = typer.Option(False, "--transcribe", "-t", help="Start transcription for each episode"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Import multiple episodes from RSS feed into a format."""
    try:
        adapter = SQLiteAdapter()
        
        format_record = adapter.get_by_id("formats", format_id)
        if not format_record:
            print_error(f"Format {format_id} not found")
            raise typer.Exit(1)
        
        rss_url = url or format_record.get("source_url")
        if not rss_url:
            print_error("No RSS URL provided and format has no source_url")
            raise typer.Exit(1)
        
        print_info(f"Fetching episodes from {rss_url}...")
        episodes_list = _parse_rss_episodes(rss_url, limit=limit)
        
        imported = []
        skipped = []
        
        for ep in episodes_list:
            if not ep.get("audio_url"):
                skipped.append({"title": ep["title"], "reason": "no audio URL"})
                continue
            
            existing = adapter.execute_raw(
                "SELECT id FROM items WHERE format_id = ? AND (audio_url = ? OR title = ?)",
                [format_id, ep["audio_url"], ep["title"]]
            )
            if existing:
                skipped.append({"title": ep["title"], "reason": "already exists"})
                continue
            
            item_id = str(uuid.uuid4())
            now = datetime.utcnow().isoformat()
            
            item_data = {
                "id": item_id,
                "format_id": format_id,
                "title": ep["title"],
                "description": ep.get("description", ""),
                "audio_url": ep["audio_url"],
                "published_at": ep.get("published", now),
                "duration_seconds": ep.get("duration", 0),
                "source_url": ep.get("link", ""),
                "guid": ep.get("guid", ""),
                "transcript_status": "pending",
                "created_at": now,
                "updated_at": now,
            }
            
            adapter.insert("items", item_data)
            imported.append({"id": item_id, "title": ep["title"]})
            print_success(f"Imported: {ep['title']}")
        
        result = {
            "format_id": format_id,
            "imported_count": len(imported),
            "skipped_count": len(skipped),
            "imported": imported,
            "skipped": skipped,
        }
        
        if transcribe and imported:
            print_info(f"Starting transcription for {len(imported)} episodes...")
            http = HTTPAdapter(timeout=30.0)
            for item in imported:
                try:
                    http.post("/api/transcripts/create", json={"item_id": item["id"]})
                    print_info(f"Transcription started: {item['title']}")
                except Exception as e:
                    print_warning(f"Failed to start transcription for {item['title']}: {e}")
        
        output_result(result, json_output=json_output, title="Import results")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command()
def info(
    url: str = typer.Argument(..., help="RSS or YouTube URL"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Get info about an RSS feed or YouTube channel."""
    try:
        print_info(f"Fetching feed info from {url}...")
        result = _parse_rss_feed(url)
        output_result(result, json_output=json_output, title="Format info")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command()
def episodes(
    url: str = typer.Argument(..., help="RSS or YouTube URL"),
    limit: int = typer.Option(20, "--limit", "-l", help="Maximum episodes"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """List episodes from an RSS feed or YouTube channel."""
    try:
        print_info(f"Fetching episodes from {url}...")
        episodes_list = _parse_rss_episodes(url, limit=limit)

        if json_output:
            output_result(episodes_list, json_output=True)
        else:
            output_result(
                episodes_list,
                columns=["title", "published", "duration", "audio_url"],
                title=f"Episodes from {url}",
            )

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("import-item")
def import_item(
    format_id: str = typer.Argument(..., help="Format ID"),
    url: str = typer.Argument(..., help="Episode URL"),
    title: Optional[str] = typer.Option(None, "--title", "-t", help="Override title"),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Wait for import to complete"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Import an episode into a format."""
    try:
        http = HTTPAdapter()

        payload = {
            "format_id": format_id,
            "url": url,
        }
        if title:
            payload["title"] = title

        if wait:
            print_info("Starting import...")
            result = http.start_and_wait(
                "/api/formats/import-item",
                payload,
                "/api/formats/import-status/{task_id}",
                verbose=True,
            )
        else:
            result = http.post("/api/formats/import-item", json=payload)
            print_success(f"Import started: task_id={result.get('task_id')}")

        output_result(result, json_output=json_output, title="Import result")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command()
def transcripts(
    format_id: str = typer.Argument(..., help="Format ID"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """List transcripts for a format."""
    try:
        http = HTTPAdapter()
        result = http.get("/api/formats/transcripts", params={"format_id": format_id})

        transcripts_list = result.get("transcripts", result) if isinstance(result, dict) else result

        output_result(
            transcripts_list,
            json_output=json_output,
            columns=["id", "item_id", "status", "created_at"],
            title=f"Transcripts for format {format_id}",
        )

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command()
def items(
    format_id: str = typer.Argument(..., help="Format ID"),
    limit: int = typer.Option(50, "--limit", "-l", help="Maximum results"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """List items (episodes) for a format."""
    try:
        adapter = SQLiteAdapter()

        results = adapter.execute_raw(
            '''
            SELECT id, title, published_at, duration_seconds, audio_url, transcript_status
            FROM items
            WHERE format_id = ?
            ORDER BY published_at DESC
            LIMIT ?
            ''',
            [format_id, limit],
        )

        output_result(
            results,
            json_output=json_output,
            columns=["id", "title", "published_at", "duration_seconds", "transcript_status"],
            title=f"Items for format {format_id}",
        )

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)
