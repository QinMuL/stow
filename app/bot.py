"""Telegram Bot:收链接 → 读分享 → TMDB → 卡片 → 频道。"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

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
from app.channel_monitor import ChannelMonitor
from app.config import ChannelConfig, Config
from app.fetcher import ResourceFetcher
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
from app.pipeline import SavePipeline
from app.processor import ProcessChain
from app.saver import Pan115Saver
from app.store import Store
from app.tmdb import TmdbClient, image_url
from app.uploader import Uploader

logger = logging.getLogger(__name__)


class DeliveryUncertain(Exception):
    """投递超时:消息可能已送达,重试/回退都会重复投递,只能让用户核实。"""


@dataclass
class PushResult:
    """一次链接推送的结果(手动推送与频道监控共用)。"""

    ok: bool
    text: str
    uncertain: bool = False  # 投递超时(可能已送达):不得重试、不得标记已推送


_PRESET_LABEL = {"115": "115链接推送频道", "ed2k": "ed2k链接推送频道"}
_HEARTBEAT_INTERVAL = 60.0   # 心跳打点间隔(秒);健康判据按"连丢 3 拍"判定卡死

_HELP = (
    "📦 Stow · 媒体推送\n\n"
    "直接发送 115 分享链接或 ed2k 链接(一条消息多个会逐个处理)\n"
    "或使用:/push <链接>\n\n"
    "💾 转存流水线:/save <115链接>\n"
    "转存到网盘 → 目录标准化 → 建永久分享 → 审核通过后自动推送并归档\n\n"
    "📂 目录监控:/scan 立即扫描监控目录(常规 30 分钟自动一轮)\n\n"
    "📡 频道监控(Web 全局配置页配置):盯住源频道,把里面的 ed2k 链接\n"
    "按本卡片模板自动转发到 ed2k 链接推送频道\n\n"
    "📁 登记推送频道(二选一):\n"
    "① 先发 /bind,5 分钟内把频道里的任意一条消息转发给本 Bot\n"
    "② 转发时在附言里写 /bind(随时有效)\n"
    "弹出面板选归属(115链接推送频道 / ed2k链接推送频道),即时生效;✖ 可取消\n\n"
    "Bot 会读取分享 → 匹配 TMDB → 按链接类型推到对应频道。"
)


class StowBot:
    def __init__(self, cfg: Config, store: Store, config_path: str | None = None) -> None:
        self.cfg = cfg
        self.store = store
        self.config_path = config_path
        self.reader = Pan115Reader(cfg.pan115_cookie)
        self.tmdb = TmdbClient(cfg.tmdb_api_key, cfg.proxy_url) if cfg.tmdb_api_key else None
        self.saver = Pan115Saver(self.reader)
        self.pipeline = SavePipeline(self)
        self.monitor = ChannelMonitor(self)  # TG 频道监控(ed2k → 卡片 → ed2k 频道)
        self.fetcher = ResourceFetcher(self)  # 获取段:openlist 监控 → 移动到本地
        self.processor = ProcessChain(self)   # 处理段:探测/重命名/ed2k/推卡 → clouddrive
        self.uploader = Uploader(self)         # 上传段:clouddrive → 115(CD2 移动)
        self._push_lock = asyncio.Lock()  # 投递串行,防 flood
        self._pending_channels: dict[str, str] = {}  # 登记选择中:chat_id → 标题(回调取)
        self._bind_wait: dict[int, float] = {}  # /bind 等待期:uid → 截止时间戳

    # ── 装配 ────────────────────────────────────────────────
    def build(self) -> Application:
        from telegram.request import HTTPXRequest

        proxy = self.cfg.proxy_url or None

        async def _post_init(app: Application) -> None:
            self._bot_ref = app.bot
            self.pipeline.start_loop()  # /save 流水线审核轮询
            self.monitor.start()        # TG 频道监控(源频道 → ed2k 卡片)
            self.fetcher.start()        # 获取段:openlist 监控目录 → 移动到 media/openlist
            self.processor.start()      # 处理段:media/openlist → 探测/ed2k/推卡 → clouddrive
            self.uploader.start()       # 上传段:clouddrive → 115(CD2 移动,串行)
            # Web 登录端点需在 Bot 事件循环里驱动 Telethon 客户端(Web 跑在另一线程)
            from app.webapp import STATE

            STATE["monitor"] = self.monitor
            STATE["fetcher"] = self.fetcher
            STATE["processor"] = self.processor
            STATE["uploader"] = self.uploader
            STATE["bot_loop"] = asyncio.get_running_loop()
            # 心跳打点:健康判据用它识别"进程在、事件循环却卡住不进展"
            self._beat_task = asyncio.create_task(self._heartbeat_loop())

        builder = (
            Application.builder()
            .token(self.cfg.tg_bot_token)
            # PTB 默认发送读写超时仅 5s,经代理发媒体经常超时;放宽(超时根因)
            .request(HTTPXRequest(
                proxy=proxy, connect_timeout=10, read_timeout=30,
                write_timeout=30, media_write_timeout=60,
            ))
            .get_updates_request(HTTPXRequest(proxy=proxy, connect_timeout=10, read_timeout=15))
            .post_init(_post_init)
            .concurrent_updates(True)
        )
        app = builder.build()
        app.add_handler(CommandHandler("start", self._cmd_help))
        app.add_handler(CommandHandler("help", self._cmd_help))
        app.add_handler(CommandHandler("push", self._cmd_push))
        app.add_handler(CommandHandler("save", self._cmd_save))
        app.add_handler(CommandHandler("scan", self._cmd_scan))
        app.add_handler(CommandHandler("fetch", self._cmd_fetch))
        app.add_handler(CommandHandler("process", self._cmd_process))
        app.add_handler(CommandHandler("upload", self._cmd_upload))
        app.add_handler(CommandHandler("bind", self._cmd_bind))
        app.add_handler(CommandHandler("bindcancel", self._cmd_bindcancel))
        app.add_handler(CallbackQueryHandler(self._on_channel_preset))
        # 频道消息:提示管理员用"转发给 Bot"登记(不再自动回填,多频道归属需人工选择)
        app.add_handler(
            MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, self._on_added_to_channel),
            group=-1,
        )
        app.add_handler(MessageHandler(filters.FORWARDED, self._on_forward))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_text))
        return app

    async def _heartbeat_loop(self) -> None:
        """每 60 秒打一次点(STATE["bot_heartbeat"])。

        **只在事件循环活着时才会跳**:循环被同步调用卡住、或整个线程僵住时这里停跳,
        健康判据据此报"无进展(可能卡死)"。`STATE["bot_running"]` 做不到这件事——
        它是启动时置 true、run() 返回才置 false,卡死期间照样是 true。
        """
        from app.webapp import STATE

        while True:
            STATE["bot_heartbeat"] = time.time()
            await asyncio.sleep(_HEARTBEAT_INTERVAL)

    # ── 频道登记(/bind 引导 + 转发触发) ──────────────────────
    async def _cmd_bind(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """进入登记等待期:5 分钟内转发频道消息即可触发归属选择。"""
        if not self._is_admin(update):
            await update.effective_message.reply_text("⛔ 仅管理员可用")
            return
        uid = update.effective_user.id
        self._bind_wait[uid] = time.monotonic() + 300
        await update.effective_message.reply_text(
            "📁 频道登记模式已开启(5 分钟内有效)\n\n"
            "现在把目标频道里的任意一条消息转发给我;\n"
            "转发时也可以直接在附言里写 /bind,随时都能触发。\n\n"
            "取消:点稍后弹出面板上的「✖ 取消」,或发 /bindcancel。"
        )

    async def _cmd_bindcancel(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        uid = update.effective_user.id
        self._bind_wait.pop(uid, None)
        await update.effective_message.reply_text("已退出登记模式。")

    async def _on_forward(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """普通转发一律忽略;仅两种方式触发登记:附言含 /bind,或 /bind 等待期内。"""
        msg = update.effective_message
        uid = update.effective_user.id if update.effective_user else None
        if uid is None or not self._is_admin(update):
            return
        caption = (msg.caption or msg.text or "").strip()
        bind_via_caption = caption.startswith("/bind")
        bind_via_wait = time.monotonic() < self._bind_wait.get(uid, 0.0)
        if not (bind_via_caption or bind_via_wait):
            logger.info("普通转发(无 /bind),忽略 uid=%s", uid)
            return
        # PTB v22:转发来源在 forward_origin(MessageOriginChannel.chat 才带来源频道)
        origin = getattr(msg, "forward_origin", None)
        fwd = getattr(origin, "chat", None) if origin is not None else None
        if fwd is None or fwd.type not in ("channel", "supergroup"):
            await msg.reply_text(
                "没识别到频道来源——请转发**频道里**的消息(不是个人聊天)。\n"
                "仍处于登记模式,可继续转发。"
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
            ],
            [InlineKeyboardButton("✖ 取消", callback_data=f"chreg:cancel:{chat_id}")],
        ])
        await msg.reply_text(f"频道「{title}」({chat_id})登记到哪个归属?", reply_markup=buttons)

    async def _on_channel_preset(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """归属选择回调:写 config + 运行中热生效;cancel 取消登记。"""
        query = update.callback_query
        if query is None or not self._is_admin(update):
            return
        try:
            _, preset, chat_id = query.data.split(":", 2)
        except ValueError:
            await query.answer("参数错误")
            return
        title = self._pending_channels.pop(chat_id, chat_id)
        if preset == "cancel":
            uid = update.effective_user.id if update.effective_user else None
            if uid is not None:
                self._bind_wait.pop(uid, None)  # 同时退出登记模式
            await query.edit_message_text(f"已取消「{title}」的登记。")
            return
        if preset not in _PRESET_LABEL:
            await query.answer("未知归属")
            return

        from app.config import fingerprint, read_raw, write_raw

        raw = read_raw(self.config_path)
        channels = [c for c in raw.get("channels", []) if str(c.get("chat_id")) != chat_id]
        channels.append({"chat_id": chat_id, "preset": preset, "title": title})
        raw["channels"] = channels
        write_raw(raw, self.config_path)
        # 热生效:同名预设替换
        self.cfg.channels = [
            c for c in self.cfg.channels if c.chat_id != chat_id
        ] + [ChannelConfig(chat_id=chat_id, preset=preset, title=title)]
        # 登记是热生效的:同步"已生效配置"指纹,免得网页误报"需重启"
        try:
            from app.webapp import STATE

            STATE["cfg_fingerprint"] = fingerprint(read_raw(self.config_path))
        except Exception as exc:  # noqa: BLE001 - 指纹刷新失败不影响登记
            logger.warning("刷新配置指纹失败:%s", exc)

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
                        "发送 /bind 后,把该频道里的任意一条消息转发给我,即可完成登记。",
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

    async def _cmd_save(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/save <115链接>:转存→整理→建永久分享→审核通过后自动推送。"""
        if not self._is_admin(update):
            await update.effective_message.reply_text("⛔ 仅管理员可用")
            return
        arg = " ".join(ctx.args) if ctx.args else ""
        link = parse_one(arg)
        if link is None:
            await update.effective_message.reply_text("用法:/save <115 分享链接>")
            return
        await self.pipeline.handle_save(update, link)

    async def _cmd_scan(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/scan:立即触发一轮目录监控扫描(常规为 30 分钟自动一轮)。"""
        if not self._is_admin(update):
            await update.effective_message.reply_text("⛔ 仅管理员可用")
            return
        status = await update.effective_message.reply_text("📂 正在扫描监控目录…")
        try:
            report = await self.pipeline.scan_now()
        except Exception as exc:  # noqa: BLE001
            logger.error("手动扫描失败:%s", exc, exc_info=exc)
            await status.edit_text(f"❌ 扫描失败:{str(exc)[:120]}")
            return
        await status.edit_text(report)

    async def _cmd_fetch(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/fetch:立即扫一轮 openlist 监控目录(常规为定时自动轮)。"""
        if not self._is_admin(update):
            await update.effective_message.reply_text("⛔ 仅管理员可用")
            return
        status = await update.effective_message.reply_text("📥 正在扫描 openlist 监控目录…")
        try:
            report = await self.fetcher.scan_now()
        except Exception as exc:  # noqa: BLE001
            logger.error("手动获取扫描失败:%s", exc, exc_info=exc)
            await status.edit_text(f"❌ 扫描失败:{str(exc)[:120]}")
            return
        await status.edit_text(report)

    async def _cmd_process(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/process:立即跑一轮处理(探测→重命名→ed2k→推卡→归档)。"""
        if not self._is_admin(update):
            await update.effective_message.reply_text("⛔ 仅管理员可用")
            return
        status = await update.effective_message.reply_text("📦 正在处理落地点里的文件…")
        try:
            report = await self.processor.scan_now()
        except Exception as exc:  # noqa: BLE001
            logger.error("手动处理失败:%s", exc, exc_info=exc)
            await status.edit_text(f"❌ 处理失败:{str(exc)[:120]}")
            return
        await status.edit_text(report)

    async def _cmd_upload(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/upload:立即跑一轮上传(把 clouddrive 里的成品移动到 115)。"""
        if not self._is_admin(update):
            await update.effective_message.reply_text("⛔ 仅管理员可用")
            return
        status = await update.effective_message.reply_text("📤 正在检查待上传文件…")
        try:
            report = await self.uploader.scan_now()
        except Exception as exc:  # noqa: BLE001
            logger.error("手动上传失败:%s", exc, exc_info=exc)
            await status.edit_text(f"❌ 上传失败:{str(exc)[:120]}")
            return
        await status.edit_text(report)

    async def _on_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        # 转发登记走 _on_forward(FORWARDED 过滤器先匹配);这里只响应直接文本
        if update.effective_user is None:
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
        await self.push_link(link, status=status, prefix=prefix)

    async def _say(self, status, text: str) -> None:
        """进度消息可选:频道监控链路没有聊天上下文,只落日志。"""
        if status is not None:
            await status.edit_text(text)

    async def push_link(
        self, link: ParsedLink, *, status=None, prefix: str = ""
    ) -> PushResult:
        """链接 → 卡片 → 归属频道(手动推送与频道监控共用同一条链路)。

        去重由调用方负责(先查 store.is_pushed);推送成功即标记已推送。
        status 为可编辑的进度消息(可选);失败原因一律落日志。
        """
        label = _PRESET_LABEL[link.provider]
        targets = self.cfg.channels_for(link.provider)
        if not targets:
            # 未登记该归属:不推送,给出明确原因与登记指引(不静默兜底)
            logger.warning("未配置%s,链接 %s 不推送", label, link.dedup_display)
            text = (
                f"📭 尚未登记{label},该链接未推送。\n"
                "登记方式:发 /bind 后转发频道消息选归属,或在全局配置页手动添加。"
            )
            await self._say(status, prefix + text)
            return PushResult(False, text)

        files, media = await self._load_media(link, status, prefix)
        if media is None:
            return PushResult(False, "读取失败")  # _load_media 已回报原因
        details = await self.tmdb.match(media) if self.tmdb else None
        logger.info("TMDB 匹配:%s → %s", media.title[:40], details["title"] if details else "未命中")

        # 同一归属可能登记了多个频道 → **逐个都投**(2026-09-12 用户要求;此前只投第一个且静默)
        # 卡片只渲染一次、海报也只取一次,重复的仅是发送动作
        sent: list[str] = []
        failed: list[str] = []
        uncertain: list[str] = []
        for target in targets:
            try:
                await self._deliver(media, details, link, files, target)
                sent.append(target)
            except DeliveryUncertain as exc:
                # 超时≠失败:消息可能已送达,重试会重复投递
                logger.warning("投递超时(结果不确定)%s:%s", target, exc)
                uncertain.append(str(exc))
            except Exception as exc:  # noqa: BLE001 - 投递失败保留状态可重试
                logger.error("卡片投递失败 %s:%s", target, exc, exc_info=exc)
                failed.append(str(exc)[:80])

        if not sent:
            # 一个都没送出去:不标记已推送,链接再次出现即可重推
            if uncertain and not failed:
                return PushResult(False, uncertain[0], uncertain=True)
            text = f"❌ 投递失败:{failed[0] if failed else '未知原因'}"
            await self._say(status, prefix + text)
            return PushResult(False, text)

        # **只要有一个成功就标记已推送**:否则下次重推会骚扰已经收到的那几个频道
        title = (details["title"] if details else media.title) or link.dedup_display
        self.store.mark_pushed(link.key, title)
        n = media.file_count or len(files)
        head = "✅ 已推送" + (f"({len(sent)}/{len(targets)} 个频道)" if len(targets) > 1 else "")
        text = f"{head} · {n} 文件 · 🎬 {title}" + (
            f" ({details['year']})" if details and details["year"] else "")
        if uncertain or failed:
            text += f"\n⚠️ {len(uncertain)} 个投递超时(结果不确定)、{len(failed)} 个失败,详见日志"
            logger.warning("投递部分失败:%s → sent=%d failed=%d uncertain=%d",
                           link.dedup_display, len(sent), len(failed), len(uncertain))
        await self._say(status, prefix + text)
        return PushResult(True, text)

    async def _load_media(self, link: ParsedLink, status=None, prefix: str = ""):
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
            await self._say(status, f"{prefix}🔐 {tip}")
            return [], None
        except ShareDead:
            logger.warning("分享 %s 已失效", link.code)
            await self._say(status, f"{prefix}💀 分享已失效或被取消")
            return [], None
        except (ShareRateLimited, ShareSnapshotting) as exc:
            logger.warning("分享 %s 限速/快照中:%s", link.code, exc)
            await self._say(status, f"{prefix}⏳ {exc},稍后重试")
            return [], None
        except ShareError as exc:
            logger.error("分享 %s 读取失败:%s", link.code, exc, exc_info=True)
            await self._say(status, f"{prefix}❌ {exc}")
            return [], None
        media = analyze_share(files) or AggregatedMedia(
            title=link.code, file_count=len(files),
            total_size=sum(f.size for f in files if not f.is_dir),
        )
        return files, media

    # ── 频道投递(串行;仅 RetryAfter 重试——超时重试会重复投递) ──
    async def _notify_uid(self, uid: int, text: str) -> None:
        """私聊通知用户(流水线状态变化);不可达仅告警。"""
        try:
            await self._bot_ref.send_message(uid, text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("通知用户 %s 失败:%s", uid, exc)

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
    logger.info("Stow Bot 启动(token 已配置;%s)", cfg.channels_summary())
    app.run_polling(bootstrap_retries=-1)
