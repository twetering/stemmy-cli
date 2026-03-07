"""Database connection tests."""

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest


def test_sqlite_connection(env_with_db):
    """get_connection returns SQLite when Turso not configured."""
    from stemmy_cli.adapters.connection import get_connection

    conn = get_connection(prefer_turso=True)
    assert conn is not None
    assert conn.is_turso is False
    row = conn.fetchone("SELECT COUNT(*) FROM formats")
    assert row[0] == 1


def test_database_connection_execute(env_with_db):
    """Connection can execute queries."""
    from stemmy_cli.adapters.connection import get_connection

    conn = get_connection(prefer_turso=False)
    rows = conn.fetchall("SELECT id, title FROM formats")
    assert len(rows) == 1
    assert rows[0][1] == "Test Format"


def test_connection_without_db_raises(monkeypatch):
    """get_connection raises when no database exists."""
    import stemmy_cli.config as config
    from stemmy_cli.adapters.connection import get_sqlite_connection

    monkeypatch.setenv("STEMMY_DB_PATH", "/nonexistent/path/stemmy.db")
    monkeypatch.delenv("TURSO_DB_URL", raising=False)
    monkeypatch.delenv("TURSO_API_KEY", raising=False)
    config._config = None
    with pytest.raises(FileNotFoundError):
        get_sqlite_connection()
