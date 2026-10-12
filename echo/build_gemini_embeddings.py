#!/usr/bin/env python3
"""
Bouw Gemini multimodale embeddings voor fragmenten uit de database.

Standaard: tekst+audio via gemini-embedding-2-preview → fragment_gemini_embeddings tabel.
Met --text-only: alleen tekst (veel sneller, geen S3-downloads).

Gebruik:
    # Test: 200 fragmenten
    python echo/build_gemini_embeddings.py --db data/stemmy-replica.db --limit 200

    # Tekst-only (snel, goedkoop)
    python echo/build_gemini_embeddings.py --db data/stemmy-replica.db --limit 500 --text-only

    # Alles (kan uren duren)
    python echo/build_gemini_embeddings.py --db data/stemmy-replica.db

    # Hervatten na onderbreking (--resume slaat al-gedane fragmenten over)
    python echo/build_gemini_embeddings.py --db data/stemmy-replica.db --limit 200 --resume
"""

import argparse
import base64
import json
import os
import sqlite3
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
os.environ["PATH"] = os.environ.get("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin"

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env",       override=True)
    load_dotenv(ROOT / ".env.local", override=True)
except ImportError:
    pass

try:
    from rich.console import Console
    from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeRemainingColumn
    console = Console()
except ImportError:
    class Console:
        def print(self, *a, **k): print(*[str(x) for x in a])
        def log(self, *a, **k): print(*[str(x) for x in a])
    console = Console()
    Progress = None

GEMINI_MODEL  = "gemini-embedding-2-preview"
GEMINI_URL    = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:embedContent"
)
TABLE         = "fragment_gemini_embeddings"
RATE_LIMIT_S  = 0.55   # ~90 req/min (Gemini free tier: 100/min)


def gemini_embed(text: str, audio_bytes: bytes | None, api_key: str) -> list | None:
    """Stuur tekst + optioneel audio naar Gemini embedding API. Geeft list[float] of None."""
    parts = []
    if text:
        parts.append({"text": text[:2000]})  # max token-budget
    if audio_bytes:
        parts.append({
            "inlineData": {
                "mimeType": "audio/mpeg",
                "data":     base64.b64encode(audio_bytes).decode(),
            }
        })
    if not parts:
        return None

    body = json.dumps({"content": {"parts": parts}}).encode()
    url  = f"{GEMINI_URL}?key={api_key}"
    req  = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
            return data.get("embedding", {}).get("values")
    except urllib.error.HTTPError as e:
        body_err = e.read().decode()[:200]
        console.print(f"[red]HTTP {e.code}: {body_err}[/red]")
        if e.code == 429:
            time.sleep(10)   # rate limit: wacht 10s
        return None
    except Exception as e:
        console.print(f"[red]Gemini error: {e}[/red]")
        return None


def extract_audio_clip(audio_url: str, start_ms: int, end_ms: int) -> bytes | None:
    """Download en extraheer een audioclip via ffmpeg (analoog aan server.py)."""
    import subprocess
    duration_s = max(0.5, (end_ms - start_ms) / 1000)
    start_s    = max(0.0, start_ms / 1000 - 0.2)  # kleine buffer voor/na

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tf:
        out_path = tf.name

    try:
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-ss", str(start_s),
            "-i", audio_url,
            "-t",  str(duration_s + 0.4),
            "-acodec", "libmp3lame",
            "-ab", "128k",
            "-ar", "44100",
            out_path,
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=60)
        if result.returncode != 0:
            return None
        data = Path(out_path).read_bytes()
        return data if len(data) > 512 else None
    except Exception:
        return None
    finally:
        try: Path(out_path).unlink()
        except: pass


def ensure_table(conn: sqlite3.Connection):
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            fragment_id  TEXT PRIMARY KEY,
            embedding    TEXT NOT NULL,
            dimensions   INTEGER,
            model        TEXT,
            mode         TEXT,
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()


def load_done(conn: sqlite3.Connection) -> set:
    try:
        rows = conn.execute(f"SELECT fragment_id FROM {TABLE}").fetchall()
        return {r[0] for r in rows}
    except sqlite3.OperationalError:
        return set()


def main():
    parser = argparse.ArgumentParser(description="Bouw Gemini embedding cache")
    parser.add_argument("--db",        default="data/stemmy-replica.db",
                        help="Pad naar SQLite database")
    parser.add_argument("--limit",     type=int, default=0,
                        help="Max aantal fragmenten (0 = alles)")
    parser.add_argument("--text-only", action="store_true",
                        help="Gebruik alleen tekst (geen audio-download)")
    parser.add_argument("--resume",    action="store_true",
                        help="Sla al-geëmbedde fragmenten over")
    parser.add_argument("--format",    default=None,
                        help="Filter op één podcast (format_id UUID)")
    parser.add_argument("--offset",    type=int, default=0,
                        help="Sla eerste N fragmenten over (voor sampling)")
    args = parser.parse_args()

    api_key = os.environ.get("GOOGLE_API_KEY", "")
    if not api_key:
        console.print("[red]Geen GOOGLE_API_KEY in omgeving. Zet deze in .env.[/red]")
        sys.exit(1)

    db_path = str(ROOT / args.db) if not Path(args.db).is_absolute() else args.db
    mode    = "text" if args.text_only else "audio+text"

    console.print(f"\n[bold cyan]Gemini Embedding Builder[/bold cyan]")
    console.print(f"  DB:    {db_path}")
    console.print(f"  Model: {GEMINI_MODEL}")
    console.print(f"  Modus: {mode}")
    if args.limit:
        console.print(f"  Limit: {args.limit} fragmenten")
    console.print()

    conn = sqlite3.connect(db_path)
    ensure_table(conn)

    already_done = load_done(conn) if args.resume else set()
    if already_done:
        console.print(f"[dim]↩ {len(already_done)} fragmenten al gedaan, overgeslagen[/dim]")

    # Haal fragmenten op met tekst + audio URL
    sql = """
        SELECT f.id, f.text, f.item_audio_url,
               CAST(f.start_time_seconds * 1000 AS INTEGER) AS start_ms,
               CAST(f.end_time_seconds   * 1000 AS INTEGER) AS end_ms
        FROM fragments f
        JOIN items i ON f.item_id = i.id
        WHERE f.text IS NOT NULL AND f.text != ''
          AND f.item_audio_url IS NOT NULL
          AND f.start_time_seconds IS NOT NULL
          AND f.end_time_seconds IS NOT NULL
          AND f.end_time_seconds > f.start_time_seconds
    """
    params: list = []
    if args.format:
        sql += " AND i.format_id = ?"
        params.append(args.format)

    # Join met bestaande text embeddings — embedded fragmenten zijn bewezen bereikbaar
    sql += """
        AND EXISTS (
            SELECT 1 FROM fragment_embeddings fe WHERE fe.fragment_id = f.id
        )
        ORDER BY f.id
    """

    if args.offset:
        sql += f" LIMIT -1 OFFSET {args.offset}"

    rows = conn.execute(sql, params).fetchall()

    # Filter al-gedane
    if args.resume:
        rows = [r for r in rows if r[0] not in already_done]

    if args.limit:
        rows = rows[:args.limit]

    total = len(rows)
    if total == 0:
        console.print("[yellow]Geen fragmenten om te embedden.[/yellow]")
        conn.close()
        return

    console.print(f"[bold]{total} fragmenten te embedden[/bold] (~{total * RATE_LIMIT_S / 60:.0f} min bij {1/RATE_LIMIT_S:.0f} req/s)\n")

    ok_count   = 0
    err_count  = 0
    t_start    = time.time()

    def _save(frag_id, emb, mode_str):
        nonlocal ok_count
        conn.execute(f"""
            INSERT OR REPLACE INTO {TABLE}
            (fragment_id, embedding, dimensions, model, mode)
            VALUES (?, ?, ?, ?, ?)
        """, (frag_id, json.dumps(emb), len(emb), GEMINI_MODEL, mode_str))
        conn.commit()
        ok_count += 1

    if Progress:
        prog = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TimeRemainingColumn(),
        )
        task = prog.add_task("embedden…", total=total)
        prog.start()
    else:
        prog = None

    for i, (frag_id, text, audio_url, start_ms, end_ms) in enumerate(rows):
        t_loop = time.time()

        audio_bytes = None
        used_mode   = "text"

        if not args.text_only:
            audio_bytes = extract_audio_clip(audio_url, int(start_ms or 0), int(end_ms or 0))
            if audio_bytes:
                used_mode = "audio+text"

        emb = gemini_embed(text or "", audio_bytes, api_key)

        if emb:
            _save(frag_id, emb, used_mode)
            if prog:
                prog.update(task, advance=1,
                            description=f"[green]OK[/green] {used_mode} — dim={len(emb)}")
            elif i % 10 == 0:
                elapsed = time.time() - t_start
                console.print(f"  {i+1}/{total} ({used_mode}) dim={len(emb)} — {elapsed:.0f}s verstreken")
        else:
            err_count += 1
            if prog:
                prog.update(task, advance=1, description="[red]fout[/red]")
            else:
                console.print(f"  [FOUT] fragment {frag_id}")

        # Rate limiting
        elapsed_loop = time.time() - t_loop
        sleep_needed = RATE_LIMIT_S - elapsed_loop
        if sleep_needed > 0:
            time.sleep(sleep_needed)

    if prog:
        prog.stop()

    elapsed_total = time.time() - t_start
    conn.close()

    console.print(f"\n[bold green]Klaar![/bold green]")
    console.print(f"  ✓ {ok_count} embeddings opgeslagen")
    if err_count:
        console.print(f"  ✗ {err_count} fouten")
    console.print(f"  Tijd: {elapsed_total:.0f}s ({elapsed_total/60:.1f} min)")
    console.print(f"  Tabel: {TABLE} in {db_path}\n")


if __name__ == "__main__":
    main()
