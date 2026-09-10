"""频道监控链路验证:模拟源频道消息 → ed2k 提取 → 去重 → 卡片投递到 ed2k 归属频道。

走的是监控真实路径(bot.push_link:读内嵌元数据 → TMDB → 卡片 → 归属频道),
只是把"源频道消息"换成命令行给的一段文本(无需 Telethon 登录即可验收卡片产出)。

用法:
  docker exec -i stow python - < scripts/e2e_monitor.py "<消息文本>"
  不带参数用内置示例(真实频道里的 ed2k 链接)。

注意:会真实投递到 ed2k 归属频道;已推送过的链接(hash 在库)自动跳过 → 重复跑不会刷屏。
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, "/app")

from telegram import Bot
from telegram.request import HTTPXRequest

from app.bot import StowBot
from app.channel_monitor import channel_rows
from app.config import load_config
from app.store import Store

# 取自用户监控频道的历史消息(老项目日志),含 {tmdbid-...} 标注 → TMDB 直连保真
_SAMPLE = (
    "📺 今日更新\n"
    "Crossroad～救命救急的约定～.2026.S01E08.第8集.1080p.Viu.WEB-DL.SDR.H.264.25fps.AAC.2.0."
    "{tmdbid-322879}.mkv\n"
    "ed2k://|file|Crossroad～救命救急的约定～.2026.S01E08.第8集.1080p.Viu.WEB-DL.SDR.H.264."
    "25fps.AAC.2.0.{tmdbid-322879}.mkv|881723377|27e418b74b968faafea3ca103e82e9e0|/\n"
)


async def main() -> None:
    text = sys.argv[1] if len(sys.argv) > 1 else _SAMPLE

    cfg = load_config("/app/data/config.json", strict=False)
    store = Store(Path("/app/data/stow.db"))
    bot = StowBot(cfg, store)
    tg = Bot(token=cfg.tg_bot_token, request=HTTPXRequest(proxy=cfg.proxy_url) or None)
    await tg.initialize()
    bot._bot_ref = tg  # 与 run() 相同的注入方式

    mon = bot.monitor
    rs = mon.runtime_status()
    print(f"监控状态:{rs['state_text']} 账号={rs['account'] or '(未登录)'} 连接={rs['connected']}")
    for row in channel_rows(cfg, store.monitor_states(), rs["unreachable"]):
        print(f"  源频道 {row['ref']} → {row['title'] or '(未解析)'} 游标={row['last_msg_id']}")
    target = cfg.channel_for("ed2k")
    print(f"ed2k 归属频道:{target or '(未登记 → 不会推送)'}")
    if target is None:
        print("[跳过] 未登记 ed2k 归属频道,先 /bind 或配置页登记")
        return

    pushed = await mon.push_text_links(text, source="e2e 模拟源频道")
    print(f"[完成] 推送成功 {pushed} 条(重复链接已跳过)")

    await tg.shutdown()
    store.close()


asyncio.run(main())
