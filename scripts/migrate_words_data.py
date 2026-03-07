#!/usr/bin/env python3
"""
Migrate words data from items.transcript_data to fragments.words

This script extracts word-level timing data from AssemblyAI transcripts
stored in items.transcript_data and assigns them to the corresponding
fragments based on time ranges. Uses top-level "words" or per-utterance "words".
"""
import sqlite3
import json
from pathlib import Path
from typing import List, Dict, Any, Optional
import sys

# Add src to path for stemmy_cli (works from stemmy_cli/ or surrounded/)
_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_root / "src"))


def get_words_for_fragment(
    all_words: List[Dict], 
    start_time: float, 
    end_time: float,
    tolerance_ms: int = 50
) -> List[Dict]:
    """
    Find words that fall within the fragment's time range.
    
    Args:
        all_words: List of word dicts with 'start' and 'end' in milliseconds
        start_time: Fragment start (could be seconds or milliseconds)
        end_time: Fragment end (could be seconds or milliseconds)
        tolerance_ms: Tolerance for matching (words slightly outside range)
    """
    # Detect if times are in seconds or milliseconds
    # If start_time < 10000, assume seconds, else milliseconds
    if start_time < 10000:
        start_ms = start_time * 1000 - tolerance_ms
        end_ms = end_time * 1000 + tolerance_ms
    else:
        start_ms = start_time - tolerance_ms
        end_ms = end_time + tolerance_ms
    
    matching = []
    for word in all_words:
        word_start = word.get('start', 0)
        word_end = word.get('end', 0)
        
        # Word is within range if it overlaps
        if word_start >= start_ms and word_end <= end_ms:
            matching.append(word)
        elif word_start < end_ms and word_end > start_ms:
            # Partial overlap - include if majority is within range
            overlap = min(word_end, end_ms) - max(word_start, start_ms)
            word_duration = word_end - word_start
            if word_duration > 0 and overlap / word_duration > 0.5:
                matching.append(word)
    
    return matching


def get_words_from_transcript(transcript: Dict, item_id: str) -> Optional[List[Dict]]:
    """
    Get words array from transcript. Tries top-level 'words' first,
    then builds from utterances[].words if each utterance has words.
    """
    all_words = transcript.get("words", [])
    if all_words:
        return all_words

    # Fallback: utterances may have per-utterance words
    utterances = transcript.get("utterances", [])
    if not utterances:
        return None

    combined = []
    for u in utterances:
        uw = u.get("words", [])
        if uw:
            combined.extend(uw)
    return combined if combined else None


def migrate_words(dry_run: bool = True, limit: Optional[int] = None):
    """
    Migrate words data from transcript_data to fragments.
    
    Args:
        dry_run: If True, don't commit changes
        limit: Limit number of items to process (for testing)
    """
    from stemmy_cli.config import get_database_path

    db_path = get_database_path()
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        return 0

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Get items with transcript_data (words can be top-level or in utterances)
    query = """
        SELECT i.id, i.title, i.transcript_data
        FROM items i
        WHERE i.transcript_data IS NOT NULL
        AND (i.transcript_data LIKE '%"words"%' OR i.transcript_data LIKE '%"utterances"%')
    """
    params = []
    if limit:
        query += " LIMIT ?"
        params.append(limit)

    items = conn.execute(query, params).fetchall()
    print(f"Processing {len(items)} items with transcript data...")

    total_updated = 0
    total_skipped = 0

    for item in items:
        try:
            transcript = json.loads(item["transcript_data"])
            all_words = get_words_from_transcript(transcript, item["id"])

            if not all_words:
                continue

            # Get fragments for this item that need words
            fragments = conn.execute(
                """
                SELECT id, text, start_time, end_time, words
                FROM fragments
                WHERE item_id = ?
                AND (words IS NULL OR words = '' OR words = '[]')
                """,
                [item["id"]],
            ).fetchall()

            for frag in fragments:
                try:
                    start_time = float(frag["start_time"]) if frag["start_time"] else 0
                    end_time = float(frag["end_time"]) if frag["end_time"] else 0
                except (ValueError, TypeError):
                    total_skipped += 1
                    continue

                if start_time >= end_time:
                    total_skipped += 1
                    continue

                matching_words = get_words_for_fragment(all_words, start_time, end_time)

                if matching_words:
                    words_json = json.dumps(matching_words)

                    if not dry_run:
                        conn.execute(
                            "UPDATE fragments SET words = ? WHERE id = ?",
                            [words_json, frag["id"]],
                        )

                    total_updated += 1

        except (json.JSONDecodeError, KeyError) as e:
            print(f"  Error processing item {item['id']}: {e}")
            continue

    if not dry_run:
        conn.commit()

    conn.close()

    print(f"\n{'DRY RUN - ' if dry_run else ''}Results:")
    print(f"  Updated: {total_updated:,} fragments")
    print(f"  Skipped: {total_skipped:,} fragments")

    return total_updated


if __name__ == "__main__":
    dry_run = "--execute" not in sys.argv
    limit = None
    
    for arg in sys.argv[1:]:
        if arg.startswith("--limit="):
            limit = int(arg.split("=")[1])
    
    if dry_run:
        print("DRY RUN MODE - use --execute to apply changes\n")
    
    migrate_words(dry_run=dry_run, limit=limit)
