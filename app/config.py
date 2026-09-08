"""配置加载:config.json(手工编辑)。缺失/非法时给人话报错。"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = "./data/config.json"


@dataclass
class Config:
    tg_bot_token: str = ""
    tg_chat_id: str = ""          # 推送目标频道
    tg_admin_ids: list[int] = field(default_factory=list)
    tmdb_api_key: str = ""
    proxy_url: str = ""           # TG/TMDB 走;115 恒直连
    data_dir: str = "./data"
    log_level: str = "INFO"

    @property
    def db_path(self) -> Path:
        return Path(self.data_dir) / "stow.db"

    @property
    def log_dir(self) -> Path:
        return Path(self.data_dir) / "logs"


def load_config(path: str | Path | None = None) -> Config:
    path = Path(path or DEFAULT_CONFIG_PATH)
    if not path.exists():
        sys.exit(
            f"[stow] 配置文件不存在:{path}\n"
            f"       请复制 config.example.json 为 {path} 并填写后再启动。"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"[stow] 配置文件不是合法 JSON:{exc}")

    cfg = Config(
        tg_bot_token=str(raw.get("tg_bot_token", "")).strip(),
        tg_chat_id=str(raw.get("tg_chat_id", "")).strip(),
        tg_admin_ids=[int(x) for x in raw.get("tg_admin_ids", []) if str(x).strip().lstrip("-").isdigit()],
        tmdb_api_key=str(raw.get("tmdb_api_key", "")).strip(),
        proxy_url=str(raw.get("proxy_url", "")).strip(),
        data_dir=str(raw.get("data_dir", "./data")),
        log_level=str(raw.get("log_level", "INFO")).upper(),
    )

    problems = []
    if not cfg.tg_bot_token:
        problems.append("tg_bot_token 未填写(Bot 无法启动)")
    if not cfg.tg_chat_id:
        problems.append("tg_chat_id 未填写(卡片无处投递)")
    if not cfg.tg_admin_ids:
        problems.append("tg_admin_ids 未填写(无人能使用 Bot)")
    if not cfg.tmdb_api_key:
        problems.append("tmdb_api_key 未填写(卡片将缺少元数据,仍可推送)")
    if problems:
        fatal = [p for p in problems if "tmdb" not in p]
        for p in problems:
            print(f"[stow] 配置缺项:{p}")
        if fatal:
            sys.exit("[stow] 必填项缺失,退出。填写 " + str(path) + " 后重试。")
    return cfg
