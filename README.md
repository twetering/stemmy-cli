# Stemmy CLI

Professional command-line interface for the Stemmy audio content platform. Create compilations from podcast fragments, search semantically, and mix with background music—all from the terminal.

## Features

- **Formats & items** – Add RSS feeds, fetch episodes, manage transcripts
- **Fragments** – Browse, search, and filter audio segments
- **Compilations** – Build MP3 compilations from fragments (with optional background music)
- **Semantic search** – Find fragments by meaning (requires embeddings)
- **Turso support** – Sync local SQLite to Turso cloud for collaboration
- **Interactive chat** – Natural language commands via `stemmy chat`
- **Standalone mode** – RSS→compilation without surrounded (S3, AssemblyAI, ElevenLabs, ffmpeg)

## Installation

### From source

```bash
git clone https://github.com/twetering/stemmy-cli.git
cd stemmy-cli
pip install -e .
# or: pip install -r requirements.txt && PYTHONPATH=src stemmy --help
```

### Requirements

- Python 3.10+
- SQLite (or Turso for cloud sync)
- Optional: OpenAI/Anthropic API keys for chat and embeddings
- For standalone: ffmpeg, AWS credentials, AssemblyAI key, ElevenLabs key

## Quick Start

```bash
# Copy env template
cp .env.example .env
# Edit .env: set STEMMY_DB_PATH to your stemmy.db

# Check connection
stemmy db info

# List formats
stemmy formats list

# Create a compilation
stemmy compilations quick --query "AI discussions"
# Or: stemmy compilations run --format <format-id> --query "..."
```

## Standalone RSS-to-Compilation

Run the full pipeline without the surrounded Flask server:

```bash
# Set in .env: AWS_*, S3_BUCKET_NAME, ASSEMBLY_API_KEY, ELEVENLABS_API_KEY

# Full flow: RSS → import episodes → transcribe → search → compilation
stemmy workflows rss-to-compilation "https://example.com/feed.xml" \
  -v <voice_id> \
  -o ./output \
  -s "Ik ben" \
  --search-mode starts_with \
  -e 10 \
  -f 10
```

- `-s "Ik ben"` – search query (e.g. sentences starting with "Ik ben")
- `--search-mode starts_with` – match prefix (or `contains`)
- `-e 10` – number of episodes to import
- `-f 10` – number of fragments per compilation

List voices and pick one: `stemmy voices list`

## Configuration

| Variable | Description |
|----------|-------------|
| `STEMMY_DB_PATH` | Path to SQLite database (default: `data/stemmy.db`) |
| `TURSO_DB_URL` | Turso database URL (optional) |
| `TURSO_API_KEY` | Turso auth token (optional) |
| `OPENAI_API_KEY` | For embeddings and chat (optional) |
| `ANTHROPIC_API_KEY` | Alternative for chat (optional) |
| `AWS_ACCESS_KEY_ID` | For standalone S3 upload (optional) |
| `AWS_SECRET_ACCESS_KEY` | For standalone S3 upload (optional) |
| `AWS_REGION` | S3 region (default: eu-north-1) |
| `S3_BUCKET_NAME` | S3 bucket for audio (optional) |
| `ASSEMBLY_API_KEY` | For standalone transcription (optional) |

See [.env.example](.env.example) for all options.

## Database

The CLI uses SQLite (or Turso). You need a database with the Stemmy schema. Options:

1. **Use existing DB** – Point `STEMMY_DB_PATH` to your `stemmy.db` (e.g. from [surrounded](https://github.com/stemmy/surrounded) or VoxPop).
2. **Fresh DB** – Create `data/stemmy.db` and apply `docs/schema.sql`.
3. **Turso** – Sync local DB with `stemmy db sync-turso`.

See [docs/DATABASE.md](docs/DATABASE.md) for details.

## Commands

```bash
stemmy --help
stemmy formats --help
stemmy items --help
stemmy fragments --help
stemmy compilations --help
stemmy db --help
stemmy chat
```

## Project Structure

```
stemmy_cli/
├── src/stemmy_cli/     # Main package
├── scripts/            # Migration scripts (e.g. migrate-words)
├── data/               # Database (gitignored)
├── output/             # Generated MP3s (gitignored)
├── docs/               # Schema, database docs
└── static/music/       # Bundled background music (in package)
```

## Development

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

## License

MIT
