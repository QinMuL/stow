"""系统工具:版本检测(GitHub API mock,不触网)。一键升级功能已移除。"""

from __future__ import annotations

from types import SimpleNamespace

from app import upgrade
from app.upgrade import parse_ver, version_report


# ── 版本解析与对比 ────────────────────────────────────────
def test_parse_ver():
    assert parse_ver("v1.2.3") == (1, 2, 3)
    assert parse_ver("1.2.3-beta.1") == (1, 2, 3)
    assert parse_ver("1.0.3") == (1, 0, 3)
    assert parse_ver("") == ()
    assert parse_ver("abc") == ()


def test_version_report():
    assert version_report("v1.1.0", "1.0.2") == {
        "current": "1.0.2", "latest": "v1.1.0", "has_update": True,
    }
    assert version_report("v1.0.2", "1.0.2")["has_update"] is False
    assert version_report("1.0.2", "1.0.3")["has_update"] is False  # 旧 tag 不算更新
    assert version_report("", "1.0.3")["has_update"] is False        # 远端解析不出不误报


# ── 版本检测(GitHub API mock) ─────────────────────────────
class _Resp:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self._data = data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._data


class _HClient:
    def __init__(self, resp):
        self._resp = resp

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url):
        return self._resp


def _fake_httpx(monkeypatch, resp):
    # httpx 是 check_version 内部局部导入,从全局 httpx 模块 patch;捕获 Client 构造参数
    captured = {}

    def _client(**kw):
        captured["kw"] = kw
        return _HClient(resp)

    monkeypatch.setattr("httpx.Client", _client)
    return captured


def test_check_version_ok(monkeypatch):
    _fake_httpx(monkeypatch, _Resp(data={"tag_name": "v9.9.9"}))
    r = upgrade.check_version(SimpleNamespace(proxy_url=""))
    assert r["has_update"] is True and r["latest"] == "v9.9.9"
    assert r["current"] == upgrade.__version__


def test_check_version_up_to_date(monkeypatch):
    _fake_httpx(monkeypatch, _Resp(data={"tag_name": f"v{upgrade.__version__}"}))
    assert upgrade.check_version(SimpleNamespace(proxy_url=""))["has_update"] is False


def test_check_version_404(monkeypatch):
    _fake_httpx(monkeypatch, _Resp(status_code=404))
    assert "error" in upgrade.check_version(SimpleNamespace(proxy_url=""))


def test_check_version_network_error(monkeypatch):
    def boom(**kw):
        raise OSError("connection reset")

    monkeypatch.setattr("httpx.Client", boom)
    r = upgrade.check_version(SimpleNamespace(proxy_url=""))
    assert r["error"].startswith("检测失败:")


def test_check_version_sends_token(monkeypatch):
    cap = _fake_httpx(monkeypatch, _Resp(data={"tag_name": "v9.9.9"}))
    r = upgrade.check_version(SimpleNamespace(proxy_url="", github_token="ghp_abc"))
    assert r["has_update"] is True
    assert cap["kw"]["headers"]["Authorization"] == "Bearer ghp_abc"


def test_check_version_no_token_no_auth_header(monkeypatch):
    cap = _fake_httpx(monkeypatch, _Resp(data={"tag_name": "v9.9.9"}))
    upgrade.check_version(SimpleNamespace(proxy_url=""))
    assert "Authorization" not in cap["kw"]["headers"]


def test_module_has_no_upgrade_function():
    """升级功能已移除(2026-09-13 用户决定,不再实现一键升级)。"""
    assert not hasattr(upgrade, "upgrade")
