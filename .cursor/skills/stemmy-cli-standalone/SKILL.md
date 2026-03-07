# Stemmy CLI Standalone Workflow Skill

Use this skill when running stemmy_cli in **standalone mode**—without the surrounded Flask server. The CLI can import RSS feeds, transcribe via AssemblyAI, and create compilations using only env vars.

## Activation Triggers

- User wants to run stemmy_cli "standalone", "without surrounded", or "in Noord Korea"
- User asks to import RSS episodes and transcribe without a server
- User mentions S3 upload or AssemblyAI direct integration

## Standalone Requirements

Set in `.env`:

| Variable | Purpose |
|----------|---------|
| `AWS_ACCESS_KEY_ID` | S3 upload for normalized audio |
| `AWS_SECRET_ACCESS_KEY` | S3 upload |
| `AWS_REGION` | S3 region (default: eu-north-1) |
| `S3_BUCKET_NAME` | S3 bucket (e.g. voxpop) |
| `ASSEMBLY_API_KEY` | Direct transcription |
| `ELEVENLABS_API_KEY` | Standalone TTS (workflows, mix) |
| `STEMMY_DB_PATH` | SQLite database path |

## Full RSS-to-Compilation Flow

```bash
stemmy workflows rss-to-compilation "https://castopod.hku.nl/@HKUenAI/feed.xml" \
  -v <voice_id> \
  -o ./output \
  -s "Ik ben" \
  --search-mode starts_with \
  -e 10 \
  -f 10
```

**Options:**
- `-s, --search` – Search query for fragments (e.g. "Ik ben")
- `--search-mode` – `starts_with` (zinnen die beginnen met) or `contains`
- `-e, --episodes` – Number of episodes to import
- `-f, --fragments` – Max fragments per compilation
- `--wait/--no-wait` – Wait for transcription (default: yes)

## Step-by-Step (Alternative)

```bash
# 1. Import episodes from RSS
stemmy formats import-episodes <format_id> "https://example.com/feed.xml" -e 10 --normalize

# 2. Transcribe pending items
stemmy workflows transcribe-all <format_id> --wait -l 10

# 3. Create compilation (standalone TTS when ELEVENLABS_API_KEY set)
stemmy workflows rss-to-compilation "..." -v <voice_id> -o ./output -s "Ik ben"
```

## Notes

- **TTS** is standalone via ElevenLabs when `ELEVENLABS_API_KEY` is set.
- **Extract** is standalone via ffmpeg (ParallelExtractor). No surrounded needed.
- **Voice ID**: Run `stemmy voices list` to get a valid voice_id.
- **Database**: Use existing stemmy.db or create fresh with `docs/schema.sql`.
