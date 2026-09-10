"""去重存储:sqlite 单表 pushed(code PK, pushed_at, title)。

外加两张表:
- monitor_state(源频道 → 已处理到的消息 ID):首次接入只记起点、不回补历史,此后重启按游标补扫
- pipeline_tasks(流水线任务):审核中的任务落库,重启后继续轮询,不再重复建分享
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

_TASK_FIELDS = (
    "share_code", "receive_code", "fid", "name", "uid", "status", "created_at", "attempts",
)


class Store:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS pushed ("
            " code TEXT PRIMARY KEY, pushed_at REAL, title TEXT)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS monitor_state ("
            " ref TEXT PRIMARY KEY, chat_id TEXT, title TEXT,"
            " last_msg_id INTEGER, updated_at REAL)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS pipeline_tasks ("
            " share_code TEXT PRIMARY KEY, receive_code TEXT, fid INTEGER, name TEXT,"
            " uid INTEGER, status TEXT, created_at REAL, attempts INTEGER)"
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

    # ── 频道监控游标 ────────────────────────────────────────
    def get_monitor_state(self, ref: str) -> dict | None:
        row = self._conn.execute(
            "SELECT ref, chat_id, title, last_msg_id, updated_at FROM monitor_state WHERE ref = ?",
            (ref,),
        ).fetchone()
        if row is None:
            return None
        return {
            "ref": row[0], "chat_id": row[1] or "", "title": row[2] or "",
            "last_msg_id": int(row[3] or 0), "updated_at": float(row[4] or 0.0),
        }

    def set_monitor_state(
        self, ref: str, last_msg_id: int, *, chat_id: str = "", title: str = ""
    ) -> None:
        """推进游标;chat_id/title 只在解析成功时覆盖(空值保留旧值)。"""
        self._conn.execute(
            "INSERT INTO monitor_state(ref, chat_id, title, last_msg_id, updated_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(ref) DO UPDATE SET"
            " chat_id=CASE WHEN excluded.chat_id != '' THEN excluded.chat_id ELSE chat_id END,"
            " title=CASE WHEN excluded.title != '' THEN excluded.title ELSE title END,"
            " last_msg_id=MAX(last_msg_id, excluded.last_msg_id),"
            " updated_at=excluded.updated_at",
            (ref, chat_id, title, int(last_msg_id), time.time()),
        )
        self._conn.commit()

    def remove_monitor_state(self, ref: str) -> None:
        self._conn.execute("DELETE FROM monitor_state WHERE ref = ?", (ref,))
        self._conn.commit()

    def monitor_states(self) -> list[dict]:
        """全部监控频道游标(按加入顺序稳定展示)。"""
        rows = self._conn.execute(
            "SELECT ref, chat_id, title, last_msg_id, updated_at FROM monitor_state ORDER BY rowid"
        ).fetchall()
        return [
            {"ref": r[0], "chat_id": r[1] or "", "title": r[2] or "",
             "last_msg_id": int(r[3] or 0), "updated_at": float(r[4] or 0.0)}
            for r in rows
        ]

    # ── 流水线任务(审核轮询状态,重启不丢) ──────────────────
    def save_pipeline_task(self, task: dict) -> None:
        """写/更新一条流水线任务(share_code 为主键,幂等)。"""
        self._conn.execute(
            "INSERT INTO pipeline_tasks"
            " (share_code, receive_code, fid, name, uid, status, created_at, attempts)"
            " VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(share_code) DO UPDATE SET"
            " receive_code=excluded.receive_code, fid=excluded.fid, name=excluded.name,"
            " uid=excluded.uid, status=excluded.status, created_at=excluded.created_at,"
            " attempts=excluded.attempts",
            (
                str(task["share_code"]), str(task.get("receive_code", "") or ""),
                int(task.get("fid", 0) or 0), str(task.get("name", "") or ""),
                int(task.get("uid", 0) or 0), str(task.get("status", "auditing")),
                float(task.get("created_at", time.time())), int(task.get("attempts", 0) or 0),
            ),
        )
        self._conn.commit()

    def load_pipeline_tasks(self, statuses: tuple[str, ...] = ("auditing",)) -> list[dict]:
        """按状态取任务(默认审核中),按创建时间正序。"""
        marks = ",".join("?" * len(statuses))
        rows = self._conn.execute(
            f"SELECT {', '.join(_TASK_FIELDS)} FROM pipeline_tasks"
            f" WHERE status IN ({marks}) ORDER BY created_at",
            tuple(statuses),
        ).fetchall()
        return [dict(zip(_TASK_FIELDS, r, strict=False)) for r in rows]

    def pipeline_task_stats(self) -> dict:
        """任务计数(按状态)——状态展示用。"""
        rows = self._conn.execute(
            "SELECT status, COUNT(*) FROM pipeline_tasks GROUP BY status"
        ).fetchall()
        return {s: n for s, n in rows}

    def close(self) -> None:
        self._conn.close()
