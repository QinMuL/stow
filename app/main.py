"""Stow 入口:加载配置 → 日志 → Bot 阻塞运行。"""

from __future__ import annotations

import logging
import os

from app.bot import run
from app.config import load_config
from app.logging_setup import setup_logging
from app.store import Store


def main() -> None:
    cfg = load_config(os.environ.get("STOW_CONFIG"))
    setup_logging(cfg.log_level, cfg.log_dir)
    logger = logging.getLogger("stow")
    logger.info("Stow 启动:频道=%s 管理员=%s", cfg.tg_chat_id, cfg.tg_admin_ids)
    if cfg.proxy_url:
        logger.info("代理(TG/TMDB):%s;115 恒直连", cfg.proxy_url)

    store = Store(cfg.db_path)
    try:
        run(cfg, store)
    finally:
        store.close()


if __name__ == "__main__":
    main()
