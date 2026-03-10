---
name: stemmy-pipeline
description: >
  Batch-transcriptie van podcasts via RunPod GPU-pods naar Turso.
  Gebruik dit skill wanneer Tom vraagt om een nieuwe podcast te transcriberen,
  een batch te starten, pods te monitoren, of de DB te verifiëren.
  Bevat ook best practices voor Turso (directe HTTP), pod-beheer, en indexering.
---

# Stemmy Pipeline Skill

## Locatie
Alle scripts staan in: `~/repos/stemmy_cli/pipeline/`
Worker-code (rp_handler.py, stemmy_batch.py) staat in: `~/repos/stemmy-transcription-worker/`

## Env vars laden
Laad altijd eerst de `.env`:
```python
from dotenv import load_dotenv
load_dotenv("~/repos/stemmy_cli/.env")
```
Of zet in shell: `export $(cat ~/repos/stemmy_cli/.env | xargs)`

Turso stemmy DB: `TURSO_URL` = `libsql://stemmy-twetering.aws-eu-west-1.turso.io`
S3 bucket: `voxpop` (eu-north-1)

---

## Workflow: nieuwe podcast transcriberen

### Stap 1 — Toon plan (altijd eerst)
```bash
python3 pipeline/launch_pods.py --rss "URL" --pods 3 --language nl --dry-run
```
Laat Tom de podcast-titel en episode-count bevestigen.

### Stap 2 — Indexes controleren
Controleer of indexes bestaan (eenmalig na nieuwe DB):
```bash
python3 pipeline/create_indexes.py
```
Dit duurt 30-120s per index. Veilig om meerdere keren te draaien.

### Stap 3 — Pods starten
```bash
python3 pipeline/launch_pods.py \
  --rss "URL" \
  --pods 3 \
  --language nl \
  --gpu RTX_A5000
```
Slaat `pods.json` op. Format ID wordt aangemaakt als het niet bestaat.

### Stap 4 — Monitoren
Na starten: zet monitoring in achtergrond of check periodiek:
```bash
python3 pipeline/monitor_pods.py --once   # eenmalige check
```
Telegram-notificatie wordt automatisch gestuurd bij voltooiing
(als TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID gezet zijn).

### Stap 5 — Verificatie
```bash
python3 pipeline/verify_db.py --format-id "uuid" --check-audio
```
Altijd uitvoeren na een batch om kwaliteit te bevestigen.

---

## Turso: KRITISCHE best practices

### ❌ NOOIT: Python libsql adapter voor pipeline-scripts
```python
# NIET DOEN — timeout op grote queries
from stemmy_cli.adapters.sqlite import SQLiteAdapter
adapter = SQLiteAdapter()
adapter.execute_raw("SELECT ... FROM fragments WHERE item_id = ?", [id])
# → timeout bij >50k rijen zonder index
```

### ✅ ALTIJD: turso_direct.py gebruiken
```python
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "pipeline"))
from turso_direct import TursoDirect

db = TursoDirect()
rows = db.execute("SELECT ... FROM fragments WHERE item_id = ?", [item_id])
```

### Waarom?
- Python libsql adapter: interne socket timeout van ~10s, niet configureerbaar
- `CREATE INDEX` op grote tabel: duurt 30-120s → adapter timeout
- Queries zonder index op >50k rijen: ook timeout
- `turso_direct.py` gebruikt httpx met volledige timeout-controle

### Bulk inserts: gebruik execute_batch
```python
statements = [
    {"sql": "INSERT OR IGNORE INTO fragments (...) VALUES (?,...)", "args": [...]},
    ...
]
db.execute_batch(statements)  # 200 inserts per HTTP-call
```
In praktijk: 465 fragments = 3 HTTP-calls (i.p.v. 465). ~10× sneller.

### Index vereisten (verplicht na import)
```python
db.ensure_indexes()  # roept create_indexes.py logica aan
```
Zonder `idx_fragments_item_id`: alle fragment-queries zijn full-scan → timeout.

---

## RunPod: pod beheer

### GPU kiezen
- **RTX_A5000** — goedkoopste, prima voor large-v3-turbo (24GB VRAM)
- **RTX_3090** — iets sneller, €0.22/hr
- **RTX_4090** — snelste optie, maar duurder
- **Nooit minder dan 24GB VRAM** — model past anders niet

### Kosten berekenen
Vuistregel: 1u audio ≈ 1.2 min transcriptietijd op A5000
```
128u audio / 3 pods / 50× realtime = ~51 min = ~$0.14 per pod = ~$0.42 totaal
```

### Pod terminatie
Pods stoppen zichzelf via `--auto-terminate`. Als een pod blijft draaien:
```python
import httpx
httpx.post(
    f"https://api.runpod.io/graphql?api_key={RUNPOD_API_KEY}",
    json={"query": f'mutation {{ podTerminate(input: {{podId: "{pod_id}"}}) }}'}
)
```

### SSH in pod voor debugging
```bash
# SSH-info staat in pods.json of haal op via monitor_pods.py
ssh root@IP -p PORT
tail -f /workspace/pod_log.txt
```

---

## Normalisatie: ffmpeg standaard

Alle audio wordt genormaliseerd identiek aan stemmy's `format_service.py`:
```bash
ffmpeg -y -i input.mp3 \
  -t {duration - 0.001} \     # 1ms trim van eind
  -b:a 192k \                  # 192k CBR
  -write_xing 0 \              # geen XING header
  -write_id3v1 1 \             # ID3v1 tag
  -id3v2_version 3 \           # ID3v2.3
  output.mp3
```
Dit is verplicht voor compatibiliteit met de stemmy-player.

---

## S3: audio opslag

Structuur:
```
s3://voxpop/audio/formats/{format_id}/{item_id}.mp3
```

URL-patroon:
```
https://voxpop.s3.eu-north-1.amazonaws.com/audio/formats/{format_id}/{item_id}.mp3
```

Gebruik `--skip-s3` alleen voor testen (zet dan `audio_url = source_url`).

---

## Verificatie checklist

Na elke grote batch, gebruik `verify_db.py`:
- ✅ Items: completion_pct > 95%
- ✅ Fragmenten: gem. >200 per aflevering (podcastlengte afhankelijk)
- ✅ Word-level timestamps: aanwezig en correct formaat
- ✅ Tijdstempels: start < end, geen gaten >60s
- ✅ Audio-URLs: HTTP 200, bestand >100KB

---

## Formaat IDs in gebruik

| Podcast | Format ID |
|---------|-----------|
| Ervaring voor Beginners | `ervaring-voor-beginners-shared` |
| POM | `dcacd05c-ff24-4403-84b1-c89857052dcb` |
| Man man man | `628b7fb7-87fd-40c2-91bb-6e1d2efd7a0c` |
| Slimmer Presteren | `4cb8f846-18f1-44e4-9ac5-e175e971aa53` |
| Poki AI | `ba64f69c-8390-4120-817e-fcf745a6a3fb` |
| Johnny Carson | `726b595f-3786-481b-9838-5113884d624d` |
| Maarten van Rossem | `3bd7d7e0-85cd-4693-8ab4-c3690ca89835` |

---

## Hervatten na onderbreking

Alle pipeline-scripts zijn idempotent:
- `stemmy_batch.py`: `INSERT OR IGNORE` + `has_fragments()` check — skip bestaande items
- `launch_pods.py`: nieuwe pods starten met zelfde `--format-id`
- `create_indexes.py`: `IF NOT EXISTS` — altijd veilig

---

## Troubleshooting snelgids

| Probleem | Oplossing |
|----------|-----------|
| Fragment-query timeout | `python3 pipeline/create_indexes.py` |
| Pod start niet | Check RUNPOD_API_KEY, probeer andere GPU |
| Items blijven pending | SSH in pod, check `/workspace/pod_log.txt` |
| Turso adapter timeout | Gebruik `turso_direct.py`, niet de Python adapter |
| S3 upload mislukt | Check AWS credentials, bucket-naam, regio |
| Audio-URL 403 | S3-object niet public — check bucket policy |
