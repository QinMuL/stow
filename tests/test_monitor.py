"""频道监控测试:游标策略(首轮不回补/补扫)、去重、登录状态机、配置解析。

用假 Bot/假 Telethon 客户端锁定行为,不触网。
"""

from __future__ import annotations

import asyncio

from app.bot import PushResult
from app.channel_monitor import (
    STATE_DISABLED,
    STATE_NO_API,
    STATE_NO_LOGIN,
    STATE_RUNNING,
    ChannelMonitor,
    channel_rows,
    normalize_ref,
    parse_proxy,
)
from app.config import Config, load_config, write_raw
from app.store import Store

_ED2K_A = "ed2k://|file|Movie.A.2023.1080p.mkv|1234567890|0123456789ABCDEF0123456789ABCDEF|/"
_ED2K_B = "ed2k://|file|Movie.B.2024.720p.mkv|987654321|FEDCBA9876543210FEDCBA9876543210|/"
_CHAT = -1001234567890  # 假频道 marked id(假 get_entity 返回 Channel(id=1234567890))
_HASH_A = "0123456789ABCDEF0123456789ABCDEF"
_HASH_B = "FEDCBA9876543210FEDCBA9876543210"


# ── 配置解析 ────────────────────────────────────────────────
def test_config_monitor_fields(tmp_path):
    p = tmp_path / "config.json"
    write_raw({
        "tg_api_id": "1234567", "tg_api_hash": "abcdef0123456789",
        "monitor_channels": "@aaa, https://t.me/bbb，-1001234567890 , @aaa",
    }, p)
    cfg = load_config(p)
    assert cfg.tg_api_id == 1234567
    assert cfg.tg_api_hash == "abcdef0123456789"
    # 逗号(中英)/换行分隔 + 去空 + 按序去重
    assert cfg.monitor_channel_list() == ["@aaa", "https://t.me/bbb", "-1001234567890"]
    assert cfg.monitor_ready() is True


def test_config_monitor_not_ready_without_api(tmp_path):
    p = tmp_path / "config.json"
    write_raw({"monitor_channels": "@aaa"}, p)
    cfg = load_config(p)
    assert cfg.tg_api_id == 0 and cfg.monitor_ready() is False


def test_config_monitor_api_id_invalid(tmp_path):
    p = tmp_path / "config.json"
    write_raw({"tg_api_id": "abc", "monitor_channels": "@aaa"}, p)
    assert load_config(p).tg_api_id == 0


def test_monitor_session_path(tmp_path):
    assert Config(data_dir=str(tmp_path)).monitor_session_path.name == "monitor.session"


def test_normalize_ref():
    assert normalize_ref("@regeng115") == "regeng115"
    assert normalize_ref("https://t.me/regeng115") == "regeng115"
    assert normalize_ref("t.me/c/1234567890/12") == -1001234567890
    assert normalize_ref("-1001234567890") == -1001234567890


def test_parse_proxy():
    assert parse_proxy("") is None
    assert parse_proxy("http://127.0.0.1:7897") == {
        "proxy_type": "http", "addr": "127.0.0.1", "port": 7897,
    }
    assert parse_proxy("socks5://u:p@host:1080") == {
        "proxy_type": "socks5", "addr": "host", "port": 1080,
        "username": "u", "password": "p",
    }
    assert parse_proxy("127.0.0.1:7897")["proxy_type"] == "http"  # 无 scheme 默认 http
    assert parse_proxy("http://127.0.0.1") is None  # 缺端口 → 直连


# ── 存储游标 ────────────────────────────────────────────────
def test_monitor_state_upsert(tmp_path):
    st = Store(tmp_path / "t.db")
    assert st.get_monitor_state("@a") is None
    st.set_monitor_state("@a", 100, chat_id="-1001", title="甲")
    assert st.get_monitor_state("@a")["last_msg_id"] == 100
    st.set_monitor_state("@a", 150)  # 不传 chat_id/title 时保留旧值
    row = st.get_monitor_state("@a")
    assert row["last_msg_id"] == 150 and row["chat_id"] == "-1001" and row["title"] == "甲"
    st.set_monitor_state("@a", 90)  # 游标只增不减
    assert st.get_monitor_state("@a")["last_msg_id"] == 150
    assert [r["ref"] for r in st.monitor_states()] == ["@a"]
    st.remove_monitor_state("@a")
    assert st.monitor_states() == []
    st.close()


def test_channel_rows_merge(tmp_path):
    cfg = Config(monitor_channels="@a,-1001234567890")
    rows = channel_rows(cfg, [
        {"ref": "@a", "chat_id": "-1001", "title": "甲", "last_msg_id": 7, "updated_at": 0},
    ], {"-1001234567890": "无法解析该频道引用"})
    assert rows[0] == {
        "ref": "@a", "title": "甲", "chat_id": "-1001", "last_msg_id": 7,
        "ok": True, "error": "",
    }
    assert rows[1]["ok"] is False and "无法解析" in rows[1]["error"]


# ── 假件:BOT / Telethon 客户端 ─────────────────────────────
class FakeBot:
    def __init__(self, cfg: Config, store: Store) -> None:
        self.cfg = cfg
        self.store = store
        self.sent: list = []
        self.results: list[PushResult] = []
        self.notified: list[str] = []

    async def push_link(self, link, *, status=None, prefix=""):
        self.sent.append(link)
        if self.results:
            return self.results.pop(0)
        return PushResult(True, "✅ 已推送")

    async def _notify_uid(self, uid, text):
        self.notified.append(text)


class FakeMessage:
    def __init__(self, mid: int, text: str) -> None:
        self.id = mid
        self.message = text


class FakeClient:
    """只实现监控用到的 Telethon 客户端表面。"""

    def __init__(self, messages: dict[int, list[FakeMessage]] | None = None,
                 authorized: bool = True) -> None:
        self._messages = messages or {}
        self._authorized = authorized
        self.joined: list = []
        self.handlers: list = []
        self.connected = True
        self.code_sent: list[str] = []
        self.signed: list[dict] = []

    async def connect(self) -> None:
        self.connected = True

    def is_connected(self) -> bool:
        return self.connected

    async def is_user_authorized(self) -> bool:
        return self._authorized

    async def get_me(self):
        class Me:
            id = 42
            first_name = "监控号"
            username = "mon"
        return Me()

    def add_event_handler(self, fn, event) -> None:
        self.handlers.append(fn)

    async def get_entity(self, ref):
        # 用真实 Telethon 类型:monitor 依赖 utils.get_peer_id 取 marked id
        from telethon.tl.types import Channel

        return Channel(id=1234567890, title=f"频道{ref}", photo=None, date=None)

    async def __call__(self, request):
        self.joined.append(request)
        return None

    async def get_messages(self, chat_id, limit=1):
        msgs = self._messages.get(chat_id, [])
        return msgs[-1:] if msgs else []

    def iter_messages(self, chat_id, min_id=0, limit=None, reverse=False):
        msgs = [m for m in self._messages.get(chat_id, []) if m.id > min_id]

        async def _gen():
            for m in msgs[: (limit or len(msgs))]:
                yield m

        return _gen()

    async def send_code_request(self, phone):
        self.code_sent.append(phone)

        class Sent:
            phone_code_hash = "hash123"
        return Sent()

    async def sign_in(self, **kwargs):
        self.signed.append(kwargs)
        if kwargs.get("code") == "needs2fa":
            from telethon.errors import SessionPasswordNeededError

            raise SessionPasswordNeededError(request=None)
        if kwargs.get("password") == "bad":
            raise ValueError("bad password")
        self._authorized = True

    async def disconnect(self) -> None:
        self.connected = False


async def _fake_make(mon):
    """替身 _make_client:直接返回已注入的假客户端(测试不触网)。"""
    return mon._client


def _monitor(tmp_path, *, channels="@src", messages=None, authorized=True):
    cfg = Config(tg_api_id=12345, tg_api_hash="hash", monitor_channels=channels,
                 data_dir=str(tmp_path), tg_admin_ids=[5406565010])
    store = Store(tmp_path / "t.db")
    bot = FakeBot(cfg, store)
    mon = ChannelMonitor(bot)
    client = FakeClient(messages, authorized=authorized)
    mon._client = client
    # 不触网:客户端构造固定返回注入的假件
    mon._make_client = lambda: _fake_make(mon)
    return mon, bot, client


# ── 启动路径 ────────────────────────────────────────────────
def test_no_api_state(tmp_path):
    mon, _, _ = _monitor(tmp_path)
    mon.bot.cfg.tg_api_id = 0
    asyncio.run(mon.start_client())
    assert mon.state == STATE_NO_API


def test_not_logged_in_state_and_notify(tmp_path):
    mon, bot, _ = _monitor(tmp_path, authorized=False)
    asyncio.run(mon.start_client())
    assert mon.state == STATE_NO_LOGIN
    assert any("未登录" in t for t in bot.notified)  # 不静默失效


def test_first_run_does_not_backfill(tmp_path):
    """首次接入:只记游标,历史消息一条都不推。"""
    msgs = {_CHAT: [FakeMessage(10, _ED2K_A), FakeMessage(11, _ED2K_B)]}
    mon, bot, _ = _monitor(tmp_path, messages=msgs)

    async def run():
        await mon._make_client()
        await mon._setup(mon._client)

    asyncio.run(run())
    assert mon.state == STATE_RUNNING
    assert bot.sent == []                      # 不回补历史
    assert mon.bot.store.get_monitor_state("@src")["last_msg_id"] == 11  # 游标=最新


def test_realtime_push_and_cursor(tmp_path):
    """实时消息:提取 ed2k → 推送 → 推进游标。"""
    mon, bot, client = _monitor(tmp_path)
    mon.bot.store.set_monitor_state("@src", 10, chat_id=str(_CHAT), title="频道")
    mon._monitored[_CHAT] = "频道"
    mon._ref_of[_CHAT] = "@src"

    class Ev:
        chat_id = _CHAT
        message = FakeMessage(12, f"新资源 {_ED2K_A} 和无效文本")

    asyncio.run(mon._on_new_message(Ev()))
    assert [lk.key for lk in bot.sent] == [_HASH_A]
    assert mon.bot.store.get_monitor_state("@src")["last_msg_id"] == 12


def test_message_without_ed2k_advances_cursor_only(tmp_path):
    mon, bot, _ = _monitor(tmp_path)
    mon.bot.store.set_monitor_state("@src", 5, chat_id=str(_CHAT), title="频道")
    mon._monitored[_CHAT] = "频道"
    mon._ref_of[_CHAT] = "@src"

    class Ev:
        chat_id = _CHAT
        message = FakeMessage(6, "只有闲聊,没有链接")

    asyncio.run(mon._on_new_message(Ev()))
    assert bot.sent == []
    assert mon.bot.store.get_monitor_state("@src")["last_msg_id"] == 6


def test_unmonitored_chat_ignored(tmp_path):
    mon, bot, _ = _monitor(tmp_path)

    class Ev:
        chat_id = 999
        message = FakeMessage(1, _ED2K_A)

    asyncio.run(mon._on_new_message(Ev()))
    assert bot.sent == []


def test_dedup_skips_pushed(tmp_path):
    mon, bot, _ = _monitor(tmp_path)
    mon.bot.store.mark_pushed(_HASH_A, "旧卡")
    asyncio.run(mon.push_text_links(f"{_ED2K_A} {_ED2K_B}", source="频道"))
    assert [lk.key for lk in bot.sent] == [_HASH_B]


def test_retry_once_on_failure(tmp_path):
    mon, bot, _ = _monitor(tmp_path)
    bot.results = [PushResult(False, "❌ 投递失败"), PushResult(True, "✅ 已推送")]
    mon.bot.push_link = bot.push_link

    async def fast_sleep(_):
        return None

    import app.channel_monitor as cm

    orig = cm.asyncio.sleep
    cm.asyncio.sleep = fast_sleep
    try:
        assert asyncio.run(mon.push_text_links(_ED2K_A, source="频道")) == 1
    finally:
        cm.asyncio.sleep = orig
    assert len(bot.sent) == 2  # 失败后重试一次


def test_no_retry_when_uncertain(tmp_path):
    """投递超时(可能已送达):不重试,避免重复投递。"""
    mon, bot, _ = _monitor(tmp_path)
    bot.results = [PushResult(False, "⚠️ 超时", uncertain=True)]

    async def fast_sleep(_):
        return None

    import app.channel_monitor as cm

    orig = cm.asyncio.sleep
    cm.asyncio.sleep = fast_sleep
    try:
        assert asyncio.run(mon.push_text_links(_ED2K_A, source="频道")) == 0
    finally:
        cm.asyncio.sleep = orig
    assert len(bot.sent) == 1


def test_catchup_pushes_missed_only(tmp_path):
    """补扫:只处理游标之后的消息,并跳过已推送过的链接。"""
    msgs = {_CHAT: [FakeMessage(9, "老消息"), FakeMessage(10, _ED2K_A), FakeMessage(11, _ED2K_B)]}
    mon, bot, _ = _monitor(tmp_path, messages=msgs)
    mon.bot.store.set_monitor_state("@src", 9, chat_id=str(_CHAT), title="频道")
    mon._monitored[_CHAT] = "频道"
    mon._ref_of[_CHAT] = "@src"
    mon.bot.store.mark_pushed(_HASH_A, "已推过")

    assert asyncio.run(mon._catchup()) == 1
    assert [lk.key for lk in bot.sent] == [_HASH_B]
    assert mon.bot.store.get_monitor_state("@src")["last_msg_id"] == 11


# ── 登录状态机 ──────────────────────────────────────────────
def test_login_flow_with_2fa(tmp_path):
    mon, _, client = _monitor(tmp_path, authorized=False)

    async def run():
        ok, msg = await mon.login_start("+8613800138000")
        assert ok and "验证码" in msg and mon.login_stage == "code"
        ok, msg = await mon.login_code("needs2fa")
        assert ok and mon.login_stage == "password"          # 转两步验证
        ok, msg = await mon.login_password("secret")
        assert ok and "登录成功" in msg

    asyncio.run(run())
    assert client.code_sent == ["+8613800138000"]
    assert mon.state == STATE_RUNNING            # 登录成功即接续装配
    assert mon.login_stage == ""


def test_login_password_rejected(tmp_path):
    mon, _, _ = _monitor(tmp_path, authorized=False)
    asyncio.run(mon.login_start("+8613800138000"))
    asyncio.run(mon.login_code("needs2fa"))
    ok, msg = asyncio.run(mon.login_password("bad"))
    assert ok is False and "失败" in msg and mon.login_stage == "password"


def test_login_requires_api(tmp_path):
    mon, _, _ = _monitor(tmp_path)
    mon.bot.cfg.tg_api_id = 0
    ok, msg = asyncio.run(mon.login_start("+8613800138000"))
    assert ok is False and "api_id" in msg


def test_login_phone_format(tmp_path):
    mon, _, _ = _monitor(tmp_path)
    ok, msg = asyncio.run(mon.login_start("13800138000"))
    assert ok is False and "国家码" in msg


def test_login_cancel(tmp_path):
    mon, _, _ = _monitor(tmp_path, authorized=False)
    asyncio.run(mon.login_start("+8613800138000"))
    asyncio.run(mon.login_cancel())
    assert mon.login_active is False
    ok, msg = asyncio.run(mon.login_code("12345"))
    assert ok is False and "过期" in msg


def test_login_disabled_without_channels(tmp_path):
    """账号已登录但未配置源频道:状态为 disabled,不注册监听。"""
    mon, _, _ = _monitor(tmp_path, channels="", authorized=True)
    asyncio.run(mon.start_client())
    assert mon.state == STATE_DISABLED


# ── 状态展示 ────────────────────────────────────────────────
def test_runtime_status_and_rows(tmp_path):
    mon, _, _ = _monitor(tmp_path)
    mon.bot.store.set_monitor_state("@src", 33, chat_id="1", title="甲")
    mon._unreachable["@missing"] = "无法解析"
    mon.bot.cfg.monitor_channels = "@src,@missing"

    rs = mon.runtime_status()
    assert rs["state"] == mon.state and "unreachable" in rs
    rows = mon.channel_rows(mon.bot.store.monitor_states())
    assert rows[0]["last_msg_id"] == 33 and rows[0]["ok"] is True
    assert rows[1]["ok"] is False
