"""配置:config.json 唯一真源。Web 与 Bot 共用。

- load_config(strict=False):供 Web 使用,配置不全也返回(带 problems),
  Web 恒活,等待用户在网页上补齐
- load_config(strict=True):供 Bot 使用,必填缺失时 sys.exit 人话报错
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = "./data/config.json"
DEFAULT_WEB_PORT = 8686

# 敏感键:Web 展示脱敏;PUT 收到掩码值表示"未修改"
SENSITIVE_KEYS = ("tg_bot_token", "tmdb_api_key", "pan115_cookie")
MASK = "••••••••"


@dataclass
class ChannelConfig:
    """推送频道:归属预设决定哪类链接推到这里。"""

    chat_id: str
    preset: str = "115"  # "115" | "ed2k"
    title: str = ""      # 频道名(Bot 登记时自动带出,展示用)


@dataclass
class Config:
    tg_bot_token: str = ""
    tg_admin_ids: list[int] = field(default_factory=list)
    tmdb_api_key: str = ""
    proxy_url: str = ""
    # 115 登录 cookie(可选):读分享走 android/proapi 通道,绕开 115 对匿名
    # webapi share_snap 的指纹封锁(405);留空则匿名 web 兜底
    pan115_cookie: str = ""
    # 多频道:归属预设分流(115 链接 → preset=115 的频道;ed2k → preset=ed2k)。
    # 未登记某归属时,该类链接不推送,日志与 Bot 明确提示原因
    channels: list[ChannelConfig] = field(default_factory=list)
    # 转存流水线(/save <链接> 触发):暂存 → 整理 → 建分享 → 推送 → 归档/违规
    # 需 115 Cookie;目录相对网盘根,不存在自动创建;子目录由根目录派生
    pipeline_root_dir: str = "stow流水线"
    # 目录监控:这些网盘目录里出现新资源时,自动标准化+建分享+推送(逗号分隔)
    monitor_dirs: str = ""
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
        if not self.tg_admin_ids:
            out.append("tg_admin_ids 未填写(无人能使用 Bot)")
        if not self.tmdb_api_key:
            out.append("tmdb_api_key 未填写(卡片将缺少元数据,仍可推送)")
        return out

    def bot_ready(self) -> bool:
        fatal = (p for p in self.problems() if "tmdb" not in p)
        return not next(fatal, None)

    def channel_for(self, provider: str) -> str | None:
        """按链接类型选归属频道;未登记该归属返回 None(调用方提示,不推送)。"""
        for ch in self.channels:
            if ch.preset == provider:
                return ch.chat_id
        return None

    def pipeline_dirs(self) -> tuple[str, str, str]:
        """流水线三目录:(暂存, 已发布, 违规),由根目录派生。"""
        root = self.pipeline_root_dir.rstrip("/")
        return (f"{root}/待整理", f"{root}/已发布", f"{root}/违规")

    def monitor_dir_list(self) -> list[str]:
        """监控目录列表(逗号/换行分隔,去空)。"""
        parts = re.split(r"[,，\n]+", self.monitor_dirs or "")
        return [p.strip() for p in parts if p.strip()]

    def channels_summary(self) -> str:
        """启动日志用:归属分流一览。"""
        if not self.channels:
            return "归属频道:未登记(收到链接将提示先登记)"
        return ";".join(
            f"{ch.preset}→{ch.chat_id}" + (f"({ch.title})" if ch.title else "")
            for ch in self.channels
        )


def _clean(value: object) -> str:
    """清洗配置值:去空白;含非 ASCII(如中文占位符)视为未填写。"""
    s = str(value or "").strip()
    if not s or not s.isascii():
        return ""
    return s


def _dir(raw: dict, key: str, default: str) -> str:
    """目录配置:去空白;空值回退默认。"""
    return str(raw.get(key, default)).strip() or default


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

    channels = []
    for c in raw.get("channels", []):
        if isinstance(c, dict) and str(c.get("chat_id", "")).strip():
            preset = str(c.get("preset", "115")).strip().lower()
            channels.append(ChannelConfig(
                chat_id=str(c["chat_id"]).strip(),
                preset=preset if preset in ("115", "ed2k") else "115",
                title=str(c.get("title", "")).strip(),
            ))

    cfg = Config(
        tg_bot_token=_clean(raw.get("tg_bot_token")),
        tg_admin_ids=[int(x) for x in raw.get("tg_admin_ids", []) if str(x).strip().lstrip("-").isdigit()],
        tmdb_api_key=_clean(raw.get("tmdb_api_key")),
        proxy_url=_clean(raw.get("proxy_url")),
        pan115_cookie=_clean(raw.get("pan115_cookie")),
        channels=channels,
        pipeline_root_dir=_dir(raw, "pipeline_root_dir", "stow流水线"),
        monitor_dirs=str(raw.get("monitor_dirs", "")).strip(),
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
    raw.setdefault("tg_admin_ids", [])
    raw.setdefault("tmdb_api_key", "")
    raw.setdefault("proxy_url", "")
    raw.setdefault("pan115_cookie", "")
    raw.setdefault("channels", [])
    raw.pop("tg_chat_id", None)  # 默认频道概念已移除,旧配置顺手清理
    raw["admin_username"] = auth.DEFAULT_ADMIN_USER
    raw["admin_password_hash"] = auth.hash_password(auth.DEFAULT_ADMIN_PASSWORD)
    write_raw(raw, path)
