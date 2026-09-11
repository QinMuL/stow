"""Web 配置台:登录 / 配置读写 / 服务状态 / 保存并重启。

Web 永远在线(哪怕 Bot 配置不全),用户在网页补齐配置后一键重启生效。
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
import time
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app import __version__, auth, sysinfo
from app.channel_monitor import channel_rows
from app.config import (
    DEFAULT_WEB_PORT,
    MASK,
    SENSITIVE_KEYS,
    ensure_admin,
    fingerprint,
    load_config,
    read_raw,
    write_raw,
)
from app.store import Store

# 进程状态(bot 线程写入,web 读取);Bot 就绪后还会写入:
#   "monitor" —— 频道监控实例(Web 登录端点用)
#   "bot_loop" —— Bot 事件循环(Telethon 客户端绑定该循环,只能投递到它执行)
STATE = {"bot_running": False, "bot_error": ""}

_STATIC = Path(__file__).parent.parent / "static" / "index.html"
_bearer_lock = threading.Lock()

# ── 链路健康探测(带 TTL 缓存,避免页面刷新打爆外部服务) ────
_PROXY_TTL = 60.0    # 代理探测:60s
_PAN115_TTL = 300.0  # cookie 校验:5min(真实请求 115)
_OPENLIST_TTL = 60.0  # 获取段连通:本机服务,60s
_CD2_TTL = 60.0       # 上传段连通:本机服务,60s
_health_cache: dict = {"proxy": (0.0, None), "pan115": (0.0, None),
                       "openlist": (0.0, None), "cd2": (0.0, None)}
_UID_RE = re.compile(r"UID=(\d+)")

# 健康判据阈值(2026-09-11 与用户确认的口径)
_DISK_WARN, _DISK_BAD = 80.0, 90.0   # 磁盘已用率
_SEG_FAIL_BAD = 5                    # 单段失败件数达到这里算异常,之下算降级
_HEARTBEAT_STALE = 180.0             # Bot 心跳超过这么久没跳 = 连丢 3 拍,判"可能卡死"


async def _check_proxy(proxy_url: str) -> dict:
    """经配置代理探测 api.telegram.org(Bot 的实际依赖);返回可达性与延迟。"""
    import httpx

    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(proxy=proxy_url or None, timeout=5) as c:
            await c.get("https://api.telegram.org")
        return {
            "configured": bool(proxy_url), "url": proxy_url, "ok": True,
            "latency_ms": round((time.monotonic() - t0) * 1000), "error": "",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "configured": bool(proxy_url), "url": proxy_url, "ok": False,
            "latency_ms": None, "error": str(exc)[:80],
        }


def _check_pan115(cookie: str) -> dict:
    """校验 115 cookie 是否仍有效(user_info 实测);未配置返回匿名模式。"""
    if not cookie:
        return {"cookie_set": False, "uid": None, "ok": None, "error": ""}
    m = _UID_RE.search(cookie)
    if m is None:
        return {"cookie_set": True, "uid": None, "ok": False, "error": "cookie 中无 UID,格式可疑"}
    uid = int(m.group(1))
    try:
        from p115client import P115Client

        client = P115Client(cookie, app="android")
        resp = client.user_info(uid, async_=False)
        ok = bool(resp and resp.get("state") is not False and resp.get("data"))
        return {
            "cookie_set": True, "uid": uid, "ok": ok,
            "error": "" if ok else str(resp.get("error") or "账号校验失败")[:80],
        }
    except Exception as exc:  # noqa: BLE001
        return {"cookie_set": True, "uid": uid, "ok": False, "error": str(exc)[:80]}


def _tools_status() -> dict:
    """处理段依赖的外部命令(本地查找,毫秒级,不需要缓存)。"""
    import shutil

    return {"ffmpeg": bool(shutil.which("ffmpeg")), "ffprobe": bool(shutil.which("ffprobe"))}


async def _check_openlist(cfg) -> dict:
    """获取段探针:令牌可用(先探根目录)+ 每个监控目录都还在。

    分两步是有意的:先打根目录判"连不上/令牌废",再逐个判"目录没了"——
    否则令牌失效会被误报成"所有监控目录都不存在"。
    """
    out = {"configured": bool(cfg.openlist_base_url and cfg.openlist_token),
           "enabled": cfg.openlist_ready(), "ok": False, "missing_dirs": [], "error": ""}
    if not out["enabled"]:
        return out
    from app.openlist import OpenListClient, OpenListError

    client = OpenListClient(cfg.openlist_base_url, cfg.openlist_token, timeout=8.0)
    try:
        try:
            await client.list_dir("/", per_page=1)      # 连通 + 令牌
        except OpenListError as exc:
            out["error"] = str(exc)[:120]
            return out
        for d in cfg.openlist_monitor_list():
            try:
                await client.list_dir(d, per_page=1)
            except OpenListError as exc:
                out["missing_dirs"].append(f"{d}({str(exc)[:60]})")
        out["ok"] = not out["missing_dirs"]
    except Exception as exc:  # noqa: BLE001 - 探针绝不抛出
        out["error"] = str(exc)[:120]
    finally:
        await client.aclose()
    return out


def _check_cd2(cfg) -> dict:
    """上传段探针:gRPC 连得上 + CD2 已登录 cloudfs(没登录就传不了)。"""
    out = {"configured": bool(cfg.cd2_address),
           "enabled": bool(cfg.cd2_address and cfg.cd2_dest_path), "ok": False, "error": ""}
    if not out["enabled"]:
        return out
    from app.cd2.client import Cd2Client

    client = Cd2Client(cfg.cd2_address, cfg.cd2_token,
                       username=cfg.cd2_username, password=cfg.cd2_password, timeout=8.0)
    try:
        client.ensure_conn()
        if not client.system_info().get("logged_in"):
            out["error"] = "CD2 未登录 cloudfs(115 侧不可达)"
            return out
        if cfg.cd2_token:
            client.token_info()      # 令牌失效会在这里抛
        out["ok"] = True
    except Exception as exc:  # noqa: BLE001 - 探针绝不抛出
        out["error"] = str(exc)[:120]
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
    return out


async def _ensure_health(cfg) -> None:
    """按 TTL 刷新探针缓存。页首每 10 秒轮询一次,探针绝不能每轮都打出去。"""
    now = time.monotonic()
    if now - _health_cache["proxy"][0] > _PROXY_TTL:
        _health_cache["proxy"] = (now, await _check_proxy(cfg.proxy_url))
    if now - _health_cache["pan115"][0] > _PAN115_TTL:
        _health_cache["pan115"] = (now, await asyncio.to_thread(_check_pan115, cfg.pan115_cookie))
    if now - _health_cache["openlist"][0] > _OPENLIST_TTL:
        _health_cache["openlist"] = (now, await _check_openlist(cfg))
    if now - _health_cache["cd2"][0] > _CD2_TTL:
        _health_cache["cd2"] = (now, await asyncio.to_thread(_check_cd2, cfg))


def _monitor_status(cfg) -> dict:
    """频道监控状态(Web 线程读只读内存字段;Bot 未运行时给"未运行")。"""
    mon = STATE.get("monitor")
    if mon is not None:
        rs = mon.runtime_status()
        return {"configured": len(cfg.monitor_channel_list()), "ready": cfg.monitor_ready(),
                "state": rs["state"], "state_text": rs["state_text"],
                "account": rs["account"], "connected": rs["connected"]}
    return {"configured": len(cfg.monitor_channel_list()), "ready": cfg.monitor_ready(),
            "state": "stopped", "state_text": "Bot 未运行", "account": "", "connected": False}


def _health_block(cfg, stats: dict, disk: dict) -> dict:
    """健康判据(2026-09-11 重构):按"段"列条目,四档 ok / warn / bad / off。

    - **off = 该功能未配置或未启用 → 不参与总判**。只用推卡、不跑流水线的人
      不该天天看到黄色;这与"115 未配 Cookie 只算降级"是同一条口径。
    - 判据放在后端是**故意的**:分级逻辑要能被 pytest 覆盖,前端只负责渲染。
    """
    rows: list[dict] = []

    def add(key: str, name: str, level: str, text: str) -> None:
        rows.append({"key": key, "name": name, "level": level, "text": text})

    # ── 推卡链 ──
    if STATE["bot_running"]:
        add("bot", "Bot 进程", "ok", "运行中")
    else:
        probs = cfg.problems()
        why = STATE.get("bot_error") or ("、".join(probs) if probs else "未启动或已退出(见系统日志)")
        add("bot", "Bot 进程", "bad", f"未运行 — {why[:110]}")

    p = _health_cache["proxy"][1] or {}
    if not p.get("configured"):
        add("proxy", "代理", "off", "未配置(直连)")
    else:
        add("proxy", "代理", "ok" if p.get("ok") else "bad",
            "可达" if p.get("ok") else f"不可达 — {(p.get('error') or '')[:90]}")

    c = _health_cache["pan115"][1] or {}
    if not c.get("cookie_set"):
        add("pan115", "115 读通道", "warn", "未配 Cookie(匿名通道易限流)")
    else:
        add("pan115", "115 读通道", "ok" if c.get("ok") else "bad",
            "Cookie 有效" if c.get("ok") else f"Cookie 失效 — {(c.get('error') or '')[:90]}")

    m = _monitor_status(cfg)
    if not m.get("configured"):
        add("monitor", "频道监控", "off", "未配置源频道")
    elif not STATE["bot_running"]:
        add("monitor", "频道监控", "off", "随 Bot 未运行")
    elif m.get("state") != "running":
        add("monitor", "频道监控", "bad", m.get("state_text") or "未运行")
    elif not m.get("connected"):
        add("monitor", "频道监控", "bad", "连接断开(重连中)")
    else:
        add("monitor", "频道监控", "ok", f"监听中({m.get('account') or '-'})")

    # ── Bot 心跳(卡死检测)──
    # bot_running 只是"run() 还没返回";事件循环卡住时它照样是 true。
    # 心跳由 Bot 侧的 asyncio 任务打点,循环停摆就不跳 → 这里才发现得了。
    if STATE["bot_running"]:
        beat = float(STATE.get("bot_heartbeat") or 0)
        if not beat:
            add("heartbeat", "Bot 心跳", "ok", "等待首次打点")
        else:
            age = time.time() - beat
            if age > _HEARTBEAT_STALE:
                add("heartbeat", "Bot 心跳", "bad",
                    f"{int(age)} 秒无进展(可能卡死,见系统日志)")
            else:
                add("heartbeat", "Bot 心跳", "ok", f"{int(age)} 秒前有进展")

    # ── 三段链 ──
    ol = _health_cache["openlist"][1] or {}
    if not ol.get("enabled"):
        add("openlist", "获取段 openlist", "off",
            "未启用" if not ol.get("configured") else "未配置监控目录")
    elif ol.get("error"):
        add("openlist", "获取段 openlist", "bad", f"连不上 — {ol['error'][:90]}")
    elif ol.get("missing_dirs"):
        add("openlist", "获取段 openlist", "bad",
            "监控目录不存在:" + "、".join(ol["missing_dirs"])[:90])
    else:
        add("openlist", "获取段 openlist", "ok", "可达,监控目录正常")

    t = _tools_status()
    if not (t["ffmpeg"] and t["ffprobe"]):
        miss = "、".join(k for k, ok in t.items() if not ok)
        add("tools", "处理段工具链", "warn", f"缺 {miss}(画质标签缺失、清洗不可用)")
    else:
        add("tools", "处理段工具链", "ok", "ffmpeg / ffprobe 就绪")

    cd2 = _health_cache["cd2"][1] or {}
    if not cd2.get("enabled"):
        add("cd2", "上传段 CD2", "off",
            "未启用" if not cd2.get("configured") else "未配置上传目标目录")
    else:
        add("cd2", "上传段 CD2", "ok" if cd2.get("ok") else "bad",
            "可达" if cd2.get("ok") else f"连不上 — {(cd2.get('error') or '')[:90]}")

    for key, name, path in (("media_a", "落地目录", cfg.openlist_dir),
                            ("media_b", "上传源目录", cfg.clouddrive_dir)):
        ok = Path(path).is_dir()
        add(key, name, "ok" if ok else "bad",
            str(path) if ok else f"{path} 不存在(挂载丢了?)")

    pct = float(disk.get("used_percent") or 0.0)
    if not disk.get("total_bytes"):
        add("disk", "磁盘", "bad", "读不到磁盘信息(挂载丢了?)")
    else:
        lvl = "bad" if pct >= _DISK_BAD else "warn" if pct >= _DISK_WARN else "ok"
        free_gb = float(disk.get("free_bytes") or 0) / 1024 ** 3
        add("disk", "磁盘", lvl, f"已用 {pct}%(剩 {free_gb:.0f}GB)")

    for key, label in (("fetch", "获取"), ("upload", "上传")):
        n = int(stats.get(key, {}).get("failed") or 0)
        if n:
            add(f"{key}_fail", f"{label}失败件", "bad" if n >= _SEG_FAIL_BAD else "warn",
                f"{n} 个待处理(见下方「需要你处理」)")
    pm = int(stats.get("process", {}).get("manual") or 0)
    if pm:
        add("process_manual", "处理待人工", "warn", f"{pm} 个待处理(见下方「需要你处理」)")

    # 总判取最差;off 不参与
    levels = [r["level"] for r in rows if r["level"] != "off"]
    if "bad" in levels:
        agg, text = "bad", "系统异常"
    elif "warn" in levels:
        agg, text = "warn", "系统运行中(降级)"
    else:
        agg, text = "ok", "系统运行中"
    issues = [f"{r['name']} {r['text']}" for r in rows if r["level"] in ("bad", "warn")]
    return {"level": agg, "text": text, "detail": " · ".join(issues), "rows": rows}


def get_store_path(request: Request) -> Path:
    return Path(request.app.state.config_path)


PathDep = Annotated[Path, Depends(get_store_path)]


def _current_user(path: PathDep, token: str) -> str:
    username = auth.verify_token(token)
    if username is None:
        raise HTTPException(status_code=401, detail="登录已过期")
    return username


class LoginRequest(BaseModel):
    username: str
    password: str


class AccountUpdate(BaseModel):
    current_password: str = ""
    new_username: str = ""
    new_password: str = ""


class ConfigUpdate(BaseModel):
    values: dict[str, object]


class ChannelItem(BaseModel):
    chat_id: str
    preset: str = "115"  # "115" | "ed2k"
    title: str = ""


class ChannelsUpdate(BaseModel):
    channels: list[ChannelItem]


class PipelineRun(BaseModel):
    segment: str = ""   # fetch | process | upload


class MonitorPhone(BaseModel):
    phone: str = ""
    resend: bool = False  # 显式要求重发验证码(默认复用进行中的会话,不重复发码)


class MonitorCode(BaseModel):
    code: str = ""


class MonitorPassword(BaseModel):
    password: str = ""


# 整数键的默认值:缺失时不能回 0 —— 表单会把 0 显示出来再存回去(踩过)
_INT_DEFAULTS = {"web_port": DEFAULT_WEB_PORT, "openlist_max_tasks": 2,
                 "fetch_interval_minutes": 5, "process_interval_minutes": 5,
                 "min_size_mb": 50, "min_age_seconds": 60,
                 "upload_interval_minutes": 5, "upload_max_tasks": 2}

# 可经 Web 修改的配置键白名单(类型: s=字符串, i=整数, ids=ID 列表)
EDITABLE = {
    "tg_bot_token": "s", "tg_admin_ids": "ids",
    "tmdb_api_key": "s", "proxy_url": "s", "log_level": "s", "web_port": "i",
    "pan115_cookie": "s",
    "pipeline_root_dir": "s", "monitor_dirs": "s",
    "tg_api_id": "i", "tg_api_hash": "s", "monitor_channels": "s",
    "openlist_base_url": "s", "openlist_token": "s", "openlist_path": "s",
    "openlist_monitor_dirs": "s", "openlist_dest_path": "s",
    "openlist_max_tasks": "i", "fetch_interval_minutes": "i",
    "process_interval_minutes": "i", "min_size_mb": "i", "min_age_seconds": "i",
    "clean_enabled": "b", "upload_interval_minutes": "i", "upload_max_tasks": "i",
    "cd2_address": "s", "cd2_token": "s", "cd2_source_path": "s", "cd2_dest_path": "s",
    "cd2_username": "s", "cd2_password": "s",
}


def create_app(config_path: str | Path) -> FastAPI:
    config_path = Path(config_path)
    ensure_admin(config_path)
    app = FastAPI(title="Stow", docs_url=None, redoc_url=None)
    app.state.config_path = str(config_path)

    def _auth_header(request: Request) -> str:
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="未登录")
        return header[7:]

    # ── 登录 / 账号 ─────────────────────────────────────────
    @app.post("/api/login")
    def login(req: LoginRequest) -> dict:
        raw = read_raw(config_path)
        stored = raw.get("admin_password_hash", "")
        if not stored or req.username != raw.get("admin_username") or not auth.verify_password(
            req.password, stored
        ):
            raise HTTPException(status_code=401, detail="账号或密码不正确")
        return {"token": auth.create_token(req.username)}

    @app.get("/api/account")
    def account(request: Request) -> dict:
        raw = read_raw(config_path)
        _current_user(config_path, _auth_header(request))
        stored = raw.get("admin_password_hash", "")
        return {
            "username": raw.get("admin_username", ""),
            "default_password": auth.is_default_password(
                auth.DEFAULT_ADMIN_PASSWORD, stored
            ) if stored else False,
        }

    @app.put("/api/account")
    def update_account(body: AccountUpdate, request: Request) -> dict:
        raw = read_raw(config_path)
        user = _current_user(config_path, _auth_header(request))
        stored = raw.get("admin_password_hash", "")
        if not stored or not auth.verify_password(body.current_password, stored):
            raise HTTPException(status_code=403, detail="当前密码不正确")
        new_username, new_password = body.new_username.strip(), body.new_password
        if not new_username and not new_password:
            raise HTTPException(status_code=400, detail="没有可变更的内容")
        if new_password and len(new_password) < 6:
            raise HTTPException(status_code=400, detail="新密码至少 6 位")
        if new_username:
            raw["admin_username"] = new_username
        if new_password:
            raw["admin_password_hash"] = auth.hash_password(new_password)
        write_raw(raw, config_path)
        return {"success": True, "username": new_username or user}

    # ── 配置 ────────────────────────────────────────────────
    def _restart_pending() -> bool:
        """文件里的配置与运行中 Bot 生效的配置是否不一致(即:改了但没重启)。

        Bot 未运行时返回 False——那种情况由"Bot 未启动"状态表达,不该催重启。
        """
        fp = STATE.get("cfg_fingerprint")
        if not fp or not STATE.get("bot_running"):
            return False
        return fingerprint(read_raw(config_path)) != fp

    @app.get("/api/config")
    def get_config(request: Request) -> dict:
        _current_user(config_path, _auth_header(request))
        raw = read_raw(config_path)
        out: dict[str, object] = {}
        for key, kind in EDITABLE.items():
            v = raw.get(key, "" if kind != "i" else _INT_DEFAULTS.get(key, 0))
            if key in SENSITIVE_KEYS:
                # 与启动加载同口径:非 ASCII(如中文占位符)视为未填写
                s = str(v or "").strip()
                out[key] = MASK if (s and s.isascii()) else ""
            else:
                out[key] = v
        out["restart_pending"] = _restart_pending()
        return out

    @app.put("/api/config")
    def put_config(body: ConfigUpdate, request: Request) -> dict:
        _current_user(config_path, _auth_header(request))
        raw = read_raw(config_path)
        for key, value in body.values.items():
            if key not in EDITABLE:
                continue
            if key in SENSITIVE_KEYS and str(value) == MASK:
                continue  # 未修改
            kind = EDITABLE[key]
            if kind == "b":
                raw[key] = str(value).strip().lower() in ("1", "true", "on", "开")
            elif kind == "i":
                try:
                    raw[key] = int(str(value))
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=f"{key} 需为整数") from exc
            elif kind == "ids":
                items = []
                for part in str(value).replace("，", ",").split(","):
                    part = part.strip()
                    if part:
                        if not part.lstrip("-").isdigit():
                            raise HTTPException(status_code=400, detail=f"管理员 ID 非法:{part}")
                        items.append(int(part))
                raw[key] = items
            else:
                s = str(value).strip()
                if kind == "s" and key in SENSITIVE_KEYS and s and not s.isascii():
                    raise HTTPException(
                        status_code=400,
                        detail=f"{key} 含非 ASCII 字符(疑似占位符,请填真实值)",
                    )
                raw[key] = s
        write_raw(raw, config_path)
        cfg = load_config(config_path)
        return {
            "success": True,
            "bot_ready": cfg.bot_ready(),
            "missing": cfg.problems(),
            "restart_pending": _restart_pending(),
        }

    # ── 日志 ────────────────────────────────────────────────
    _log_line_re = re.compile(
        r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) (DEBUG|INFO|WARNING|ERROR|CRITICAL)"
        r"\s+\[([^\]]+)\] (.*)$"
    )
    _log_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    _log_tail_bytes = 2 * 1024 * 1024  # 只解析尾部窗口,防大文件全量解析

    @app.get("/api/logs")
    def logs(
        request: Request, file: str = "stow.log", limit: int = 300, level: str = "", q: str = ""
    ) -> dict:
        """日志查看:结构化解析 + 级别/关键字过滤,返回尾部 limit 条(旧→新)。"""
        _current_user(config_path, _auth_header(request))
        cfg = load_config(config_path)
        log_dir = cfg.log_dir
        avail = sorted(
            (p.name for p in log_dir.glob("stow.log*") if p.is_file()),
            key=lambda n: (n != "stow.log", n),
        )
        files = [
            {"name": p.name, "size": p.stat().st_size} for p in (log_dir / n for n in avail)
        ]
        if not re.fullmatch(r"stow\.log(\.\d{1,2})?", file or ""):
            raise HTTPException(status_code=400, detail="非法日志文件名")
        path = log_dir / file
        if not path.is_file():
            return {"files": files, "current": file, "items": [], "truncated": False}

        size = path.stat().st_size
        with path.open("rb") as f:
            if size > _log_tail_bytes:
                f.seek(size - _log_tail_bytes)
                f.readline()  # 丢弃窗口起始的半行
            text = f.read().decode("utf-8", errors="replace")

        entries: list[dict] = []
        for line in text.splitlines():
            m = _log_line_re.match(line)
            if m:
                entries.append(
                    {"ts": m.group(1), "level": m.group(2), "logger": m.group(3), "msg": m.group(4)}
                )
            elif entries and line.strip():
                entries[-1]["msg"] += "\n" + line  # traceback 等续行归入上一条

        if level.upper() in _log_levels:  # 最低级别过滤(WARNING=含 ERROR/CRITICAL)
            floor = _log_levels.index(level.upper())
            entries = [e for e in entries if _log_levels.index(e["level"]) >= floor]
        if q:
            needle = q.lower()
            entries = [
                e for e in entries
                if needle in e["msg"].lower() or needle in e["logger"].lower()
            ]
        limit = max(1, min(limit, 1000))
        return {
            "files": files,
            "current": file,
            "items": entries[-limit:],
            "truncated": len(entries) > limit,
        }

    # ── 推送频道管理 ────────────────────────────────────────
    @app.get("/api/channels")
    def get_channels(request: Request) -> dict:
        _current_user(config_path, _auth_header(request))
        cfg = load_config(config_path)
        return {
            "channels": [
                {"chat_id": c.chat_id, "preset": c.preset, "title": c.title}
                for c in cfg.channels
            ],
        }

    @app.put("/api/channels")
    def put_channels(body: ChannelsUpdate, request: Request) -> dict:
        _current_user(config_path, _auth_header(request))
        seen: set[str] = set()
        items = []
        for c in body.channels:
            cid = c.chat_id.strip()
            if not cid or not cid.lstrip("-").isdigit():
                raise HTTPException(status_code=400, detail=f"频道 ID 非法:{c.chat_id}")
            if cid in seen:
                raise HTTPException(status_code=400, detail=f"频道 ID 重复:{cid}")
            seen.add(cid)
            preset = c.preset if c.preset in ("115", "ed2k") else "115"
            items.append({"chat_id": cid, "preset": preset, "title": c.title.strip()})
        raw = read_raw(config_path)
        raw["channels"] = items
        write_raw(raw, config_path)
        return {
            "success": True,
            "message": "已保存;重启后生效(或在 Bot 中转发频道消息登记)",
            "restart_pending": _restart_pending(),
        }

    # ── openlist 目录浏览(获取段监控目录/落地点的选择器数据源) ──
    @app.get("/api/openlist/dirs")
    async def list_openlist_dirs(request: Request, path: str = "/") -> dict:
        """列出 openlist 某路径下的**子目录**(目录选择器用);只列目录,不列文件。"""
        _current_user(config_path, _auth_header(request))
        cfg = load_config(config_path)
        if not (cfg.openlist_base_url and cfg.openlist_token):
            raise HTTPException(status_code=503, detail="未配置 openlist 地址/令牌,无法浏览")
        from app.openlist import OpenListClient, OpenListError

        client = OpenListClient(cfg.openlist_base_url, cfg.openlist_token)
        target = (path or "/").strip() or "/"
        try:
            items = await client.list_dir(target)
        except OpenListError as exc:
            raise HTTPException(status_code=502, detail=f"列目录失败:{str(exc)[:120]}") from exc
        finally:
            await client.aclose()
        dirs = [
            {"path": f"{target.rstrip('/')}/{it['name']}" if target != "/" else f"/{it['name']}",
             "name": it["name"]}
            for it in items if it.get("is_dir")
        ]
        return {"path": target, "items": dirs}

    # ── 总览:三段链聚合 + 趋势 + 待处理 + 就地触发 ──────────
    def _dir_usage(path: Path) -> tuple[int, int]:
        """目录 (文件数, 总字节);不存在返回 (0,0)。小目录直接遍历。"""
        files = total = 0
        try:
            with os.scandir(path) as it:
                for e in it:
                    if e.is_file(follow_symlinks=False):
                        files += 1
                        total += e.stat(follow_symlinks=False).st_size
        except OSError:
            pass
        return files, total

    _disk_cache: dict = {"at": 0.0, "data": {}}

    def _disk_info(cfg) -> dict:
        """media 两目录占用 + 所在盘剩余(60s 缓存,避免每次刷新都遍历)。"""
        now = time.monotonic()
        if now - _disk_cache["at"] < 60 and _disk_cache["data"]:
            return _disk_cache["data"]
        used = 0
        counts = {}
        for name, d in (("openlist", cfg.openlist_dir), ("clouddrive", cfg.clouddrive_dir)):
            n, size = _dir_usage(d)
            counts[name] = n
            used += size
        try:
            import shutil

            du = shutil.disk_usage(str(cfg.openlist_dir))   # 跨平台(Win 无 statvfs)
            free, total = du.free, du.total
        except (OSError, FileNotFoundError):
            free = total = 0
        data = {
            "used_bytes": used, "files": counts,
            "free_bytes": free, "total_bytes": total,
            "used_percent": round((total - free) / total * 100, 1) if total else 0.0,
        }
        _disk_cache.update({"at": now, "data": data})
        return data

    def _attention(cfg, store: Store) -> list[dict]:
        """待人工清单;处理类的条目若文件已不在落地点(已改名/人工删)则不再误报。"""
        present = {p.name for p in Path(cfg.openlist_dir).rglob("*") if p.is_file()}
        out = []
        for a in store.attention_items():
            if a["kind"] in ("处理失败", "未识别") and a["text"] not in present:
                continue
            out.append(a)
        return out

    def _segment_payload(cfg, store: Store) -> dict:
        """三段链:今日/在途/失败/最后活动 + 在途明细(带进度)。"""
        stats = store.segment_stats()
        disk = _disk_info(cfg)         # 磁盘卡与健康判据共用(内部 60s 缓存)
        from app.webapp import STATE  # 同进程,取运行中的三段实例(只读内存字段)

        fetcher, uploader = STATE.get("fetcher"), STATE.get("uploader")

        fetch_items = []
        prog = fetcher.progress_snapshot() if fetcher is not None else {}
        for row in store.list_fetch("moving"):
            base = row["src_path"].rsplit("/", 1)[-1]
            fetch_items.append({"name": base, "progress": prog.get(base, 0.0),
                                "size": row["src_size"], "note": "搬运中"})

        queue_files, queue_bytes = _dir_usage(cfg.openlist_dir)
        process_items = []
        for p in sorted(Path(cfg.openlist_dir).glob("*")):
            if p.is_file():
                process_items.append({"name": p.name, "size": p.stat().st_size, "progress": 0.0,
                                      "note": "排队中"})

        upload_items = []
        uprog = uploader.progress_snapshot() if uploader is not None else {}
        for row in store.list_uploads("uploading"):
            upload_items.append({"name": row["name"], "size": row["size"],
                                 "progress": uprog.get(row["name"], 0.0), "note": "上传中"})

        def seg(key: str, name: str, src: str, dst: str, items: list[dict], extra: dict) -> dict:
            s = stats[key]
            d = {"key": key, "name": name, "from": src, "to": dst, "items": items,
                 "last_activity": s.get("last_activity", 0), **extra}
            return d

        return {
            "segments": [
                seg("fetch", "获取", "、".join(cfg.openlist_monitor_list()) or "(未配置监控目录)",
                    cfg.openlist_dest_path or "/项目测试", fetch_items,
                    {"inflight": stats["fetch"]["inflight"], "limit": cfg.openlist_max_tasks,
                     "today": stats["fetch"]["today"], "failed": stats["fetch"]["failed"]}),
                seg("process", "处理", str(cfg.openlist_dir), str(cfg.clouddrive_dir),
                    process_items,
                    {"queued": len(process_items), "queued_bytes": queue_bytes,
                     "today": stats["process"]["today"], "manual": stats["process"]["manual"]}),
                seg("upload", "上传", cfg.cd2_source_path or "(未配置上传源)",
                    cfg.cd2_dest_path or "(未配置上传目标)", upload_items,
                    {"inflight": stats["upload"]["inflight"], "limit": cfg.upload_max_tasks,
                     "today": stats["upload"]["today"], "failed": stats["upload"]["failed"],
                     "enabled": bool(cfg.cd2_dest_path)}),
            ],
            "numbers": {
                # 今日推卡/搬运/上传三个数已删(2026-09-11):7 日趋势的图例里已有,重复且前端无人读
                "disk": disk,
            },
            "trend": store.daily_series(7),
            "health": _health_block(cfg, stats, disk),
            "sys": sysinfo.snapshot(),      # 本机 CPU/内存/网络(磁盘在 numbers.disk)
            "attention": _attention(cfg, store),
            "recent": store.recent(5),
        }

    @app.get("/api/pipeline")
    async def pipeline(request: Request) -> dict:
        """总览页主数据:三段链 + 关键数字 + 7 日趋势 + 健康判据 + 待处理 + 最近推送。"""
        _current_user(config_path, _auth_header(request))
        cfg = load_config(config_path)
        await _ensure_health(cfg)      # 健康判据要用探针结果(带 TTL,不会每轮打出去)
        store = Store(cfg.db_path)
        try:
            return _segment_payload(cfg, store)
        finally:
            store.close()

    @app.post("/api/pipeline/run")
    def pipeline_run(body: PipelineRun, request: Request) -> dict:
        """就地触发某一段(= Bot 的 /fetch /process /upload),在 Bot 事件循环里跑。"""
        _current_user(config_path, _auth_header(request))
        seg = (body.segment or "").strip()
        target = {"fetch": "fetcher", "process": "processor", "upload": "uploader"}.get(seg)
        if target is None:
            raise HTTPException(status_code=400, detail="未知的段(限 fetch/process/upload)")
        obj = STATE.get(target)
        if obj is None:
            raise HTTPException(status_code=503, detail="Bot 未运行或该段未装配")
        loop = STATE.get("bot_loop")
        if loop is None:
            raise HTTPException(status_code=503, detail="Bot 事件循环未就绪")
        fut = asyncio.run_coroutine_threadsafe(obj.scan_now(), loop)
        try:
            return {"success": True, "segment": seg, "message": fut.result(timeout=180)}
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"执行失败:{str(exc)[:120]}") from exc

    # ── CD2 目录浏览(上传目标目录的选择器数据源) ────────────
    @app.get("/api/cd2/dirs")
    async def list_cd2_dirs(request: Request, path: str = "/") -> dict:
        """列出 CD2 某路径下的**子目录**(目录选择器用);只列目录。"""
        _current_user(config_path, _auth_header(request))
        cfg = load_config(config_path)
        if not (cfg.cd2_address and (cfg.cd2_token or (cfg.cd2_username and cfg.cd2_password))):
            raise HTTPException(status_code=503, detail="未配置 CD2 地址/令牌,无法浏览")
        from app.cd2.client import Cd2Client, Cd2Error

        client = Cd2Client(cfg.cd2_address, cfg.cd2_token,
                           username=cfg.cd2_username, password=cfg.cd2_password)
        target = (path or "/").strip() or "/"
        try:
            files = await asyncio.to_thread(client.sub_files, target)
        except Cd2Error as exc:
            raise HTTPException(status_code=502, detail=f"列目录失败:{str(exc)[:120]}") from exc
        finally:
            client.close()
        dirs = [
            {"path": f"{target.rstrip('/')}/{f.name}" if target != "/" else f"/{f.name}",
             "name": f.name}
            for f in files if f.is_dir
        ]
        return {"path": target, "items": dirs}

    # ── CD2 连通状态(总览/配置页展示用) ─────────────────────
    @app.get("/api/cd2/status")
    async def cd2_status(request: Request) -> dict:
        _current_user(config_path, _auth_header(request))
        cfg = load_config(config_path)
        if not cfg.cd2_address:
            return {"configured": False, "ok": False, "error": "未配置 CD2 地址"}
        from app.cd2.client import Cd2Client, Cd2Error

        client = Cd2Client(cfg.cd2_address, cfg.cd2_token,
                           username=cfg.cd2_username, password=cfg.cd2_password)
        try:
            info = await asyncio.to_thread(client.system_info)
            return {"configured": True, "ok": True, **info,
                    "dest": cfg.cd2_dest_path, "source": cfg.cd2_source_path}
        except Cd2Error as exc:
            return {"configured": True, "ok": False, "error": str(exc)[:160]}
        finally:
            client.close()

    # ── 频道监控(源频道 ed2k → 本项目卡片) ──────────────────
    def _monitor_or_503():
        mon = STATE.get("monitor")
        if mon is None:
            raise HTTPException(
                status_code=503,
                detail="Bot 未运行,频道监控不可用(先在配置页补齐配置并重启)",
            )
        return mon

    def _monitor_call(name: str, *args):
        """在 Bot 事件循环里跑监控协程:Telethon 客户端绑定该循环,不能跨循环直调。

        Web 跑在独立线程/FastAPI 线程池,故用 run_coroutine_threadsafe 投递。
        """
        mon = _monitor_or_503()
        loop = STATE.get("bot_loop")
        if loop is None:
            raise HTTPException(status_code=503, detail="Bot 事件循环未就绪,请稍后重试")
        fut = asyncio.run_coroutine_threadsafe(getattr(mon, name)(*args), loop)
        try:
            return fut.result(timeout=120)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"操作失败:{str(exc)[:120]}") from exc

    def _monitor_payload(cfg) -> dict:
        """监控状态:内存字段取自监控实例,游标行用本线程自己的 Store 读。"""
        mon = STATE.get("monitor")
        store = Store(cfg.db_path)
        try:
            states = store.monitor_states()
        finally:
            store.close()
        data = mon.runtime_status() if mon is not None else {
            "state": "stopped", "state_text": "Bot 未运行", "account": "",
            "connected": False, "login_stage": "", "login_phone": "",
            "last_error": "", "unreachable": {},
        }
        data["channels"] = channel_rows(cfg, states, data.pop("unreachable"))
        data["api_set"] = bool(cfg.tg_api_id and cfg.tg_api_hash)
        return data

    @app.get("/api/monitor")
    def monitor_state(request: Request) -> dict:
        _current_user(config_path, _auth_header(request))
        return _monitor_payload(load_config(config_path))

    @app.post("/api/monitor/login/start")
    def monitor_login_start(body: MonitorPhone, request: Request) -> dict:
        """发验证码(手机号含国家码);resend=false 时复用进行中的会话,不重复发码。"""
        _current_user(config_path, _auth_header(request))
        ok, message = _monitor_call("login_start", body.phone, body.resend)
        return {"success": ok, "message": message, **_monitor_payload(load_config(config_path))}

    @app.post("/api/monitor/login/code")
    def monitor_login_code(body: MonitorCode, request: Request) -> dict:
        _current_user(config_path, _auth_header(request))
        ok, message = _monitor_call("login_code", body.code)
        return {"success": ok, "message": message, **_monitor_payload(load_config(config_path))}

    @app.post("/api/monitor/login/password")
    def monitor_login_password(body: MonitorPassword, request: Request) -> dict:
        _current_user(config_path, _auth_header(request))
        ok, message = _monitor_call("login_password", body.password)
        return {"success": ok, "message": message, **_monitor_payload(load_config(config_path))}

    @app.post("/api/monitor/login/cancel")
    def monitor_login_cancel(request: Request) -> dict:
        _current_user(config_path, _auth_header(request))
        message = _monitor_call("login_cancel")
        return {"success": True, "message": message, **_monitor_payload(load_config(config_path))}

    @app.post("/api/monitor/logout")
    def monitor_logout(request: Request) -> dict:
        """退出登录:断开客户端并删除会话文件(下次需重新登录)。"""
        _current_user(config_path, _auth_header(request))
        message = _monitor_call("logout")
        return {"success": True, "message": message, **_monitor_payload(load_config(config_path))}

    # ── 网盘目录浏览(目录选择器数据源) ──────────────────────
    @app.get("/api/115/dirs")
    async def list_115_dirs(request: Request, cid: int = 0) -> dict:
        """列出网盘某目录下的子目录(需 115 Cookie;目录选择器用)。"""
        _current_user(config_path, _auth_header(request))
        cfg = load_config(config_path)
        if not cfg.pan115_cookie:
            raise HTTPException(status_code=503, detail="未配置 115 Cookie,无法浏览网盘目录")
        from app.pan115 import Pan115Reader

        reader = Pan115Reader(cfg.pan115_cookie)
        try:
            items = await reader.list_dir(cid, nf=1)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"列目录失败:{str(exc)[:100]}") from exc
        return {
            "cid": cid,
            "items": [{"cid": str(it["fid"]), "name": it["name"]} for it in items],
        }

    # ── 状态 / 历史 / 重启 ──────────────────────────────────
    @app.get("/api/status")
    async def status(request: Request) -> dict:
        _current_user(config_path, _auth_header(request))
        cfg = load_config(config_path)
        await _ensure_health(cfg)
        return {
            "version": __version__,
            "bot_running": STATE["bot_running"],
            "bot_error": STATE["bot_error"],
            "bot_ready": cfg.bot_ready(),
            "missing": cfg.problems(),
            "proxy": _health_cache["proxy"][1],
            "pan115": _health_cache["pan115"][1],
            "openlist": _health_cache["openlist"][1],
            "cd2": _health_cache["cd2"][1],
            "tools": _tools_status(),
            # 频道监控:未配置源频道时不参与健康判定(configured=0)
            "monitor": _monitor_status(cfg),
        }

    @app.get("/api/history")
    def history(request: Request, limit: int = 20) -> dict:
        """总览数据:最近推送 + 今日/累计统计。"""
        _current_user(config_path, _auth_header(request))
        cfg = load_config(config_path)
        store = Store(cfg.db_path)
        try:
            return {
                "items": store.recent(max(1, min(limit, 100))),
                **store.stats(),
            }
        finally:
            store.close()

    @app.post("/api/restart")
    def restart(request: Request) -> dict:
        _current_user(config_path, _auth_header(request))

        def _exit_later() -> None:
            import time

            time.sleep(1)
            os._exit(0)  # restart: unless-stopped 会带新配置拉起

        threading.Thread(target=_exit_later, daemon=True).start()
        return {"success": True, "message": "服务将在 1 秒后重启(容器自动拉起)"}

    # ── 前端(Vite 构建产物:index.html + /assets/*) ────────
    from fastapi.staticfiles import StaticFiles

    assets_dir = _STATIC.parent / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/{full_path:path}")
    def index(full_path: str) -> FileResponse:
        candidate = (_STATIC.parent / full_path).resolve()
        if full_path and candidate.is_file() and candidate.is_relative_to(_STATIC.parent):
            return FileResponse(candidate)
        # 入口 HTML 禁缓存:发版后浏览器立即用新页(assets 带 hash 可长缓存)
        return FileResponse(_STATIC, headers={"Cache-Control": "no-cache"})

    return app
