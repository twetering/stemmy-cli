#!/usr/bin/env python3
"""
create_indexes.py — Eenmalige setup: maak alle Turso-indexes aan.

Voer dit uit NA een grote batch-import.
Zonder indexes: fragment-queries ≥30s timeout op >50k rijen.
Met indexes: queries <1s.

Indexes die worden aangemaakt:
  - idx_fragments_item_id         (fragments WHERE item_id = ?)
  - idx_items_format_status       (items WHERE format_id = ? AND status = ?)
  - idx_items_source_url          (items WHERE source_url = ?)  ← dedupe check

Vereist: TURSO_URL + TURSO_TOKEN in env of .env-bestand.
Duurt: 30-60s per index op grote tabel — gebruikt directe HTTP (geen Python adapter).
"""

import os
import sys
import time
from pathlib import Path

import httpx

PIPELINE_DIR = Path(__file__).parent


def setup_env():
    env_file = PIPELINE_DIR.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


def create_index(url: str, token: str, name: str, ddl: str, timeout: float = 120.0):
    print(f"  Aanmaken: {name}...", flush=True)
    t0 = time.time()
    resp = httpx.post(
        url.replace("libsql://", "https://") + "/v2/pipeline",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"requests": [
            {"type": "execute", "stmt": {"sql": ddl}},
            {"type": "close"},
        ]},
        timeout=timeout,
    )
    resp.raise_for_status()
    result = resp.json()["results"][0]
    elapsed = round(time.time() - t0, 1)

    if result.get("type") == "error":
        raise RuntimeError(f"SQL fout: {result['error']}")

    rs = result.get("response", {}).get("result", {})
    rows_read = rs.get("rows_read", "?")
    duration_ms = round(rs.get("query_duration_ms", 0))
    print(f"  ✅ {name} ({elapsed}s, {rows_read} rows read, {duration_ms}ms query)", flush=True)


def main():
    setup_env()

    url = os.environ.get("TURSO_URL") or os.environ.get("TURSO_DB_URL")
    token = os.environ.get("TURSO_TOKEN") or os.environ.get("TURSO_API_KEY")

    if not url or not token:
        print("❌ TURSO_URL en TURSO_TOKEN zijn verplicht", file=sys.stderr)
        sys.exit(1)

    print(f"🔧 Indexes aanmaken op {url}")
    print(f"   (kan 30-120s duren per index — wacht rustig af)\n")

    indexes = [
        (
            "idx_fragments_item_id",
            "CREATE INDEX IF NOT EXISTS idx_fragments_item_id ON fragments(item_id)",
        ),
        (
            "idx_items_format_status",
            "CREATE INDEX IF NOT EXISTS idx_items_format_status ON items(format_id, transcript_status)",
        ),
        (
            "idx_items_source_url",
            "CREATE INDEX IF NOT EXISTS idx_items_source_url ON items(source_url)",
        ),
    ]

    ok = 0
    for name, ddl in indexes:
        try:
            create_index(url, token, name, ddl)
            ok += 1
        except Exception as e:
            print(f"  ❌ {name}: {e}")

    print(f"\n{'='*50}")
    print(f"✅ {ok}/{len(indexes)} indexes aangemaakt")
    print(f"\nFragment-queries zijn nu veel sneller.")
    print(f"Test: python3 pipeline/verify_db.py --list-formats")


if __name__ == "__main__":
    main()
