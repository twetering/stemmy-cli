"""Pytest configuration and fixtures."""

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_db():
    """Create a temporary SQLite database with minimal schema."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE formats (id TEXT, title TEXT, source_type TEXT);
        CREATE TABLE items (id TEXT, format_id TEXT, title TEXT);
        CREATE TABLE fragments (id TEXT, item_id TEXT, text TEXT, words TEXT);
        CREATE TABLE entities (id TEXT, item_id TEXT, text TEXT);
        INSERT INTO formats VALUES ('f1', 'Test Format', 'rss');
        INSERT INTO items VALUES ('i1', 'f1', 'Test Item');
        INSERT INTO fragments VALUES ('fr1', 'i1', 'Hello world', '[]');
    """)
    conn.commit()
    conn.close()
    yield path
    Path(path).unlink(missing_ok=True)


@pytest.fixture
def env_with_db(tmp_db, monkeypatch):
    """Set STEMMY_DB_PATH to tmp database, unset Turso to force SQLite."""
    monkeypatch.setenv("STEMMY_DB_PATH", tmp_db)
    monkeypatch.delenv("TURSO_DB_URL", raising=False)
    monkeypatch.delenv("TURSO_API_KEY", raising=False)
    # Clear cached config so it reloads with new env
    import stemmy_cli.config as config
    config._config = None
    return tmp_db
