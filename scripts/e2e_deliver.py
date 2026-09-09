"""端到端投递验证:直接调用 StowBot._deliver(生产投递路径,含 TMDB 按钮/回退/重试)。

用法:docker exec -i stow python - < scripts/e2e_deliver.py <分享码> <访问码>
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, "/app")

from app.bot import StowBot
from app.config import load_config
from app.media import analyze_share
from app.pan115 import Pan115Reader, ShareLink
from app.store import Store
from app.tmdb import TmdbClient
from telegram import Bot
from telegram.request import HTTPXRequest


async def main() -> None:
    code = sys.argv[1] if len(sys.argv) > 1 else "swsrg4f3hib"
    password = sys.argv[2] if len(sys.argv) > 2 else None

    cfg = load_config("/app/data/config.json", strict=False)
    bot = StowBot(cfg, Store(Path("/app/data/stow.db")))

    tg = Bot(token=cfg.tg_bot_token, request=HTTPXRequest(proxy=cfg.proxy_url) or None)
    await tg.initialize()
    bot._bot_ref = tg  # 与 run() 相同的注入方式

    link = ShareLink(code, password)
    files = await bot.reader.read_share(link)
    media = analyze_share(files)
    print(f"聚合:{media.title!r} ({media.year}) {media.media_type} q={media.quality_info[:3]}")
    t = TmdbClient(cfg.tmdb_api_key, cfg.proxy_url)
    try:
        details = await t.match(media)
    finally:
        await t.aclose()
    print(f"TMDB:{details['title']!r} id={details['tmdb_id'] if details else None}")

    await bot._deliver(media, details, link, files)
    print("[OK] _deliver(生产路径)投递成功")
    await tg.shutdown()


asyncio.run(main())
