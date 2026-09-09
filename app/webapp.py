"""Web 配置台:登录 / 配置读写 / 服务状态 / 保存并重启。

Web 永远在线(哪怕 Bot 配置不全),用户在网页补齐配置后一键重启生效。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app import auth
from app.config import (
    MASK,
    SENSITIVE_KEYS,
    ensure_admin,
    load_config,
    read_raw,
    write_raw,
)
from app.store import Store

# 进程状态(bot 线程写入,web 读取)
STATE = {"bot_running": False, "bot_error": ""}

_STATIC = Path(__file__).parent.parent / "static" / "index.html"
_bearer_lock = threading.Lock()


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


# 可经 Web 修改的配置键白名单(类型: s=字符串, i=整数, ids=ID 列表)
EDITABLE = {
    "tg_bot_token": "s", "tg_chat_id": "s", "tg_admin_ids": "ids",
    "tmdb_api_key": "s", "proxy_url": "s", "log_level": "s", "web_port": "i",
    "pan115_cookie": "s",
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
            if kind == "i":
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
        return {"success": True, "bot_ready": cfg.bot_ready(), "missing": cfg.problems()}

    # ── 状态 / 历史 / 重启 ──────────────────────────────────
    @app.get("/api/status")
    def status(request: Request) -> dict:
        _current_user(config_path, _auth_header(request))
        cfg = load_config(config_path)
        return {
            "bot_running": STATE["bot_running"],
            "bot_error": STATE["bot_error"],
            "bot_ready": cfg.bot_ready(),
            "missing": cfg.problems(),
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
        return FileResponse(_STATIC)

    return app
