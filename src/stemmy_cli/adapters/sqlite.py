"""SQLite adapter for direct database access."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

from stemmy_cli.config import get_database_path


class SQLiteAdapter:
    """Direct SQLite access for fast queries."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or get_database_path()

    @contextmanager
    def connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Get a database connection."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _rows_to_dicts(self, rows: List[sqlite3.Row]) -> List[Dict[str, Any]]:
        """Convert sqlite3.Row objects to dictionaries."""
        return [dict(row) for row in rows]

    def query(
        self,
        table: str,
        where: Optional[Dict[str, Any]] = None,
        order_by: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        select: str = "*",
    ) -> List[Dict[str, Any]]:
        """Execute a query on a table."""
        conditions = []
        params = []

        if where:
            for key, value in where.items():
                if value is None:
                    conditions.append(f'"{key}" IS NULL')
                else:
                    conditions.append(f'"{key}" = ?')
                    params.append(value)

        where_clause = " AND ".join(conditions) if conditions else "1=1"
        order_clause = f' ORDER BY "{order_by}"' if order_by else ""

        query = f'SELECT {select} FROM "{table}" WHERE {where_clause}{order_clause} LIMIT ? OFFSET ?'
        params.extend([limit, offset])

        with self.connection() as conn:
            cursor = conn.execute(query, params)
            return self._rows_to_dicts(cursor.fetchall())

    def get_by_id(self, table: str, id_value: str) -> Optional[Dict[str, Any]]:
        """Get a single record by ID."""
        results = self.query(table, where={"id": id_value}, limit=1)
        return results[0] if results else None

    def search_text(
        self,
        table: str,
        column: str,
        query: str,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Search for text in a column using LIKE."""
        sql = f'SELECT * FROM "{table}" WHERE "{column}" LIKE ? LIMIT ?'
        params = [f"%{query}%", limit]

        with self.connection() as conn:
            cursor = conn.execute(sql, params)
            return self._rows_to_dicts(cursor.fetchall())

    def count(self, table: str, where: Optional[Dict[str, Any]] = None) -> int:
        """Count records in a table."""
        conditions = []
        params = []

        if where:
            for key, value in where.items():
                if value is None:
                    conditions.append(f'"{key}" IS NULL')
                else:
                    conditions.append(f'"{key}" = ?')
                    params.append(value)

        where_clause = " AND ".join(conditions) if conditions else "1=1"
        sql = f'SELECT COUNT(*) FROM "{table}" WHERE {where_clause}'

        with self.connection() as conn:
            cursor = conn.execute(sql, params)
            return cursor.fetchone()[0]

    def list_tables(self) -> List[str]:
        """List all tables in the database."""
        with self.connection() as conn:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            return [row["name"] for row in cursor.fetchall()]

    def get_table_info(self, table: str) -> List[Dict[str, Any]]:
        """Get column information for a table."""
        with self.connection() as conn:
            cursor = conn.execute(f'PRAGMA table_info("{table}")')
            return self._rows_to_dicts(cursor.fetchall())

    def execute_raw(self, sql: str, params: Optional[List[Any]] = None) -> List[Dict[str, Any]]:
        """Execute raw SQL query."""
        with self.connection() as conn:
            cursor = conn.execute(sql, params or [])
            if cursor.description:
                return self._rows_to_dicts(cursor.fetchall())
            conn.commit()
            return []

    def insert(
        self,
        table: str,
        data: Dict[str, Any],
    ) -> str:
        """Insert a new record into a table."""
        if not data:
            raise ValueError("No data provided for insert")

        columns = []
        placeholders = []
        params = []
        
        for key, value in data.items():
            columns.append(f'"{key}"')
            placeholders.append("?")
            if isinstance(value, (dict, list)):
                params.append(json.dumps(value))
            else:
                params.append(value)

        sql = f'INSERT INTO "{table}" ({", ".join(columns)}) VALUES ({", ".join(placeholders)})'

        with self.connection() as conn:
            conn.execute(sql, params)
            conn.commit()
            return data.get("id", "")

    def update(
        self,
        table: str,
        id_value: str,
        data: Dict[str, Any],
    ) -> bool:
        """Update a record by ID."""
        if not data:
            return False

        set_parts = []
        params = []
        for key, value in data.items():
            set_parts.append(f'"{key}" = ?')
            if isinstance(value, (dict, list)):
                params.append(json.dumps(value))
            else:
                params.append(value)

        params.append(id_value)
        sql = f'UPDATE "{table}" SET {", ".join(set_parts)} WHERE "id" = ?'

        with self.connection() as conn:
            cursor = conn.execute(sql, params)
            conn.commit()
            return cursor.rowcount > 0
