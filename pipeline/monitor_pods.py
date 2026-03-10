#!/usr/bin/env python3
"""
monitor_pods.py — Volg RunPod pods + Turso voortgang in realtime.

Leest pods.json (aangemaakt door launch_pods.py).
Pollt elke N seconden: pod-status via RunPod API + items-tellingen via Turso.
Stuurt Telegram-melding zodra alle pods klaar (of gefaald) zijn.

Gebruik:
    python3 pipeline/monitor_pods.py                   # leest pipeline/pods.json
    python3 pipeline/monitor_pods.py --state mijn.json
    python3 pipeline/monitor_pods.py --format-id uuid  # zonder pods.json
    python3 pipeline/monitor_pods.py --once             # één check, dan exit

Env vars (naast RUNPOD_API_KEY + TURSO_*):
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

PIPELINE_DIR = Path(__file__).parent
DEFAULT_STATE = PIPELINE_DIR / "pods.json"
POLL_INTERVAL = 60  # seconden tussen polls

# Status → emoji mapping
STATUS_EMOJI = {
    "RUNNING": "🟢",
    "EXITED": "✅",
    "FAILED": "❌",
    "TERMINATED": "🔴",
    "UNKNOWN": "❓",
    "launching": "⏳",
}


def runpod_pod_status(api_key: str, pod_id: str) -> dict:
    query = f"""
    query {{
      pod(input: {{podId: "{pod_id}"}}) {{
        id
        name
        desiredStatus
        runtime {{
          uptimeInSeconds
          ports {{
            ip
            isIpPublic
            privatePort
            publicPort
          }}
        }}
      }}
    }}
    """
    try:
        resp = httpx.post(
            f"https://api.runpod.io/graphql?api_key={api_key}",
            json={"query": query},
            timeout=20.0,
        )
        resp.raise_for_status()
        return resp.json().get("data", {}).get("pod", {}) or {}
    except Exception as e:
        return {"error": str(e)}


def turso_query(url: str, token: str, sql: str, params: list = None) -> list:
    stmt = {"sql": sql}
    if params:
        stmt["args"] = [
            {"type": "null"} if v is None
            else {"type": "text", "value": str(v)}
            for v in params
        ]
    try:
        resp = httpx.post(
            url.replace("libsql://", "https://") + "/v2/pipeline",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"requests": [{"type": "execute", "stmt": stmt}, {"type": "close"}]},
            timeout=20.0,
        )
        resp.raise_for_status()
        result = resp.json()["results"][0]
        if result.get("type") == "error":
            return []
        rs = result.get("response", {}).get("result", {})
        cols = [c["name"] for c in rs.get("cols", [])]
        return [
            {c: (cell["value"] if cell["type"] != "null" else None)
             for c, cell in zip(cols, row)}
            for row in rs.get("rows", [])
        ]
    except Exception as e:
        print(f"  [warn] Turso query mislukt: {e}")
        return []


def get_turso_progress(turso_url: str, turso_token: str, format_id: str) -> dict:
    """Haal voortgang op uit Turso: items per status."""
    rows = turso_query(
        turso_url, turso_token,
        "SELECT transcript_status, COUNT(*) as n FROM items WHERE format_id = ? GROUP BY transcript_status",
        [format_id],
    )
    stats = {r["transcript_status"]: int(r["n"]) for r in rows}
    total = sum(stats.values())

    # Haal ook fragment-count op (snelle approximation via completed items)
    frag_rows = turso_query(
        turso_url, turso_token,
        "SELECT COUNT(*) as n FROM items WHERE format_id = ? AND transcript_status = 'completed'",
        [format_id],
    )
    completed_items = int(frag_rows[0]["n"]) if frag_rows else stats.get("completed", 0)

    return {
        "total": total,
        "completed": stats.get("completed", 0),
        "pending": stats.get("pending", 0),
        "failed": stats.get("failed", 0),
        "pct": round(completed_items / max(total, 1) * 100, 1),
    }


def notify_telegram(message: str, bot_token: str = None, chat_id: str = None):
    bot_token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        return
    try:
        httpx.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception:
        pass


def format_uptime(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds//60}m{seconds%60:02d}s"
    return f"{seconds//3600}h{(seconds%3600)//60:02d}m"


def check_once(state: dict, api_key: str, turso_url: str, turso_token: str) -> dict:
    """Doe één ronde checks. Retourneert samenvatting."""
    format_id = state.get("format_id")
    pod_splits = state.get("pod_splits", [])
    feed_title = state.get("feed_title", "?")
    total_eps = state.get("total_episodes", 0)

    now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    print(f"\n{'─'*55}")
    print(f"⏱  {now}  |  {feed_title}")
    print(f"{'─'*55}")

    # Turso voortgang
    if format_id:
        prog = get_turso_progress(turso_url, turso_token, format_id)
        bar_filled = int(prog["pct"] / 5)
        bar = "█" * bar_filled + "░" * (20 - bar_filled)
        print(f"📊 Turso: [{bar}] {prog['pct']}%")
        print(f"   {prog['completed']}/{prog['total']} items completed "
              f"| {prog['pending']} pending | {prog['failed']} failed")
    else:
        prog = {}

    # Pod statussen
    pod_statuses = []
    for ps in pod_splits:
        pod_id = ps.get("pod_id")
        if not pod_id:
            pod_statuses.append({"status": "FAILED", "uptime": 0, "split": ps})
            print(f"  Pod {ps['pod_index']}: ❌ niet gestart")
            continue

        info = runpod_pod_status(api_key, pod_id)
        if "error" in info:
            status = "UNKNOWN"
        else:
            status = info.get("desiredStatus", "UNKNOWN")

        uptime = (info.get("runtime") or {}).get("uptimeInSeconds", 0)
        emoji = STATUS_EMOJI.get(status, "❓")
        print(f"  Pod {ps['pod_index']} ({pod_id[:8]}..): {emoji} {status} "
              f"| uptime: {format_uptime(uptime)} "
              f"| episodes {ps['offset']}-{ps['offset']+ps['limit']-1}")

        pod_statuses.append({"status": status, "uptime": uptime, "split": ps, "info": info})

    # Conclusie
    all_done = all(
        ps["status"] in ("EXITED", "TERMINATED", "FAILED")
        for ps in pod_statuses
    )
    all_ok = all(ps["status"] == "EXITED" for ps in pod_statuses)

    return {
        "done": all_done,
        "ok": all_ok,
        "prog": prog,
        "pod_statuses": pod_statuses,
        "format_id": format_id,
        "feed_title": feed_title,
        "total_eps": total_eps,
    }


def main():
    ap = argparse.ArgumentParser(description="Monitor RunPod pods + Turso voortgang")
    ap.add_argument("--state", default=str(DEFAULT_STATE),
                    help=f"State-bestand van launch_pods.py (default: {DEFAULT_STATE})")
    ap.add_argument("--format-id", default=None,
                    help="Format ID om Turso-voortgang te monitoren (zonder pods.json)")
    ap.add_argument("--interval", type=int, default=POLL_INTERVAL,
                    help=f"Poll-interval in seconden (default: {POLL_INTERVAL})")
    ap.add_argument("--once", action="store_true",
                    help="Eén check dan exit")
    args = ap.parse_args()

    # Laad state
    state_path = Path(args.state)
    if state_path.exists():
        state = json.loads(state_path.read_text())
        print(f"📂 State geladen: {state_path}")
        print(f"   Feed: {state.get('feed_title', '?')}")
        print(f"   Format: {state.get('format_id', '?')}")
        print(f"   Pods: {state.get('pods', '?')}")
    elif args.format_id:
        state = {"format_id": args.format_id, "pod_splits": [], "feed_title": "?"}
        print(f"📊 Alleen Turso-monitoring voor format {args.format_id}")
    else:
        print(f"❌ {state_path} niet gevonden en --format-id niet opgegeven", file=sys.stderr)
        sys.exit(1)

    # Env vars
    api_key = os.environ.get("RUNPOD_API_KEY", "")
    turso_url = os.environ.get("TURSO_URL") or os.environ.get("TURSO_DB_URL", "")
    turso_token = os.environ.get("TURSO_TOKEN") or os.environ.get("TURSO_API_KEY", "")

    if not turso_url:
        # Probeer uit .env laden
        env_file = PIPELINE_DIR.parent / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if "=" in line and not line.startswith("#"):
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())
            turso_url = os.environ.get("TURSO_URL") or os.environ.get("TURSO_DB_URL", "")
            turso_token = os.environ.get("TURSO_TOKEN") or os.environ.get("TURSO_API_KEY", "")

    notify_sent = False

    while True:
        result = check_once(state, api_key, turso_url, turso_token)

        if result["done"] and not notify_sent:
            prog = result["prog"]
            msg = (
                f"{'✅' if result['ok'] else '⚠️'} <b>Stemmy batch {'klaar' if result['ok'] else 'gestopt'}</b>\n"
                f"Feed: {result['feed_title']}\n"
                f"Transcribed: {prog.get('completed', '?')}/{prog.get('total', '?')} items\n"
                f"Format: <code>{result['format_id']}</code>\n"
                f"\nVerificatie: python3 pipeline/verify_db.py --format-id {result['format_id']}"
            )
            notify_telegram(msg)
            notify_sent = True
            print(f"\n📨 Telegram notificatie verstuurd")

        if args.once or result["done"]:
            break

        print(f"\n⏳ Volgende check over {args.interval}s... (Ctrl+C om te stoppen)")
        time.sleep(args.interval)

    print(f"\n{'='*55}")
    if result.get("done"):
        print("✅ Monitoring klaar.")
        prog = result.get("prog", {})
        if prog:
            print(f"   Resultaat: {prog.get('completed','?')}/{prog.get('total','?')} items transcribed")
    else:
        print("📊 Check klaar (pods nog bezig).")


if __name__ == "__main__":
    main()
