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

    def recent(self, limit: int = 20) -> list[dict]:
        """最近推送(新→旧)。同时间戳按写入顺序决胜(Windows 时钟精度粗,连推会同戳)。"""
        rows = self._conn.execute(
            "SELECT code, title, pushed_at FROM pushed ORDER BY pushed_at DESC, rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [{"code": c, "title": t or c, "pushed_at": ts} for c, t, ts in rows]

    def stats(self) -> dict:
        """推送统计:今日/累计。"""
        import datetime as _dt

        midnight = _dt.datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()
        today = self._conn.execute(
            "SELECT COUNT(*) FROM pushed WHERE pushed_at >= ?", (midnight,)
        ).fetchone()[0]
        total = self._conn.execute("SELECT COUNT(*) FROM pushed").fetchone()[0]
        return {"today": today, "total": total}

    def close(self) -> None:
        self._conn.close()
