"""Web 配置台测试:登录 / 配置脱敏读写 / 账号 / 状态 / 重启(不真退)。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.webapp import create_app


def _client(tmp_path):
    return TestClient(create_app(tmp_path / "config.json"))


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
        json={"values": {"tg_bot_token": "T0KEN", "tg_chat_id": "-1001"}},
        headers=_h(token),
    )
    r = client.get("/api/config", headers=_h(token))
    assert r.json()["tg_bot_token"] == "••••••••"  # 已设置 → 掩码
    assert r.json()["tg_chat_id"] == "-1001"       # 非敏感 → 明文


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
    assert any("tg_chat_id" in m for m in body["missing"])


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
