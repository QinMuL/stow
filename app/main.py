"""Stow 入口:Web 配置台(恒启)+ Bot(配置齐全才启动)。

Web 永远在线——哪怕零配置,用户也能打开网页补齐;保存后点"保存并重启"
(容器 restart 策略带新配置拉起)。Bot 在主线程阻塞运行;配置不全时主线程
等待,Web 仍可服务。
"""

from __future__ import annotations

import logging
import threading

from app.config import DEFAULT_CONFIG_PATH, ensure_admin, load_config
from app.logging_setup import setup_logging
from app.store import Store


def _start_web(port: int, config_path: str) -> None:
    import uvicorn

    from app.webapp import STATE, create_app

    app = create_app(config_path)
    threading.Thread(
        target=uvicorn.run,
        args=(app,),
        kwargs={"host": "0.0.0.0", "port": port, "log_level": "warning", "access_log": False},
        daemon=True,
        name="web",
    ).start()
    logging.getLogger("stow").info("Web 配置台:http://0.0.0.0:%s", port)
    _ = STATE  # webapp 进程状态共享( bot 线程写入)


def _run_bot(config_path: str) -> None:
    from app.bot import run
    from app.webapp import STATE

    cfg = load_config(config_path, strict=True)
    store = Store(cfg.db_path)
    STATE["bot_running"] = True
    STATE["bot_error"] = ""
    try:
        run(cfg, store)
    except Exception as exc:  # noqa: BLE001 - Bot 异常不拖垮 Web
        STATE["bot_running"] = False
        STATE["bot_error"] = str(exc)[:200]
        logging.getLogger("stow").error("Bot 退出:%s", exc, exc_info=exc)
    finally:
        STATE["bot_running"] = False
        store.close()


def main() -> None:
    import os

    config_path = os.environ.get("STOW_CONFIG", DEFAULT_CONFIG_PATH)
    ensure_admin(config_path)  # 无凭据时落默认 admin/admin
    cfg = load_config(config_path)  # 非严格:允许不全
    setup_logging(cfg.log_level, cfg.log_dir)
    logger = logging.getLogger("stow")

    _start_web(cfg.web_port, config_path)

    if cfg.bot_ready():
        logger.info("配置齐全,启动 Bot(频道 %s)", cfg.tg_chat_id)
        _run_bot(config_path)
        # Bot 退出(异常/令牌失效)不拖垮 Web:保活等待,网页可查原因、改配置
        logger.error("Bot 已退出——Web 配置台保持运行,可在总览页查看原因、修改配置后重启")
        threading.Event().wait()
    else:
        for p in cfg.problems():
            logger.warning("配置缺项:%s", p)
        logger.warning("Bot 暂不启动——打开 Web 配置台补齐后点「保存并重启」")
        threading.Event().wait()  # Web 恒活


if __name__ == "__main__":
    main()
