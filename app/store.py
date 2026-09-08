"""去重存储:sqlite 单表 pushed(code PK, pushed_at, title)。"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path


class Store:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS pushed ("
            " code TEXT PRIMARY KEY, pushed_at REAL, title TEXT)"
        )
        self._conn.commit()

    def is_pushed(self, code: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM pushed WHERE code = ?", (code,)
        ).fetchone()
        return row is not None

    def mark_pushed(self, code: str, title: str = "") -> None:
        self._conn.execute(
            "INSERT INTO pushed(code, pushed_at, title) VALUES(?,?,?) "
            "ON CONFLICT(code) DO UPDATE SET pushed_at=excluded.pushed_at, title=excluded.title",
            (code, time.time(), title),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
