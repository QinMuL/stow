"""TG 频道监控:用用户账号(Telethon)盯源频道,提取 ed2k → 本项目卡片 → ed2k 归属频道。

触发方式(显式门控:源频道列表非空 + 账号已登录,二者缺一不启动):
  1. 实时 —— 源频道新消息 → 消息文本提取 ed2k(app.links.parse_all,与主链路同一套正则)
  2. 补扫 —— 启动/登录成功后按游标 iter_messages(min_id) 追停机期间漏掉的消息
  3. 首次接入某频道 —— 只记录当前消息 ID 作起点,**不回补历史**(避免一接上就刷几百条旧档)

推送:每条 ed2k 走 bot.push_link —— 与手动 /push 完全同一条卡片链路,
      去重 key = 文件 hash(store.pushed),推过的链接再也不会重推。
失败:投递失败重试一次;投递超时(结果不确定)不重试不标记,防重复投递。
登录:Web 全局配置页弹窗(手机号 → 验证码 → 两步密码);未登录/断线时告警一次,
      不静默失效。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from urllib.parse import urlparse

from app.links import ParsedLink, parse_all

logger = logging.getLogger(__name__)

_WATCHDOG_INTERVAL = 60    # 连接状态巡检(断线告警/恢复通知)
_CATCHUP_LIMIT = 100       # 补扫每频道最多回溯消息数(停机恢复)
_RETRY_DELAY = 30          # 单条推送失败后重试间隔(秒)
_LOGIN_TTL = 600           # 登录会话有效期(秒,超时需重新获取验证码)

STATE_DISABLED = "disabled"   # 已登录但未配置源频道
STATE_NO_API = "no-api"       # 缺 api_id/api_hash
STATE_NO_LOGIN = "no-login"   # 账号未登录(等 Web 弹窗登录)
STATE_RUNNING = "running"     # 监听中
STATE_STOPPED = "stopped"     # 客户端未运行

STATE_TEXT = {
    STATE_DISABLED: "未配置源频道",
    STATE_NO_API: "缺 API 凭据",
    STATE_NO_LOGIN: "账号未登录",
    STATE_RUNNING: "监听中",
    STATE_STOPPED: "未运行",
}


def parse_proxy(url: str) -> dict | None:
    """proxy_url → Telethon 代理 dict(经 python-socks,http/socks 均支持)。"""
    if not url:
        return None
    raw = url if "://" in url else f"http://{url}"
    p = urlparse(raw)
    scheme = (p.scheme or "http").lower()
    if scheme not in {"socks4", "socks5", "http", "https"}:
        logger.warning("监控不支持代理协议 %r,走直连", p.scheme)
        return None
    if not p.hostname or not p.port:
        logger.warning("代理地址不完整 %r,监控走直连", url)
        return None
    proxy: dict = {"proxy_type": "http" if scheme == "https" else scheme,
                   "addr": p.hostname, "port": p.port}
    if p.username:
        proxy["username"] = p.username
    if p.password:
        proxy["password"] = p.password
    return proxy


def normalize_ref(ref: str) -> str | int:
    """@xxx / t.me/xxx / t.me/c/123 / 纯数字 → Telethon 可解析形式。"""
    m = re.search(r"t\.me/(?:c/)?(\w+)", ref)
    if m:
        return int("-100" + m.group(1)) if "c/" in ref else m.group(1)
    raw = ref.strip().removeprefix("@")
    return int(raw) if raw.lstrip("-").isdigit() else raw


class ChannelMonitor:
    """频道监控服务;持有 bot 引用复用其 cfg/store/tmdb/_deliver。"""

    def __init__(self, bot) -> None:
        self.bot = bot
        self.state = STATE_STOPPED
        self.last_error = ""
        self.login_stage = ""          # "" | "code" | "password"
        self.account = ""              # 已登录账号展示
        self._client = None
        self._run_task: asyncio.Task | None = None
        self._watchdog: asyncio.Task | None = None
        self._monitored: dict[int, str] = {}   # chat_id → 频道标题
        self._ref_of: dict[int, str] = {}      # chat_id → 配置里的引用串(游标用)
        self._unreachable: dict[str, str] = {}  # ref → 失败原因(状态展示)
        self._inflight: set[str] = set()        # 去重占位(实时与补扫并发窗口)
        self._login: dict = {}                  # 登录状态机:phone/code_hash/stage/at
        self._down = False                      # 连接状态(断线告警去重)
        self._handler_on = False                # 事件处理器已注册
        self._notified: set[str] = set()        # 已告警过的一次性事件(避免反复打扰)

    # ── 生命周期 ────────────────────────────────────────────
    def start(self) -> None:
        """由 Bot 启动期调用:后台建连(不阻塞 Bot 就绪)。"""
        if self._run_task is None or self._run_task.done():
            self._run_task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            await self.start_client()
        except Exception as exc:  # noqa: BLE001 - 监控异常不影响 Bot 主链路
            self.state = STATE_STOPPED
            self.last_error = str(exc)[:200]
            logger.error("频道监控启动失败:%s", exc, exc_info=exc)
        if self._watchdog is None or self._watchdog.done():
            self._watchdog = asyncio.create_task(self._watchdog_loop())

    async def start_client(self) -> None:
        """连接 → 鉴权 → 装配监听;未登录则停在 no-login 等 Web 登录。"""
        cfg = self.bot.cfg
        if not cfg.tg_api_id or not cfg.tg_api_hash:
            self.state = STATE_NO_API
            logger.info("频道监控未启动:缺 tg_api_id/tg_api_hash(Web 全局配置页填写)")
            return
        client = await self._make_client()
        if not await client.is_user_authorized():
            self.state = STATE_NO_LOGIN
            logger.warning("频道监控账号未登录:请在 Web 全局配置页点「登录账号」")
            await self._notify_once(
                "monitor_no_login",
                "📡 频道监控账号未登录(缺 session)。请打开 Web 全局配置页 → 频道监控 → 登录账号。",
            )
            return
        await self._setup(client)

    async def _make_client(self):
        """建 Telethon 客户端并连接(代理沿用项目配置);telethon 缺失即报错。"""
        try:
            from telethon import TelegramClient
        except ImportError as exc:  # pragma: no cover - 依赖缺失时的兜底
            raise RuntimeError("未安装 telethon,频道监控不可用") from exc

        cfg = self.bot.cfg
        client = TelegramClient(
            str(cfg.monitor_session_path), cfg.tg_api_id, cfg.tg_api_hash,
            proxy=parse_proxy(cfg.proxy_url),
        )
        await client.connect()
        self._client = client
        return client

    async def _setup(self, client) -> None:
        """登录态就绪后的装配:账号信息 + 事件处理器 + 频道加载/补扫。"""
        me = await client.get_me()
        self.account = (
            f"{getattr(me, 'first_name', '') or ''}"
            f"{(' @' + me.username) if getattr(me, 'username', '') else ''}"
            f" (id {getattr(me, 'id', '')})"
        ).strip()
        if not self._handler_on:
            from telethon import events

            client.add_event_handler(self._on_new_message, events.NewMessage())
            self._handler_on = True
        self._login = {}
        self.login_stage = ""
        self.last_error = ""
        logger.info("频道监控已登录账号:%s", self.account)
        await self._load_channels()
        if not self._monitored:
            self.state = STATE_DISABLED
            logger.info("频道监控:未配置可用源频道(Web 全局配置页添加后重启生效)")
            return
        self.state = STATE_RUNNING
        caught = await self._catchup()
        logger.info(
            "频道监控就绪:%d 个频道%s",
            len(self._monitored), f",补扫漏档推送 {caught} 条" if caught else "",
        )
        await self._notify_once(
            "monitor_started",
            f"📡 频道监控已就绪:{len(self._monitored)} 个源频道"
            + (f",补扫补推 {caught} 条 ed2k" if caught else ""),
        )

    async def stop_client(self) -> None:
        """断开客户端(退出登录用)。"""
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None
        client, self._client = self._client, None
        self._monitored.clear()
        self._ref_of.clear()
        self.state = STATE_STOPPED
        if client is not None:
            try:
                await client.disconnect()
            except Exception as exc:  # noqa: BLE001
                logger.warning("断开监控客户端失败:%s", exc)

    async def _watchdog_loop(self) -> None:
        """连接巡检:断线/恢复各告警一次,避免监控静默失效无人知晓。"""
        while True:
            await asyncio.sleep(_WATCHDOG_INTERVAL)
            client = self._client
            if client is None:
                continue
            connected = client.is_connected()
            if connected and self._down:
                self._down = False
                logger.info("频道监控连接已恢复")
                await self._notify_admin("✅ 频道监控连接已恢复,继续监听源频道。")
            elif not connected and not self._down:
                self._down = True
                logger.warning("频道监控连接已断开,Telethon 自动重连中")
                await self._notify_admin("⚠️ 频道监控与 Telegram 连接断开,正在自动重连…")

    async def _notify_admin(self, text: str) -> None:
        if self.bot.cfg.tg_admin_ids:
            await self.bot._notify_uid(self.bot.cfg.tg_admin_ids[0], text)

    async def _notify_once(self, key: str, text: str) -> None:
        """一次性事件只通知一次(进程内;重启后重置)。"""
        if key in self._notified:
            return
        self._notified.add(key)
        await self._notify_admin(text)

    # ── 登录(Web 弹窗驱动) ─────────────────────────────────
    @property
    def login_active(self) -> bool:
        return bool(self._login) and time.monotonic() - self._login.get("at", 0) < _LOGIN_TTL

    async def login_start(self, phone: str) -> tuple[bool, str]:
        """发验证码:手机号含国家码,如 +8613800138000。"""
        cfg = self.bot.cfg
        if not cfg.tg_api_id or not cfg.tg_api_hash:
            return False, "tg_api_id / tg_api_hash 尚未生效:填好后点「保存并重启」,再回来登录"
        phone = (phone or "").strip().replace(" ", "")
        if not phone.startswith("+") or not phone[1:].isdigit():
            return False, "手机号需含国家码且以 + 开头,如 +8613800138000"
        try:
            client = self._client or await self._make_client()
            if await client.is_user_authorized():
                await self._setup(client)
                return True, "该账号已登录,监控已启动"
            sent = await client.send_code_request(phone)
        except Exception as exc:  # noqa: BLE001 - 网络/风控等一律回执原文
            logger.error("获取验证码失败:%s", exc, exc_info=exc)
            return False, f"获取验证码失败:{str(exc)[:120]}"
        self._login = {
            "phone": phone, "code_hash": sent.phone_code_hash,
            "stage": "code", "at": time.monotonic(),
        }
        self.login_stage = "code"
        logger.info("频道监控:已向 %s 发送登录验证码", phone)
        return True, "验证码已发送(Telegram 内查收),请填写"

    async def login_code(self, code: str) -> tuple[bool, str]:
        code = (code or "").strip()
        if not self.login_active or self._login.get("stage") != "code":
            return False, "登录会话已过期,请重新获取验证码"
        from telethon.errors import SessionPasswordNeededError

        try:
            await self._client.sign_in(
                phone=self._login["phone"], code=code,
                phone_code_hash=self._login["code_hash"],
            )
        except SessionPasswordNeededError:
            self._login["stage"] = "password"
            self.login_stage = "password"
            return True, "该账号开启了两步验证,请填写两步密码"
        except Exception as exc:  # noqa: BLE001
            logger.warning("验证码登录失败:%s", exc)
            return False, f"验证失败:{str(exc)[:120]}"
        return True, await self._after_login()

    async def login_password(self, password: str) -> tuple[bool, str]:
        if not self.login_active or self._login.get("stage") != "password":
            return False, "登录会话已过期,请重新获取验证码"
        try:
            await self._client.sign_in(password=(password or ""))
        except Exception as exc:  # noqa: BLE001
            logger.warning("两步密码登录失败:%s", exc)
            return False, f"两步密码验证失败:{str(exc)[:120]}"
        return True, await self._after_login()

    async def login_cancel(self) -> str:
        self._login = {}
        self.login_stage = ""
        return "已取消登录。"

    async def logout(self) -> str:
        """退出登录:断开客户端并删除会话文件(下次需重新登录)。"""
        await self.stop_client()
        self.account = ""
        path = self.bot.cfg.monitor_session_path
        for p in (path, path.with_suffix(".session-journal")):
            try:
                p.unlink(missing_ok=True)
            except OSError as exc:  # noqa: BLE001
                logger.warning("删除会话文件失败(%s):%s", p, exc)
        logger.info("频道监控已退出登录(会话文件已删除)")
        return "已退出登录,会话已清除。"

    async def _after_login(self) -> str:
        """登录成功后的接续装配 + 补扫(返回给 UI 的提示文本)。"""
        self._login = {}
        self.login_stage = ""
        client = self._client
        await self._setup(client)
        if self.state == STATE_RUNNING:
            return f"登录成功,频道监控已启动:{len(self._monitored)} 个源频道"
        if self.state == STATE_DISABLED:
            return "登录成功。尚未配置源频道 —— 在配置页添加后重启生效"
        return "登录成功"

    # ── 频道加载 / 实体解析 ─────────────────────────────────
    async def _load_channels(self) -> None:
        """解析源频道 → 加入(幂等) → 首次接入只记起点不回补。"""
        bot = self.bot
        self._unreachable.clear()
        self._monitored.clear()
        self._ref_of.clear()
        for ref in bot.cfg.monitor_channel_list():
            chat_id, title, err = await self._resolve(ref)
            if chat_id is None:
                self._unreachable[ref] = err
                logger.warning("源频道不可达(%s):%s", ref, err)
                await self._notify_once(
                    f"monitor_bad_ref:{ref}",
                    f"⚠️ 频道监控无法接入「{ref}」:{err}\n请在配置页检查该频道引用。",
                )
                continue
            self._monitored[chat_id] = title
            self._ref_of[chat_id] = ref
            st = bot.store.get_monitor_state(ref)
            if st is None:
                # 首次接入:记当前最新消息 ID 作起点,不回补历史(防一接上就刷旧档)
                latest = await self._latest_msg_id(chat_id)
                bot.store.set_monitor_state(
                    ref, latest, chat_id=str(chat_id), title=title
                )
                logger.info(
                    "频道监控首次接入 %s(%s),从消息 %s 起监听(不回补历史)", title, ref, latest
                )
            else:
                bot.store.set_monitor_state(
                    ref, st["last_msg_id"], chat_id=str(chat_id), title=title
                )
                logger.info(
                    "频道监控接入 %s(%s),游标消息 %s", title, ref, st["last_msg_id"]
                )

    async def _resolve(self, ref: str) -> tuple[int | None, str, str]:
        """频道引用 → (chat_id, 标题, 失败原因);成功时顺带确保已加入。"""
        from telethon import utils
        from telethon.errors import (
            ChannelPrivateError,
            ChannelPublicGroupNaError,
            UserAlreadyParticipantError,
            UserBannedInChannelError,
        )
        from telethon.tl.functions.channels import JoinChannelRequest

        st = self.bot.store.get_monitor_state(ref)
        candidates: list[object] = []
        if st and st["chat_id"].lstrip("-").isdigit():
            candidates.append(int(st["chat_id"]))  # 旧 chat_id 优先(最稳)
        candidates.append(normalize_ref(ref))
        entity = None
        err = "无法解析该频道引用"
        for cand in candidates:
            try:
                entity = await self._client.get_entity(cand)
                break
            except ValueError as exc:
                err = f"频道不存在或不可见({exc})"
            except Exception as exc:  # noqa: BLE001
                err = str(exc)[:100]
        if entity is None:
            return None, "", err
        if getattr(entity, "megagroup", False):
            return None, "", "这是群组,频道监控仅支持频道"
        title = getattr(entity, "title", "") or str(ref)
        chat_id = utils.get_peer_id(entity)
        try:
            await self._client(JoinChannelRequest(entity))  # 幂等:未加入则不实时下发
        except UserAlreadyParticipantError:
            pass  # 已在频道内,正常的重复接入
        except (
            ChannelPrivateError, ChannelPublicGroupNaError, UserBannedInChannelError,
        ) as exc:
            logger.warning("加入频道失败(%s):%s——若为私有频道,请先用该账号加入", title, exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("加入频道异常(%s):%s", title, exc)
        return chat_id, title, ""

    async def _latest_msg_id(self, chat_id: int) -> int:
        try:
            msgs = await self._client.get_messages(chat_id, limit=1)
        except Exception as exc:  # noqa: BLE001 - 取不到就当 0(下轮补扫会补齐)
            logger.warning("取频道最新消息失败(%s):%s", chat_id, exc)
            return 0
        return int(msgs[0].id) if msgs else 0

    # ── 消息处理 ────────────────────────────────────────────
    async def _on_new_message(self, event) -> None:
        """实时消息:仅处理监控集内频道;异常不外抛(不拖垮更新循环)。"""
        try:
            chat_id = event.chat_id
            title = self._monitored.get(chat_id)
            if title is None:
                return
            msg = event.message
            await self._handle_message(
                chat_id, title, getattr(msg, "message", "") or "", msg.id
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("频道监控消息处理异常:%s", exc, exc_info=exc)

    async def _handle_message(
        self, chat_id: int, title: str, text: str, msg_id: int
    ) -> int:
        """处理一条源频道消息:提取/推送 → 推进游标(推完才推进,防丢档)。"""
        pushed = await self.push_text_links(text, source=title)
        ref = self._ref_of.get(chat_id)
        if ref:
            self.bot.store.set_monitor_state(
                ref, msg_id, chat_id=str(chat_id), title=title
            )
        return pushed

    async def push_text_links(self, text: str, *, source: str = "") -> int:
        """提取文本中的 ed2k → 去重 → 卡片推送;返回推送成功条数。

        与游标无关(供实时处理与人工/脚本验证共用)。
        """
        links = [p for p in parse_all(text or "") if p.provider == "ed2k"]
        if not links:
            return 0
        logger.info("频道监控捕获 %d 条 ed2k:%s", len(links), source or "-")
        done = 0
        for link in links:
            if self.bot.store.is_pushed(link.key) or link.key in self._inflight:
                logger.info("频道监控跳过已推送:%s", link.dedup_display)
                continue
            self._inflight.add(link.key)
            try:
                done += 1 if await self._push(link, source) else 0
            finally:
                self._inflight.discard(link.key)
        return done

    async def _push(self, link: ParsedLink, source: str) -> bool:
        """单链接推送:失败重试一次;投递超时(结果不确定)不重试。"""
        result = await self.bot.push_link(link)
        if result.ok:
            logger.info("频道监控推送成功:%s(%s)", link.dedup_display, source or "-")
            return True
        if result.uncertain:
            # 超时≠失败:消息可能已送达,重试会重复投递
            logger.warning("频道监控投递结果不确定,不重试:%s", link.dedup_display)
            return False
        logger.warning("频道监控推送未成功(%s),%ds 后重试:%s", link.dedup_display, _RETRY_DELAY, result.text)
        await asyncio.sleep(_RETRY_DELAY)
        result = await self.bot.push_link(link)
        if result.ok:
            logger.info("频道监控重试推送成功:%s(%s)", link.dedup_display, source or "-")
            return True
        logger.error("频道监控推送失败:%s:%s", link.dedup_display, result.text)
        return False

    # ── 补扫 ────────────────────────────────────────────────
    async def _catchup(self) -> int:
        """按游标补扫停机漏档(每频道 ≤_CATCHUP_LIMIT 条);返回补推条数。"""
        total = 0
        for chat_id, title in list(self._monitored.items()):
            ref = self._ref_of.get(chat_id, "")
            st = self.bot.store.get_monitor_state(ref)
            if st is None:
                continue
            cursor = int(st["last_msg_id"])
            count = 0
            try:
                async for msg in self._client.iter_messages(
                    chat_id, min_id=cursor, limit=_CATCHUP_LIMIT, reverse=True
                ):
                    count += 1
                    total += await self._handle_message(
                        chat_id, title, getattr(msg, "message", "") or "", msg.id
                    )
            except Exception as exc:  # noqa: BLE001 - 单频道失败不拖垮其余
                logger.warning("补扫异常(%s):%s", title, exc)
                continue
            if count >= _CATCHUP_LIMIT:
                logger.warning(
                    "补扫 %s 达上限 %d 条,更早的漏档未处理(可在配置页调小保留或人工确认)",
                    title, _CATCHUP_LIMIT,
                )
            elif count:
                logger.info("补扫 %s:%d 条消息", title, count)
        return total

    # ── 状态展示 ────────────────────────────────────────────
    # Web 与 Bot 同进程不同线程:sqlite 连接绑定 Bot 线程,所以这里只暴露
    # 纯内存字段(锁外可读),游标行由 Web 侧用自己的 Store 读取后合并。
    def runtime_status(self) -> dict:
        """运行状态(不触碰 sqlite,可跨线程读)。"""
        return {
            "state": self.state,
            "state_text": STATE_TEXT.get(self.state, self.state),
            "account": self.account,
            "connected": bool(self._client is not None and self._client.is_connected()),
            "login_stage": self.login_stage if self.login_active else "",
            "last_error": self.last_error,
            "unreachable": dict(self._unreachable),
        }

    def channel_rows(self, states: list[dict]) -> list[dict]:
        """配置的源频道 × 已存游标 → 展示行(不可达原因一并带上)。"""
        return channel_rows(self.bot.cfg, states, self._unreachable)


def channel_rows(cfg, states: list[dict], unreachable: dict[str, str] | None = None) -> list[dict]:
    """源频道展示行:引用 / 标题 / chat_id / 游标 / 是否可达。

    cfg 为 Config(取 monitor_channel_list);states 为 Store.monitor_states() 结果。
    纯函数,供 Web 线程在 Bot 未运行/Bot 运行两种情况下复用。
    """
    by_ref = {s["ref"]: s for s in states}
    bad = unreachable or {}
    rows = []
    for ref in cfg.monitor_channel_list():
        st = by_ref.get(ref) or {}
        chat_id = str(st.get("chat_id", "") or "")
        err = bad.get(ref, "")
        rows.append({
            "ref": ref,
            "title": st.get("title", "") or "",
            "chat_id": chat_id,
            "last_msg_id": int(st.get("last_msg_id", 0) or 0),
            "ok": not err and bool(chat_id),
            "error": err,
        })
    return rows
