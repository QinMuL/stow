"""系统工具:版本检测与一键升级(Watchtower 触发式,全 mock 不触网不碰 daemon)。"""

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
    def __init__(self, status_code=200, data=None, text=""):
        self.status_code = status_code
        self._data = data
        self.text = text

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


# ── 一键升级(Watchtower HTTP API mock) ──────────────────────
class _WtClient:
    """mock httpx.Client:记录 post 的 url/headers,按预设返回或抛异常。"""

    def __init__(self, resp=None, exc=None):
        self._resp, self._exc = resp, exc
        self.request = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, headers=None, **kw):
        self.request = {"url": url, "headers": headers}
        if self._exc:
            raise self._exc
        return self._resp


def _fake_wt(monkeypatch, resp=None, exc=None):
    client = _WtClient(resp, exc)
    monkeypatch.setattr("httpx.Client", lambda **kw: client)
    return client


def test_upgrade_success_default_endpoint(monkeypatch):
    """默认打 127.0.0.1:8080/v1/update,令牌取 compose 对齐的默认值。"""
    client = _fake_wt(monkeypatch, resp=_Resp(status_code=204))
    ok, msg = upgrade.upgrade(SimpleNamespace())
    assert ok and "已触发" in msg
    assert client.request["url"] == "http://127.0.0.1:8080/v1/update"
    assert client.request["headers"]["Authorization"] == "Bearer stow-upgrade"


def test_upgrade_success_custom_url_token(monkeypatch):
    """配置了 watchtower_url/token 时用配置值(末尾斜杠要去掉)。"""
    client = _fake_wt(monkeypatch, resp=_Resp(status_code=200))
    cfg = SimpleNamespace(watchtower_url="http://192.168.1.202:8080/",
                          watchtower_token="my-secret")
    ok, msg = upgrade.upgrade(cfg)
    assert ok
    assert client.request["url"] == "http://192.168.1.202:8080/v1/update"
    assert client.request["headers"]["Authorization"] == "Bearer my-secret"


def test_upgrade_token_from_env(monkeypatch):
    """配置为空时回退读环境变量 WATCHTOWER_TOKEN(compose 注入的那份)。"""
    client = _fake_wt(monkeypatch, resp=_Resp(status_code=204))
    monkeypatch.setenv("WATCHTOWER_TOKEN", "from-env")
    ok, _ = upgrade.upgrade(SimpleNamespace())
    assert ok
    assert client.request["headers"]["Authorization"] == "Bearer from-env"


def test_upgrade_rejected_bad_token(monkeypatch):
    _fake_wt(monkeypatch, resp=_Resp(status_code=401))
    ok, msg = upgrade.upgrade(SimpleNamespace())
    assert not ok and "令牌不匹配" in msg


def test_upgrade_connect_error(monkeypatch):
    import httpx

    _fake_wt(monkeypatch, exc=httpx.ConnectError("connection refused"))
    ok, msg = upgrade.upgrade(SimpleNamespace())
    assert not ok and "连不上 Watchtower" in msg


def test_upgrade_http_error(monkeypatch):
    _fake_wt(monkeypatch, resp=_Resp(status_code=500))
    ok, msg = upgrade.upgrade(SimpleNamespace())
    assert not ok and "HTTP 500" in msg


def test_upgrade_unexpected_error(monkeypatch):
    _fake_wt(monkeypatch, exc=OSError("boom"))
    ok, msg = upgrade.upgrade(SimpleNamespace())
    assert not ok and "触发失败" in msg


# ── 旧方案的教训(防回归注释,不测实现细节) ──────────────────
def test_upgrade_module_has_no_docker_sdk_usage():
    """升级不再 import docker SDK —— 重建交给 Watchtower,stow 不碰宿主机 daemon。"""
    import inspect

    src = inspect.getsource(upgrade)
    assert "import docker" not in src and "from_env" not in src
