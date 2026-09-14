"""去重存储:sqlite 单表 pushed(code PK, pushed_at, title)。

外加两张表:
- monitor_state(源频道 → 已处理到的消息 ID):首次接入只记起点、不回补历史,此后重启按游标补扫
- pipeline_tasks(流水线任务):审核中的任务落库,重启后继续轮询,不再重复建分享
- fetch_state(获取段):openlist 侧每个源条目的搬运动作与任务 id,防重复提交、支撑重启续跑
- local_files(处理段):本地文件的处理结果(重命名/ed2k/状态),防重复处理。
  status:`processed`(已处理)/`unrecognized`(未识别,待人工)/`failed`(处理失败,待人工)/
  `pushing`(推卡中)/**`gone`(文件已不在落地点——被改名或人工删,记录自动收尾)**。
  ⚠️ 判定"待人工"的两个口径都必须排除 `gone`,否则陈旧记录会让提示永远不消
- upload_tasks(上传段):本地文件的上传(移动)状态,串行闸门与重启续跑用
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

# ed2k 去重 key = 32 位十六进制文件 hash;老数据没有 provider 时靠它兜底判别(115 分享码不会恰好是这个形态)
_ED2K_HASH_RE = re.compile(r"^[0-9A-Fa-f]{32}$")

# 推送来源(入口):这条推送是从项目哪个入口产出的。存英文 key,前端映射中文标签。
SOURCE_LABELS = {
    "manual": "手动推送",
    "channel": "频道监控",
    "process": "处理段",
    "save": "转存流水线",
    "dir_watch": "目录监控",
}
_DEFAULT_SOURCE = "manual"

_TASK_FIELDS = (
    "share_code", "receive_code", "fid", "name", "uid", "status", "created_at", "attempts",
    "source",
)


class Store:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS pushed ("
            " code TEXT PRIMARY KEY, pushed_at REAL, title TEXT, provider TEXT DEFAULT '',"
            " url TEXT DEFAULT '', source TEXT DEFAULT '')"
        )
        # 迁移:老表(建表时还没有这些列)补列;列已存在则跳过。
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(pushed)")}
        if "provider" not in cols:
            self._conn.execute("ALTER TABLE pushed ADD COLUMN provider TEXT DEFAULT ''")
        if "url" not in cols:
            self._conn.execute("ALTER TABLE pushed ADD COLUMN url TEXT DEFAULT ''")
        if "source" not in cols:
            self._conn.execute("ALTER TABLE pushed ADD COLUMN source TEXT DEFAULT ''")
        # 失效撤卡(2026-09-13):msg_ids 存推卡消息的 JSON 数组
        # [{"chat_id": -100xxx, "message_id": 123}, ...](一个链接可能投了多个频道);
        # revoked_at/revoked_reason 记录失效标记(撤卡后写)
        if "msg_ids" not in cols:
            self._conn.execute("ALTER TABLE pushed ADD COLUMN msg_ids TEXT DEFAULT ''")
        if "revoked_at" not in cols:
            self._conn.execute("ALTER TABLE pushed ADD COLUMN revoked_at REAL")
        if "revoked_reason" not in cols:
            self._conn.execute("ALTER TABLE pushed ADD COLUMN revoked_reason TEXT DEFAULT ''")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS monitor_state ("
            " ref TEXT PRIMARY KEY, chat_id TEXT, title TEXT,"
            " last_msg_id INTEGER, updated_at REAL)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS pipeline_tasks ("
            " share_code TEXT PRIMARY KEY, receive_code TEXT, fid INTEGER, name TEXT,"
            " uid INTEGER, status TEXT, created_at REAL, attempts INTEGER,"
            " source TEXT DEFAULT 'save')"
        )
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(pipeline_tasks)")}
        if "source" not in cols:
            self._conn.execute("ALTER TABLE pipeline_tasks ADD COLUMN source TEXT DEFAULT 'save'")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS local_files ("
            " name TEXT PRIMARY KEY, size INTEGER, status TEXT, ed2k TEXT,"
            " tmdb_id INTEGER, error TEXT, updated_at REAL)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS upload_tasks ("
            " name TEXT PRIMARY KEY, size INTEGER, status TEXT, dest TEXT,"
            " error TEXT, attempts INTEGER, updated_at REAL)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS fetch_state ("
            " src_path TEXT PRIMARY KEY, src_size INTEGER, dest_path TEXT, task_id TEXT,"
            " status TEXT, attempts INTEGER, error TEXT, updated_at REAL)"
        )
        self._conn.commit()

    def is_pushed(self, code: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM pushed WHERE code = ?", (code,)
        ).fetchone()
        return row is not None

    def mark_pushed(self, code: str, title: str = "", provider: str = "", url: str = "",
                    source: str = "") -> None:
        self._conn.execute(
            "INSERT INTO pushed(code, pushed_at, title, provider, url, source) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(code) DO UPDATE SET pushed_at=excluded.pushed_at, title=excluded.title,"
            " provider=excluded.provider, url=excluded.url, source=excluded.source",
            (code, time.time(), title, provider, url, source or _DEFAULT_SOURCE),
        )
        self._conn.commit()

    @staticmethod
    def _item(c: str, t: str, ts: float, p: str, u: str, src: str = "",
              msgs: str = "", revoked_at=None, revoked_reason: str = "") -> dict:
        """pushed 行 → 展示 dict;老数据没有 provider/source 时按去重 key 兜底。

        msgs/revoked_at/revoked_reason 是失效撤卡(2026-09-13)新增列,
        recent/search/revoked 都带出来,前端据此显示失效标记与撤卡入口。
        """
        from app.links import ed2k_file

        prov = p or ("ed2k" if _ED2K_HASH_RE.fullmatch(c) else "115")
        d = {"code": c, "title": t or c, "pushed_at": ts, "provider": prov,
             "url": u or "", "source": src or _DEFAULT_SOURCE,
             "msg_ids": msgs or "", "revoked_at": revoked_at,
             "revoked_reason": revoked_reason or ""}
        # ed2k 的文件名/大小内嵌在链接里(url 列存了完整 URI),展开详情直接给;
        # 115 的文件清单走 /api/share/files 按需读,不带这两个键
        if prov == "ed2k":
            f = ed2k_file(u or c)
            d["file_name"], d["file_size"] = (f[0], f[1]) if f else (None, None)
        return d

    def recent(self, limit: int = 20) -> list[dict]:
        """最近推送(新→旧)。同时间戳按写入顺序决胜(Windows 时钟精度粗,连推会同戳)。"""
        rows = self._conn.execute(
            "SELECT code, title, pushed_at, provider, url, source, msg_ids, revoked_at,"
            " revoked_reason FROM pushed"
            " ORDER BY pushed_at DESC, rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._item(*r) for r in rows]

    def search(self, q: str, limit: int = 50, offset: int = 0, sort: str = "time_desc") -> list[dict]:
        """按关键词搜推送历史(标题/分享码/完整链接模糊匹配);支持翻页与排序。

        q 为空 = 全部记录(最近推送分页浏览用)。
        sort: time_desc(时间新→旧,默认) | time_asc(时间旧→新) | title_asc(标题 A-Z)。
        """
        like = f"%{q}%"
        order_by = {
            "time_desc": "pushed_at DESC, rowid DESC",
            "time_asc": "pushed_at ASC, rowid ASC",
            "title_asc": "title ASC, pushed_at DESC, rowid DESC",
        }.get(sort, "pushed_at DESC, rowid DESC")   # 白名单,防注入
        rows = self._conn.execute(
            "SELECT code, title, pushed_at, provider, url, source, msg_ids, revoked_at,"
            " revoked_reason FROM pushed"
            " WHERE title LIKE ? OR code LIKE ? OR url LIKE ?"
            f" ORDER BY {order_by} LIMIT ? OFFSET ?",
            (like, like, like, limit, offset),
        ).fetchall()
        return [self._item(*r) for r in rows]

    def search_total(self, q: str = "") -> int:
        """历史记录总数(翻页用);q 为空 = 全部。"""
        if q:
            like = f"%{q}%"
            row = self._conn.execute(
                "SELECT COUNT(*) FROM pushed"
                " WHERE title LIKE ? OR code LIKE ? OR url LIKE ?",
                (like, like, like),
            ).fetchone()
        else:
            row = self._conn.execute("SELECT COUNT(*) FROM pushed").fetchone()
        return int(row[0] or 0)

    def get_pushed(self, code: str) -> dict | None:
        """单条推送记录(撤卡/详情用);不存在返回 None。"""
        row = self._conn.execute(
            "SELECT code, title, pushed_at, provider, url, source, msg_ids, revoked_at,"
            " revoked_reason FROM pushed WHERE code = ?",
            (code,),
        ).fetchone()
        return self._item(*row) if row else None

    def add_push_msg(self, code: str, chat_id, message_id: int) -> None:
        """记录推卡消息(chat_id + message_id),支持一卡多频道追加。

        记录不存在时**先建占位**(2026-09-14 修):`push_link` 是**先投递后 mark_pushed**,
        原来静默跳过导致 msg_ids 永远为空 —— 撤卡只能标记失效,删不了频道里的卡片。
        占位记录会被随后执行的 `mark_pushed`(ON CONFLICT UPDATE)补全标题等字段。
        """
        import json as _json

        cur = self._conn.execute("SELECT msg_ids FROM pushed WHERE code = ?", (code,)).fetchone()
        if cur is None:
            self._conn.execute(
                "INSERT INTO pushed(code, pushed_at, title, provider, url, source, msg_ids)"
                " VALUES(?,?,?,?,?,?,?)",
                (code, time.time(), "", "", "", _DEFAULT_SOURCE, "[]"),
            )
            cur = ("[]",)
        try:
            msgs = _json.loads(cur[0] or "[]")
        except ValueError:
            msgs = []
        msgs.append({"chat_id": str(chat_id), "message_id": int(message_id)})
        self._conn.execute(
            "UPDATE pushed SET msg_ids = ? WHERE code = ?",
            (_json.dumps(msgs, ensure_ascii=False), code),
        )
        self._conn.commit()

    def mark_revoked(self, code: str, reason: str = "") -> None:
        """标记推送失效(撤卡后写)。已标记过则只更新失效时间。"""
        self._conn.execute(
            "UPDATE pushed SET revoked_at = ?, revoked_reason = ? WHERE code = ?",
            (time.time(), reason[:200], code),
        )
        self._conn.commit()

    def revoked(self, limit: int = 50) -> list[dict]:
        """已失效(撤卡)的推送,新→旧。"""
        rows = self._conn.execute(
            "SELECT code, title, pushed_at, provider, url, source, msg_ids, revoked_at,"
            " revoked_reason FROM pushed WHERE revoked_at IS NOT NULL"
            " ORDER BY revoked_at DESC, rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._item(*r) for r in rows]

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
            " (share_code, receive_code, fid, name, uid, status, created_at, attempts, source)"
            " VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(share_code) DO UPDATE SET"
            " receive_code=excluded.receive_code, fid=excluded.fid, name=excluded.name,"
            " uid=excluded.uid, status=excluded.status, created_at=excluded.created_at,"
            " attempts=excluded.attempts, source=excluded.source",
            (
                str(task["share_code"]), str(task.get("receive_code", "") or ""),
                int(task.get("fid", 0) or 0), str(task.get("name", "") or ""),
                int(task.get("uid", 0) or 0), str(task.get("status", "auditing")),
                float(task.get("created_at", time.time())), int(task.get("attempts", 0) or 0),
                str(task.get("source", "save")),
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

    def ack_pipeline_task(self, share_code: str) -> bool:
        """把终态任务(违规/超时)标记为已确认,不再进待处理清单;返回是否命中。"""
        cur = self._conn.execute(
            "UPDATE pipeline_tasks SET status='acknowledged' WHERE share_code=?"
            " AND status IN ('timeout','violated')", (share_code,))
        self._conn.commit()
        return cur.rowcount > 0

    # ── 获取段状态(openlist 搬运) ──────────────────────────
    def save_fetch(self, src_path: str, src_size: int, *, dest_path: str = "",
                   task_id: str = "", status: str = "moving", attempts: int = 0,
                   error: str = "") -> None:
        self._conn.execute(
            "INSERT INTO fetch_state(src_path, src_size, dest_path, task_id, status,"
            " attempts, error, updated_at) VALUES(?,?,?,?,?,?,?,?)"
            " ON CONFLICT(src_path) DO UPDATE SET src_size=excluded.src_size,"
            " dest_path=excluded.dest_path, task_id=excluded.task_id, status=excluded.status,"
            " attempts=excluded.attempts, error=excluded.error, updated_at=excluded.updated_at",
            (src_path, int(src_size or 0), dest_path, task_id, status,
             int(attempts or 0), error, time.time()),
        )
        self._conn.commit()

    def get_fetch(self, src_path: str) -> dict | None:
        row = self._conn.execute(
            "SELECT src_path, src_size, dest_path, task_id, status, attempts, error, updated_at"
            " FROM fetch_state WHERE src_path = ?", (src_path,)).fetchone()
        if row is None:
            return None
        return dict(zip(("src_path", "src_size", "dest_path", "task_id", "status",
                         "attempts", "error", "updated_at"), row, strict=False))

    def get_fetch_by_relpath(self, rel: str) -> dict | None:
        """按**相对路径**找搬运记录:本地 `<落地点>/<rel>` ↔ 记录 `<监控目录>/<rel>`。

        落地保持相对子目录结构,所以两边的 rel 是同一串 —— 处理段据此做分级守门
        (还在搬运 → 不碰;搬运完成且大小一致 → 直接处理)。
        同一 rel 可能有多条历史记录,取**最新**那条。
        """
        if not rel:
            return None
        row = self._conn.execute(
            "SELECT src_path, src_size, dest_path, task_id, status, attempts, error, updated_at"
            " FROM fetch_state WHERE src_path = ? OR src_path LIKE ?"
            " ORDER BY updated_at DESC LIMIT 1", (rel, f"%/{rel}")).fetchone()
        if row is None:
            return None
        return dict(zip(("src_path", "src_size", "dest_path", "task_id", "status",
                         "attempts", "error", "updated_at"), row, strict=False))

    def list_fetch(self, status: str | None = None) -> list[dict]:
        sql = ("SELECT src_path, src_size, dest_path, task_id, status, attempts, error, updated_at"
               " FROM fetch_state")
        args: tuple = ()
        if status:
            sql += " WHERE status = ?"
            args = (status,)
        rows = self._conn.execute(sql + " ORDER BY updated_at", args).fetchall()
        return [dict(zip(("src_path", "src_size", "dest_path", "task_id", "status",
                          "attempts", "error", "updated_at"), r, strict=False)) for r in rows]

    def fetch_stats(self) -> dict:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) FROM fetch_state GROUP BY status").fetchall()
        return {s: n for s, n in rows}

    # ── 处理段状态(本地文件) ────────────────────────────────
    def save_local_file(self, *, name: str, size: int, status: str, ed2k: str = "",
                        tmdb_id: int | None = None, error: str = "") -> None:
        self._conn.execute(
            "INSERT INTO local_files(name, size, status, ed2k, tmdb_id, error, updated_at)"
            " VALUES(?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET size=excluded.size,"
            " status=excluded.status, ed2k=excluded.ed2k, tmdb_id=excluded.tmdb_id,"
            " error=excluded.error, updated_at=excluded.updated_at",
            (name, int(size or 0), status, ed2k, tmdb_id, error, time.time()),
        )
        self._conn.commit()

    def get_local_file(self, name: str, size: int | None = None) -> dict | None:
        """按文件名取记录;size 给了且不一致 → 视为新文件(同名不同版本)。"""
        row = self._conn.execute(
            "SELECT name, size, status, ed2k, tmdb_id, error, updated_at"
            " FROM local_files WHERE name = ?", (name,)).fetchone()
        if row is None:
            return None
        out = dict(zip(("name", "size", "status", "ed2k", "tmdb_id", "error",
                        "updated_at"), row, strict=False))
        if size is not None and int(size) != int(out["size"] or 0):
            return None
        return out

    def delete_local_file(self, name: str) -> int:
        """删掉某文件的处理记录(改名成功后清理旧名的陈旧失败行用)。"""
        n = self._conn.execute("DELETE FROM local_files WHERE name = ?", (name,)).rowcount
        self._conn.commit()
        return n

    def list_local_files(self, status: str | None = None) -> list[dict]:
        sql = ("SELECT name, size, status, ed2k, tmdb_id, error, updated_at FROM local_files")
        args: tuple = ()
        if status:
            sql += " WHERE status = ?"
            args = (status,)
        rows = self._conn.execute(sql + " ORDER BY updated_at", args).fetchall()
        return [dict(zip(("name", "size", "status", "ed2k", "tmdb_id", "error",
                          "updated_at"), r, strict=False)) for r in rows]

    def local_file_stats(self) -> dict:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) FROM local_files GROUP BY status").fetchall()
        return {s: n for s, n in rows}

    # ── 上传段状态 ──────────────────────────────────────────
    def save_upload(self, name: str, size: int, *, status: str, dest: str = "",
                    error: str = "", attempts: int = 0) -> None:
        self._conn.execute(
            "INSERT INTO upload_tasks(name, size, status, dest, error, attempts, updated_at)"
            " VALUES(?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET size=excluded.size,"
            " status=excluded.status, dest=excluded.dest, error=excluded.error,"
            " attempts=excluded.attempts, updated_at=excluded.updated_at",
            (name, int(size or 0), status, dest, error, int(attempts or 0), time.time()),
        )
        self._conn.commit()

    def get_upload(self, name: str, size: int | None = None) -> dict | None:
        row = self._conn.execute(
            "SELECT name, size, status, dest, error, attempts, updated_at"
            " FROM upload_tasks WHERE name = ?", (name,)).fetchone()
        if row is None:
            return None
        out = dict(zip(("name", "size", "status", "dest", "error", "attempts",
                        "updated_at"), row, strict=False))
        if size is not None and int(size) != int(out["size"] or 0):
            return None            # 同名不同大小 = 新文件
        return out

    def list_uploads(self, status: str | None = None) -> list[dict]:
        sql = ("SELECT name, size, status, dest, error, attempts, updated_at FROM upload_tasks")
        args: tuple = ()
        if status:
            sql += " WHERE status = ?"
            args = (status,)
        rows = self._conn.execute(sql + " ORDER BY updated_at", args).fetchall()
        return [dict(zip(("name", "size", "status", "dest", "error", "attempts",
                          "updated_at"), r, strict=False)) for r in rows]

    def upload_stats(self) -> dict:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) FROM upload_tasks GROUP BY status").fetchall()
        return {s: n for s, n in rows}

    # ── 总览聚合(三段链 + 趋势 + 待处理) ────────────────────
    @staticmethod
    def _today_start() -> float:
        import datetime as _dt

        return _dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    def segment_stats(self) -> dict:
        """三段今日/在途/失败统计(供总览页)。"""
        t0 = self._today_start()

        def count(sql: str, *args) -> int:
            return int(self._conn.execute(sql, args).fetchone()[0] or 0)

        fetch_done_today = count(
            "SELECT COUNT(*) FROM fetch_state WHERE status='done' AND updated_at>=?", t0)
        upload_done_today = count(
            "SELECT COUNT(*) FROM upload_tasks WHERE status='done' AND updated_at>=?", t0)
        process_done_today = count(
            "SELECT COUNT(*) FROM local_files WHERE status='processed' AND updated_at>=?", t0)
        # 最后活动时间(每段取全表最大 updated_at,不限于今日——用于判断"是否卡死")
        def last(tbl: str) -> float:
            return float(self._conn.execute(
                f"SELECT COALESCE(MAX(updated_at),0) FROM {tbl}").fetchone()[0] or 0)

        return {
            "fetch": {"inflight": len(self.list_fetch("moving")), "today": fetch_done_today,
                      "failed": count("SELECT COUNT(*) FROM fetch_state WHERE status='failed'"),
                      "last_activity": last("fetch_state")},
            "process": {"today": process_done_today,
                        "manual": count("SELECT COUNT(*) FROM local_files"
                                        " WHERE status NOT IN ('processed', 'gone')"),
                        "last_activity": last("local_files")},
            "upload": {"inflight": len(self.list_uploads("uploading")), "today": upload_done_today,
                       "failed": count("SELECT COUNT(*) FROM upload_tasks WHERE status='failed'"),
                       "last_activity": last("upload_tasks")},
        }

    def daily_series(self, days: int = 7) -> dict:
        """近 N 日按天聚合:推卡条数 + 搬运字节 + 上传条数(趋势图用)。"""
        import datetime as _dt

        today = _dt.date.today()
        labels = [(today - _dt.timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
        since = self._today_start() - (days - 1) * 86400
        pushed = dict(self._conn.execute(
            "SELECT date(pushed_at,'unixepoch','localtime') d, COUNT(*) FROM pushed"
            " WHERE pushed_at >= ? GROUP BY d", (since,)).fetchall())
        moved = dict(self._conn.execute(
            "SELECT date(updated_at,'unixepoch','localtime') d, COALESCE(SUM(src_size),0)"
            " FROM fetch_state WHERE status='done' AND updated_at >= ? GROUP BY d",
            (since,)).fetchall())
        uploaded = dict(self._conn.execute(
            "SELECT date(updated_at,'unixepoch','localtime') d, COUNT(*) FROM upload_tasks"
            " WHERE status='done' AND updated_at >= ? GROUP BY d", (since,)).fetchall())
        return {
            "labels": labels,
            "pushed": [int(pushed.get(d, 0) or 0) for d in labels],
            "moved_gb": [round(int(moved.get(d, 0) or 0) / 1024 ** 3, 2) for d in labels],
            "uploaded": [int(uploaded.get(d, 0) or 0) for d in labels],
        }

    def attention_items(self, limit: int = 8) -> list[dict]:
        """待人工处理:未识别 / 失败 / 超时(带原因与可跳日志的查询词)。"""
        out: list[dict] = []
        for r in self._conn.execute(
                "SELECT name, size, status, error, updated_at FROM local_files"
                " WHERE status NOT IN ('processed', 'gone')"
                " ORDER BY updated_at DESC LIMIT ?", (limit,)):
            kind = "未识别" if r[2] == "unrecognized" else "处理失败"
            out.append({"kind": kind, "text": r[0], "size": int(r[1] or 0),
                        "reason": r[3] or "", "at": r[4]})
        for r in self._conn.execute(
                "SELECT src_path, error, updated_at FROM fetch_state"
                " WHERE status='failed' ORDER BY updated_at DESC LIMIT ?", (limit,)):
            out.append({"kind": "获取失败", "text": r[0].rsplit("/", 1)[-1], "reason": r[1] or "",
                        "at": r[2]})
        for r in self._conn.execute(
                "SELECT name, error, updated_at FROM upload_tasks"
                " WHERE status='failed' ORDER BY updated_at DESC LIMIT ?", (limit,)):
            out.append({"kind": "上传失败", "text": r[0], "reason": r[1] or "", "at": r[2]})
        # pipeline_tasks 没有 error 列(只有 attempts),原因是固定文案
        for r in self._conn.execute(
                "SELECT share_code, name, status, attempts, created_at FROM pipeline_tasks"
                " WHERE status IN ('timeout','violated') ORDER BY created_at DESC LIMIT ?", (limit,)):
            kind = "流水线超时" if r[2] == "timeout" else "流水线违规"
            out.append({"kind": kind, "text": r[1],
                        "reason": f"等待 {r[3]} 轮未通过" if r[2] == "timeout" else "审核未通过/违规",
                        "at": r[4], "share_code": r[0]})
        out.sort(key=lambda x: x["at"] or 0, reverse=True)
        return out[:limit]

    def close(self) -> None:
        self._conn.close()
