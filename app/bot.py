"""Telegram Bot:收链接 → 读分享 → TMDB → 卡片 → 频道。"""

from __future__ import annotations

import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app import card
from app.config import ChannelConfig, Config
from app.links import ParsedLink, ed2k_file, parse_all, parse_one
from app.media import AggregatedMedia, analyze_share
from app.pan115 import (
    Pan115Reader,
    ShareDead,
    ShareError,
    ShareFile,
    ShareLink,
    ShareNeedCode,
    ShareRateLimited,
    ShareSnapshotting,
)
from app.store import Store
from app.tmdb import TmdbClient, image_url

logger = logging.getLogger(__name__)


class DeliveryUncertain(Exception):
    """投递超时:消息可能已送达,重试/回退都会重复投递,只能让用户核实。"""


_PRESET_LABEL = {"115": "115 网盘", "ed2k": "ed2k"}

_HELP = (
    "📦 Stow · 媒体推送\n\n"
    "直接发送 115 分享链接或 ed2k 链接(一条消息多个会逐个处理)\n"
    "或使用:/push <链接>\n\n"
    "📁 登记推送频道:把频道里的任意一条消息**转发**给本 Bot,"
    "再按提示选归属(115 网盘 / ed2k)即可,无需重启\n"
    "Bot 会读取分享 → 匹配 TMDB → 按链接类型推到对应频道。"
)


class StowBot:
    def __init__(self, cfg: Config, store: Store, config_path: str | None = None) -> None:
        self.cfg = cfg
        self.store = store
        self.config_path = config_path
        self.reader = Pan115Reader(cfg.pan115_cookie)
        self.tmdb = TmdbClient(cfg.tmdb_api_key, cfg.proxy_url) if cfg.tmdb_api_key else None
        self._push_lock = asyncio.Lock()  # 投递串行,防 flood
        self._pending_channels: dict[str, str] = {}  # 转发登记:chat_id → 标题(回调取)

    # ── 装配 ────────────────────────────────────────────────
    def build(self) -> Application:
        from telegram.request import HTTPXRequest

        proxy = self.cfg.proxy_url or None
        builder = (
            Application.builder()
            .token(self.cfg.tg_bot_token)
            # PTB 默认发送读写超时仅 5s,经代理发媒体经常超时;放宽(超时根因)
            .request(HTTPXRequest(
                proxy=proxy, connect_timeout=10, read_timeout=30,
                write_timeout=30, media_write_timeout=60,
            ))
            .get_updates_request(HTTPXRequest(proxy=proxy, connect_timeout=10, read_timeout=15))
            .concurrent_updates(True)
        )
        app = builder.build()
        app.add_handler(CommandHandler("start", self._cmd_help))
        app.add_handler(CommandHandler("help", self._cmd_help))
        app.add_handler(CommandHandler("push", self._cmd_push))
        app.add_handler(CallbackQueryHandler(self._on_channel_preset))
        # 频道消息:提示管理员用"转发给 Bot"登记(不再自动回填,多频道归属需人工选择)
        app.add_handler(
            MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, self._on_added_to_channel),
            group=-1,
        )
        app.add_handler(MessageHandler(filters.FORWARDED, self._on_forward))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_text))
        return app

    # ── 频道登记(转发给 Bot) ────────────────────────────────
    async def _on_forward(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """管理员把频道消息转发给 Bot → 弹归属选择按钮,选定即登记(热生效)。"""
        msg = update.effective_message
        if not self._is_admin(update):
            logger.warning("非管理员转发,忽略(uid=%s)", update.effective_user and update.effective_user.id)
            return
        # PTB v22:转发来源在 forward_origin(MessageOriginChannel.chat 才带来源频道)
        origin = getattr(msg, "forward_origin", None)
        fwd = getattr(origin, "chat", None) if origin is not None else None
        if fwd is None or fwd.type not in ("channel", "supergroup"):
            await msg.reply_text(
                "没识别到频道来源——请转发**频道里**的消息(不是个人聊天或匿名频道消息)。\n"
                "若频道开了「隐藏成员/匿名」,任意一条带来源的频道消息都可以。"
            )
            return
        chat_id = str(fwd.id)
        title = getattr(fwd, "title", "") or chat_id
        # callback_data 上限 64 字节,长标题放不下 → 标题暂存内存,回调按 chat_id 取
        self._pending_channels[chat_id] = title
        buttons = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("💿 115 网盘", callback_data=f"chreg:115:{chat_id}"),
                InlineKeyboardButton("🔗 ed2k", callback_data=f"chreg:ed2k:{chat_id}"),
            ]
        ])
        await msg.reply_text(f"频道「{title}」({chat_id})登记到哪个归属?", reply_markup=buttons)

    async def _on_channel_preset(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """归属选择回调:写 config + 运行中热生效。"""
        query = update.callback_query
        if query is None or not self._is_admin(update):
            return
        try:
            _, preset, chat_id = query.data.split(":", 2)
        except ValueError:
            await query.answer("参数错误")
            return
        if preset not in _PRESET_LABEL:
            await query.answer("未知归属")
            return
        title = self._pending_channels.get(chat_id, chat_id)

        from app.config import read_raw, write_raw

        raw = read_raw(self.config_path)
        channels = [c for c in raw.get("channels", []) if str(c.get("chat_id")) != chat_id]
        channels.append({"chat_id": chat_id, "preset": preset, "title": title})
        raw["channels"] = channels
        write_raw(raw, self.config_path)
        # 热生效:同名预设替换
        self.cfg.channels = [
            c for c in self.cfg.channels if c.chat_id != chat_id
        ] + [ChannelConfig(chat_id=chat_id, preset=preset, title=title)]

        await query.edit_message_text(
            f"✅ 已登记「{title}」({chat_id}) → {_PRESET_LABEL[preset]}\n"
            f"对应类型链接将推送到该频道,即时生效。"
        )

    async def _on_added_to_channel(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Bot 被加进频道时,提示管理员用转发方式登记。"""
        chat = update.effective_chat
        if chat is None:
            return
        for member in update.effective_message.new_chat_members or []:
            if member.id == ctx.bot.id:
                logger.info("Bot 被加入 %s(%s),等待管理员转发登记", chat.title, chat.id)
                try:
                    await ctx.bot.send_message(
                        self.cfg.tg_admin_ids[0],
                        f"Bot 已加入频道「{chat.title}」({chat.id})。\n"
                        "把该频道里的任意一条消息转发给我,即可完成登记。",
                    )
                except (IndexError, Exception):  # noqa: BLE001 - 管理员不可达不阻塞
                    logger.info("无法私聊通知管理员,跳过")

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
        link = parse_one(arg)
        if link is None:
            await update.effective_message.reply_text("用法:/push <115 分享链接/分享码 或 ed2k 链接>")
            return
        await self._handle(update, link)

    async def _on_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        # 转发登记走 _on_forward;这里只响应私聊/群里的直接文本
        if update.effective_user is None or update.effective_message.forward_from_chat:
            return
        uid = update.effective_user.id
        text = update.effective_message.text or ""
        logger.info("收到消息 from=%s: %s", uid, text[:50].replace("\n", " "))
        if not self._is_admin(update):
            logger.warning("非管理员(uid=%s,配置=%s),忽略", uid, self.cfg.tg_admin_ids)
            return
        links = parse_all(text)
        if not links and (p := parse_one(text)):
            links = [p]
        if not links:
            logger.info("消息中未识别到分享链接,忽略")
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
    async def _handle(self, update: Update, link: ParsedLink, prefix: str = "") -> None:
        msg = update.effective_message
        if self.store.is_pushed(link.key):
            await msg.reply_text(f"{prefix}🔁 已推送过:{link.dedup_display}")
            return
        verb = "ed2k 链接" if link.provider == "ed2k" else "分享"
        status = await msg.reply_text(f"{prefix}⏳ 正在读取{verb}…")
        files, media = await self._load_media(link, status, prefix)
        if media is None:
            return  # _load_media 已回报错误
        details = await self.tmdb.match(media) if self.tmdb else None
        logger.info("TMDB 匹配:%s → %s", media.title[:40], details["title"] if details else "未命中")

        target = self.cfg.channel_for(link.provider)
        try:
            await self._deliver(media, details, link, files, target)
        except DeliveryUncertain as exc:
            # 不标记已推送:若实际没送达,用户重发链接即可重推
            logger.warning("投递超时(结果不确定):%s", exc)
            await status.edit_text(f"{prefix}⚠️ {exc}")
            return
        except Exception as exc:  # noqa: BLE001 - 投递失败保留状态可重试
            logger.error("卡片投递失败:%s", exc, exc_info=exc)
            await status.edit_text(f"{prefix}❌ 投递失败:{str(exc)[:120]}")
            return

        title = (details["title"] if details else media.title) or link.dedup_display
        self.store.mark_pushed(link.key, title)
        n = media.file_count or len(files)
        label = f"🎬 {title}" + (f" ({details['year']})" if details and details["year"] else "")
        await status.edit_text(f"{prefix}✅ 已推送 · {n} 文件 · {label}")

    async def _load_media(self, link: ParsedLink, status, prefix: str):
        """按 provider 读取内容 → 聚合。失败已回报,返回 (files, None)。"""
        if link.provider == "ed2k":
            name, size, _ = ed2k_file(link.code)
            return [ShareFile(name, size, False)], analyze_share([ShareFile(name, size, False)])
        try:
            share = ShareLink(link.code, link.password)
            files = await self.reader.read_share(share)
            logger.info("分享读取完成:%s → %d 个文件", link.code, len(files))
        except ShareNeedCode as exc:
            tip = (
                "访问码被分享者修改"
                if exc.code_changed
                else "该分享需要访问码,请在链接中带上 ?password= 或正文注明"
            )
            logger.warning("分享 %s 需要访问码(code_changed=%s)", link.code, exc.code_changed)
            await status.edit_text(f"{prefix}🔐 {tip}")
            return [], None
        except ShareDead:
            logger.warning("分享 %s 已失效", link.code)
            await status.edit_text(f"{prefix}💀 分享已失效或被取消")
            return [], None
        except (ShareRateLimited, ShareSnapshotting) as exc:
            logger.warning("分享 %s 限速/快照中:%s", link.code, exc)
            await status.edit_text(f"{prefix}⏳ {exc},稍后重试")
            return [], None
        except ShareError as exc:
            logger.error("分享 %s 读取失败:%s", link.code, exc, exc_info=True)
            await status.edit_text(f"{prefix}❌ {exc}")
            return [], None
        media = analyze_share(files) or AggregatedMedia(
            title=link.code, file_count=len(files),
            total_size=sum(f.size for f in files if not f.is_dir),
        )
        return files, media

    # ── 频道投递(串行;仅 RetryAfter 重试——超时重试会重复投递) ──
    async def _send_with_retry(self, sender):
        """发送并返回 Message;记 message_id 便于事后撤卡。"""
        for attempt in range(3):
            try:
                sent = await sender()
                mid = getattr(sent, "message_id", None)
                if mid:
                    logger.info("已投递到频道 message_id=%s", mid)
                return sent
            except RetryAfter as exc:
                if attempt == 2:
                    raise
                logger.warning("Flood control,%ss 后重试(第 %d 次)", exc.retry_after, attempt + 1)
                await asyncio.sleep(exc.retry_after + 1)

    async def _deliver(self, media, details: dict | None, link: ParsedLink, files, target: str) -> None:
        async with self._push_lock:
            markup = None
            if details and details.get("tmdb_id"):
                markup = InlineKeyboardMarkup(
                    [[InlineKeyboardButton("📚 TMDB 详情", url=card.tmdb_url(details))]]
                )
            # 海报字节本地经代理下载(URL 直发依赖 Telegram 拉图,易超时)
            photo = None
            url = image_url(details) if details else None
            if url and self.tmdb:
                photo = await self.tmdb.fetch_image(url)
            if photo:
                caption = card.render_caption(media, details, link, files)
                try:
                    await self._send_with_retry(lambda: self._bot_ref.send_photo(
                        target, photo=photo,
                        caption=caption, parse_mode=ParseMode.HTML,
                        reply_markup=markup,
                    ))
                    await asyncio.sleep(2)  # 限速
                    return
                except TimedOut as exc:
                    # 超时≠失败:消息可能已送达,重试/回退都会造成重复投递
                    raise DeliveryUncertain(
                        "投递超时,消息可能已送达,请到频道核实"
                    ) from exc
                except Exception as exc:  # noqa: BLE001 - 海报发送失败回退纯文本
                    logger.warning("send_photo 失败,回退纯文本:%s", exc)
            text = card.render_text(media, details, link, files)
            await self._send_with_retry(lambda: self._bot_ref.send_message(
                target, text, parse_mode=ParseMode.HTML,
                reply_markup=markup,
            ))
            await asyncio.sleep(2)  # 限速

    _bot_ref = None  # run() 时注入


def run(cfg: Config, store: Store, config_path: str | None = None) -> None:
    """构建并阻塞运行。启动期网络瞬断自动重试(bootstrap_retries=-1);
    令牌无效仍快速失败,由调用方保持 Web 存活。"""
    bot = StowBot(cfg, store, config_path)
    app = bot.build()
    bot._bot_ref = app.bot
    logger.info("Stow Bot 启动(token 已配置,目标频道 %s)", cfg.tg_chat_id)
    app.run_polling(bootstrap_retries=-1)
