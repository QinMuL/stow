"""Web 配置台测试:登录 / 配置脱敏读写 / 账号 / 状态 / 重启(不真退)。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.webapp import create_app


def _client(tmp_path):
    import json as _json

    p = tmp_path / "config.json"
    p.write_text(_json.dumps({"data_dir": str(tmp_path)}), encoding="utf-8")
    return TestClient(create_app(p))


def _login(client, username="admin", password="admin") -> str:
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── 登录 ────────────────────────────────────────────────────
def test_default_admin_created_and_login(tmp_path):
    client = _client(tmp_path)
    token = _login(client)
    assert token


def test_login_wrong_password(tmp_path):
    client = _client(tmp_path)
    assert client.post("/api/login", json={"username": "admin", "password": "x"}).status_code == 401


# ── 配置 ────────────────────────────────────────────────────
def test_config_requires_auth(tmp_path):
    client = _client(tmp_path)
    assert client.get("/api/config").status_code == 401


def test_config_get_masks_sensitive(tmp_path):
    client = _client(tmp_path)
    token = _login(client)
    client.put(
        "/api/config",
        json={"values": {"tg_bot_token": "T0KEN", "proxy_url": "http://127.0.0.1:7897"}},
        headers=_h(token),
    )
    r = client.get("/api/config", headers=_h(token))
    assert r.json()["tg_bot_token"] == "••••••••"  # 已设置 → 掩码
    assert r.json()["proxy_url"] == "http://127.0.0.1:7897"  # 非敏感 → 明文


def test_config_put_masked_keeps_old(tmp_path):
    client = _client(tmp_path)
    token = _login(client)
    client.put("/api/config", json={"values": {"tg_bot_token": "REAL"}}, headers=_h(token))
    # 提交掩码 = 未修改,不清空
    client.put("/api/config", json={"values": {"tg_bot_token": "••••••••"}}, headers=_h(token))
    import json as _json

    raw = _json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert raw["tg_bot_token"] == "REAL"


def test_config_put_reports_missing(tmp_path):
    client = _client(tmp_path)
    token = _login(client)
    r = client.put("/api/config", json={"values": {"tg_bot_token": "t"}}, headers=_h(token))
    body = r.json()
    assert body["success"] is True and body["bot_ready"] is False
    assert any("tg_admin_ids" in m for m in body["missing"])


def test_config_admin_ids_parsing(tmp_path):
    client = _client(tmp_path)
    token = _login(client)
    client.put("/api/config", json={"values": {"tg_admin_ids": "123, 456"}}, headers=_h(token))
    import json as _json

    raw = _json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert raw["tg_admin_ids"] == [123, 456]


def test_config_rejects_bad_admin_id(tmp_path):
    client = _client(tmp_path)
    token = _login(client)
    r = client.put("/api/config", json={"values": {"tg_admin_ids": "abc"}}, headers=_h(token))
    assert r.status_code == 400


# ── 账号 ────────────────────────────────────────────────────
def test_account_flag_and_change(tmp_path):
    client = _client(tmp_path)
    token = _login(client)
    assert client.get("/api/account", headers=_h(token)).json()["default_password"] is True
    r = client.put(
        "/api/account",
        json={"current_password": "admin", "new_password": "newpass6"},
        headers=_h(token),
    )
    assert r.status_code == 200
    assert client.post("/api/login", json={"username": "admin", "password": "admin"}).status_code == 401
    token2 = _login(client, password="newpass6")
    assert client.get("/api/account", headers=_h(token2)).json()["default_password"] is False


def test_account_wrong_current(tmp_path):
    client = _client(tmp_path)
    token = _login(client)
    r = client.put(
        "/api/account",
        json={"current_password": "wrong", "new_password": "x123456"},
        headers=_h(token),
    )
    assert r.status_code == 403


# ── 日志 ────────────────────────────────────────────────────
def test_logs_parse_filter_and_guard(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "stow.log").write_text(
        "2026-09-09 10:00:00,111 INFO  [app.bot] 普通消息\n"
        "2026-09-09 10:00:01,222 WARNING [app.bot] 限速警告\n"
        "2026-09-09 10:00:02,333 ERROR [app.pan115] 读取失败\n"
        "Traceback (most recent call last):\n"
        '  File "x.py", line 1, in <module>\n'
        "ValueError: boom\n"
        "2026-09-09 10:00:03,444 INFO  [app.bot] 尾部消息\n",
        encoding="utf-8",
    )
    client = _client(tmp_path)
    token = _login(client)

    body = client.get("/api/logs", headers=_h(token)).json()
    assert [e["level"] for e in body["items"]] == ["INFO", "WARNING", "ERROR", "INFO"]
    err = body["items"][2]
    assert "ValueError: boom" in err["msg"]  # traceback 续行归入上一条
    assert [f["name"] for f in body["files"]] == ["stow.log"]

    # 级别过滤:WARN+ 只剩 2 条
    body = client.get("/api/logs?level=WARNING", headers=_h(token)).json()
    assert [e["level"] for e in body["items"]] == ["WARNING", "ERROR"]

    # 关键字过滤
    body = client.get("/api/logs?q=boom", headers=_h(token)).json()
    assert len(body["items"]) == 1 and body["items"][0]["level"] == "ERROR"

    # 尾部条数限制(新→旧窗口)
    body = client.get("/api/logs?limit=2", headers=_h(token)).json()
    assert [e["level"] for e in body["items"]] == ["ERROR", "INFO"]
    assert body["truncated"] is True

    # 越权文件名拒绝
    assert client.get("/api/logs?file=../../etc/passwd", headers=_h(token)).status_code == 400


# ── 状态 ────────────────────────────────────────────────────
def test_status_reports_missing(tmp_path, monkeypatch):
    import app.webapp as web

    async def fake_proxy(url):
        return {"configured": bool(url), "url": url, "ok": True, "latency_ms": 1, "error": ""}

    monkeypatch.setattr(web, "_check_proxy", fake_proxy)
    web._health_cache.update({"proxy": (0.0, None), "pan115": (0.0, None)})

    client = _client(tmp_path)
    token = _login(client)
    body = client.get("/api/status", headers=_h(token)).json()
    assert body["bot_running"] is False
    assert body["bot_ready"] is False
    assert len(body["missing"]) >= 3
    # 链路健康:代理探测(桩) + 115 未配置 cookie → 匿名模式
    assert body["proxy"]["ok"] is True
    assert body["pan115"] == {"cookie_set": False, "uid": None, "ok": None, "error": ""}


# ── 重启(不真退) ───────────────────────────────────────────
def test_restart_schedules_exit(tmp_path, monkeypatch):
    import threading

    import app.webapp as webapp

    client = _client(tmp_path)
    token = _login(client)
    called = threading.Event()

    def fake_exit(code):
        called.set()

    monkeypatch.setattr(webapp.os, "_exit", fake_exit)
    r = client.post("/api/restart", headers=_h(token))
    assert r.status_code == 200
    assert called.wait(3)  # 1 秒后被调度调用


# ── 历史 / 统计 ─────────────────────────────────────────────
def test_history_endpoint(tmp_path):
    from app.store import Store

    client = _client(tmp_path)
    token = _login(client)
    # 直接写库造两条记录
    cfg_raw = tmp_path / "config.json"
    import json as _json

    raw = _json.loads(cfg_raw.read_text(encoding="utf-8"))
    raw["data_dir"] = str(tmp_path)
    cfg_raw.write_text(_json.dumps(raw), encoding="utf-8")
    s = Store(tmp_path / "stow.db")
    s.mark_pushed("c1", "剧A")
    s.mark_pushed("c2", "影B")
    s.close()

    r = client.get("/api/history", headers=_h(token))
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2 and body["today"] == 2
    assert body["items"][0]["code"] == "c2"  # 新→旧
    assert body["items"][0]["title"] == "影B"


def test_store_recent_and_stats(tmp_path):
    from app.store import Store

    s = Store(tmp_path / "t.db")
    assert s.stats() == {"today": 0, "total": 0}
    s.mark_pushed("a", "A")
    s.mark_pushed("b", "B")
    assert s.stats()["total"] == 2
    rec = s.recent(1)
    assert len(rec) == 1 and rec[0]["code"] == "b"
    s.close()


def test_config_get_treats_placeholder_as_empty(tmp_path):
    """中文占位符不显示为「已保存」(与启动加载同口径)。"""
    import json as _json

    p = tmp_path / "config.json"
    p.write_text(_json.dumps({"tg_bot_token": "123456:ABC-你的BotToken"}), encoding="utf-8")
    client = TestClient(create_app(p))
    token = _login(client)
    r = client.get("/api/config", headers=_h(token))
    assert r.json()["tg_bot_token"] == ""


def test_config_put_rejects_placeholder(tmp_path):
    client = _client(tmp_path)
    token = _login(client)
    r = client.put(
        "/api/config",
        json={"values": {"tg_bot_token": "占位符"}},
        headers=_h(token),
    )
    assert r.status_code == 400
    assert "占位符" in r.json()["detail"]


# ── 频道监控(Web 侧) ───────────────────────────────────────
def test_config_accepts_monitor_keys(tmp_path):
    """API 凭据与源频道可经 Web 保存;api_hash 脱敏展示。"""
    client = _client(tmp_path)
    token = _login(client)
    r = client.put(
        "/api/config",
        json={"values": {"tg_api_id": "36174218", "tg_api_hash": "e778abcdef0123456789abcdef012345",
                         "monitor_channels": "@src1,t.me/src2"}},
        headers=_h(token),
    )
    assert r.status_code == 200, r.text
    body = client.get("/api/config", headers=_h(token)).json()
    assert body["tg_api_id"] == 36174218
    assert body["tg_api_hash"] == "••••••••"          # 敏感键脱敏
    assert body["monitor_channels"] == "@src1,t.me/src2"


def test_config_put_keeps_masked_api_hash(tmp_path):
    """掩码值表示未修改,不覆盖已存 api_hash。"""
    import json as _json

    p = tmp_path / "config.json"
    p.write_text(_json.dumps({"data_dir": str(tmp_path), "tg_api_hash": "realhash123"}),
                 encoding="utf-8")
    client = TestClient(create_app(p))
    token = _login(client)
    client.put("/api/config", json={"values": {"tg_api_hash": "••••••••"}}, headers=_h(token))
    assert _json.loads(p.read_text(encoding="utf-8"))["tg_api_hash"] == "realhash123"


def test_monitor_status_without_bot(tmp_path):
    """Bot 未运行时:状态接口照常可用,并明确标注 Bot 未运行。"""
    import json as _json

    p = tmp_path / "config.json"
    p.write_text(_json.dumps({"data_dir": str(tmp_path), "tg_api_id": 1, "tg_api_hash": "h",
                              "monitor_channels": "@src"}), encoding="utf-8")
    client = TestClient(create_app(p))
    token = _login(client)
    r = client.get("/api/monitor", headers=_h(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "stopped" and body["state_text"] == "Bot 未运行"
    assert body["api_set"] is True
    assert [c["ref"] for c in body["channels"]] == ["@src"]   # 配置行照常展示
    assert body["channels"][0]["ok"] is False                 # 未接入 → 不可达


def test_monitor_login_requires_bot(tmp_path):
    client = _client(tmp_path)
    token = _login(client)
    r = client.post("/api/monitor/login/start", json={"phone": "+8613800138000"}, headers=_h(token))
    assert r.status_code == 503


def test_status_includes_monitor_block(tmp_path):
    import json as _json

    p = tmp_path / "config.json"
    p.write_text(_json.dumps({"data_dir": str(tmp_path), "monitor_channels": "@a,@b"}),
                 encoding="utf-8")
    client = TestClient(create_app(p))
    token = _login(client)
    mon = client.get("/api/status", headers=_h(token)).json()["monitor"]
    assert mon["configured"] == 2 and mon["ready"] is False   # 缺 API 凭据 → 未就绪


def test_monitor_call_runs_on_bot_loop(tmp_path):
    """跨线程调度:Web 线程把监控协程投递到 Bot 事件循环里执行。

    Telethon 客户端绑定 Bot 循环,Web 侧必须走 run_coroutine_threadsafe——
    这里用真事件循环(另一个线程)+ 假监控实例验证这条通路。
    """
    import asyncio
    import threading

    from app import webapp as wa

    client = _client(tmp_path)
    token = _login(client)

    class FakeMonitor:
        def __init__(self):
            self.ran_in = None

        async def login_cancel(self):
            self.ran_in = threading.get_ident()
            return "已取消登录。"

        def runtime_status(self):
            return {"state": "stopped", "state_text": "未运行", "account": "",
                    "connected": False, "login_stage": "", "last_error": "",
                    "unreachable": {}}

    mon = FakeMonitor()
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    wa.STATE["monitor"] = mon
    wa.STATE["bot_loop"] = loop
    try:
        r = client.post("/api/monitor/login/cancel", headers=_h(token))
        assert r.status_code == 200, r.text
        assert r.json()["message"] == "已取消登录。"
        assert mon.ran_in == thread.ident       # 协程确实跑在 Bot 线程的循环里
    finally:
        wa.STATE.pop("monitor", None)
        wa.STATE.pop("bot_loop", None)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()


def test_monitor_login_503_when_loop_missing(tmp_path):
    """有监控实例但事件循环未就绪:明确 503 而非卡死。"""
    from app import webapp as wa

    client = _client(tmp_path)
    token = _login(client)
    wa.STATE["monitor"] = object()
    try:
        r = client.post("/api/monitor/login/cancel", headers=_h(token))
        assert r.status_code == 503
    finally:
        wa.STATE.pop("monitor", None)


# ── 待重启检测(顶部「保存并重启」条的判据) ────────────────
def test_fingerprint_tracks_value_changes(tmp_path):
    """指纹:键序无关;任一值变化即变。"""
    from app.config import fingerprint

    a = {"b": 2, "a": 1, "channels": [{"chat_id": "-100", "preset": "ed2k"}]}
    b = {"a": 1, "channels": [{"chat_id": "-100", "preset": "ed2k"}], "b": 2}
    assert fingerprint(a) == fingerprint(b)          # 键序不影响
    c = {"a": 1, "b": 3, "channels": a["channels"]}
    assert fingerprint(a) != fingerprint(c)          # 值变化 → 变化


def test_config_restart_pending_false_without_bot(tmp_path):
    """Bot 未运行:不催重启(由"未启动"状态表达)。"""
    client = _client(tmp_path)
    token = _login(client)
    assert client.get("/api/config", headers=_h(token)).json()["restart_pending"] is False


def test_config_restart_pending_after_change(tmp_path):
    """Bot 运行中(有启动指纹)+ 文件被改 → restart_pending=True;重启(刷新指纹)后回到 False。"""
    import json as _json

    from app import webapp as wa
    from app.config import fingerprint

    p = tmp_path / "config.json"
    p.write_text(_json.dumps({"data_dir": str(tmp_path), "proxy_url": "old"}), encoding="utf-8")
    client = TestClient(create_app(p))
    token = _login(client)

    # 模拟 Bot 启动:记下当时的配置指纹
    wa.STATE["bot_running"] = True
    wa.STATE["cfg_fingerprint"] = fingerprint(_json.loads(p.read_text(encoding="utf-8")))
    try:
        assert client.get("/api/config", headers=_h(token)).json()["restart_pending"] is False
        r = client.put("/api/config", json={"values": {"proxy_url": "new"}}, headers=_h(token))
        assert r.json()["restart_pending"] is True          # 改了但 Bot 还没吃上
        assert client.get("/api/config", headers=_h(token)).json()["restart_pending"] is True
        # 重启后 Bot 会重新取指纹 → 不再提示
        wa.STATE["cfg_fingerprint"] = fingerprint(
            _json.loads(p.read_text(encoding="utf-8"))
        )
        assert client.get("/api/config", headers=_h(token)).json()["restart_pending"] is False
    finally:
        wa.STATE["bot_running"] = False
        wa.STATE.pop("cfg_fingerprint", None)


def test_channels_save_sets_restart_pending(tmp_path):
    """网页改频道归属也要重启才生效 → 同样触发顶部提示。"""
    import json as _json

    from app import webapp as wa
    from app.config import fingerprint

    p = tmp_path / "config.json"
    p.write_text(_json.dumps({"data_dir": str(tmp_path)}), encoding="utf-8")
    client = TestClient(create_app(p))
    token = _login(client)
    wa.STATE["bot_running"] = True
    wa.STATE["cfg_fingerprint"] = fingerprint(_json.loads(p.read_text(encoding="utf-8")))
    try:
        r = client.put("/api/channels", json={"channels": [
            {"chat_id": "-1004300548880", "preset": "ed2k", "title": "ewewe"},
        ]}, headers=_h(token))
        assert r.status_code == 200 and r.json()["restart_pending"] is True
    finally:
        wa.STATE["bot_running"] = False
        wa.STATE.pop("cfg_fingerprint", None)


# ── 本地媒体流转目录(首次部署自动创建) ────────────────────
def test_media_dirs_derived_from_root(tmp_path):
    from app.config import Config

    cfg = Config(media_root=str(tmp_path / "media"))
    assert cfg.openlist_dir == tmp_path / "media" / "openlist"
    assert cfg.clouddrive_dir == tmp_path / "media" / "clouddrive"


def test_ensure_media_dirs_creates_once_then_idempotent(tmp_path):
    """首次部署建 openlist / clouddrive 两个子目录;再跑不重复创建。"""
    from app.config import Config

    cfg = Config(media_root=str(tmp_path / "media"))
    created = cfg.ensure_media_dirs()
    assert [Path(p).name for p in created] == ["media", "openlist", "clouddrive"]
    assert cfg.openlist_dir.is_dir() and cfg.clouddrive_dir.is_dir()
    assert cfg.ensure_media_dirs() == []          # 幂等
    assert cfg.openlist_dir.is_dir()


def test_media_root_from_config_file(tmp_path):
    import json as _json

    from app.config import load_config

    p = tmp_path / "config.json"
    p.write_text(_json.dumps({"media_root": str(tmp_path / "m")}), encoding="utf-8")
    assert load_config(p).openlist_dir == tmp_path / "m" / "openlist"


def test_status_reports_version(tmp_path):
    """侧栏版本号取自后端单一真源(app.__version__),且形如 x.y.z。

    别断言具体版本号(发版一改就红)——只锁"格式对 + 与真源一致"。
    """
    import re

    from app import __version__

    client = _client(tmp_path)
    token = _login(client)
    assert client.get("/api/status", headers=_h(token)).json()["version"] == __version__
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__), __version__


# ── openlist 目录选择器数据源 ───────────────────────────────
def test_openlist_dirs_requires_config(tmp_path):
    client = _client(tmp_path)
    token = _login(client)
    r = client.get("/api/openlist/dirs?path=/", headers=_h(token))
    assert r.status_code == 503 and "未配置 openlist" in r.json()["detail"]


def test_openlist_dirs_reports_upstream_error(tmp_path):
    """地址通但连不上 → 502 且带原因(便于排查,不是白屏)。"""
    import json as _json

    p = tmp_path / "config.json"
    p.write_text(_json.dumps({"data_dir": str(tmp_path),
                              "openlist_base_url": "http://127.0.0.1:1",
                              "openlist_token": "tok"}), encoding="utf-8")
    client = TestClient(create_app(p))
    token = _login(client)
    r = client.get("/api/openlist/dirs?path=/", headers=_h(token))
    assert r.status_code == 502 and "列目录失败" in r.json()["detail"]


def test_integer_keys_have_sane_defaults(tmp_path):
    """未写入 config.json 的整数键不能回 0(表单会把 0 显示出来再存回去)。"""
    client = _client(tmp_path)
    token = _login(client)
    d = client.get("/api/config", headers=_h(token)).json()
    assert d["openlist_max_tasks"] == 2 and d["fetch_interval_minutes"] == 5


# ── 总览聚合端点 ────────────────────────────────────────────
def test_pipeline_endpoint_shape(tmp_path):
    """总览主数据:三段 + 数字 + 趋势 + 待处理 + 最近,字段齐全。"""
    client = _client(tmp_path)
    token = _login(client)
    d = client.get("/api/pipeline", headers=_h(token)).json()
    assert [s["key"] for s in d["segments"]] == ["fetch", "process", "upload"]
    for s in d["segments"]:
        assert {"name", "from", "to", "items", "today", "last_activity"} <= set(s)
    # 只留磁盘卡(今日三数已删,读数看 7 日趋势图例)
    assert set(d["numbers"]) == {"disk"}
    # 本机实时资源(CPU/内存/网络)—— 读不到 /proc 时全是 None,字段必须在
    assert set(d["sys"]) >= {"cpu_percent", "mem_percent", "net_rx_bps", "net_tx_bps",
                             "cpu_count", "load1"}
    assert len(d["trend"]["labels"]) == 7 and len(d["trend"]["pushed"]) == 7
    assert len(d["trend"]["moved_gb"]) == 7 and len(d["trend"]["uploaded"]) == 7
    assert isinstance(d["attention"], list) and isinstance(d["recent"], list)
    # 未配置 CD2 时上传段标记为未启用
    assert d["segments"][2]["enabled"] is False


def test_daily_series_uploaded_counts(tmp_path):
    """趋势第三序列(上传条数):按天聚合、只算 status=done(失败不计),跟随 7 日窗口。"""
    import time as _time

    from app.store import Store

    s = Store(tmp_path / "stow.db")
    s.save_upload("a.mkv", 1, status="done")
    s.save_upload("b.mkv", 1, status="done")
    s.save_upload("c.mkv", 1, status="failed", error="目标不可写")   # 同一天但失败 → 不该计入
    s.save_upload("old.mkv", 1, status="done")
    # 把 old.mkv 拨到 3 天前(测试里直接拨 updated_at 是既有惯例,见 test_fetcher._age_backoff)
    s._conn.execute("UPDATE upload_tasks SET updated_at=? WHERE name=?",
                    (_time.time() - 3 * 86400, "old.mkv"))
    s._conn.commit()

    tr = s.daily_series(7)
    assert set(tr) == {"labels", "pushed", "moved_gb", "uploaded"}
    assert len(tr["uploaded"]) == len(tr["labels"]) == 7
    assert tr["uploaded"][-1] == 2        # 今天:两条成功(失败那条不算)
    assert tr["uploaded"][3] == 1         # 3 天前:一条
    assert sum(tr["uploaded"]) == 3       # 7 日窗口外的更早数据不进来


def test_pipeline_attention_lists_failures(tmp_path):
    import json as _json

    from app.store import Store

    media = tmp_path / "media/openlist"
    media.mkdir(parents=True)
    (media / "某片.mkv").write_bytes(b"x")          # 文件还在落地点,才该被列为待处理
    p = tmp_path / "config.json"
    p.write_text(_json.dumps({"data_dir": str(tmp_path), "media_root": str(tmp_path / "media")}),
                 encoding="utf-8")
    client = TestClient(create_app(p))
    token = _login(client)
    s = Store(tmp_path / "stow.db")
    s.save_local_file(name="某片.mkv", size=1, status="unrecognized", error="TMDB 未命中")
    s.close()
    items = client.get("/api/pipeline", headers=_h(token)).json()["attention"]
    assert items and items[0]["kind"] == "未识别" and "TMDB 未命中" in items[0]["reason"]


def test_pipeline_run_rejects_unknown_segment(tmp_path):
    client = _client(tmp_path)
    token = _login(client)
    r = client.post("/api/pipeline/run", json={"segment": "bogus"}, headers=_h(token))
    assert r.status_code == 400


def test_pipeline_run_requires_bot(tmp_path):
    client = _client(tmp_path)
    token = _login(client)
    r = client.post("/api/pipeline/run", json={"segment": "fetch"}, headers=_h(token))
    assert r.status_code == 503


# ── 待处理清单不误报"文件已不在" ────────────────────────────
def test_attention_skips_records_without_file(tmp_path):
    """处理类失败/未识别:落地点里已无该文件(已改名或人工删)→ 不再列入待处理。"""
    import json as _json

    from app.store import Store

    media = tmp_path / "media/openlist"
    media.mkdir(parents=True)
    (media / "还在.mkv").write_bytes(b"x")
    p = tmp_path / "config.json"
    p.write_text(_json.dumps({"data_dir": str(tmp_path), "media_root": str(tmp_path / "media")}),
                 encoding="utf-8")
    client = TestClient(create_app(p))
    token = _login(client)
    s = Store(tmp_path / "stow.db")
    s.save_local_file(name="还在.mkv", size=1, status="unrecognized", error="TMDB 未命中")
    s.save_local_file(name="早改名了.mkv", size=1, status="failed", error="PermissionError")
    s.close()
    items = client.get("/api/pipeline", headers=_h(token)).json()["attention"]
    texts = [a["text"] for a in items]
    assert "还在.mkv" in texts and "早改名了.mkv" not in texts


def test_attention_keeps_fetch_and_upload_failures(tmp_path):
    """获取/上传失败不受"文件是否存在"过滤影响(它们是别的位置)。"""
    from app.store import Store

    client = _client(tmp_path)
    token = _login(client)
    s = Store(tmp_path / "stow.db")
    s.save_fetch("/夸克云盘/x/a.mkv", 1, status="failed", error="任务丢失")
    s.save_upload("b.mkv", 1, status="failed", error="目标不可写")
    s.close()
    kinds = {a["kind"] for a in client.get("/api/pipeline", headers=_h(token)).json()["attention"]}
    assert {"获取失败", "上传失败"} <= kinds


# ── 健康判据(_health_block:按段列条目,四档 ok/warn/bad/off) ──────
# 2026-09-11 重构:旧判据只覆盖 Bot/代理/115/频道监控(推卡+转存那半套),
# 三段链(openlist/ffmpeg/CD2)完全没查过;而且旧文案还谎报"三段链 全部正常"。
def _health_cfg(tmp_path, **over):
    """造一份能"全绿"的配置:媒体目录真实存在,探针结果由 _probes 桩掉。"""
    import json as _json

    from app.config import load_config

    p = tmp_path / "config.json"
    p.write_text(_json.dumps({"data_dir": str(tmp_path),
                              "media_root": str(tmp_path / "media"), **over}), encoding="utf-8")
    cfg = load_config(p)
    cfg.ensure_media_dirs()
    return cfg


def _probes(web, *, proxy=True, pan115=True, openlist=None, cd2=None):
    """把四个探针的缓存直接写成指定结果(不触网)。"""
    import time as _time

    off_ol = {"configured": False, "enabled": False, "ok": False,
              "missing_dirs": [], "error": ""}
    off_cd2 = {"configured": False, "enabled": False, "ok": False, "error": ""}
    web._health_cache.update({
        "proxy": (_time.monotonic(),
                  {"configured": True, "ok": proxy, "error": "" if proxy else "连不上"}),
        "pan115": (_time.monotonic(),
                   {"cookie_set": True, "ok": pan115, "uid": 1, "error": "" if pan115 else "失效"}),
        "openlist": (_time.monotonic(), openlist or off_ol),
        "cd2": (_time.monotonic(), cd2 or off_cd2),
    })


_STATS_OK = {"fetch": {"failed": 0}, "upload": {"failed": 0}, "process": {"manual": 0}}


def _disk(pct: float = 33.6) -> dict:
    total = 476 * 1024 ** 3
    return {"used_percent": pct, "total_bytes": total, "free_bytes": total * (1 - pct / 100)}


def _levels(block) -> dict:
    return {r["key"]: r["level"] for r in block["rows"]}


def _green(web, monkeypatch, tmp_path, **over):
    """全绿基线:代理/115 通、Bot 在跑、编码工具就绪。"""
    monkeypatch.setattr(web, "_tools_status", lambda: {"ffmpeg": True, "ffprobe": True})
    _probes(web)
    monkeypatch.setitem(web.STATE, "bot_running", True)
    monkeypatch.setitem(web.STATE, "bot_error", "")
    return _health_cfg(tmp_path, **over)


def test_health_all_green_and_off_segments_excluded(tmp_path, monkeypatch):
    """全绿;且**未配置的段只标 off、不参与总判**(否则只用推卡的人天天看黄)。"""
    import app.webapp as web

    cfg = _green(web, monkeypatch, tmp_path)
    block = web._health_block(cfg, _STATS_OK, _disk())
    assert block["level"] == "ok" and block["text"] == "系统运行中"
    lv = _levels(block)
    assert lv["openlist"] == "off" and lv["cd2"] == "off" and lv["monitor"] == "off"
    assert lv["bot"] == "ok" and lv["disk"] == "ok"
    # 已配但"未启用"(缺监控目录/缺上传目标)同样只是 off
    _probes(web, openlist={"configured": True, "enabled": False, "ok": False,
                           "missing_dirs": [], "error": ""})
    assert web._health_block(cfg, _STATS_OK, _disk())["level"] == "ok"


def test_health_bot_down_carries_reason(tmp_path, monkeypatch):
    """Bot 未运行 → 异常,且**带上原因**(旧判据只说"未运行",不说为什么)。"""
    import app.webapp as web

    cfg = _green(web, monkeypatch, tmp_path)
    monkeypatch.setitem(web.STATE, "bot_running", False)
    monkeypatch.setitem(web.STATE, "bot_error", "Conflict: terminated by other getUpdates")
    block = web._health_block(cfg, _STATS_OK, _disk())
    assert block["level"] == "bad" and block["text"] == "系统异常"
    bot = next(r for r in block["rows"] if r["key"] == "bot")
    assert "未运行" in bot["text"] and "Conflict" in bot["text"]


def test_health_disk_thresholds(tmp_path, monkeypatch):
    """磁盘:79→正常、85→降级、95→异常。"""
    import app.webapp as web

    cfg = _green(web, monkeypatch, tmp_path)
    assert web._health_block(cfg, _STATS_OK, _disk(79.0))["level"] == "ok"
    assert web._health_block(cfg, _STATS_OK, _disk(85.0))["level"] == "warn"
    assert web._health_block(cfg, _STATS_OK, _disk(95.0))["level"] == "bad"
    # 读不到磁盘信息(挂载丢了)= 异常
    assert web._health_block(cfg, _STATS_OK, {"used_percent": 0, "total_bytes": 0})["level"] == "bad"


def test_health_segment_failures_graded(tmp_path, monkeypatch):
    """段失败积压:1 个→降级,5 个→异常。"""
    import app.webapp as web

    cfg = _green(web, monkeypatch, tmp_path)
    one = {"fetch": {"failed": 1}, "upload": {"failed": 0}, "process": {"manual": 0}}
    assert web._health_block(cfg, one, _disk())["level"] == "warn"
    many = {"fetch": {"failed": 5}, "upload": {"failed": 0}, "process": {"manual": 0}}
    blk = web._health_block(cfg, many, _disk())
    assert blk["level"] == "bad" and _levels(blk)["fetch_fail"] == "bad"
    # 处理待人工 → 降级(它是"有待办",不是"坏了")
    manual = {"fetch": {"failed": 0}, "upload": {"failed": 0}, "process": {"manual": 3}}
    assert web._health_block(cfg, manual, _disk())["level"] == "warn"


def test_health_bot_heartbeat_detects_hang(tmp_path, monkeypatch):
    """心跳:连丢 3 拍 → 异常。这是"进程在、事件循环卡住"的唯一信号——
    bot_running 是启动标志,卡死期间照样 true,所以必须有这一条。"""
    import time as _time

    import app.webapp as web

    cfg = _green(web, monkeypatch, tmp_path)
    # 还没打点过 → 不算异常(刚启动)
    assert _row(web._health_block(cfg, _STATS_OK, _disk()), "heartbeat")["level"] == "ok"
    monkeypatch.setitem(web.STATE, "bot_heartbeat", _time.time())
    assert _row(web._health_block(cfg, _STATS_OK, _disk()), "heartbeat")["level"] == "ok"
    # 超时未跳 → 异常
    monkeypatch.setitem(web.STATE, "bot_heartbeat",
                        _time.time() - (web._HEARTBEAT_STALE + 30))
    blk = web._health_block(cfg, _STATS_OK, _disk())
    assert blk["level"] == "bad" and "无进展" in _row(blk, "heartbeat")["text"]
    # Bot 没跑时不该出现这一条(免得和 Bot 那条重复)
    monkeypatch.setitem(web.STATE, "bot_running", False)
    assert not any(r["key"] == "heartbeat"
                   for r in web._health_block(cfg, _STATS_OK, _disk())["rows"])


def test_health_openlist_unreachable_vs_missing_dir(tmp_path, monkeypatch):
    """获取段:已配却连不上 = 异常;连得上但监控目录没了 = 异常(两种文案要分清)。"""
    import app.webapp as web

    cfg = _green(web, monkeypatch, tmp_path, openlist_base_url="http://127.0.0.1:5244",
                 openlist_token="t", openlist_monitor_dirs="/夸克云盘/影库")
    _probes(web, openlist={"configured": True, "enabled": True, "ok": False,
                           "missing_dirs": [], "error": "ConnectError: 拒绝连接"})
    blk = web._health_block(cfg, _STATS_OK, _disk())
    assert blk["level"] == "bad" and "连不上" in _row(blk, "openlist")["text"]

    _probes(web, openlist={"configured": True, "enabled": True, "ok": False,
                           "missing_dirs": ["/夸克云盘/影库(not found)"], "error": ""})
    blk = web._health_block(cfg, _STATS_OK, _disk())
    assert blk["level"] == "bad" and "监控目录不存在" in _row(blk, "openlist")["text"]


def _row(block, key: str) -> dict:
    return next(r for r in block["rows"] if r["key"] == key)


def test_health_cd2_and_tools_and_media(tmp_path, monkeypatch):
    """上传段连不上=异常;编码工具缺失=降级;落地目录没了=异常。"""
    import shutil

    import app.webapp as web

    cfg = _green(web, monkeypatch, tmp_path, cd2_address="127.0.0.1:19798",
                 cd2_dest_path="/115open/工具测试目录")
    _probes(web, cd2={"configured": True, "enabled": True, "ok": False, "error": "CD2 未登录 cloudfs"})
    blk = web._health_block(cfg, _STATS_OK, _disk())
    assert blk["level"] == "bad" and "连不上" in _row(blk, "cd2")["text"]

    _probes(web)                                   # CD2 未配置 → off,不参与
    monkeypatch.setattr(web, "_tools_status", lambda: {"ffmpeg": False, "ffprobe": True})
    blk = web._health_block(cfg, _STATS_OK, _disk())
    assert blk["level"] == "warn" and "ffmpeg" in _row(blk, "tools")["text"]

    shutil.rmtree(tmp_path / "media/openlist")
    blk = web._health_block(cfg, _STATS_OK, _disk())
    assert blk["level"] == "bad" and "不存在" in _row(blk, "media_a")["text"]


def test_pipeline_payload_includes_health(tmp_path, monkeypatch):
    """健康判据随 /api/pipeline 一起下发(前端只渲染,不再自己算判据)。"""
    import app.webapp as web

    async def fake_proxy(url):
        return {"configured": bool(url), "url": url, "ok": True, "latency_ms": 1, "error": ""}

    monkeypatch.setattr(web, "_check_proxy", fake_proxy)
    monkeypatch.setattr(web, "_check_openlist", lambda cfg: _async({
        "configured": False, "enabled": False, "ok": False, "missing_dirs": [], "error": ""}))
    monkeypatch.setattr(web, "_check_cd2",
                        lambda cfg: {"configured": False, "enabled": False, "ok": False, "error": ""})
    web._health_cache.update({k: (0.0, None) for k in web._health_cache})

    client = _client(tmp_path)
    token = _login(client)
    d = client.get("/api/pipeline", headers=_h(token)).json()
    assert set(d["health"]) == {"level", "text", "detail", "rows"}
    assert d["health"]["level"] in ("ok", "warn", "bad")
    keys = {r["key"] for r in d["health"]["rows"]}
    assert {"bot", "proxy", "pan115", "monitor", "openlist",
            "tools", "cd2", "media_a", "media_b", "disk"} <= keys


def _async(value):
    async def _coro():
        return value
    return _coro()
