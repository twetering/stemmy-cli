# Stemmy CLI

Professional command-line interface for the Stemmy audio content platform. Create compilations from podcast fragments, search semantically, and mix with background music—all from the terminal.

## Features

- **Formats & items** – Add RSS feeds, fetch episodes, manage transcripts
- **Fragments** – Browse, search, and filter audio segments
- **Compilations** – Build MP3 compilations from fragments (with optional background music)
- **Semantic search** – Find fragments by meaning (requires embeddings)
- **Turso support** – Sync local SQLite to Turso cloud for collaboration
- **Interactive chat** – Natural language commands via `stemmy chat`

## Installation

### From source

```bash
git clone https://github.com/stemmy/stemmy-cli.git
cd stemmy-cli
pip install -e .
# or: pip install -r requirements.txt && PYTHONPATH=src stemmy --help
```

### Requirements

- Python 3.10+
- SQLite (or Turso for cloud sync)
- Optional: OpenAI/Anthropic API keys for chat and embeddings

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

## Configuration

| Variable | Description |
|----------|-------------|
| `STEMMY_DB_PATH` | Path to SQLite database (default: `data/stemmy.db`) |
| `TURSO_DB_URL` | Turso database URL (optional) |
| `TURSO_API_KEY` | Turso auth token (optional) |
| `OPENAI_API_KEY` | For embeddings and chat (optional) |
| `ANTHROPIC_API_KEY` | Alternative for chat (optional) |

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

## License

MIT
