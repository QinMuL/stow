"""端到端链路验证:读分享 → 解析聚合 → TMDB 匹配 → 渲染 → 真实投递频道。

用法:docker exec -i stow python - < scripts/e2e_check.py [分享码] [访问码]
与 bot 生产路径共用同一批模块(Pan115Reader/media/tmdb/card),仅触发方式不同。
"""

import asyncio
import sys

sys.path.insert(0, "/app")

from app import card
from app.config import load_config
from app.media import analyze_share
from app.pan115 import Pan115Reader, ShareLink
from app.tmdb import TmdbClient, image_url


async def main() -> None:
    code = sys.argv[1] if len(sys.argv) > 1 else "swsrac33hib"
    password = sys.argv[2] if len(sys.argv) > 2 else None

    cfg = load_config("/app/data/config.json", strict=False)
    link = ShareLink(code, password)
    files = await Pan115Reader().read_share(link)
    print(f"[1] 读分享 OK:{len(files)} 项,前 6 项:")
    for f in files[:6]:
        print(f"    {'DIR ' if f.is_dir else 'FILE'} {f.name}  {f.size}")

    media = analyze_share(files)
    print(f"[2] 聚合:title={media.title!r} year={media.year} type={media.media_type} "
          f"seasons={media.seasons} file_count={media.file_count}")
    print(f"    quality_info={media.quality_info}")
    print(f"    tmdb_id={media.tmdb_id} alt_queries={media.alt_queries} "
          f"episode={media.episode_start}-{media.episode_end}")

    tmdb = TmdbClient(cfg.tmdb_api_key, cfg.proxy_url)
    details = await tmdb.match(media)
    if not details:
        print("[3] TMDB 未命中!")
    else:
        print(f"[3] TMDB:{details['title']!r} ({details['year']}) id={details['tmdb_id']} "
              f"vote={details['vote_average']} cast={details['cast'][:3]} countries={details['countries']}")

    caption = card.render_caption(media, details, link, files)
    print("---- caption 预览 ----")
    print(caption)
    print("---- 预览结束 ----")

    from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.constants import ParseMode
    from telegram.request import HTTPXRequest

    request = HTTPXRequest(proxy=cfg.proxy_url) if cfg.proxy_url else None
    async with Bot(token=cfg.tg_bot_token, request=request) as bot:
        markup = None
        if details and details.get("tmdb_id"):
            markup = InlineKeyboardMarkup(
                [[InlineKeyboardButton("📚 TMDB 详情", url=card.tmdb_url(details))]]
            )
        photo = image_url(details) if details else None
        if photo:
            sent = await bot.send_photo(
                cfg.tg_chat_id, photo=photo, caption=caption,
                parse_mode=ParseMode.HTML, reply_markup=markup,
            )
            mode = "海报"
        else:
            sent = await bot.send_message(
                cfg.tg_chat_id, card.render_text(media, details, link, files),
                parse_mode=ParseMode.HTML, reply_markup=markup,
            )
            mode = "纯文本"
        print(f"[4] 已投递({mode}) message_id={sent.message_id} chat={sent.chat.id}")
    await tmdb.aclose()


asyncio.run(main())
