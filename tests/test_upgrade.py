"""系统工具:版本检测与一键升级(docker SDK / GitHub API 全 mock,不触网不碰 daemon)。"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest import mock

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


# ── 一键升级(docker SDK mock) ─────────────────────────────
class _Old:
    name = "stow"
    attrs = {
        "Config": {"Env": ["A=1"], "Cmd": ["python", "-m", "app.main"],
                   "Entrypoint": None, "WorkingDir": "/app", "Labels": None},
        "HostConfig": {"Binds": ["/x:/app/data"], "NetworkMode": "host",
                       "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
                       "Init": True, "Privileged": False, "ExtraHosts": None, "Dns": None},
    }

    def __init__(self):
        self.stopped = self.removed = False

    def stop(self):
        self.stopped = True

    def remove(self):
        self.removed = True


class _New:
    def __init__(self):
        self.started = False

    def start(self):
        self.started = True


class _Images:
    def __init__(self):
        self.pulled = []

    def pull(self, image):
        self.pulled.append(image)


class _Containers:
    def __init__(self):
        self.old = _Old()
        self.created = []
        self.runs = []           # sidecar 启动参数 (image, kw)
        self.removed_sidecars = []

    def get(self, name):
        if name.endswith("-rebuild") and not self.old.stopped:
            raise RuntimeError("no such container")
        return self.old

    def remove(self, force=False):
        self.removed_sidecars.append(force)

    def create(self, image, **kw):
        self.created.append((image, kw))
        return _New()

    def run(self, image, **kw):
        self.runs.append((image, kw))
        return _New()


class _Api:
    def create_host_config(self, **kw):
        return dict(kw)


class _Client:
    def __init__(self):
        self.images = _Images()
        self.containers = _Containers()
        self.api = _Api()


def _fake_docker(monkeypatch):
    client = _Client()
    mod = mock.MagicMock()
    mod.from_env.return_value = client
    monkeypatch.setitem(sys.modules, "docker", mod)
    return client


def test_upgrade_success(monkeypatch, tmp_path):
    """主进程:拉镜像 + 启动 sidecar(--rebuild),不自己重建。"""
    client = _fake_docker(monkeypatch)
    ok, msg = upgrade.upgrade(SimpleNamespace(), data_dir=str(tmp_path))
    assert ok
    assert client.images.pulled == [upgrade.IMAGE]
    assert client.containers.old.stopped is False  # 主进程不动旧容器
    img, kw = client.containers.runs[0]
    assert img == upgrade.IMAGE
    assert kw["name"] == "stow-rebuild"
    assert kw["command"] == ["python", "-m", "app.upgrade", "--rebuild"]
    assert kw["detach"] is True
    assert kw["network_mode"] == "host"
    assert "/var/run/docker.sock" in kw["volumes"]
    # data 挂载:从旧容器 Binds 解析宿主机路径
    assert "/x:/app/data" in kw["volumes"] or kw["volumes"].get("/x")


def test_upgrade_pull_fails(monkeypatch):
    client = _fake_docker(monkeypatch)

    def boom(image):
        raise RuntimeError("denied")

    client.images.pull = boom
    ok, msg = upgrade.upgrade(SimpleNamespace())
    assert not ok and "拉取镜像失败" in msg


def test_upgrade_connect_fails(monkeypatch):
    mod = mock.MagicMock()
    mod.from_env.side_effect = RuntimeError("cannot connect")
    monkeypatch.setitem(sys.modules, "docker", mod)
    ok, msg = upgrade.upgrade(SimpleNamespace())
    assert not ok and "连接 docker daemon 失败" in msg


# ── sidecar 重建(--rebuild) ───────────────────────────────
def test_rebuild_sidecar_success(monkeypatch, tmp_path):
    """sidecar:停/删旧 → create 原名 → start;结果落盘。"""
    client = _fake_docker(monkeypatch)
    client.containers.old.stopped = False  # 模拟旧容器在跑
    upgrade._rebuild_sidecar()
    old = client.containers.old
    assert old.stopped and old.removed
    img, kw = client.containers.created[0]
    assert img == upgrade.IMAGE
    assert kw["name"] == "stow"  # 原名(旧的已删,不再冲突)
    assert kw["environment"] == ["A=1"]
    assert kw["network_mode"] == "host"
    assert kw["restart_policy"]["Name"] == "unless-stopped"
    assert kw["init"] is True
    assert kw["volumes"] == ["/x:/app/data"]
    assert client.containers.created[0][1] is not None
    # 结果落盘到 /app/data(测试里 mock 写不动,用临时目录验证 _write_state 行为)
    # sidecar 固定写 /app/data;这里单独验证 _write_state
    state_dir = tmp_path / "d"
    upgrade._write_state(str(state_dir), True, "升级完成,已用新镜像重建容器")
    state = upgrade.read_upgrade_state(str(state_dir))
    assert state["ok"] is True and "升级完成" in state["message"]
    assert state["to_version"] == upgrade.__version__


def test_rebuild_sidecar_failure_marks_state(monkeypatch):
    """sidecar 内异常 → 结果写 False(前端能读到失败原因)。"""
    client = _fake_docker(monkeypatch)

    def boom(name):
        raise RuntimeError("no such container")

    client.containers.create = boom
    ok, msg = upgrade._rebuild_sidecar()
    assert ok is False and "重建容器失败" in msg


def test_upgrade_missing_docker_sdk(monkeypatch):
    """没装 docker SDK → 明确错误,不抛。"""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "docker":
            raise ImportError("No module named 'docker'")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    ok, msg = upgrade.upgrade(SimpleNamespace())
    assert not ok and "缺少 docker SDK" in msg
