#!/usr/bin/env python3
"""
launch_pods.py — Start N RunPod GPU-pods parallel voor batch-transcriptie.

Verdeelt een RSS-feed automatisch over N pods via offset/limit.
Elke pod draait stemmy_batch.py uit de twetering/stemmy-transcription-worker repo.

Gebruik:
    python3 pipeline/launch_pods.py --rss https://... --pods 3
    python3 pipeline/launch_pods.py --rss https://... --pods 4 --gpu RTX_A5000 --dry-run
    python3 pipeline/launch_pods.py --rss https://... --pods 3 --format-id bestaand-uuid

Output: pods.json met pod-IDs, SSH-info en offset/limit per pod.

Vereiste env vars:
    RUNPOD_API_KEY
    TURSO_URL / TURSO_TOKEN
    AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
    S3_BUCKET_NAME (default: voxpop)
    AWS_REGION (default: eu-north-1)

Optioneel:
    TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID — voor notificaties
"""

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import httpx

PIPELINE_DIR = Path(__file__).parent
STATE_FILE = PIPELINE_DIR / "pods.json"

# RunPod GraphQL endpoint
RUNPOD_GQL = "https://api.runpod.io/graphql"

# GPU presets met prijs-indicatie
GPU_PRESETS = {
    "RTX_A5000": {"gpuCount": 1, "gpuTypeId": "NVIDIA RTX A5000", "minMemoryInGb": 24},
    "RTX_3090":  {"gpuCount": 1, "gpuTypeId": "NVIDIA GeForce RTX 3090", "minMemoryInGb": 24},
    "RTX_4090":  {"gpuCount": 1, "gpuTypeId": "NVIDIA GeForce RTX 4090", "minMemoryInGb": 24},
    "A100_40":   {"gpuCount": 1, "gpuTypeId": "NVIDIA A100 40GB PCIe", "minMemoryInGb": 40},
}

# Docker image (uit stemmy-transcription-worker Dockerfile)
DOCKER_IMAGE = "twetering/stemmy-transcription-worker:latest"
FALLBACK_IMAGE = "runpod/base:0.6.1-cuda12.1.0"  # als custom image niet beschikbaar is


def count_rss_episodes(rss_url: str) -> tuple[int, str, str]:
    """Tel episodes in RSS-feed. Retourneert (count, feed_title, feed_image)."""
    print(f"[rss] Fetching {rss_url}...", flush=True)
    feed = feedparser.parse(rss_url)
    episodes_with_audio = [
        ep for ep in feed.entries
        if any("audio" in enc.get("type", "") for enc in ep.get("enclosures", []))
    ]
    title = feed.feed.get("title", "Onbekend")
    image = (feed.feed.get("image", {}).get("href") or
             feed.feed.get("itunes_image", {}).get("href") or "")
    return len(episodes_with_audio), title, image


def runpod_api(query: str, api_key: str) -> dict:
    resp = httpx.post(
        f"{RUNPOD_GQL}?api_key={api_key}",
        json={"query": query},
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"RunPod API fout: {data['errors']}")
    return data.get("data", {})


def build_env_vars(extra: dict = None) -> list[dict]:
    """Bouw env-var lijst voor RunPod pod."""
    required = {
        "TURSO_URL": os.environ["TURSO_URL"],
        "TURSO_TOKEN": os.environ.get("TURSO_TOKEN") or os.environ.get("TURSO_API_KEY"),
        "AWS_ACCESS_KEY_ID": os.environ["AWS_ACCESS_KEY_ID"],
        "AWS_SECRET_ACCESS_KEY": os.environ["AWS_SECRET_ACCESS_KEY"],
        "AWS_REGION": os.environ.get("AWS_REGION", "eu-north-1"),
        "S3_BUCKET_NAME": os.environ.get("S3_BUCKET_NAME", "voxpop"),
        "RUNPOD_API_KEY": os.environ["RUNPOD_API_KEY"],
        "AUTO_TERMINATE": "1",
    }
    if extra:
        required.update(extra)
    return [{"key": k, "value": v} for k, v in required.items() if v]


def build_start_command(rss_url: str, format_id: str, offset: int, limit: int,
                        language: str, beam_size: int) -> str:
    """Bouw het bash-commando dat de pod uitvoert na opstarten."""
    # Installeer deps als ze nog niet aanwezig zijn (FALLBACK_IMAGE heeft geen Python packages)
    setup = (
        "set -e; "
        "pip install -q faster-whisper httpx feedparser boto3 2>/dev/null || true; "
        "which python3 || ln -sf /usr/bin/python3.11 /usr/bin/python3 || true; "
    )

    # Kloon/update de worker repo
    clone = (
        "if [ ! -d /workspace/stemmy-transcription-worker ]; then "
        "  git clone https://github.com/twetering/stemmy-transcription-worker.git "
        "  /workspace/stemmy-transcription-worker; "
        "else "
        "  git -C /workspace/stemmy-transcription-worker pull --ff-only 2>/dev/null || true; "
        "fi; "
        "cd /workspace/stemmy-transcription-worker; "
    )

    lang_arg = f"--language {language}" if language else "--language auto"

    run = (
        f"python3 stemmy_batch.py "
        f"--rss '{rss_url}' "
        f"--format-id '{format_id}' "
        f"--offset {offset} --limit {limit} "
        f"{lang_arg} "
        f"--beam-size {beam_size} "
        f"--auto-terminate "
        f"2>&1 | tee /workspace/pod_log.txt"
    )

    return setup + clone + run


def launch_pod(api_key: str, gpu_preset: str, pod_name: str,
               start_cmd: str, env_vars: list, container_disk_gb: int = 20) -> dict:
    """Start één RunPod pod. Retourneert pod-info dict."""
    gpu = GPU_PRESETS[gpu_preset]
    env_str = ", ".join(
        f'{{key: "{e["key"]}", value: "{e["value"]}"}}'
        for e in env_vars
    )

    mutation = f"""
    mutation {{
      podFindAndDeployOnDemand(input: {{
        name: "{pod_name}",
        imageName: "{DOCKER_IMAGE}",
        gpuCount: {gpu["gpuCount"]},
        gpuTypeId: "{gpu["gpuTypeId"]}",
        minMemoryInGb: {gpu["minMemoryInGb"]},
        containerDiskInGb: {container_disk_gb},
        volumeInGb: 0,
        startJupyter: false,
        startSsh: true,
        dockerStartCmd: "{start_cmd.replace('"', '\\"')}",
        envs: [{env_str}],
        supportPublicIp: true,
        ports: "22/tcp",
      }})
      {{
        id
        name
        machineId
        desiredStatus
        runtime {{
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

    data = runpod_api(mutation, api_key)
    return data.get("podFindAndDeployOnDemand", {})


def get_pod_info(api_key: str, pod_id: str) -> dict:
    """Haal actuele pod-status op."""
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
    data = runpod_api(query, api_key)
    return data.get("pod", {})


def ssh_info(pod: dict) -> str:
    """Maak SSH-commando string van pod-info."""
    runtime = pod.get("runtime") or {}
    ports = runtime.get("ports") or []
    for p in ports:
        if p.get("privatePort") == 22 and p.get("isIpPublic"):
            return f"ssh root@{p['ip']} -p {p['publicPort']}"
    return "(SSH nog niet beschikbaar)"


def notify_telegram(message: str):
    """Stuur Telegram notificatie als env vars beschikbaar zijn."""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
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


def main():
    ap = argparse.ArgumentParser(description="Start N RunPod pods voor batch-transcriptie")
    ap.add_argument("--rss", required=True, help="RSS feed URL")
    ap.add_argument("--pods", type=int, default=3, help="Aantal pods (default: 3)")
    ap.add_argument("--gpu", default="RTX_A5000",
                    choices=list(GPU_PRESETS.keys()),
                    help="GPU type per pod (default: RTX_A5000)")
    ap.add_argument("--format-id", default=None,
                    help="Bestaand format UUID (anders: auto-aanmaken door pod)")
    ap.add_argument("--language", default=None,
                    help="Taalcode voor Whisper (nl/en/auto)")
    ap.add_argument("--beam-size", type=int, default=5)
    ap.add_argument("--total", type=int, default=0,
                    help="Totaal afleveringen (0 = auto-detecteer via RSS)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Toon plan zonder pods te starten")
    ap.add_argument("--wait-for-ssh", action="store_true",
                    help="Poll totdat SSH beschikbaar is voor elke pod")
    args = ap.parse_args()

    api_key = os.environ.get("RUNPOD_API_KEY")
    if not api_key:
        print("❌ RUNPOD_API_KEY niet gezet", file=sys.stderr)
        sys.exit(1)

    # Check env vars voor de pods
    for key in ["TURSO_URL", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"]:
        if not os.environ.get(key) and not (key == "TURSO_URL" and os.environ.get("TURSO_DB_URL")):
            print(f"⚠️  {key} niet gezet", file=sys.stderr)

    # Tel episodes
    total = args.total
    if not total:
        total, feed_title, _ = count_rss_episodes(args.rss)
        print(f"[rss] {feed_title}: {total} afleveringen met audio")
    else:
        feed_title = args.rss

    if total == 0:
        print("❌ Geen afleveringen gevonden in RSS-feed", file=sys.stderr)
        sys.exit(1)

    # Verdeel over N pods
    n = min(args.pods, total)
    per_pod = math.ceil(total / n)
    splits = []
    for i in range(n):
        offset = i * per_pod
        limit = min(per_pod, total - offset)
        if limit <= 0:
            break
        splits.append({"pod_index": i + 1, "offset": offset, "limit": limit})

    print(f"\n📦 Plan: {n} pods × ~{per_pod} afleveringen = {total} totaal")
    print(f"   GPU: {args.gpu} | Language: {args.language or 'auto'}\n")
    for s in splits:
        print(f"   Pod {s['pod_index']}: offset={s['offset']}, limit={s['limit']}")

    if args.dry_run:
        print("\n[dry-run] Geen pods gestart.")
        return

    # Format ID: verplicht of auto-genereren
    format_id = args.format_id
    if not format_id:
        import uuid
        format_id = str(uuid.uuid4())
        print(f"\n[format] Nieuw format ID: {format_id}")
        print("  (Gebruik --format-id {format_id} als je meer pods later toevoegt)")

    # Start pods
    launched = []
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")

    for s in splits:
        pod_name = f"stemmy-{timestamp}-pod{s['pod_index']}"
        print(f"\n🚀 Starten pod {s['pod_index']}/{n}: {pod_name}...")

        env_vars = build_env_vars()
        start_cmd = build_start_command(
            rss_url=args.rss,
            format_id=format_id,
            offset=s["offset"],
            limit=s["limit"],
            language=args.language,
            beam_size=args.beam_size,
        )

        try:
            pod = launch_pod(api_key, args.gpu, pod_name, start_cmd, env_vars)
            pod_id = pod.get("id", "?")
            print(f"  ✅ Pod ID: {pod_id}")

            s.update({
                "pod_id": pod_id,
                "pod_name": pod_name,
                "format_id": format_id,
                "status": "launching",
                "started_at": _now(),
                "ssh": "(ophalen na opstart...)",
            })
            launched.append(s)
        except Exception as e:
            print(f"  ❌ Mislukt: {e}")
            s.update({"pod_id": None, "error": str(e)})
            launched.append(s)

    # Wacht op SSH indien gevraagd
    if args.wait_for_ssh:
        print("\n⏳ Wachten op SSH...")
        for pod_info in launched:
            if not pod_info.get("pod_id"):
                continue
            for attempt in range(30):
                time.sleep(10)
                info = get_pod_info(api_key, pod_info["pod_id"])
                ssh = ssh_info(info)
                if "ssh root@" in ssh:
                    pod_info["ssh"] = ssh
                    print(f"  Pod {pod_info['pod_index']}: {ssh}")
                    break
                print(f"  Pod {pod_info['pod_index']}: wachten... ({attempt+1}/30)")

    # Sla state op
    state = {
        "rss": args.rss,
        "feed_title": feed_title,
        "format_id": format_id,
        "total_episodes": total,
        "pods": n,
        "gpu": args.gpu,
        "language": args.language,
        "launched_at": _now(),
        "pod_splits": launched,
    }

    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    print(f"\n💾 State opgeslagen: {STATE_FILE}")

    # Samenvatting
    print(f"\n{'='*55}")
    print(f"✅ {len([p for p in launched if p.get('pod_id')])} pods gestart")
    print(f"   Format ID: {format_id}")
    print(f"   Monitoren: python3 pipeline/monitor_pods.py")
    print(f"   Verificatie: python3 pipeline/verify_db.py --format-id {format_id}")

    notify_telegram(
        f"🚀 <b>Stemmy batch gestart</b>\n"
        f"Feed: {feed_title}\n"
        f"Pods: {len(launched)} × {args.gpu}\n"
        f"Afleveringen: {total}\n"
        f"Format ID: <code>{format_id}</code>"
    )


def _now():
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    main()
