"""
benchmark/prepare.py — VAST benchmark fixture builder.

Bouw een reproduceerbare testset uit de live database.
Run dit script eenmalig; de output wordt gecommit naar git.

Gebruik:
    python benchmark/prepare.py --db data/stemmy-replica.db
    python benchmark/prepare.py --db data/stemmy-replica.db --holdout-only
    python benchmark/prepare.py --verify  # Controleer integriteit van bestaande fixtures
"""

import argparse
import hashlib
import json
import random
import sqlite3
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

FIXTURE_DIR = Path(__file__).parent / "results" / "fixtures"
DATASET_PATH = FIXTURE_DIR / "dataset.json"
HOLDOUT_PATH = FIXTURE_DIR / "holdout.json"
MANIFEST_PATH = FIXTURE_DIR / "manifest.json"

# Fixture grootte
N_DENSE = 20      # Korte segmenten dicht bij elkaar (test grouping)
N_SPARSE = 20     # Segmenten ver van elkaar (test single-group path)
N_CROSS = 20      # 1 fragment uit elk van 20 afleveringen (test URL-parallellisme)
N_HOLDOUT = 20    # Cross-validatie set uit andere podcasts

HASH_RANGE_BYTES = 512 * 1024  # 512KB voor integriteitscheck


@dataclass
class SegmentRecord:
    id: str
    item_id: str
    text: str
    start_time: float   # seconden
    end_time: float     # seconden
    audio_url: str
    tier: str           # "dense" | "sparse" | "cross" | "holdout"
    format_id: str = ""
    duration: float = 0.0

    def __post_init__(self):
        self.duration = round(self.end_time - self.start_time, 3)


def _rows_to_dicts(cursor) -> List[Dict]:
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def _normalize_time(val) -> float:
    """Converteer milliseconden naar seconden indien nodig."""
    if val is None:
        return 0.0
    v = float(val)
    return v / 1000.0 if v > 10000 else v


class FixtureBuilder:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        if not db_path.exists():
            raise FileNotFoundError(f"Database niet gevonden: {db_path}")

    def build(self, seed: int = 42) -> Tuple[List[SegmentRecord], List[SegmentRecord]]:
        """Bouw primaire fixture (60) + holdout (20)."""
        random.seed(seed)
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            dense = self._sample_dense(conn)
            sparse = self._sample_sparse(conn, exclude_items={s.item_id for s in dense})
            cross = self._sample_cross(conn, exclude_items={s.item_id for s in dense + sparse})
            primary = dense + sparse + cross

            primary_format_ids = {s.format_id for s in primary}
            holdout = self._sample_holdout(conn, exclude_format_ids=primary_format_ids)

        return primary, holdout

    def _sample_dense(self, conn) -> List[SegmentRecord]:
        """
        20 korte segmenten (2-5s) dicht bij elkaar in dezelfde aflevering.
        Zoek afleveringen met veel korte fragmenten en selecteer clusters.
        """
        cursor = conn.execute("""
            SELECT f.id, f.item_id, f.text,
                   f.start_time_seconds, f.end_time_seconds,
                   i.audio_url, i.format_id
            FROM fragments f
            JOIN items i ON f.item_id = i.id
            WHERE i.audio_url IS NOT NULL
              AND i.audio_url != ''
              AND f.start_time_seconds IS NOT NULL
              AND f.end_time_seconds IS NOT NULL
              AND (f.end_time_seconds - f.start_time_seconds) BETWEEN 2.0 AND 5.0
              AND f.start_time_seconds > 10.0
            ORDER BY i.audio_url, f.start_time_seconds
            LIMIT 500
        """)
        rows = _rows_to_dicts(cursor)
        if not rows:
            print("WAARSCHUWING: Geen dense-kandidaten gevonden, probeer brede selectie")
            return self._sample_fallback(conn, "dense", N_DENSE)

        # Groepeer per audio_url, zoek clusters van ≥3 dichtbij segmenten
        from itertools import groupby
        clusters = []
        for url, group in groupby(rows, key=lambda r: r["audio_url"]):
            segs = list(group)
            cluster = [segs[0]]
            for seg in segs[1:]:
                gap = _normalize_time(seg["start_time_seconds"]) - _normalize_time(cluster[-1]["end_time_seconds"])
                if gap < 15.0 and len(cluster) < 8:
                    cluster.append(seg)
                else:
                    if len(cluster) >= 2:
                        clusters.append(cluster)
                    cluster = [seg]
            if len(cluster) >= 2:
                clusters.append(cluster)

        # Selecteer willekeurig N_DENSE segmenten uit clusters
        selected = []
        random.shuffle(clusters)
        for cluster in clusters:
            needed = N_DENSE - len(selected)
            if needed <= 0:
                break
            take = min(len(cluster), needed, 4)
            selected.extend(random.sample(cluster, take))

        # Aanvullen indien te weinig
        while len(selected) < N_DENSE and rows:
            candidate = random.choice(rows)
            if not any(s["id"] == candidate["id"] for s in selected):
                selected.append(candidate)

        return [self._row_to_record(r, "dense") for r in selected[:N_DENSE]]

    def _sample_sparse(self, conn, exclude_items: set) -> List[SegmentRecord]:
        """
        20 segmenten ver van elkaar (>60s apart) in dezelfde URL.
        Test het single-group-per-segment pad.
        """
        placeholders = ",".join("?" * len(exclude_items)) if exclude_items else "'__none__'"
        params = list(exclude_items) if exclude_items else []

        cursor = conn.execute(f"""
            SELECT f.id, f.item_id, f.text,
                   f.start_time_seconds, f.end_time_seconds,
                   i.audio_url, i.format_id
            FROM fragments f
            JOIN items i ON f.item_id = i.id
            WHERE i.audio_url IS NOT NULL
              AND i.audio_url != ''
              AND f.start_time_seconds IS NOT NULL
              AND f.end_time_seconds IS NOT NULL
              AND (f.end_time_seconds - f.start_time_seconds) BETWEEN 3.0 AND 15.0
              AND f.start_time_seconds > 60.0
              {'AND f.item_id NOT IN (' + placeholders + ')' if exclude_items else ''}
            ORDER BY RANDOM()
            LIMIT 300
        """, params)
        rows = _rows_to_dicts(cursor)

        if len(rows) < N_SPARSE:
            return self._sample_fallback(conn, "sparse", N_SPARSE, exclude_items)

        # Selecteer zodat segmenten per URL >60s van elkaar liggen
        selected = []
        last_end_per_url: Dict[str, float] = {}
        random.shuffle(rows)
        for row in rows:
            if len(selected) >= N_SPARSE:
                break
            url = row["audio_url"]
            start = _normalize_time(row["start_time_seconds"])
            last = last_end_per_url.get(url, -999)
            if start - last > 60.0:
                selected.append(row)
                last_end_per_url[url] = _normalize_time(row["end_time_seconds"])

        return [self._row_to_record(r, "sparse") for r in selected[:N_SPARSE]]

    def _sample_cross(self, conn, exclude_items: set) -> List[SegmentRecord]:
        """
        20 segmenten uit 20 verschillende afleveringen.
        Test URL-parallellisme in ThreadPoolExecutor.
        """
        placeholders = ",".join("?" * len(exclude_items)) if exclude_items else "'__none__'"
        params = list(exclude_items) if exclude_items else []

        cursor = conn.execute(f"""
            SELECT f.id, f.item_id, f.text,
                   f.start_time_seconds, f.end_time_seconds,
                   i.audio_url, i.format_id
            FROM fragments f
            JOIN items i ON f.item_id = i.id
            WHERE i.audio_url IS NOT NULL
              AND i.audio_url != ''
              AND f.start_time_seconds IS NOT NULL
              AND f.end_time_seconds IS NOT NULL
              AND (f.end_time_seconds - f.start_time_seconds) BETWEEN 2.0 AND 20.0
              {'AND f.item_id NOT IN (' + placeholders + ')' if exclude_items else ''}
            GROUP BY f.item_id
            ORDER BY RANDOM()
            LIMIT {N_CROSS * 3}
        """, params)
        rows = _rows_to_dicts(cursor)

        # Één per item_id
        seen_items = set()
        selected = []
        for row in rows:
            if row["item_id"] not in seen_items:
                selected.append(row)
                seen_items.add(row["item_id"])
            if len(selected) >= N_CROSS:
                break

        return [self._row_to_record(r, "cross") for r in selected[:N_CROSS]]

    def _sample_holdout(self, conn, exclude_format_ids: set) -> List[SegmentRecord]:
        """
        20 fragmenten uit ANDERE podcasts (format_ids) voor cross-validatie.
        """
        placeholders = ",".join("?" * len(exclude_format_ids)) if exclude_format_ids else "'__none__'"
        params = list(exclude_format_ids) if exclude_format_ids else []

        cursor = conn.execute(f"""
            SELECT f.id, f.item_id, f.text,
                   f.start_time_seconds, f.end_time_seconds,
                   i.audio_url, i.format_id
            FROM fragments f
            JOIN items i ON f.item_id = i.id
            WHERE i.audio_url IS NOT NULL
              AND i.audio_url != ''
              AND f.start_time_seconds IS NOT NULL
              AND f.end_time_seconds IS NOT NULL
              AND (f.end_time_seconds - f.start_time_seconds) BETWEEN 2.0 AND 15.0
              {'AND i.format_id NOT IN (' + placeholders + ')' if exclude_format_ids else ''}
            ORDER BY RANDOM()
            LIMIT {N_HOLDOUT * 3}
        """, params)
        rows = _rows_to_dicts(cursor)
        return [self._row_to_record(r, "holdout") for r in rows[:N_HOLDOUT]]

    def _sample_fallback(self, conn, tier: str, n: int, exclude_items: set = None) -> List[SegmentRecord]:
        """Brede fallback-selectie als specifieke criteria te weinig resultaten geven."""
        exclude_items = exclude_items or set()
        placeholders = ",".join("?" * len(exclude_items)) if exclude_items else "'__none__'"
        params = list(exclude_items) if exclude_items else []
        cursor = conn.execute(f"""
            SELECT f.id, f.item_id, f.text,
                   f.start_time_seconds, f.end_time_seconds,
                   i.audio_url, i.format_id
            FROM fragments f
            JOIN items i ON f.item_id = i.id
            WHERE i.audio_url IS NOT NULL
              AND i.audio_url != ''
              AND f.start_time_seconds IS NOT NULL
              AND f.end_time_seconds IS NOT NULL
              {'AND f.item_id NOT IN (' + placeholders + ')' if exclude_items else ''}
            ORDER BY RANDOM()
            LIMIT {n * 2}
        """, params)
        rows = _rows_to_dicts(cursor)
        return [self._row_to_record(r, tier) for r in rows[:n]]

    def _row_to_record(self, row: dict, tier: str) -> SegmentRecord:
        return SegmentRecord(
            id=str(row["id"]),
            item_id=str(row["item_id"]),
            text=str(row.get("text", "") or ""),
            start_time=_normalize_time(row["start_time_seconds"]),
            end_time=_normalize_time(row["end_time_seconds"]),
            audio_url=str(row["audio_url"]),
            tier=tier,
            format_id=str(row.get("format_id", "") or ""),
        )


def hash_audio_range(url: str, byte_count: int = HASH_RANGE_BYTES) -> Optional[str]:
    """Download eerste byte_count bytes van een audio-URL en geef SHA-256 hash."""
    try:
        headers = {"Range": f"bytes=0-{byte_count - 1}"}
        resp = requests.get(url, headers=headers, timeout=15, stream=True)
        if resp.status_code not in (200, 206):
            return None
        data = b""
        for chunk in resp.iter_content(chunk_size=65536):
            data += chunk
            if len(data) >= byte_count:
                break
        return hashlib.sha256(data).hexdigest()
    except Exception as e:
        print(f"  Hash-fout voor {url[:60]}...: {e}")
        return None


def save_fixtures(primary: List[SegmentRecord], holdout: List[SegmentRecord]) -> None:
    """Sla fixtures op naar JSON-bestanden."""
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)

    primary_dicts = [asdict(s) for s in primary]
    holdout_dicts = [asdict(s) for s in holdout]

    with open(DATASET_PATH, "w") as f:
        json.dump(primary_dicts, f, indent=2)
    print(f"Primaire fixture opgeslagen: {DATASET_PATH} ({len(primary)} segmenten)")

    with open(HOLDOUT_PATH, "w") as f:
        json.dump(holdout_dicts, f, indent=2)
    print(f"Holdout fixture opgeslagen: {HOLDOUT_PATH} ({len(holdout)} segmenten)")


def build_manifest(primary: List[SegmentRecord], holdout: List[SegmentRecord]) -> None:
    """Bereken en sla SHA-256 hashes op voor integriteitscontrole."""
    all_urls = list({s.audio_url for s in primary + holdout})
    print(f"\nIntegriteitscheck: hashing {len(all_urls)} unieke audio-URLs...")
    hashes = {}
    for i, url in enumerate(all_urls, 1):
        print(f"  [{i}/{len(all_urls)}] {url[:70]}...")
        h = hash_audio_range(url)
        hashes[url] = h
        if h:
            print(f"    ✓ {h[:16]}...")
        else:
            print(f"    ✗ kon niet ophalen")

    manifest = {
        "url_hashes": hashes,
        "primary_count": len(primary),
        "holdout_count": len(holdout),
        "tiers": {
            "dense": sum(1 for s in primary if s.tier == "dense"),
            "sparse": sum(1 for s in primary if s.tier == "sparse"),
            "cross": sum(1 for s in primary if s.tier == "cross"),
        }
    }
    with open(MANIFEST_PATH, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"\nManifest opgeslagen: {MANIFEST_PATH}")


def load_fixture(path: Path = DATASET_PATH) -> List[Dict]:
    """Laad een opgeslagen fixture-dataset."""
    if not path.exists():
        raise FileNotFoundError(
            f"Fixture niet gevonden: {path}\n"
            f"Draai eerst: python benchmark/prepare.py --db <pad-naar-db>"
        )
    with open(path) as f:
        return json.load(f)


def verify_fixture_integrity() -> bool:
    """Controleer of audio-URL-inhoud nog overeenkomt met opgeslagen hashes."""
    if not MANIFEST_PATH.exists():
        print("Geen manifest gevonden. Draai prepare.py eerst.")
        return False

    with open(MANIFEST_PATH) as f:
        manifest = json.load(f)

    hashes = manifest.get("url_hashes", {})
    mismatches = 0
    for url, expected_hash in hashes.items():
        if expected_hash is None:
            continue
        actual = hash_audio_range(url)
        if actual != expected_hash:
            print(f"MISMATCH: {url[:60]}...")
            mismatches += 1

    if mismatches == 0:
        print(f"✓ Alle {len(hashes)} audio-URLs ongewijzigd.")
        return True
    else:
        print(f"✗ {mismatches}/{len(hashes)} URLs gewijzigd — fixtures opnieuw bouwen!")
        return False


def print_summary(primary: List[SegmentRecord], holdout: List[SegmentRecord]) -> None:
    urls_primary = {s.audio_url for s in primary}
    urls_holdout = {s.audio_url for s in holdout}
    format_ids = {s.format_id for s in primary}

    print("\n" + "=" * 60)
    print("FIXTURE SAMENVATTING")
    print("=" * 60)
    print(f"Primaire set:    {len(primary)} segmenten")
    print(f"  Dense:         {sum(1 for s in primary if s.tier == 'dense')}")
    print(f"  Sparse:        {sum(1 for s in primary if s.tier == 'sparse')}")
    print(f"  Cross-episode: {sum(1 for s in primary if s.tier == 'cross')}")
    print(f"  Unieke URLs:   {len(urls_primary)}")
    print(f"  Unieke formats:{len(format_ids)}")
    durations = [s.duration for s in primary]
    if durations:
        print(f"  Gem. duur:     {sum(durations)/len(durations):.1f}s")
        print(f"  Min/Max duur:  {min(durations):.1f}s / {max(durations):.1f}s")
    print(f"\nHoldout set:     {len(holdout)} segmenten")
    print(f"  Unieke URLs:   {len(urls_holdout)}")
    overlapping = urls_primary & urls_holdout
    if overlapping:
        print(f"  WAARSCHUWING: {len(overlapping)} overlappende URLs met primaire set!")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Bouw benchmark fixture dataset")
    parser.add_argument("--db", type=Path, help="Pad naar SQLite database")
    parser.add_argument("--seed", type=int, default=42, help="Random seed (default: 42)")
    parser.add_argument("--no-manifest", action="store_true", help="Sla manifest-hashing over")
    parser.add_argument("--verify", action="store_true", help="Controleer bestaande fixture integriteit")
    parser.add_argument("--holdout-only", action="store_true", help="Bouw alleen holdout set opnieuw")
    args = parser.parse_args()

    if args.verify:
        ok = verify_fixture_integrity()
        sys.exit(0 if ok else 1)

    if not args.db:
        # Probeer standaard paden
        for candidate in [
            Path("data/stemmy-replica.db"),
            Path("data/stemmy.db"),
            Path("stemmy.db"),
        ]:
            if candidate.exists():
                args.db = candidate
                break
        if not args.db:
            print("Geef --db <pad> op of zorg dat data/stemmy-replica.db bestaat.")
            sys.exit(1)

    print(f"Database: {args.db}")
    print(f"Random seed: {args.seed}")
    print("Segmenten samplen...")

    builder = FixtureBuilder(args.db)
    primary, holdout = builder.build(seed=args.seed)

    print_summary(primary, holdout)
    save_fixtures(primary, holdout)

    if not args.no_manifest:
        build_manifest(primary, holdout)
    else:
        print("Manifest-hashing overgeslagen (--no-manifest)")

    print("\n✓ Fixture klaar. Commit nu de results/fixtures/ map naar git.")


if __name__ == "__main__":
    main()
