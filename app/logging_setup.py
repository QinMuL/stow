"""日志:控制台(INFO)+ 文件(DEBUG,按大小轮转,保留 7 天)。"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path


def setup_logging(level: str, log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    fmt = logging.Formatter("%(asctime)s %(levelname)-5s [%(name)s] %(message)s")

    console = logging.StreamHandler()
    console.setLevel(getattr(logging, level, logging.INFO))
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "stow.log", maxBytes=5 * 1024 * 1024,
        backupCount=10, encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
