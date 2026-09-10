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

from app import __version__, auth
from app.channel_monitor import channel_rows
from app.config import (
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
_health_cache: dict = {"proxy": (0.0, None), "pan115": (0.0, None)}
_UID_RE = re.compile(r"UID=(\d+)")


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


class MonitorPhone(BaseModel):
    phone: str = ""
    resend: bool = False  # 显式要求重发验证码(默认复用进行中的会话,不重复发码)


class MonitorCode(BaseModel):
    code: str = ""


class MonitorPassword(BaseModel):
    password: str = ""


# 可经 Web 修改的配置键白名单(类型: s=字符串, i=整数, ids=ID 列表)
EDITABLE = {
    "tg_bot_token": "s", "tg_admin_ids": "ids",
    "tmdb_api_key": "s", "proxy_url": "s", "log_level": "s", "web_port": "i",
    "pan115_cookie": "s",
    "pipeline_root_dir": "s", "monitor_dirs": "s",
    "tg_api_id": "i", "tg_api_hash": "s", "monitor_channels": "s",
    "openlist_base_url": "s", "openlist_token": "s", "openlist_path": "s",
    "cd2_address": "s", "cd2_token": "s", "cd2_source_path": "s",
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
            v = raw.get(key, "" if kind != "i" else 0)
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
        now = time.monotonic()
        if now - _health_cache["proxy"][0] > _PROXY_TTL:
            _health_cache["proxy"] = (now, await _check_proxy(cfg.proxy_url))
        if now - _health_cache["pan115"][0] > _PAN115_TTL:
            _health_cache["pan115"] = (
                now, await asyncio.to_thread(_check_pan115, cfg.pan115_cookie)
            )
        # 频道监控:未配置源频道时不参与健康判定(configured=0)
        mon = STATE.get("monitor")
        if mon is not None:
            rs = mon.runtime_status()
            monitor = {
                "configured": len(cfg.monitor_channel_list()), "ready": cfg.monitor_ready(),
                "state": rs["state"], "state_text": rs["state_text"],
                "account": rs["account"], "connected": rs["connected"],
            }
        else:
            monitor = {
                "configured": len(cfg.monitor_channel_list()), "ready": cfg.monitor_ready(),
                "state": "stopped", "state_text": "Bot 未运行",
                "account": "", "connected": False,
            }
        return {
            "version": __version__,
            "bot_running": STATE["bot_running"],
            "bot_error": STATE["bot_error"],
            "bot_ready": cfg.bot_ready(),
            "missing": cfg.problems(),
            "proxy": _health_cache["proxy"][1],
            "pan115": _health_cache["pan115"][1],
            "monitor": monitor,
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
