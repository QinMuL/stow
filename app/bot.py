"""Telegram Bot:收链接 → 读分享 → TMDB → 卡片 → 频道。"""

from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import RetryAfter
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app import card
from app.config import Config
from app.media import aggregate
from app.pan115 import (
    Pan115Reader,
    ShareDead,
    ShareError,
    ShareLink,
    ShareNeedCode,
    ShareRateLimited,
    ShareSnapshotting,
    parse_links,
    parse_single,
)
from app.store import Store
from app.tmdb import TmdbClient

logger = logging.getLogger(__name__)

_HELP = (
    "📦 Stow · 媒体推送\n\n"
    "直接发送 115 分享链接(可带访问码;一条消息多个链接会逐个处理)\n"
    "或使用:/push <链接>\n\n"
    "Bot 会读取分享 → 匹配 TMDB → 推送海报卡片到频道。"
)


class StowBot:
    def __init__(self, cfg: Config, store: Store) -> None:
        self.cfg = cfg
        self.store = store
        self.reader = Pan115Reader()
        self.tmdb = TmdbClient(cfg.tmdb_api_key, cfg.proxy_url) if cfg.tmdb_api_key else None
        self._push_lock = asyncio.Lock()  # 投递串行,防 flood

    # ── 装配 ────────────────────────────────────────────────
    def build(self) -> Application:
        builder = Application.builder().token(self.cfg.tg_bot_token)
        if self.cfg.proxy_url:
            builder = builder.proxy(self.cfg.proxy_url).get_updates_proxy(self.cfg.proxy_url)
        builder = builder.concurrent_updates(True)
        app = builder.build()
        app.add_handler(CommandHandler("start", self._cmd_help))
        app.add_handler(CommandHandler("help", self._cmd_help))
        app.add_handler(CommandHandler("push", self._cmd_push))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_text))
        return app

    # ── 鉴权 ────────────────────────────────────────────────
    def _is_admin(self, update: Update) -> bool:
        uid = update.effective_user.id if update.effective_user else None
        return uid in self.cfg.tg_admin_ids

    # ── 命令 ────────────────────────────────────────────────
    async def _cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(_HELP)

    async def _cmd_push(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update):
            await update.effective_message.reply_text("⛔ 仅管理员可用")
            return
        arg = " ".join(ctx.args) if ctx.args else ""
        link = parse_single(arg)
        if link is None:
            await update.effective_message.reply_text("用法:/push <115 分享链接或分享码>")
            return
        await self._handle(update, link)

    async def _on_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        uid = update.effective_user.id if update.effective_user else None
        text = update.effective_message.text or ""
        logger.info("收到消息 from=%s: %s", uid, text[:50].replace("\n", " "))
        if not self._is_admin(update):
            logger.warning("非管理员(uid=%s,配置=%s),忽略", uid, self.cfg.tg_admin_ids)
            return
        text = update.effective_message.text or ""
        links = parse_links(text)
        if not links:
            link = parse_single(text)
            links = [link] if link else []
        if not links:
            logger.info("消息中未识别到 115 链接,忽略")
            return
        if len(links) == 1:
            await self._handle(update, links[0])
            return
        note = await update.effective_message.reply_text(
            f"📋 识别到 {len(links)} 个链接,逐个处理…"
        )
        for i, link in enumerate(links, 1):
            await self._handle(update, link, prefix=f"[{i}/{len(links)}] ")
        await note.delete()

    # ── 单链接处理主流程 ────────────────────────────────────
    async def _handle(self, update: Update, link: ShareLink, prefix: str = "") -> None:
        msg = update.effective_message
        if self.store.is_pushed(link.code):
            await msg.reply_text(f"{prefix}🔁 已推送过:{link.code}")
            return
        status = await msg.reply_text(f"{prefix}⏳ 正在读取分享…")
        try:
            files = await self.reader.read_share(link)
        except ShareNeedCode as exc:
            tip = (
                "访问码被分享者修改"
                if exc.code_changed
                else "该分享需要访问码,请在链接中带上 ?password= 或正文注明"
            )
            await status.edit_text(f"{prefix}🔐 {tip}")
            return
        except ShareDead:
            await status.edit_text(f"{prefix}💀 分享已失效或被取消")
            return
        except (ShareRateLimited, ShareSnapshotting) as exc:
            await status.edit_text(f"{prefix}⏳ {exc},稍后重试")
            return
        except ShareError as exc:
            await status.edit_text(f"{prefix}❌ {exc}")
            return

        media = aggregate([(f.name, f.size, f.is_dir) for f in files])
        match = await self.tmdb.match(media) if self.tmdb else None
        caption = card.render(media, match, link)

        try:
            await self._deliver(caption, match)
        except Exception as exc:  # noqa: BLE001 - 投递失败保留状态可重试
            logger.error("卡片投递失败:%s", exc, exc_info=exc)
            await status.edit_text(f"{prefix}❌ 投递失败:{str(exc)[:120]}")
            return

        title = (match.title if match else media.title) or link.code
        self.store.mark_pushed(link.code, title)
        n = len(media.video_files) or media.file_count
        label = f"🎬 {title}" + (f" ({match.year})" if match and match.year else "")
        await status.edit_text(f"{prefix}✅ 已推送 · {n} 文件 · {label}")

    # ── 频道投递(串行 + flood 退避) ─────────────────────────
    async def _deliver(self, caption: str, match) -> None:
        async with self._push_lock:
            poster = await self.tmdb.fetch_poster(match) if (match and self.tmdb) else None
            for _ in range(3):
                try:
                    if poster:
                        await self._bot_ref.send_photo(
                            self.cfg.tg_chat_id, photo=poster,
                            caption=caption, parse_mode=ParseMode.HTML,
                        )
                    else:
                        await self._bot_ref.send_message(
                            self.cfg.tg_chat_id, caption, parse_mode=ParseMode.HTML,
                        )
                    await asyncio.sleep(2)  # 限速
                    return
                except RetryAfter as exc:
                    await asyncio.sleep(exc.retry_after + 1)
            raise RuntimeError("连续触发限流,投递失败")

    _bot_ref = None  # run() 时注入


def run(cfg: Config, store: Store) -> None:
    """构建并阻塞运行。启动期网络瞬断自动重试(bootstrap_retries=-1);
    令牌无效仍快速失败,由调用方保持 Web 存活。"""
    bot = StowBot(cfg, store)
    app = bot.build()
    bot._bot_ref = app.bot
    logger.info("Stow Bot 启动(token 已配置,目标频道 %s)", cfg.tg_chat_id)
    app.run_polling(bootstrap_retries=-1)
