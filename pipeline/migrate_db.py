#!/usr/bin/env python3
"""
migrate_db.py — DB-sanering en performance-fixes voor stemmy Turso.

Fixes in volgorde:
  1. Duplicaat format-rijen verwijderen (via rowid)
  2. Slug-format-IDs omzetten naar proper UUIDs (EvB: ervaring-voor-beginners-shared)
  3. Ontbrekende identifier (shortname) invullen bij alle formats
  4. Missende indexes aanmaken voor items-tabel (format_id, source_url, transcript_status)
  5. UNIQUE index op formats(id) zodat toekomstige duplicaten onmogelijk zijn
  6. Verify: toon eindstatus

Veilig om te herdraaien: alle stappen zijn idempotent (IF NOT EXISTS / check first).

Gebruik:
    cd ~/repos/stemmy_cli
    python3 pipeline/migrate_db.py
    python3 pipeline/migrate_db.py --dry-run
"""

import argparse
import os
import sys
import time
import uuid
from pathlib import Path

import httpx

PIPELINE_DIR = Path(__file__).parent


# ── Turso HTTP client ─────────────────────────────────────────────────────────

def _setup() -> tuple[str, str]:
    env_file = PIPELINE_DIR.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
    url = os.environ.get("TURSO_URL") or os.environ.get("TURSO_DB_URL")
    token = os.environ.get("TURSO_TOKEN") or os.environ.get("TURSO_API_KEY")
    if not url or not token:
        raise ValueError("TURSO_URL + TURSO_TOKEN zijn verplicht")
    return url.replace("libsql://", "https://"), token


_URL, _TOKEN = None, None


def _headers():
    return {"Authorization": f"Bearer {_TOKEN}", "Content-Type": "application/json"}


def _val(v):
    if v is None: return {"type": "null"}
    return {"type": "text", "value": str(v)}


def q(sql: str, params: list = None, timeout: float = 20.0) -> list:
    stmt = {"sql": sql}
    if params:
        stmt["args"] = [_val(p) for p in params]
    r = httpx.post(
        _URL + "/v2/pipeline", headers=_headers(),
        json={"requests": [{"type": "execute", "stmt": stmt}, {"type": "close"}]},
        timeout=timeout,
    )
    r.raise_for_status()
    result = r.json()["results"][0]
    if result.get("type") == "error":
        raise RuntimeError(f"SQL: {result['error']}")
    rs = result.get("response", {}).get("result", {})
    cols = [c["name"] for c in rs.get("cols", [])]
    return [{c: (cell["value"] if cell["type"] != "null" else None)
             for c, cell in zip(cols, row)} for row in rs.get("rows", [])]


def ddl(sql: str, timeout: float = 120.0) -> dict:
    """DDL-statements (CREATE INDEX / etc.) — lange timeout."""
    r = httpx.post(
        _URL + "/v2/pipeline", headers=_headers(),
        json={"requests": [{"type": "execute", "stmt": {"sql": sql}}, {"type": "close"}]},
        timeout=timeout,
    )
    r.raise_for_status()
    result = r.json()["results"][0]
    if result.get("type") == "error":
        raise RuntimeError(f"DDL: {result['error']}")
    return result.get("response", {}).get("result", {})


def batch(stmts: list, timeout: float = 60.0) -> int:
    """Batch van meerdere statements (UPDATE/DELETE) — 200 per call."""
    chunk_size = 200
    total = 0
    for i in range(0, len(stmts), chunk_size):
        chunk = stmts[i:i + chunk_size]
        reqs = [{"type": "execute", "stmt": s} for s in chunk] + [{"type": "close"}]
        r = httpx.post(
            _URL + "/v2/pipeline", headers=_headers(),
            json={"requests": reqs}, timeout=timeout,
        )
        r.raise_for_status()
        for j, res in enumerate(r.json().get("results", [])[:-1]):
            if res.get("type") == "error":
                print(f"  [warn] batch[{i+j}]: {res['error']}")
        total += len(chunk)
    return total


# ── Slug → identifier mapping ─────────────────────────────────────────────────

def title_to_identifier(title: str) -> str:
    """Genereer een shortname/identifier uit een format-titel.
    Consistent met bestaande identifiers (lowercase, underscores, kort).
    """
    import re
    s = title.lower()
    # Verwijder emoji, speciale tekens
    s = re.sub(r"[^\w\s]", "", s, flags=re.UNICODE)
    # Woorden samenvoegen met underscore
    words = s.split()
    # Verwijder stopwoorden die niet betekenisvol zijn
    stopwords = {"de", "het", "een", "van", "en", "voor", "over", "bij", "op", "in",
                 "met", "aan", "om", "uit", "the", "a", "an", "of", "and", "for"}
    words_clean = [w for w in words if w not in stopwords] or words
    # Max 4 woorden voor leesbaarheid
    identifier = "_".join(words_clean[:4])
    # Max 40 tekens
    return identifier[:40]


# Handmatige overrides voor bekende formats (betere shortnames)
IDENTIFIER_OVERRIDES = {
    "ervaring-voor-beginners-shared":        "ervaring_voor_beginners",
    "c763c512-77a7-4db6-9d70-da3a50fa4c7a":  "hku_en_ai",  # HKU en AI
}


# ── Migratie-stappen ──────────────────────────────────────────────────────────

def step_remove_duplicates(dry_run: bool) -> int:
    """Verwijder duplicate format-rijen op basis van rowid (behoud laagste)."""
    print("\n[1/6] Duplicate format-rijen verwijderen...")

    # Vind alle duplicaten: zelfde id, meerdere rows
    rows = q("""
        SELECT id, MIN(rowid) as keep_rowid, COUNT(*) as cnt
        FROM formats
        GROUP BY id
        HAVING COUNT(*) > 1
    """)

    if not rows:
        print("  ✅ Geen duplicaten gevonden")
        return 0

    total_deleted = 0
    for r in rows:
        fmt_id = r["id"]
        keep = int(r["keep_rowid"])
        cnt = int(r["cnt"])
        print(f"  Format '{fmt_id}': {cnt} rows, behoud rowid={keep}, verwijder {cnt-1}")

        if not dry_run:
            q(f"DELETE FROM formats WHERE id = ? AND rowid != ?", [fmt_id, str(keep)])
            # Verify
            remaining = q("SELECT COUNT(*) as n FROM formats WHERE id = ?", [fmt_id])
            n = int(remaining[0]["n"])
            if n == 1:
                print(f"  ✅ Duplicaat verwijderd (nu 1 rij)")
            else:
                print(f"  ⚠️  Nog {n} rijen voor '{fmt_id}'")
        total_deleted += cnt - 1

    return total_deleted


def step_fix_slug_ids(dry_run: bool) -> dict:
    """
    Converteer niet-UUID format IDs naar echte UUIDs.
    Update ook alle items.format_id die verwijzen naar de oude ID.
    Retourneert mapping {old_id: new_id}.
    """
    print("\n[2/6] Slug-IDs omzetten naar UUIDs...")

    # Detecteer non-UUID ids (echte UUIDs hebben 36 chars met 4 streepjes)
    import re
    uuid_pattern = re.compile(
        r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
        re.IGNORECASE
    )

    all_formats = q("SELECT id, title FROM formats")
    slug_ids = [f for f in all_formats if not uuid_pattern.match(f["id"] or "")]

    if not slug_ids:
        print("  ✅ Alle format IDs zijn al UUIDs")
        return {}

    id_mapping = {}
    for fmt in slug_ids:
        old_id = fmt["id"]
        new_id = str(uuid.uuid4())
        id_mapping[old_id] = new_id
        print(f"  '{old_id}' → {new_id} ({fmt['title']})")

        if not dry_run:
            # Stap 2a: count items die geraakt worden
            item_count = q("SELECT COUNT(*) as n FROM items WHERE format_id = ?", [old_id])
            n_items = int(item_count[0]["n"])

            # Stap 2b: update items.format_id (één statement, gebruikt index als die er is)
            print(f"    Updating {n_items} items.format_id...", flush=True)
            t0 = time.time()
            q(f"UPDATE items SET format_id = ? WHERE format_id = ?", [new_id, old_id], timeout=60.0)
            print(f"    ✅ {n_items} items updated in {time.time()-t0:.1f}s")

            # Stap 2c: update formats.id (niet mogelijk met UPDATE op PK... maar id is geen PK hier)
            # → INSERT nieuwe rij, DELETE oude rij
            fmt_row = q("SELECT * FROM formats WHERE id = ?", [old_id])
            if fmt_row:
                row = fmt_row[0]
                cols = [k for k, v in row.items() if v is not None]
                vals = [row[c] for c in cols]
                # Vervang id
                id_idx = cols.index("id")
                vals[id_idx] = new_id
                ph = ", ".join("?" * len(cols))
                q(f"INSERT OR IGNORE INTO formats ({', '.join(cols)}) VALUES ({ph})", vals)
                q("DELETE FROM formats WHERE id = ?", [old_id])
                print(f"    ✅ formats.id bijgewerkt")

    return id_mapping


def step_fix_identifiers(dry_run: bool, id_mapping: dict) -> int:
    """Vul ontbrekende identifier (shortname) in bij alle formats."""
    print("\n[3/6] Ontbrekende identifiers invullen...")

    # Gebruik de nieuwe IDs als mapping heeft plaatsgevonden
    all_formats = q("SELECT id, title, identifier FROM formats")
    missing = [f for f in all_formats if not f["identifier"]]

    if not missing:
        print("  ✅ Alle formats hebben een identifier")
        return 0

    updated = 0
    for fmt in missing:
        fmt_id = fmt["id"]
        # Gebruik override als beschikbaar, anders automatisch genereren
        # Check ook old_id → new_id mapping
        original_id = next((old for old, new in id_mapping.items() if new == fmt_id), fmt_id)
        identifier = (
            IDENTIFIER_OVERRIDES.get(fmt_id)
            or IDENTIFIER_OVERRIDES.get(original_id)
            or title_to_identifier(fmt["title"] or "")
        )

        print(f"  '{fmt['title']}' → identifier='{identifier}'")

        if not dry_run:
            q("UPDATE formats SET identifier = ? WHERE id = ?", [identifier, fmt_id])
            updated += 1

    return updated


def step_create_indexes(dry_run: bool):
    """Maak alle ontbrekende performance-indexes aan."""
    print("\n[4/6] Indexes aanmaken...")

    # Bestaande indexes ophalen
    existing = {r["name"] for r in q("SELECT name FROM sqlite_master WHERE type='index'")}
    print(f"  Bestaand: {', '.join(sorted(existing)) or '(geen)'}")

    indexes = [
        # items-tabel — nu volledig leeg qua indexes
        ("idx_items_format_id",
         "CREATE INDEX IF NOT EXISTS idx_items_format_id ON items(format_id)"),
        ("idx_items_format_status",
         "CREATE INDEX IF NOT EXISTS idx_items_format_status ON items(format_id, transcript_status)"),
        ("idx_items_source_url",
         "CREATE INDEX IF NOT EXISTS idx_items_source_url ON items(source_url)"),

        # fragments-tabel — idx_fragments_item_id bestaat al, rest toevoegen
        ("idx_fragments_item_id",
         "CREATE INDEX IF NOT EXISTS idx_fragments_item_id ON fragments(item_id)"),

        # formats-tabel — UNIQUE index voorkomt toekomstige duplicaten
        ("idx_formats_id_unique",
         "CREATE UNIQUE INDEX IF NOT EXISTS idx_formats_id_unique ON formats(id)"),
        ("idx_formats_identifier",
         "CREATE INDEX IF NOT EXISTS idx_formats_identifier ON formats(identifier)"),
        ("idx_formats_source_url",
         "CREATE INDEX IF NOT EXISTS idx_formats_source_url ON formats(source_url)"),
    ]

    for name, sql in indexes:
        if name in existing and "UNIQUE" not in sql:
            print(f"  ⏭  {name} (bestaat al)")
            continue
        status = "[dry-run]" if dry_run else ""
        print(f"  ⏳ {name}... {status}", flush=True)
        if not dry_run:
            t0 = time.time()
            try:
                result = ddl(sql, timeout=120.0)
                dur = round(result.get("query_duration_ms", 0))
                print(f"  ✅ {name} ({time.time()-t0:.0f}s, {dur}ms query)")
            except Exception as e:
                print(f"  ❌ {name}: {e}")


def step_verify(id_mapping: dict):
    """Toon eindstatus van de database."""
    print("\n[5/6] Verificatie...")

    # Formats
    formats = q("SELECT id, identifier, title FROM formats ORDER BY title")
    print(f"\n  Formats ({len(formats)}):")
    for f in formats:
        uuid_ok = len(f["id"]) == 36 and f["id"].count("-") == 4
        id_icon = "✅" if uuid_ok else "⚠️ "
        id_icon2 = "✅" if f["identifier"] else "❌"
        print(f"    {id_icon} id={f['id'][:38]!s:<40} {id_icon2} identifier={f['identifier']!r:<30} {f['title']}")

    # Items per format
    print(f"\n  Items per format (transcript_status):")
    items_stats = q("""
        SELECT format_id, transcript_status, COUNT(*) as n
        FROM items
        GROUP BY format_id, transcript_status
        ORDER BY format_id
    """)
    by_format = {}
    for r in items_stats:
        fid = r["format_id"]
        by_format.setdefault(fid, {})[r["transcript_status"]] = int(r["n"])

    for fid, stats in by_format.items():
        # Zoek titel
        fmt = next((f for f in formats if f["id"] == fid), None)
        title = fmt["title"] if fmt else (fid[:30] if fid else "(NULL format_id)")
        total = sum(stats.values())
        completed = stats.get("completed", 0)
        pct = round(completed / max(total, 1) * 100)
        print(f"    {title[:35]:<35} {completed:>3}/{total} ({pct}%)")

    # Indexes
    print(f"\n  Indexes:")
    idxs = q("SELECT name, tbl_name FROM sqlite_master WHERE type='index' ORDER BY tbl_name, name")
    for idx in idxs:
        print(f"    [{idx['tbl_name']}] {idx['name']}")

    # Duplicaten check
    dups = q("SELECT id, COUNT(*) as n FROM formats GROUP BY id HAVING COUNT(*) > 1")
    if dups:
        print(f"\n  ⚠️  Nog {len(dups)} duplicate format-IDs!")
    else:
        print(f"\n  ✅ Geen duplicate format-IDs")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    global _URL, _TOKEN

    ap = argparse.ArgumentParser(description="DB-sanering + performance-fixes voor stemmy Turso")
    ap.add_argument("--dry-run", action="store_true",
                    help="Toon plan zonder wijzigingen door te voeren")
    ap.add_argument("--skip-ids", action="store_true",
                    help="Sla ID-migratie over (alleen indexes + identifiers)")
    ap.add_argument("--verify-only", action="store_true",
                    help="Alleen verificatie tonen, geen wijzigingen")
    args = ap.parse_args()

    _URL, _TOKEN = _setup()

    if args.dry_run:
        print("🔍 DRY-RUN modus — geen wijzigingen")
    if args.verify_only:
        step_verify({})
        return

    print(f"🔧 Stemmy DB migratie")
    print(f"   {_URL}")
    print(f"{'='*55}")

    # Stap 1: Duplicaten verwijderen (vóór UNIQUE index aanmaken)
    n_deleted = step_remove_duplicates(args.dry_run)

    # Stap 2: items.format_id index eerst — zodat de slug-ID UPDATE snel is
    print("\n[2/6] items.format_id index aanmaken (voor snelle UPDATE)...")
    if not args.dry_run:
        try:
            t0 = time.time()
            ddl("CREATE INDEX IF NOT EXISTS idx_items_format_id ON items(format_id)", timeout=120.0)
            print(f"  ✅ idx_items_format_id ({time.time()-t0:.0f}s)")
        except Exception as e:
            print(f"  ⚠️  {e} (doorgaan)")
    else:
        print("  [dry-run] zou idx_items_format_id aanmaken")

    # Stap 3: Slug-IDs → UUIDs (nu met index = snel)
    id_mapping = {}
    if not args.skip_ids:
        id_mapping = step_fix_slug_ids(args.dry_run)
    else:
        print("\n[3/6] Slug-ID migratie overgeslagen (--skip-ids)")

    # Stap 4: Identifiers invullen
    n_updated = step_fix_identifiers(args.dry_run, id_mapping)

    # Stap 5: Overige indexes aanmaken
    step_create_indexes(args.dry_run)

    # Stap 5: Verify
    if not args.dry_run:
        step_verify(id_mapping)

    print(f"\n{'='*55}")
    if args.dry_run:
        print("✅ Dry-run klaar. Voer zonder --dry-run uit om toe te passen.")
    else:
        print(f"✅ Migratie klaar:")
        print(f"   - {n_deleted} duplicaten verwijderd")
        print(f"   - {len(id_mapping)} slug-IDs → UUIDs geconverteerd")
        print(f"   - {n_updated} identifiers ingevuld")


if __name__ == "__main__":
    main()
