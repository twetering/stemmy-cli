# Stemmy Transcription Pipeline

Schaalbare batch-transcriptie van podcasts via RunPod GPU-pods → Turso.

## Pipeline overzicht

```
RSS-feed
   ↓
launch_pods.py      ← Verdeelt episodes, start N pods parallel
   ↓
[RunPod pods]       ← Elk pod: download → normalize → S3 → faster-whisper → Turso
   ↓
monitor_pods.py     ← Volgt voortgang, stuurt Telegram-notificatie bij klaar
   ↓
verify_db.py        ← Controleert kwaliteit van de data in Turso
```

## Snelstart

### 1. Env vars instellen
Zet in je shell of `.env`:
```bash
TURSO_URL=libsql://stemmy-twetering.aws-eu-west-1.turso.io
TURSO_TOKEN=eyJ...
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_REGION=eu-north-1
S3_BUCKET_NAME=voxpop
RUNPOD_API_KEY=rpa_...
TELEGRAM_BOT_TOKEN=...    # optioneel, voor notificaties
TELEGRAM_CHAT_ID=...      # optioneel
```

### 2. Indexes aanmaken (eenmalig, na eerste grote import)
```bash
python3 pipeline/create_indexes.py
```
⚠️ Duurt 30-60s per index. Zonder indexes zijn fragment-queries traag (>30s timeout).

### 3. Batch starten
```bash
# 3 pods, auto-detect episodes
python3 pipeline/launch_pods.py \
  --rss "https://feeds.soundcloud.com/users/soundcloud:users:xxx/sounds.rss" \
  --pods 3 \
  --language nl

# Of met bestaand format-id (bij hervatten)
python3 pipeline/launch_pods.py \
  --rss "..." \
  --pods 3 \
  --format-id "bestaand-uuid"
```

Slaat `pipeline/pods.json` op met pod-IDs en state.

### 4. Monitoren
```bash
# Continue monitoring (Ctrl+C om te stoppen)
python3 pipeline/monitor_pods.py

# Eén check
python3 pipeline/monitor_pods.py --once

# Alleen Turso (zonder pods.json)
python3 pipeline/monitor_pods.py --format-id "uuid"
```

### 5. Verificatie
```bash
# Standaard verificatie
python3 pipeline/verify_db.py --format-id "uuid"

# Met audio-URL check
python3 pipeline/verify_db.py --format-id "uuid" --check-audio

# Alle formats tonen
python3 pipeline/verify_db.py --list-formats

# Rapport opslaan
python3 pipeline/verify_db.py --format-id "uuid" --output verify_report.json
```

---

## Scripts

| Script | Doel |
|--------|------|
| `launch_pods.py` | Start N RunPod pods, verdeelt RSS-episodes over pods |
| `monitor_pods.py` | Poll pod-status + Turso voortgang, stuur Telegram-notificatie |
| `verify_db.py` | Controleer data-kwaliteit (timestamps, words, audio-URLs) |
| `create_indexes.py` | Eenmalige Turso-index aanmaak (verplicht na import) |
| `turso_direct.py` | Herbruikbare Turso HTTP-client (Hrana v2, geen Python adapter) |

---

## Turso: waarom direct HTTP?

De Python `libsql`-adapter heeft een interne verbindingstimeout die te kort is voor:
- `CREATE INDEX` op grote tabellen (30-60s) → timeout → index niet aangemaakt
- Queries op grote tabellen zonder index → full scan → timeout

**Oplossing:** gebruik `turso_direct.py` (Hrana v2 protocol direct via `httpx`).
Dit geeft volledige controle over timeouts en is stabiel op grote datasets.

### Performance benchmark (na index)

| Query | Zonder index | Met index |
|-------|-------------|-----------|
| `SELECT ... WHERE item_id = ?` | 30s+ (timeout) | <1s |
| `SELECT ... WHERE format_id = ?` | ~5s | <1s |
| `CREATE INDEX` | 30-120s via HTTP | — |

---

## GPU-keuze en kosten

| GPU | VRAM | Snelheid | Prijs/uur |
|-----|------|----------|-----------|
| RTX A5000 | 24GB | ~50× realtime | ~$0.16 |
| RTX 3090 | 24GB | ~48-52× realtime | ~$0.22 |
| RTX 4090 | 24GB | ~60× realtime | ~$0.44 |
| A100 40GB | 40GB | ~80× realtime | ~$1.64 |

**Aanbevolen:** RTX A5000 (goedkoopste, voldoende snel)

**Schaalberekening:** 128 uur podcast → 3 × RTX 3090 → ~2.7 uur → ~$0.66 totaal

---

## Parallelisatie: N pods

`launch_pods.py` verdeelt episodes automatisch:
- 126 eps / 3 pods = pods 0-41, 42-83, 84-125 (via `--offset`/`--limit`)
- Dedupe in Turso: `INSERT OR IGNORE` + check op `source_url` — veilig om te herdraaien
- Pods termineren zichzelf na afloop (`--auto-terminate`)

### Hervatten na fout
Pods die stoppen worden automatisch overgeslagen dankzij dedupe.
Herstart gewoon met dezelfde `--format-id`:
```bash
python3 pipeline/launch_pods.py --rss "..." --format-id "uuid" --pods 3
```

---

## Troubleshooting

**Pod start niet:**
- Check `pods.json` voor error-melding
- Controleer RUNPOD_API_KEY
- GPU mogelijk niet beschikbaar: probeer ander type (`--gpu RTX_3090`)

**Turso-queries timen out:**
- Voer `create_indexes.py` uit (eenmalig)
- Gebruik `turso_direct.py` i.p.v. de Python adapter

**Items blijven "pending":**
- SSH in pod: `ssh root@IP -p PORT`
- Check log: `tail -f /workspace/pod_log.txt`
- Controleer env vars op pod: `env | grep TURSO`

**Fragmenten leeg (words = []):**
- faster-whisper soms geen word-timestamps bij zeer korte segmenten
- Controleer met `verify_db.py --check-audio`
