"""
turso_direct.py — Lichtgewicht Turso/libSQL HTTP client (Hrana v2).

Gebruik dit altijd i.p.v. de Python libsql adapter voor pipeline-scripts.
De adapter heeft een te korte interne timeout voor grote DDL-operaties (CREATE INDEX).
Hrana v2 direct geeft volledige controle over timeout + geen hidden reconnect-logica.

Gebruik:
    from turso_direct import TursoDirect

    db = TursoDirect()                    # leest TURSO_URL + TURSO_TOKEN uit env
    rows = db.execute("SELECT ...")
    db.execute_batch([...])              # bulk inserts, 200 per HTTP-call
    db.execute_ddl("CREATE INDEX ...")   # lange timeout (120s)
"""

import json
import os
from typing import Any

import httpx


class TursoDirect:
    def __init__(
        self,
        url: str = None,
        token: str = None,
        chunk_size: int = 200,
    ):
        raw_url = url or os.environ.get("TURSO_URL") or os.environ.get("TURSO_DB_URL")
        if not raw_url:
            raise ValueError("TURSO_URL env var niet gezet")
        self.base_url = raw_url.replace("libsql://", "https://")
        self.token = token or os.environ.get("TURSO_TOKEN") or os.environ.get("TURSO_API_KEY")
        if not self.token:
            raise ValueError("TURSO_TOKEN env var niet gezet")
        self.chunk_size = chunk_size

    @property
    def _headers(self):
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def _val(self, v: Any) -> dict:
        """Python waarde → Hrana typed value."""
        if v is None:
            return {"type": "null"}
        if isinstance(v, bool):
            return {"type": "integer", "value": str(int(v))}
        if isinstance(v, int):
            return {"type": "integer", "value": str(v)}
        if isinstance(v, float):
            return {"type": "float", "value": str(v)}
        return {"type": "text", "value": str(v)}

    def _parse_result(self, result: dict) -> list[dict]:
        if result.get("type") == "error":
            raise RuntimeError(f"Turso SQL fout: {result['error']}")
        rs = result.get("response", {}).get("result", {})
        cols = [c["name"] for c in rs.get("cols", [])]
        return [
            {col: (cell["value"] if cell["type"] != "null" else None)
             for col, cell in zip(cols, row)}
            for row in rs.get("rows", [])
        ]

    def execute(self, sql: str, params: list = None, timeout: float = 30.0) -> list[dict]:
        """Voer één SQL-statement uit. Retourneert rijen als list[dict]."""
        stmt = {"sql": sql}
        if params:
            stmt["args"] = [self._val(p) for p in params]
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                f"{self.base_url}/v2/pipeline",
                headers=self._headers,
                json={"requests": [{"type": "execute", "stmt": stmt}, {"type": "close"}]},
            )
        resp.raise_for_status()
        return self._parse_result(resp.json()["results"][0])

    def execute_ddl(self, sql: str, timeout: float = 120.0) -> None:
        """DDL-operaties (CREATE INDEX etc.) met langere timeout."""
        self.execute(sql, timeout=timeout)

    def execute_batch(self, statements: list[dict], timeout_per_chunk: float = 60.0) -> int:
        """
        Bulk-uitvoer van meerdere statements in batches van chunk_size.
        statements = [{"sql": "...", "args": [...]}, ...]
        Retourneert totaal aantal uitgevoerde statements.
        """
        total = 0
        with httpx.Client(timeout=timeout_per_chunk) as client:
            for i in range(0, len(statements), self.chunk_size):
                chunk = statements[i: i + self.chunk_size]
                requests = [{"type": "execute", "stmt": s} for s in chunk]
                requests.append({"type": "close"})
                resp = client.post(
                    f"{self.base_url}/v2/pipeline",
                    headers=self._headers,
                    json={"requests": requests},
                )
                resp.raise_for_status()
                for j, result in enumerate(resp.json().get("results", [])[:-1]):
                    if result.get("type") == "error":
                        print(f"  [warn] batch[{i+j}]: {result['error']}")
                total += len(chunk)
        return total

    def upsert(self, table: str, data: dict, on_conflict: str = "IGNORE") -> None:
        """INSERT OR {IGNORE/REPLACE} helper."""
        cols = list(data.keys())
        ph = ", ".join("?" * len(cols))
        sql = f"INSERT OR {on_conflict} INTO {table} ({', '.join(cols)}) VALUES ({ph})"
        self.execute(sql, [data[c] for c in cols])

    def ensure_indexes(self):
        """
        Maak alle benodigde indexes aan als ze nog niet bestaan.
        Veilig om meerdere keren te draaien (IF NOT EXISTS).
        Gebruik lange timeout — kan 30-60s duren op grote tabel.
        """
        indexes = [
            ("idx_fragments_item_id",
             "CREATE INDEX IF NOT EXISTS idx_fragments_item_id ON fragments(item_id)"),
            ("idx_items_format_status",
             "CREATE INDEX IF NOT EXISTS idx_items_format_status ON items(format_id, transcript_status)"),
            ("idx_items_source_url",
             "CREATE INDEX IF NOT EXISTS idx_items_source_url ON items(source_url)"),
        ]
        for name, ddl in indexes:
            print(f"  Index: {name}...", flush=True)
            self.execute_ddl(ddl, timeout=120.0)
            print(f"  ✅ {name}", flush=True)
