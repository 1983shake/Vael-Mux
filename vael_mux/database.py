from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional

from .models import Node


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init(self) -> None:
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS nodes (
                    key TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    protocol TEXT NOT NULL,
                    server TEXT NOT NULL,
                    port INTEGER NOT NULL,
                    params TEXT NOT NULL,
                    source TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS status (
                    key TEXT PRIMARY KEY,
                    alive INTEGER DEFAULT 0,
                    latency_ms REAL,
                    download_mbps REAL,
                    stability REAL DEFAULT 0,
                    score REAL DEFAULT 0,
                    last_checked REAL,
                    last_error TEXT
                );
                """)

    # ---- nodes ----

    def upsert_nodes(self, nodes: List[Node]) -> None:
        if not nodes:
            return
        rows = [
            (
                n.key,
                n.name,
                n.protocol,
                n.server,
                n.port,
                json.dumps(n.params, ensure_ascii=False),
                n.source,
            )
            for n in nodes
        ]
        with self._conn() as c:
            c.executemany(
                "INSERT OR REPLACE INTO nodes " "(key,name,protocol,server,port,params,source) VALUES (?,?,?,?,?,?,?)",
                rows,
            )

    def all_nodes(self) -> List[Node]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM nodes").fetchall()
        return [
            Node(
                name=r["name"],
                protocol=r["protocol"],
                server=r["server"],
                port=r["port"],
                params=json.loads(r["params"]),
                source=r["source"],
            )
            for r in rows
        ]

    def clear_nodes(self, source: Optional[str] = None) -> None:
        with self._conn() as c:
            if source is None:
                c.execute("DELETE FROM nodes")
            else:
                c.execute("DELETE FROM nodes WHERE source=?", (source,))

    # ---- status ----

    def set_status(self, key: str, **kwargs) -> None:
        valid = [
            "alive",
            "latency_ms",
            "download_mbps",
            "stability",
            "score",
            "last_checked",
            "last_error",
        ]
        data = {k: kwargs[k] for k in valid if k in kwargs}
        if "last_checked" not in data:
            data["last_checked"] = time.time()
        cols = ",".join(data.keys())
        placeholders = ",".join("?" * len(data))
        updates = ",".join(f"{k}=excluded.{k}" for k in data)
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO status (key) VALUES (?)", (key,))
            c.execute(
                f"INSERT INTO status (key,{cols}) VALUES (?,{placeholders}) " f"ON CONFLICT(key) DO UPDATE SET {updates}",
                (key, *data.values()),
            )

    def get_status(self, key: str) -> Optional[Dict]:
        with self._conn() as c:
            r = c.execute("SELECT * FROM status WHERE key=?", (key,)).fetchone()
        return dict(r) if r else None

    def all_status(self) -> Dict[str, Dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM status").fetchall()
        return {r["key"]: dict(r) for r in rows}
