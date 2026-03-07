"""Workflow commands that combine multiple operations."""

import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

import typer

from stemmy_cli.adapters.sqlite import SQLiteAdapter
from stemmy_cli.adapters.http import HTTPAdapter
from stemmy_cli.config import get_config
from stemmy_cli.output import output_result, print_error, print_success, print_info, print_warning
from stemmy_cli.commands.formats import _parse_rss_feed, _parse_rss_episodes
from stemmy_cli.commands.transcripts import _import_transcript_to_db
from stemmy_cli.transcribe import transcribe_item

app = typer.Typer(help="Automated workflows combining multiple steps")


@app.command("rss-to-compilation")
def rss_to_compilation(
    rss_url: str = typer.Argument(..., help="RSS feed URL"),
    voice_id: str = typer.Option(..., "--voice", "-v", help="Voice ID for TTS"),
    output_dir: Optional[str] = typer.Option(None, "--output", "-o", help="Output directory for audio files"),
    episodes: int = typer.Option(1, "--episodes", "-e", help="Number of episodes to process"),
    search_query: Optional[str] = typer.Option(None, "--search", "-s", help="Search query for fragments"),
    search_mode: str = typer.Option("contains", "--search-mode", help="contains or starts_with (e.g. 'Ik ben')"),
    entity_type: Optional[str] = typer.Option(None, "--entity-type", help="Entity type to extract"),
    fragment_limit: int = typer.Option(5, "--fragments", "-f", help="Number of fragments per compilation"),
    wait_for_transcript: bool = typer.Option(True, "--wait/--no-wait", help="Wait for transcription to complete"),
    intro_text: Optional[str] = typer.Option(None, "--intro", help="Intro text for compilation"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """
    Full workflow: RSS feed -> Episodes -> Transcript -> Search -> Compilation
    
    This command performs the complete pipeline:
      1. Creates or finds format from RSS
      2. Imports episodes 
      3. Starts transcription and waits for completion
      4. Imports fragments and entities
      5. Searches for relevant fragments
      6. Generates audio compilation
    
    Examples:
      stemmy workflows rss-to-compilation "https://example.com/feed.xml" -v voice_id -o ./output -s "technology"
      stemmy workflows rss-to-compilation "https://example.com/feed.xml" -v voice_id --entity-type person_name --fragments 10
    """
    try:
        adapter = SQLiteAdapter()
        http = HTTPAdapter(timeout=60.0)
        
        results = {
            "rss_url": rss_url,
            "steps_completed": [],
            "format_id": None,
            "items_imported": [],
            "transcripts_completed": [],
            "fragments_found": [],
            "compilation": None,
        }
        
        print_info(f"Step 1: Fetching RSS feed info from {rss_url}...")
        feed_info = _parse_rss_feed(rss_url)
        print_success(f"Found podcast: {feed_info['title']}")
        results["steps_completed"].append("fetch_rss")
        
        existing = adapter.execute_raw(
            "SELECT id, title FROM formats WHERE source_url = ?",
            [rss_url]
        )
        
        if existing:
            format_id = existing[0]["id"]
            print_info(f"Using existing format: {existing[0]['title']} (ID: {format_id})")
        else:
            format_id = str(uuid.uuid4())
            now = datetime.utcnow().isoformat()
            format_data = {
                "id": format_id,
                "title": feed_info["title"],
                "description": feed_info.get("description", ""),
                "source_url": rss_url,
                "source_type": "rss",
                "image_url": feed_info.get("image", ""),
                "created_at": now,
                "updated_at": now,
            }
            adapter.insert("formats", format_data)
            print_success(f"Created format: {feed_info['title']} (ID: {format_id})")
        
        results["format_id"] = format_id
        results["steps_completed"].append("create_format")
        
        print_info(f"Step 2: Importing {episodes} episode(s) (with audio normalization)...")
        episodes_list = _parse_rss_episodes(rss_url, limit=episodes)
        api_base = get_config().api_base_url
        subfolder = f"formats/{format_id}"
        
        items_to_process = []
        for ep in episodes_list:
            source_url = ep.get("audio_url", "")
            if not source_url:
                print_warning(f"Skipping '{ep['title']}' - no audio URL")
                continue
            
            # Dedupe on source_url (original mp3)
            existing_item = adapter.execute_raw(
                "SELECT id, audio_url FROM items WHERE format_id = ? AND source_url = ?",
                [format_id, source_url]
            )
            
            if existing_item:
                item_id = existing_item[0]["id"]
                audio_url = existing_item[0]["audio_url"]
                print_info(f"Using existing item: {ep['title']}")
            else:
                item_id = str(uuid.uuid4())
                now = datetime.utcnow().isoformat()
                print_info(f"Normalizing: {ep['title'][:50]}...")
                try:
                    from stemmy_cli.audio.normalize import process_audio_for_import
                    audio_url = process_audio_for_import(
                        source_url=source_url,
                        item_id=item_id,
                        subfolder=subfolder,
                        upload_to_api=api_base,
                    )
                except Exception as e:
                    print_warning(f"Normalization failed, using original: {e}")
                    audio_url = source_url
                
                item_data = {
                    "id": item_id,
                    "format_id": format_id,
                    "title": ep["title"],
                    "description": ep.get("description", ""),
                    "audio_url": audio_url,
                    "source_url": source_url,
                    "published_at": ep.get("published", now),
                    "duration_seconds": ep.get("duration", 0),
                    "transcript_status": "pending",
                    "created_at": now,
                    "updated_at": now,
                }
                adapter.insert("items", item_data)
                print_success(f"Imported: {ep['title']}")
            
            items_to_process.append({"id": item_id, "title": ep["title"], "audio_url": audio_url})
            results["items_imported"].append({"id": item_id, "title": ep["title"]})
        
        results["steps_completed"].append("import_episodes")
        
        print_info("Step 3: Transcribing episodes...")
        for item in items_to_process:
            fragment_count = adapter.count("fragments", where={"item_id": item["id"]})
            if fragment_count > 0:
                print_info(f"Already has {fragment_count} fragments: {item['title']}")
                results["transcripts_completed"].append({"item_id": item["id"], "status": "existing"})
                continue
            
            print_info(f"Starting transcription for: {item['title']}")
            try:
                transcript_id, status = transcribe_item(
                    item["audio_url"],
                    item["id"],
                    item["title"],
                    {"language_code": "nl", "speaker_labels": True, "entity_detection": True},
                    wait=wait_for_transcript,
                    http=http,
                )
                if wait_for_transcript and status:
                    print_success(f"Transcription completed: {item['title']}")
                    import_result = _import_transcript_to_db(
                        adapter, item["id"], item["audio_url"], status
                    )
                    print_success(
                        f"Imported {import_result['fragments_imported']} fragments "
                        f"and {import_result['entities_imported']} entities"
                    )
                    adapter.update("items", item["id"], {"transcript_status": "completed"})
                    results["transcripts_completed"].append({
                        "item_id": item["id"],
                        "transcript_id": transcript_id,
                        "fragments": import_result["fragments_imported"],
                        "entities": import_result["entities_imported"],
                    })
                elif transcript_id:
                    print_info(f"Transcription started: {transcript_id}")
                    results["transcripts_completed"].append({"item_id": item["id"], "transcript_id": transcript_id, "status": "started"})
                elif wait_for_transcript:
                    results["transcripts_completed"].append({"item_id": item["id"], "status": "error"})
                    
            except Exception as e:
                print_error(f"Transcription error for {item['title']}: {e}")
        
        results["steps_completed"].append("transcribe")

        # Step 3b: Build embeddings for new fragments (enables semantic search)
        try:
            needs_embedding = adapter.execute_raw(
                """
                SELECT COUNT(*) as n FROM fragments f
                LEFT JOIN fragment_embeddings e ON f.id = e.fragment_id
                WHERE f.item_id IN (
                    SELECT id FROM items WHERE format_id = ?
                ) AND f.text IS NOT NULL AND f.text != '' AND e.fragment_id IS NULL
                """,
                [format_id],
            )
            count = (needs_embedding[0]["n"] or 0) if needs_embedding else 0
            if count > 0:
                print_info(f"Step 3b: Building embeddings for {count} new fragments...")
                from stemmy_cli.embeddings.service import FragmentEmbedder
                from stemmy_cli.config import get_database_path
                import sqlite3

                db_path = get_database_path()
                provider = "openai" if os.environ.get("OPENAI_API_KEY") else "local"
                embedder = FragmentEmbedder(provider=provider, db_path=str(db_path))
                conn = sqlite3.connect(str(db_path))
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    """
                    SELECT f.id, f.text FROM fragments f
                    LEFT JOIN fragment_embeddings e ON f.id = e.fragment_id
                    WHERE f.item_id IN (SELECT id FROM items WHERE format_id = ?)
                    AND f.text IS NOT NULL AND f.text != '' AND e.fragment_id IS NULL
                    """,
                    [format_id],
                )
                fragments = [{"id": row[0], "text": row[1]} for row in cursor.fetchall()]
                conn.close()
                if fragments:
                    results_emb = embedder.embed_fragments(fragments, batch_size=32)
                    embedder.save_embeddings_to_db(results_emb)
                    print_success(f"Embedded {len(results_emb)} fragments")
                    results["steps_completed"].append("build_embeddings")
        except Exception as e:
            print_warning(f"Embeddings build skipped: {e}")

        print_info("Step 4: Searching for fragments...")
        all_fragments = []
        
        # Search more broadly: fragment_limit * 3 per item, then cap total
        per_item_limit = max(fragment_limit * 3, 20)
        
        for item in items_to_process:
            conditions = ["item_id = ?"]
            params = [item["id"]]
            
            if search_query:
                conditions.append("LOWER(text) LIKE ?")
                q = search_query.lower()
                params.append(f"{q}%" if search_mode == "starts_with" else f"%{q}%")
            
            where_clause = " AND ".join(conditions)
            params.append(per_item_limit)
            
            fragments = adapter.execute_raw(
                f'''
                SELECT id, text, start_time_seconds as start_time, 
                       end_time_seconds as end_time, item_audio_url as audio_url,
                       speaker_label, item_id
                FROM fragments
                WHERE {where_clause}
                ORDER BY RANDOM()
                LIMIT ?
                ''',
                params,
            )
            all_fragments.extend(fragments)
        
        if entity_type and not all_fragments:
            print_info(f"Searching for entities of type: {entity_type}")
            for item in items_to_process:
                entities = adapter.execute_raw(
                    '''
                    SELECT e.text, e.start_time, e.end_time, e.source_audio_url as audio_url,
                           e.item_id, e.entity_type
                    FROM entities e
                    WHERE e.item_id = ? AND e.entity_type = ?
                    ORDER BY RANDOM()
                    LIMIT ?
                    ''',
                    [item["id"], entity_type, fragment_limit],
                )
                all_fragments.extend([{
                    "id": str(uuid.uuid4()),
                    "text": e["text"],
                    "start_time": str(e["start_time"] / 1000 if e["start_time"] > 1000 else e["start_time"]),
                    "end_time": str(e["end_time"] / 1000 if e["end_time"] > 1000 else e["end_time"]),
                    "audio_url": e["audio_url"],
                    "item_id": e["item_id"],
                } for e in entities])
        
        all_fragments = all_fragments[:fragment_limit]
        results["fragments_found"] = [{"id": f["id"], "text": f["text"][:100]} for f in all_fragments]
        
        if not all_fragments:
            print_warning("No fragments found matching criteria")
            output_result(results, json_output=json_output, title="Workflow results (no fragments)")
            return
        
        print_success(f"Found {len(all_fragments)} fragments")
        results["steps_completed"].append("search_fragments")
        
        print_info("Step 5: Extracting audio segments (standalone ffmpeg)...")
        segments_payload = []
        for f in all_fragments:
            start = f.get("start_time")
            end = f.get("end_time")
            if start is not None and end is not None:
                start_f = float(start) if not isinstance(start, (int, float)) else start
                end_f = float(end) if not isinstance(end, (int, float)) else end
            else:
                continue
            segments_payload.append({
                "id": f.get("id", str(uuid.uuid4())),
                "start_time": start_f,
                "end_time": end_f,
                "audio_url": f.get("audio_url", ""),
            })
        
        if not segments_payload:
            print_warning("No valid segments to extract (missing start/end times)")
            output_result(results, json_output=json_output, title="Workflow results")
            return
        
        from stemmy_cli.audio.parallel_extractor import ParallelExtractor
        from stemmy_cli.tts import generate_tts, is_tts_configured
        import tempfile
        from pydub import AudioSegment

        if not is_tts_configured():
            print_error("ELEVENLABS_API_KEY not set. Add to .env for standalone TTS. See .env.example.")
            results["compilation"] = {"error": "ELEVENLABS_API_KEY required for TTS"}
            output_result(results, json_output=json_output, title="Workflow results")
            return

        print_info("Step 6: Extracting + compiling (standalone)...")
        final_intro = intro_text or f"Welkom bij deze compilatie van {feed_info['title']}"

        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                tmp = Path(tmpdir)
                # Extract (standalone ffmpeg, files stay in tmp until concat)
                extractor = ParallelExtractor(max_workers=4, output_dir=tmp)
                successes, failures = extractor.extract_segments(segments_payload)
                if not successes:
                    err_msgs = [f"{r.segment_id}: {r.error}" for r in failures[:3]]
                    print_error("Extraction failed: " + "; ".join(err_msgs))
                    results["compilation"] = {"error": "Extraction failed"}
                    output_result(results, json_output=json_output, title="Workflow results")
                    return
                if failures:
                    print_warning(f"Extracted {len(successes)}/{len(segments_payload)} segments ({len(failures)} failed)")
                else:
                    print_success(f"Extraction complete: {len(successes)} segments")

                extracted_paths = {r.segment_id: r.output_path for r in successes}

                # TTS intro
                intro_path = tmp / "intro.mp3"
                generate_tts(final_intro, voice_id, output_path=intro_path)
                segments_to_concat = [intro_path]

                # Add extracted segment paths in order
                for f in all_fragments:
                    frag_id = f.get("id")
                    path = extracted_paths.get(str(frag_id)) or extracted_paths.get(frag_id)
                    if path and Path(path).exists():
                        segments_to_concat.append(Path(path))

                # Concatenate
                combined = AudioSegment.empty()
                for p in segments_to_concat:
                    combined += AudioSegment.from_mp3(str(p))

                # Save
                if output_dir:
                    out_dir = Path(output_dir)
                    out_dir.mkdir(parents=True, exist_ok=True)
                    safe_title = feed_info["title"].replace(" ", "_").replace("/", "-")[:30]
                    output_file = out_dir / f"compilation_{safe_title}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp3"
                else:
                    output_file = tmp / "compilation.mp3"

                combined.export(str(output_file), format="mp3", bitrate="128k")
                print_success(f"Compilation saved: {output_file}")
                results["compilation"] = {"audio_file": str(output_file)}
                results["steps_completed"].append("generate_compilation")

        except Exception as e:
            print_error(f"Compilation generation error: {e}")
            results["compilation"] = {"error": str(e)}
        
        output_result(results, json_output=json_output, title="Workflow completed")
        
    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("transcribe-all")
def transcribe_all(
    format_id: str = typer.Argument(..., help="Format ID"),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Wait for each transcription"),
    limit: int = typer.Option(10, "--limit", "-l", help="Maximum items to transcribe"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Transcribe all pending items in a format."""
    try:
        adapter = SQLiteAdapter()
        http = HTTPAdapter(timeout=60.0)
        
        items = adapter.execute_raw(
            '''
            SELECT id, title, audio_url, transcript_status
            FROM items
            WHERE format_id = ? AND (transcript_status IS NULL OR transcript_status = 'pending')
            LIMIT ?
            ''',
            [format_id, limit],
        )
        
        if not items:
            print_info("No pending items to transcribe")
            return
        
        print_info(f"Found {len(items)} items to transcribe")
        
        results = []
        for item in items:
            print_info(f"Transcribing: {item['title']}")
            
            try:
                transcript_id, status = transcribe_item(
                    item["audio_url"],
                    item["id"],
                    item["title"],
                    {"language_code": "nl", "speaker_labels": True, "entity_detection": True},
                    wait=wait,
                    http=http,
                )
                if wait and status:
                    import_result = _import_transcript_to_db(
                        adapter, item["id"], item["audio_url"], status
                    )
                    adapter.update("items", item["id"], {"transcript_status": "completed"})
                    print_success(
                        f"Completed: {item['title']} "
                        f"({import_result['fragments_imported']} fragments, "
                        f"{import_result['entities_imported']} entities)"
                    )
                    results.append({"item_id": item["id"], "status": "completed", **import_result})
                elif transcript_id:
                    results.append({"item_id": item["id"], "transcript_id": transcript_id, "status": "started"})
                else:
                    results.append({"item_id": item["id"], "status": "error"})
                    
            except Exception as e:
                print_error(f"Error transcribing {item['title']}: {e}")
                results.append({"item_id": item["id"], "status": "error", "error": str(e)})
        
        output_result(results, json_output=json_output, title="Transcription results")
        
    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)
