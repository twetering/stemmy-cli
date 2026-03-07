# Database Setup

Stemmy CLI uses SQLite (or Turso/libSQL) for storage. No authentication required.

## Quick Start

1. **Use existing database**: Point `STEMMY_DB_PATH` to your `stemmy.db` (e.g. from surrounded).
2. **Empty database**: Create `data/stemmy.db` and run schema from `docs/schema.sql`.
3. **Turso cloud**: Set `TURSO_DB_URL` and `TURSO_API_KEY` for sync and remote access.

## Database Location

The CLI looks for a database in this order:

1. `STEMMY_DB_PATH` or `VOXPOP_DB_PATH` (env)
2. `{project_root}/data/stemmy.db`
3. `./data/stemmy.db`
4. `./stemmy.db`
5. `~/.stemmy/stemmy.db`

## Tables Used by CLI

| Table | Purpose |
|-------|---------|
| formats | Podcast/show definitions (RSS feeds) |
| items | Episodes per format |
| fragments | Audio segments with text, timing, words |
| entities | Extracted entities (people, orgs) |
| voices | TTS voice configs |
| sounds | Sound effects, jingles |
| stemmies | Generated compilations |
| fragment_embeddings | Semantic search (optional) |

Other tables (users, profiles, stars, shares, etc.) exist for web app compatibility but are not used by the CLI.

## Creating a Fresh Database

```bash
# Create data dir
mkdir -p data

# Apply schema
sqlite3 data/stemmy.db < docs/schema.sql

# Or from Python
python -c "
import sqlite3
with open('docs/schema.sql') as f:
    sqlite3.connect('data/stemmy.db').executescript(f.read())
"
```

## Populating Data

- **RSS import**: `stemmy formats add <rss-url>` then `stemmy items fetch`
- **Transcripts**: Items need `transcript_data` and `transcript_status=completed`
- **Fragments**: Created from transcripts; run `stemmy db migrate-words` for word-level timing
- **Embeddings**: `stemmy embeddings index` for semantic search

## Turso Sync

To sync local SQLite to Turso:

```bash
# 1. Install Turso CLI: brew install tursodatabase/tap/turso
# 2. Create database: turso db create stemmy
# 3. Set env: TURSO_DB_URL, TURSO_API_KEY
# 4. Sync
stemmy db sync-turso
```

For large databases (1GB+), sync can take 10+ minutes.
