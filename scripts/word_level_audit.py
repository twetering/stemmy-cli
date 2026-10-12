#!/usr/bin/env python3
"""
Rapport: word-level dekking (fragments.words) + optioneel migratie draaien.

Voorbeelden:
  ./scripts/with_venv.sh python scripts/word_level_audit.py
  ./scripts/with_venv.sh python scripts/word_level_audit.py --db data/stemmy-replica.db
  ./scripts/with_venv.sh python scripts/word_level_audit.py --db data/stemmy-replica.db --migrate
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
DEFAULT_DB = _root / "data" / "stemmy-replica.db"


def report(db_path: Path) -> dict[str, int]:
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()

    total = cur.execute(
        "SELECT COUNT(*) FROM fragments"
    ).fetchone()[0]
    with_words = cur.execute(
        """SELECT COUNT(*) FROM fragments
           WHERE words IS NOT NULL AND words != '' AND words != '[]'"""
    ).fetchone()[0]
    missing = total - with_words

    orphans = cur.execute(
        """SELECT COUNT(*) FROM fragments fr
           LEFT JOIN items i ON i.id = fr.item_id
           WHERE i.id IS NULL"""
    ).fetchone()[0]

    missing_with_item = cur.execute(
        """SELECT COUNT(*) FROM fragments fr
           INNER JOIN items i ON i.id = fr.item_id
           WHERE fr.words IS NULL OR fr.words = '' OR fr.words = '[]'"""
    ).fetchone()[0]

    # Alleen fragmenten die nog aan een item hangen (migratie/repair is relevant)
    bad_time = cur.execute(
        """SELECT COUNT(*) FROM fragments fr
           INNER JOIN items i ON i.id = fr.item_id
           WHERE (fr.words IS NULL OR fr.words = '' OR fr.words = '[]')
             AND fr.start_time IS NOT NULL AND fr.end_time IS NOT NULL
             AND CAST(fr.start_time AS REAL) >= CAST(fr.end_time AS REAL)"""
    ).fetchone()[0]

    ok_window_item_missing = cur.execute(
        """SELECT COUNT(*) FROM fragments fr
           INNER JOIN items i ON i.id = fr.item_id
           WHERE (fr.words IS NULL OR fr.words = '' OR fr.words = '[]')
             AND fr.start_time IS NOT NULL AND fr.end_time IS NOT NULL
             AND CAST(fr.start_time AS REAL) < CAST(fr.end_time AS REAL)"""
    ).fetchone()[0]

    items_partial = cur.execute(
        """SELECT COUNT(DISTINCT i.id) FROM items i
           JOIN fragments fr ON fr.item_id = i.id
           WHERE i.transcript_data IS NOT NULL
             AND (i.transcript_data LIKE '%"words"%'
                  OR i.transcript_data LIKE '%"utterances"%')
             AND (fr.words IS NULL OR fr.words = '' OR fr.words = '[]')"""
    ).fetchone()[0]

    conn.close()

    pct = (100.0 * with_words / total) if total else 0.0
    print(f"\n  Database: {db_path}")
    print(f"  Fragmenten totaal:     {total:,}")
    print(f"  Met word-level JSON:   {with_words:,} ({pct:.2f}%)")
    print(f"  Nog leeg / zonder:     {missing:,}")
    print(f"    — zonder parent item:   {orphans:,} (replica-sync: opruimen of items terugzetten)")
    print(f"    — met item, nog leeg:   {missing_with_item:,} (migratie / transcript)")
    print(f"    — met item, start≥end: {bad_time:,} (timing/outro — datafix segmentgrenzen)")
    print(f"    — met item, geldig venster: {ok_window_item_missing:,} (probeer migrate_words; anders transcript)")
    print(f"  Items met transcript maar ≥1 frag zonder words: {items_partial:,}")
    print()

    return {
        "total": total,
        "with_words": with_words,
        "missing": missing,
        "orphans": orphans,
        "missing_with_item": missing_with_item,
        "bad_time": bad_time,
        "ok_window_item_missing": ok_window_item_missing,
        "items_partial": items_partial,
    }


def formats_low_coverage(db_path: Path, limit: int = 12) -> None:
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        f"""
        SELECT f.title,
               COUNT(fr.id) AS n,
               SUM(CASE WHEN fr.words IS NOT NULL AND fr.words != ''
                             AND fr.words != '[]' THEN 1 ELSE 0 END) AS w
        FROM formats f
        JOIN items i ON i.format_id = f.id
        JOIN fragments fr ON fr.item_id = i.id
        GROUP BY f.id
        HAVING w < n
        ORDER BY (n - w) DESC
        LIMIT {int(limit)}
        """
    ).fetchall()
    conn.close()
    if not rows:
        print("  Alle formats: 100% word-level op fragment-niveau (geen gaten).\n")
        return
    print("  Formats met nog gemiste fragmenten (top op aantal missing):")
    for title, n, w in rows:
        miss = n - w
        pct = 100.0 * w / n if n else 0
        t = (title or "(zonder titel)")[:56]
        print(f"    — {t:56}  {w:,}/{n:,}  ({pct:.1f}%, −{miss:,})")
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description="Word-level audit stemmy DB")
    ap.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help=f"SQLite-pad (default: {DEFAULT_DB})",
    )
    ap.add_argument(
        "--migrate",
        action="store_true",
        help="Daarna migrate_words_data.py --execute op dezelfde DB",
    )
    args = ap.parse_args()
    db_path = args.db.expanduser().resolve()
    if not db_path.exists():
        print(f"DB niet gevonden: {db_path}", file=sys.stderr)
        sys.exit(1)

    print("  === Word-level audit ===")
    before = report(db_path)
    formats_low_coverage(db_path)

    if args.migrate:
        print("  === Migratie (transcript_data → fragments.words) ===\n")
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from migrate_words_data import migrate_words  # noqa: E402

        migrate_words(dry_run=False, limit=None, db_path=db_path)
        print("\n  === Na migratie ===")
        after = report(db_path)
        formats_low_coverage(db_path)
        gained = after["with_words"] - before["with_words"]
        print(f"  Toename fragmenten met words: +{gained:,}\n")
        if after["missing"] and gained == 0:
            print(
                "  Geen winst: vaak ontbreken woord-tijden in transcript_data, of fragment-"
                "grenzen sluiten niet aan op AAI-words. Zie plan (re-transcribe / segmentfix).\n",
                file=sys.stderr,
            )


if __name__ == "__main__":
    main()
