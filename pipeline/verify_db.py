#!/usr/bin/env python3
"""
verify_db.py — Verificeer en benchmark transcriptiedata in Turso.

Controleert:
  1. Items per transcript_status (completed/pending/failed)
  2. Fragment-tellingen per item
  3. Aanwezigheid van word-level timestamps (words-veld niet leeg)
  4. Audio-URL bereikbaarheid (sample N items)
  5. Tijdstempel-kwaliteit (start/end monotoon, geen gaten >60s)
  6. Gemiddeld fragmenten-per-aflevering vs. verwachting

Gebruik:
    python3 pipeline/verify_db.py --format-id uuid
    python3 pipeline/verify_db.py --format-id uuid --sample 10
    python3 pipeline/verify_db.py --format-id uuid --check-audio   # extra: test HTTP HEAD
    python3 pipeline/verify_db.py --list-formats                    # toon alle formats

Output: samenvatting in terminal + optioneel verify_report.json.
"""

import argparse
import json
import os
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

PIPELINE_DIR = Path(__file__).parent


# ── Turso helpers ─────────────────────────────────────────────────────────────

def _turso_setup() -> tuple[str, str]:
    url = os.environ.get("TURSO_URL") or os.environ.get("TURSO_DB_URL")
    token = os.environ.get("TURSO_TOKEN") or os.environ.get("TURSO_API_KEY")
    if not url or not token:
        env_file = PIPELINE_DIR.parent / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if "=" in line and not line.startswith("#"):
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())
        url = os.environ.get("TURSO_URL") or os.environ.get("TURSO_DB_URL")
        token = os.environ.get("TURSO_TOKEN") or os.environ.get("TURSO_API_KEY")
    if not url:
        raise ValueError("TURSO_URL / TURSO_DB_URL niet gezet")
    if not token:
        raise ValueError("TURSO_TOKEN / TURSO_API_KEY niet gezet")
    return url.replace("libsql://", "https://"), token


def tq(sql: str, params: list = None, timeout: float = 20.0) -> list:
    """Turso query — retourneert rows als list[dict]."""
    url, token = _turso_setup()
    stmt = {"sql": sql}
    if params:
        stmt["args"] = [
            {"type": "null"} if v is None else {"type": "text", "value": str(v)}
            for v in params
        ]
    resp = httpx.post(
        url + "/v2/pipeline",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"requests": [{"type": "execute", "stmt": stmt}, {"type": "close"}]},
        timeout=timeout,
    )
    resp.raise_for_status()
    result = resp.json()["results"][0]
    if result.get("type") == "error":
        raise RuntimeError(f"SQL fout: {result['error']}")
    rs = result.get("response", {}).get("result", {})
    cols = [c["name"] for c in rs.get("cols", [])]
    return [
        {c: (cell["value"] if cell["type"] != "null" else None)
         for c, cell in zip(cols, row)}
        for row in rs.get("rows", [])
    ]


# ── Checks ────────────────────────────────────────────────────────────────────

def check_item_status(format_id: str) -> dict:
    rows = tq(
        "SELECT transcript_status, COUNT(*) as n FROM items WHERE format_id = ? GROUP BY transcript_status",
        [format_id]
    )
    stats = {r["transcript_status"]: int(r["n"]) for r in rows}
    total = sum(stats.values())
    completed = stats.get("completed", 0)
    return {
        "total": total,
        "completed": completed,
        "pending": stats.get("pending", 0),
        "failed": stats.get("failed", 0),
        "completion_pct": round(completed / max(total, 1) * 100, 1),
        "ok": completed == total and total > 0,
    }


def check_fragment_counts(format_id: str) -> dict:
    """
    Haal fragment-tellingen per item op.
    Opmerking: items zonder index zijn langzaam — gebruik index.
    """
    # Haal alle completed items op
    items = tq(
        "SELECT id, title FROM items WHERE format_id = ? AND transcript_status = 'completed'",
        [format_id]
    )
    if not items:
        return {"ok": False, "error": "Geen completed items"}

    # Sample 10 items voor fragment-check (niet alle — Turso is remote)
    sample = random.sample(items, min(10, len(items)))

    counts = []
    items_no_frags = []
    for item in sample:
        rows = tq(
            "SELECT COUNT(*) as n FROM fragments WHERE item_id = ?",
            [item["id"]]
        )
        n = int(rows[0]["n"]) if rows else 0
        counts.append(n)
        if n == 0:
            items_no_frags.append(item["title"])

    avg = round(sum(counts) / len(counts), 1) if counts else 0
    min_count = min(counts) if counts else 0
    max_count = max(counts) if counts else 0

    return {
        "sampled": len(sample),
        "avg_fragments": avg,
        "min_fragments": min_count,
        "max_fragments": max_count,
        "items_no_fragments": items_no_frags,
        "ok": avg > 50 and not items_no_frags,  # >50 fragmenten/afl = redelijk
    }


def check_word_timestamps(format_id: str, sample_n: int = 5) -> dict:
    """
    Controleer of word-level timestamps aanwezig zijn.
    Samples N items, kijkt naar het words-veld.
    """
    items = tq(
        "SELECT id FROM items WHERE format_id = ? AND transcript_status = 'completed' LIMIT 20",
        [format_id]
    )
    if not items:
        return {"ok": False, "error": "Geen items"}

    sample = random.sample(items, min(sample_n, len(items)))
    results = []

    for item in sample:
        rows = tq(
            "SELECT words FROM fragments WHERE item_id = ? AND words IS NOT NULL AND words != '[]' LIMIT 3",
            [item["id"]]
        )
        has_words = len(rows) > 0
        word_count = 0
        if has_words:
            try:
                words = json.loads(rows[0]["words"])
                word_count = len(words)
                # Controleer structuur: elk word heeft start/end/text
                if words:
                    w = words[0]
                    valid = all(k in w for k in ("text", "start", "end"))
                    results.append({
                        "item_id": item["id"],
                        "has_words": True,
                        "word_count_sample": word_count,
                        "structure_ok": valid,
                        "sample_word": w,
                    })
            except Exception:
                results.append({"item_id": item["id"], "has_words": False, "parse_error": True})
        else:
            results.append({"item_id": item["id"], "has_words": False})

    ok = all(r.get("has_words") and r.get("structure_ok", True) for r in results)
    return {"sampled": len(results), "results": results, "ok": ok}


def check_timestamp_quality(format_id: str) -> dict:
    """
    Controleer tijdstempel-kwaliteit in een sample:
    - start < end voor elk fragment
    - geen grote gaten (>60s) tussen opeenvolgende fragmenten
    """
    items = tq(
        "SELECT id, title FROM items WHERE format_id = ? AND transcript_status = 'completed' LIMIT 10",
        [format_id]
    )
    if not items:
        return {"ok": False}

    item = random.choice(items)
    frags = tq(
        "SELECT start_time_seconds, end_time_seconds FROM fragments WHERE item_id = ? ORDER BY CAST(start_time_seconds AS REAL)",
        [item["id"]]
    )

    issues = []
    prev_end = None
    for f in frags:
        try:
            start = float(f["start_time_seconds"])
            end = float(f["end_time_seconds"])
        except (TypeError, ValueError):
            issues.append(f"Niet-numeriek tijdstempel: {f}")
            continue

        if start >= end:
            issues.append(f"start >= end: {start} >= {end}")
        if prev_end is not None and start - prev_end > 60:
            issues.append(f"Groot gat: {prev_end:.1f}s → {start:.1f}s ({start-prev_end:.0f}s)")
        prev_end = end

    return {
        "item_id": item["id"],
        "item_title": item.get("title", ""),
        "fragment_count": len(frags),
        "issues": issues[:10],
        "ok": len(issues) == 0,
    }


def check_audio_urls(format_id: str, sample_n: int = 5) -> dict:
    """
    Controleer of audio-URLs bereikbaar zijn (HTTP HEAD request).
    Samples N items.
    """
    items = tq(
        "SELECT id, title, audio_url FROM items WHERE format_id = ? AND audio_url IS NOT NULL LIMIT 20",
        [format_id]
    )
    if not items:
        return {"ok": False, "error": "Geen items met audio_url"}

    sample = random.sample(items, min(sample_n, len(items)))
    results = []

    for item in sample:
        url = item["audio_url"]
        try:
            resp = httpx.head(url, timeout=10.0, follow_redirects=True)
            content_length = resp.headers.get("content-length", "?")
            ok = resp.status_code == 200
            results.append({
                "title": item["title"][:50],
                "status": resp.status_code,
                "size_bytes": content_length,
                "ok": ok,
            })
        except Exception as e:
            results.append({"title": item["title"][:50], "error": str(e), "ok": False})

    all_ok = all(r["ok"] for r in results)
    return {"sampled": len(results), "results": results, "ok": all_ok}


def list_formats() -> list:
    rows = tq(
        "SELECT f.id, f.title, COUNT(*) as items "
        "FROM formats f JOIN items i ON i.format_id = f.id "
        "GROUP BY f.id ORDER BY items DESC"
    )
    return rows


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Verificeer transcriptiedata in Turso")
    ap.add_argument("--format-id", help="Format UUID om te verifiëren")
    ap.add_argument("--sample", type=int, default=5,
                    help="Aantal items voor steekproef (default: 5)")
    ap.add_argument("--check-audio", action="store_true",
                    help="Test ook audio-URL bereikbaarheid via HTTP HEAD")
    ap.add_argument("--list-formats", action="store_true",
                    help="Toon alle formats in Turso")
    ap.add_argument("--output", default=None,
                    help="Sla rapport op als JSON (bijv. verify_report.json)")
    args = ap.parse_args()

    if args.list_formats:
        formats = list_formats()
        print(f"\n{'='*60}")
        print(f"{'Format ID':<38} {'Title':<25} Items")
        print(f"{'─'*60}")
        for f in formats:
            print(f"{f['id']:<38} {str(f['title'])[:25]:<25} {f['items']}")
        return

    if not args.format_id:
        ap.error("--format-id is verplicht (of gebruik --list-formats)")

    format_id = args.format_id
    print(f"\n🔍 Verificatie: {format_id}")
    print(f"{'='*55}")

    report = {
        "format_id": format_id,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "checks": {},
    }

    # 1. Item status
    print("\n📋 Item-status check...")
    status = check_item_status(format_id)
    report["checks"]["item_status"] = status
    emoji = "✅" if status["ok"] else "⚠️"
    print(f"  {emoji} {status['completed']}/{status['total']} completed "
          f"({status['completion_pct']}%) | pending: {status['pending']} | failed: {status['failed']}")

    # 2. Fragment-tellingen (sample)
    print("\n📊 Fragment-tellingen (sample)...")
    frags = check_fragment_counts(format_id)
    report["checks"]["fragment_counts"] = frags
    if "error" in frags:
        print(f"  ⚠️  {frags['error']}")
    else:
        emoji = "✅" if frags["ok"] else "⚠️"
        print(f"  {emoji} Gem. {frags['avg_fragments']} fragmenten/afl "
              f"(min: {frags['min_fragments']}, max: {frags['max_fragments']})")
        if frags["items_no_fragments"]:
            print(f"  ❌ Items ZONDER fragmenten:")
            for t in frags["items_no_fragments"][:5]:
                print(f"     - {t}")

    # 3. Word-level timestamps
    print(f"\n🔤 Word-level timestamps (sample {args.sample})...")
    words = check_word_timestamps(format_id, args.sample)
    report["checks"]["word_timestamps"] = words
    if "error" in words:
        print(f"  ⚠️  {words['error']}")
    else:
        ok_count = sum(1 for r in words["results"] if r.get("has_words"))
        emoji = "✅" if words["ok"] else "⚠️"
        print(f"  {emoji} {ok_count}/{words['sampled']} items hebben word-timestamps")
        for r in words["results"]:
            if r.get("sample_word"):
                w = r["sample_word"]
                print(f"     Sample: '{w.get('text')}' @ {w.get('start')}ms-{w.get('end')}ms "
                      f"(conf: {w.get('confidence', '?')})")
                break

    # 4. Tijdstempel-kwaliteit
    print("\n⏱  Tijdstempel-kwaliteit check...")
    ts = check_timestamp_quality(format_id)
    report["checks"]["timestamp_quality"] = ts
    emoji = "✅" if ts["ok"] else "⚠️"
    print(f"  {emoji} Item: {ts.get('item_title', '?')[:50]}")
    print(f"     {ts.get('fragment_count', '?')} fragmenten gecontroleerd")
    if ts.get("issues"):
        print(f"     Issues:")
        for issue in ts["issues"]:
            print(f"       - {issue}")

    # 5. Audio-URL check (optioneel)
    if args.check_audio:
        print(f"\n🔊 Audio-URL check (sample {args.sample})...")
        audio = check_audio_urls(format_id, args.sample)
        report["checks"]["audio_urls"] = audio
        if "error" in audio:
            print(f"  ⚠️  {audio['error']}")
        else:
            for r in audio["results"]:
                emoji = "✅" if r["ok"] else "❌"
                size_mb = f"{int(r.get('size_bytes', 0)) / 1e6:.1f}MB" if r.get("size_bytes", "?") != "?" else "?"
                print(f"  {emoji} {r['title']} — HTTP {r.get('status', '?')} {size_mb}")

    # Totaaloordeel
    all_ok = all(
        check.get("ok", False)
        for check in report["checks"].values()
        if isinstance(check, dict) and "ok" in check
    )
    report["overall_ok"] = all_ok

    print(f"\n{'='*55}")
    print(f"{'✅ ALLES OK' if all_ok else '⚠️  ISSUES GEVONDEN'}")
    print(f"Format: {format_id}")
    print(f"Items: {report['checks']['item_status']['completed']}/{report['checks']['item_status']['total']} transcribed")

    if args.output:
        out = Path(args.output)
        out.write_text(json.dumps(report, indent=2, ensure_ascii=False))
        print(f"\n💾 Rapport: {out}")

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
