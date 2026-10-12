#!/usr/bin/env python3
"""
Importeer een podcast-archief uit een podcast-mcp server (bramdehart/podcast-mcp)
volgens de stemmy-logica:

  format → items (audio genormaliseerd naar 192k CBR, op S3) → fragments (+ transcript_data)

De MCP levert segmenten met tijden en sprekernamen, maar geen woordtijden.
Woordtijden worden naar tekstlengte over het segment verdeeld (gemarkeerd als
'interpolated'); precies knippen gebeurt pas bij het maken van een compilatie
(supercut.laughs.precise: ElevenLabs STT op het venster).

Hervatbaar: items worden ontdubbeld op source_url (de originele audio-URL).
Voorzichtig met de MCP: één call tegelijk, pauze ertussen, backoff bij 429.

    PYTHONPATH=src:. python scripts/import_podcast_mcp.py --limit 2          # test
    PYTHONPATH=src:. python scripts/import_podcast_mcp.py                    # alles
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=True)

from stemmy_cli.adapters.sqlite import SQLiteAdapter  # noqa: E402
from stemmy_cli.audio.normalize import process_audio_for_import  # noqa: E402
from stemmy_cli.commands.transcripts import _import_transcript_to_db  # noqa: E402
from supercut.podmcp import PodMCP  # noqa: E402

FORMAT_TITLE = "AI Report"
FORMAT_SOURCE = "https://ai-report.bramdehart.nl/mcp/"
MCP_PAUZE = 2.0                    # seconden tussen MCP-calls
_mcp_lock = threading.Lock()


def fix(t: str) -> str:
    """De MCP-tekst is dubbel UTF-8-gecodeerd (Ã¶ → ö)."""
    try:
        return t.encode("latin1").decode("utf8")
    except Exception:
        return t


def mcp_call(m: PodMCP, name: str, **args):
    with _mcp_lock:
        for k in range(6):
            try:
                r = m.call(name, **args)
                time.sleep(MCP_PAUZE)
                return r
            except Exception as e:
                wait = 10 * (k + 1)
                print(f"    MCP-fout ({e}); {wait}s wachten", flush=True)
                time.sleep(wait)
        raise RuntimeError(f"MCP-call {name} blijft mislukken")


def transcript(m: PodMCP, ep: dict) -> list[dict]:
    """Volledige segmentlijst: vensters van een uur, ontdubbeld op starttijd."""
    segs, t = {}, 1800.0
    while True:
        r = mcp_call(m, "get_transcript_around_timestamp", episode_id=ep["id"], timestamp_seconds=t, context_seconds=1850)
        for s in r.get("segments", []):
            segs[round(s["start_seconds"], 2)] = s
        if t + 1850 >= ep["duration"]:
            break
        t += 3600
    return [segs[k] for k in sorted(segs)]


def utterances(segs: list[dict]) -> list[dict]:
    """
    MCP-segmenten → AssemblyAI-achtige utterances (ms), opeenvolgende segmenten van
    dezelfde spreker samengevoegd tot max ~30s. Woordtijden geïnterpoleerd.
    """
    out, cur = [], None
    for s in segs:
        text = fix(s["text"]).strip()
        if not text:
            continue
        name = fix(s.get("speaker_name") or "") or s.get("speaker_id") or "?"
        conf = s.get("speaker_confidence") or 0
        spk = name if conf >= 0.6 and not name.startswith("CHUNK_") else (s.get("speaker_id") or "?")
        st, en = int(s["start_seconds"] * 1000), int(s["end_seconds"] * 1000)
        toks = text.split()
        total = sum(len(w) + 1 for w in toks) or 1
        words, pos = [], st
        for w in toks:
            d = (en - st) * (len(w) + 1) / total
            words.append(dict(text=w, start=int(pos), end=int(pos + d * 0.92), speaker=spk, confidence=conf))
            pos += d
        if cur and cur["speaker"] == spk and st - cur["end"] < 1500 and en - cur["start"] < 30000:
            cur["text"] += " " + text
            cur["end"] = en
            cur["words"] += words
        else:
            if cur:
                out.append(cur)
            cur = dict(text=text, start=st, end=en, speaker=spk, confidence=conf, words=words)
    if cur:
        out.append(cur)
    return out


def ensure_format(db: SQLiteAdapter) -> str:
    rows = db.execute_raw("SELECT id FROM formats WHERE title = ?", [FORMAT_TITLE])
    if rows:
        return rows[0]["id"]
    fid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    db.insert("formats", dict(id=fid, title=FORMAT_TITLE, source_url=FORMAT_SOURCE, source_type="podcast-mcp",
                              description="AI Report (voorheen Poki), geïmporteerd via podcast-mcp van Bram de Hart.",
                              created_at=now, updated_at=now))
    return fid


def import_episode(db: SQLiteAdapter, m: PodMCP, fid: str, ep: dict) -> str:
    if db.execute_raw("SELECT id FROM items WHERE format_id = ? AND source_url = ?", [fid, ep["audio_url"]]):
        return "al aanwezig"
    t0 = time.time()
    segs = transcript(m, ep)                                  # eerst transcript: geen S3-upload voor niets
    utts = utterances(segs)
    item_id = str(uuid.uuid4())
    audio_url = process_audio_for_import(source_url=ep["audio_url"], item_id=item_id, subfolder=f"formats/{fid}")
    now = datetime.now(timezone.utc).isoformat()
    td = dict(source="podcast-mcp", word_timing="interpolated", text=" ".join(u["text"] for u in utts),
              utterances=[{k: v for k, v in u.items() if k != "words"} for u in utts])
    with db.connection() as conn:
        db.insert("items", dict(id=item_id, format_id=fid, title=fix(ep["title"]), audio_url=audio_url,
                                source_url=ep["audio_url"], published_at=ep["published_at"],
                                duration_seconds=str(ep["duration"]), transcript_status="completed",
                                transcript_data=json.dumps(td, ensure_ascii=False),
                                metadata=json.dumps(dict(podcast_mcp_episode_id=ep["id"])),
                                created_at=now, updated_at=now), connection=conn)
        res = _import_transcript_to_db(db, item_id, audio_url, {"utterances": utts}, connection=conn)
        conn.execute("UPDATE fragments SET import_source = 'podcast-mcp' WHERE item_id = ?", [item_id])
        conn.commit()
    return f"{res['fragments_imported']} fragmenten, {len(segs)} segmenten, {time.time() - t0:.0f}s"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=3, help="parallelle audio-normalisaties (MCP blijft serieel)")
    ap.add_argument("--db", default=str(ROOT / "data" / "stemmy-replica.db"),
                    help="doel-database (expliciet: STEMMY_DB_PATH in .env wijst naar surrounded)")
    a = ap.parse_args()
    db = SQLiteAdapter(Path(a.db))
    print(f"DB: {db.db_path}")
    m = PodMCP()
    eps = mcp_call(m, "list_episodes", limit=500)["result"]
    eps.sort(key=lambda e: e["published_at"])
    if a.limit:
        eps = eps[: a.limit]
    fid = ensure_format(db)
    print(f"{len(eps)} afleveringen → format {FORMAT_TITLE} ({fid})", flush=True)

    def one(i_ep):
        i, ep = i_ep
        mm = PodMCP() if a.workers > 1 else m
        try:
            msg = import_episode(db, mm, fid, ep)
        except Exception as e:
            msg = f"MISLUKT: {e}"[:200]
        print(f"[{i+1}/{len(eps)}] {ep['published_at'][:10]} {fix(ep['title'])[:55]} — {msg}", flush=True)

    with ThreadPoolExecutor(a.workers) as ex:
        list(ex.map(one, enumerate(eps)))


if __name__ == "__main__":
    main()
