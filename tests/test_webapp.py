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
    """侧栏版本号取自后端单一真源(app.__version__)。"""
    from app import __version__

    client = _client(tmp_path)
    token = _login(client)
    assert client.get("/api/status", headers=_h(token)).json()["version"] == __version__
    assert __version__.startswith("0.2")


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
