"""
Database connection factory with Turso cloud support.

Provides a unified interface for database connections that can use:
1. Turso cloud database with local embedded replica (preferred)
2. Local SQLite database (fallback)

The embedded replica provides:
- Local read speed (no network latency for queries)
- Automatic sync to cloud
- Offline capability with later sync
"""

import os
import sqlite3
from pathlib import Path
from typing import Any, Optional

from stemmy_cli.config import get_database_path


# Try to import libsql for Turso support (prefer official libsql over libsql-experimental)
try:
    import libsql
    LIBSQL_AVAILABLE = True
except ImportError:
    try:
        import libsql_experimental as libsql
        LIBSQL_AVAILABLE = True
    except ImportError:
        LIBSQL_AVAILABLE = False


class DatabaseConnection:
    """Unified database connection wrapper."""
    
    def __init__(self, conn: Any, is_turso: bool = False):
        self._conn = conn
        self._is_turso = is_turso
    
    @property
    def is_turso(self) -> bool:
        return self._is_turso
    
    def execute(self, sql: str, params: tuple = ()) -> Any:
        cursor = self._conn.cursor()
        cursor.execute(sql, tuple(params) if params else ())
        return cursor
    
    def executemany(self, sql: str, params_list: list) -> Any:
        cursor = self._conn.cursor()
        cursor.executemany(sql, params_list)
        return cursor
    
    def fetchall(self, sql: str, params: tuple = ()) -> list:
        cursor = self.execute(sql, params)
        return cursor.fetchall()
    
    def fetchone(self, sql: str, params: tuple = ()) -> Optional[Any]:
        cursor = self.execute(sql, params)
        return cursor.fetchone()
    
    def commit(self):
        self._conn.commit()
    
    def rollback(self):
        self._conn.rollback()
    
    def close(self):
        self._conn.close()
    
    def sync(self):
        """Sync embedded replica with cloud (Turso only)."""
        if self._is_turso and hasattr(self._conn, 'sync'):
            self._conn.sync()
    
    def cursor(self) -> Any:
        return self._conn.cursor()
    
    @property
    def row_factory(self):
        return self._conn.row_factory
    
    @row_factory.setter
    def row_factory(self, value):
        self._conn.row_factory = value
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


def get_turso_connection() -> Optional[DatabaseConnection]:
    """
    Try to establish a Turso cloud connection with embedded replica.
    
    Returns None if Turso is not configured or available.
    """
    if not LIBSQL_AVAILABLE:
        return None
    
    # Support both stemmy_cli and Turso docs env var names
    turso_url = os.environ.get("TURSO_DB_URL") or os.environ.get("TURSO_DATABASE_URL")
    turso_token = os.environ.get("TURSO_API_KEY") or os.environ.get("TURSO_AUTH_TOKEN")

    if not turso_url or not turso_token:
        return None
    
    try:
        # Local replica path
        from stemmy_cli.paths import get_project_root
        replica_dir = get_project_root() / "data"
        replica_dir.mkdir(parents=True, exist_ok=True)
        replica_path = str(replica_dir / "stemmy-replica.db")
        
        conn = libsql.connect(
            replica_path,
            sync_url=turso_url,
            auth_token=turso_token,
        )
        
        # Initial sync
        conn.sync()
        
        return DatabaseConnection(conn, is_turso=True)
    
    except Exception as e:
        print(f"[warning] Turso connection failed: {e}")
        return None


def get_sqlite_connection() -> DatabaseConnection:
    """
    Get a local SQLite connection.
    """
    db_path = get_database_path()
    
    if not db_path.exists():
        raise FileNotFoundError(
            f"Database not found at {db_path}. "
            "Set STEMMY_DB_PATH environment variable or ensure data/stemmy.db exists."
        )
    
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    
    return DatabaseConnection(conn, is_turso=False)


def get_connection(prefer_turso: bool = True) -> DatabaseConnection:
    """
    Get the best available database connection.
    
    Args:
        prefer_turso: If True, try Turso first, then fallback to SQLite
    
    Returns:
        DatabaseConnection wrapper
    """
    # Load .env before checking Turso (config loads from cwd, project root, ~/.stemmy)
    from stemmy_cli.config import get_config
    get_config()

    if prefer_turso:
        turso_conn = get_turso_connection()
        if turso_conn:
            return turso_conn
    
    return get_sqlite_connection()


def sync_to_turso(replace: bool = False):
    """
    Sync full local SQLite database to Turso cloud.

    Uses Turso CLI `db import` (official bulk import) when available.
    Falls back to libsql replica sync otherwise.
    """
    import subprocess

    # Ensure .env is loaded
    get_database_path()

    turso_url = os.environ.get("TURSO_DB_URL") or os.environ.get("TURSO_DATABASE_URL")
    turso_token = os.environ.get("TURSO_API_KEY") or os.environ.get("TURSO_AUTH_TOKEN")

    if not turso_url or not turso_token:
        raise RuntimeError("Set TURSO_DB_URL (or TURSO_DATABASE_URL) and TURSO_API_KEY (or TURSO_AUTH_TOKEN)")

    local_db = get_database_path()
    if not local_db.exists():
        raise FileNotFoundError(f"Local database not found: {local_db}")

    db_name = local_db.stem  # e.g. stemmy

    # 1. Ensure WAL mode (required for Turso import)
    print("  Preparing SQLite (WAL mode)...", flush=True)
    prep_conn = sqlite3.connect(str(local_db))
    prep_conn.execute("PRAGMA journal_mode=WAL")
    prep_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    prep_conn.close()

    # 2. Try Turso CLI import (official, reliable)
    turso_path = subprocess.run(["which", "turso"], capture_output=True, text=True)
    if turso_path.returncode == 0 and turso_path.stdout.strip():
        if replace:
            print(f"  Destroying existing {db_name}...", flush=True)
            subprocess.run(["turso", "db", "destroy", db_name, "-y"], capture_output=True, timeout=30)
        print("  Using Turso CLI import (recommended)...", flush=True)
        result = subprocess.run(
            ["turso", "db", "import", str(local_db)],
            capture_output=True,
            text=True,
            timeout=3600,  # 1hr for large DBs (1GB+)
        )
        if result.returncode == 0:
            print("  Import complete.", flush=True)
            # Verify
            verify = subprocess.run(
                ["turso", "db", "shell", db_name, "--", "SELECT COUNT(*) FROM fragments"],
                capture_output=True,
                text=True,
                timeout=30,
                env={**os.environ, "TURSO_DATABASE_URL": turso_url, "TURSO_AUTH_TOKEN": turso_token},
            )
            if verify.returncode == 0:
                print("  Verify fragments count:", verify.stdout.strip() or "(ok)", flush=True)
            return {"success": True, "method": "turso_cli_import"}
        else:
            err = result.stderr or result.stdout
            if "already exists" in err.lower() or "exists" in err.lower():
                print(f"  Database '{db_name}' already exists. Run: stemmy db sync-turso --replace", flush=True)
            else:
                print(f"  Turso CLI failed: {err[:500]}", flush=True)
            # Fall through to libsql

    # 3. Fallback: libsql replica sync
    print("  Using libsql replica sync...", flush=True)
    if not LIBSQL_AVAILABLE:
        raise RuntimeError("libsql not installed. pip install libsql. Or use Turso CLI: brew install tursodatabase/tap/turso")

    replica_path = str(local_db.parent / "stemmy-turso-sync.db")
    if Path(replica_path).exists():
        Path(replica_path).unlink()  # Fresh replica to avoid "stream not found"

    conn = libsql.connect(
        replica_path,
        sync_url=turso_url,
        auth_token=turso_token,
    )

    local_conn = sqlite3.connect(str(local_db))
    local_conn.row_factory = sqlite3.Row

    # Schema
    for name, sql in local_conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND sql IS NOT NULL"
    ).fetchall():
        try:
            conn.execute(sql)
        except Exception:
            pass
    for (sql,) in local_conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%' AND sql IS NOT NULL"
    ).fetchall():
        try:
            conn.execute(sql)
        except Exception:
            pass
    conn.commit()

    # Data
    tables = local_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    total_ok, total_fail = 0, 0

    for table_row in tables:
        table_name = table_row[0]
        rows = local_conn.execute(f"SELECT * FROM {table_name}").fetchall()
        if not rows:
            continue

        cursor = local_conn.execute(f"SELECT * FROM {table_name} LIMIT 1")
        cols = [d[0] for d in cursor.description]
        sql = f"INSERT OR REPLACE INTO {table_name} ({','.join(cols)}) VALUES ({','.join(['?']*len(cols))})"

        ok, fail = 0, 0
        for i in range(0, len(rows), 2000):
            for row in rows[i : i + 2000]:
                try:
                    conn.execute(sql, tuple(row))
                    ok += 1
                except Exception as e:
                    fail += 1
                    if fail <= 2:
                        print(f"  [warn] {table_name}: {e}", flush=True)
            print(f"  {table_name}: {min(i+2000,len(rows)):,}/{len(rows):,}", end="\r", flush=True)
        total_ok += ok
        total_fail += fail
        print(f"  {table_name}: {ok:,} rows" + (f" ({fail} failed)" if fail else ""), flush=True)

    conn.commit()
    print("  Syncing to Turso...", flush=True)
    conn.sync()
    local_conn.close()
    conn.close()

    return {"success": True, "method": "libsql", "rows_inserted": total_ok, "rows_failed": total_fail}
