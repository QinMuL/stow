"""配置:config.json 唯一真源。Web 与 Bot 共用。

- load_config(strict=False):供 Web 使用,配置不全也返回(带 problems),
  Web 恒活,等待用户在网页上补齐
- load_config(strict=True):供 Bot 使用,必填缺失时 sys.exit 人话报错
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = "./data/config.json"
DEFAULT_WEB_PORT = 8686

# 敏感键:Web 展示脱敏;PUT 收到掩码值表示"未修改"
SENSITIVE_KEYS = ("tg_bot_token", "tmdb_api_key", "pan115_cookie")
MASK = "••••••••"


@dataclass
class Config:
    tg_bot_token: str = ""
    tg_chat_id: str = ""
    tg_admin_ids: list[int] = field(default_factory=list)
    tmdb_api_key: str = ""
    proxy_url: str = ""
    # 115 登录 cookie(可选):读分享走 android/proapi 通道,绕开 115 对匿名
    # webapi share_snap 的指纹封锁(405);留空则匿名 web 兜底
    pan115_cookie: str = ""
    data_dir: str = "./data"
    log_level: str = "INFO"
    web_port: int = DEFAULT_WEB_PORT
    # Web 管理员(哈希存储;首启自动建 admin/admin)
    admin_username: str = ""
    admin_password_hash: str = ""

    @property
    def db_path(self) -> Path:
        return Path(self.data_dir) / "stow.db"

    @property
    def log_dir(self) -> Path:
        return Path(self.data_dir) / "logs"

    def problems(self) -> list[str]:
        """必填项缺失清单(空 = 可跑 Bot)。"""
        out = []
        if not self.tg_bot_token:
            out.append("tg_bot_token 未填写(Bot 无法启动)")
        if not self.tg_chat_id:
            out.append("tg_chat_id 未填写(卡片无处投递)")
        if not self.tg_admin_ids:
            out.append("tg_admin_ids 未填写(无人能使用 Bot)")
        if not self.tmdb_api_key:
            out.append("tmdb_api_key 未填写(卡片将缺少元数据,仍可推送)")
        return out

    def bot_ready(self) -> bool:
        fatal = (p for p in self.problems() if "tmdb" not in p)
        return not next(fatal, None)


def _clean(value: object) -> str:
    """清洗配置值:去空白;含非 ASCII(如中文占位符)视为未填写。"""
    s = str(value or "").strip()
    if not s or not s.isascii():
        return ""
    return s


def read_raw(path: str | Path | None = None) -> dict:
    path = Path(path or DEFAULT_CONFIG_PATH)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def write_raw(raw: dict, path: str | Path | None = None) -> None:
    path = Path(path or DEFAULT_CONFIG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    tmp.replace(path)


def load_config(path: str | Path | None = None, strict: bool = False) -> Config:
    path = Path(path or DEFAULT_CONFIG_PATH)
    if not path.exists():
        hint = f"请复制 config.example.json 为 {path} 并填写后再启动。"
        if strict:
            sys.exit(f"[stow] 配置文件不存在:{path}\n       {hint}")
        return Config()

    raw = read_raw(path)
    if not raw and path.exists():
        if strict:
            sys.exit(f"[stow] 配置文件不是合法 JSON:{path}")
        return Config()

    cfg = Config(
        tg_bot_token=_clean(raw.get("tg_bot_token")),
        tg_chat_id=_clean(raw.get("tg_chat_id")),
        tg_admin_ids=[int(x) for x in raw.get("tg_admin_ids", []) if str(x).strip().lstrip("-").isdigit()],
        tmdb_api_key=_clean(raw.get("tmdb_api_key")),
        proxy_url=_clean(raw.get("proxy_url")),
        pan115_cookie=_clean(raw.get("pan115_cookie")),
        data_dir=str(raw.get("data_dir", "./data")),
        log_level=str(raw.get("log_level", "INFO")).upper(),
        web_port=int(raw.get("web_port", DEFAULT_WEB_PORT) or DEFAULT_WEB_PORT),
        admin_username=str(raw.get("admin_username", "")).strip(),
        admin_password_hash=str(raw.get("admin_password_hash", "")).strip(),
    )

    if strict:
        problems = cfg.problems()
        if problems:
            for p in problems:
                print(f"[stow] 配置缺项:{p}")
            if not cfg.bot_ready():
                sys.exit(f"[stow] 必填项缺失,退出。填写 {path} 后重试(或经 Web 配置页填写)。")
    return cfg


def ensure_admin(cfg_path: str | Path | None = None) -> None:
    """config.json 无管理员凭据时写入默认 admin/admin(幂等)。"""
    path = Path(cfg_path or DEFAULT_CONFIG_PATH)
    from app import auth

    raw = read_raw(path)
    if raw.get("admin_username") and raw.get("admin_password_hash"):
        return
    raw.setdefault("tg_bot_token", "")
    raw.setdefault("tg_chat_id", "")
    raw.setdefault("tg_admin_ids", [])
    raw.setdefault("tmdb_api_key", "")
    raw.setdefault("proxy_url", "")
    raw.setdefault("pan115_cookie", "")
    raw["admin_username"] = auth.DEFAULT_ADMIN_USER
    raw["admin_password_hash"] = auth.hash_password(auth.DEFAULT_ADMIN_PASSWORD)
    write_raw(raw, path)
