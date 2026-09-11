"""本机资源检测测试:解析做成纯函数(吃文本),所以在 Windows 开发机上也能跑。

真实 /proc 只在容器/Linux 里才有(本机开发没有),所以这里一律喂固定文本,
不依赖运行环境;速率那部分用桩时钟推进。
"""

from __future__ import annotations

from pathlib import Path

import app.sysinfo as si

# user nice system idle iowait irq softirq steal guest guest_nice
_CPU0 = "cpu  100 0 50 800 50 0 0 0 0 0\ncpu0 50 0 25 400 25 0 0 0 0 0\n"
# 同一时刻,但 guest/guest_nice 有值(它们已计入 user/nice,不能再加一遍)
_CPU0_GUEST = "cpu  100 0 50 800 50 0 0 0 30 40\n"
_CPU1 = "cpu  200 0 60 900 60 0 0 0 0 0\n"

_MEM = "MemTotal:       8000000 kB\nMemFree:         500000 kB\nMemAvailable:   2000000 kB\n"

_NET0 = (
    "Inter-|   Receive     |  Transmit\n"
    " face |bytes  packets errs drop fifo frame compressed multicast|bytes\n"
    "    lo: 999999999 1000 0 0 0 0 0 0 999999999 1000 0 0 0 0 0 0\n"
    "  eth0: 1000 10 0 0 0 0 0 0 500 5 0 0 0 0 0 0\n"
)
_NET1 = (
    "Inter-|   Receive     |  Transmit\n"
    " face |bytes  packets errs drop fifo frame compressed multicast|bytes\n"
    "    lo: 999999999 1000 0 0 0 0 0 0 999999999 1000 0 0 0 0 0 0\n"
    "  eth0: 3048 20 0 0 0 0 0 0 700 7 0 0 0 0 0 0\n"
)


def test_parse_cpu_busy_excludes_idle_and_iowait():
    """/proc/stat 汇总行:idle+iowait 才算空闲(与 top 口径一致)。"""
    busy, total = si.parse_cpu(_CPU0)
    assert (busy, total) == (150, 1000)          # total = user..steal,不含 guest


def test_parse_cpu_guest_not_double_counted():
    """guest/guest_nice 已计入 user/nice —— 有 guest 时 total 不能变。"""
    assert si.parse_cpu(_CPU0_GUEST) == (150, 1000)


def test_parse_cpu_rejects_garbage():
    assert si.parse_cpu("no cpu line here\n") is None
    assert si.parse_cpu("cpu  1 2 3\n") is None   # 字段不足


def test_parse_mem_uses_available_not_free():
    """可用内存用 MemAvailable(比 MemFree 准 —— 缓存也算可用)。"""
    m = si.parse_mem(_MEM)
    assert m["total_bytes"] == 8000000 * 1024
    assert m["available_bytes"] == 2000000 * 1024     # 不是 MemFree 的 500000
    assert m["used_bytes"] == 6000000 * 1024
    assert m["percent"] == 75.0


def test_parse_mem_rejects_empty():
    assert si.parse_mem("SomethingElse: 1 kB\n") is None


def test_parse_net_skips_loopback():
    """lo 不算网络流量(否则本机内部通信会虚高)。"""
    assert si.parse_net(_NET0) == (1000, 500)
    assert si.parse_net(_NET1) == (3048, 700)


def _fake_read(texts: dict):
    return lambda p: texts.get(Path(p).name)


def test_snapshot_first_call_has_no_rates(monkeypatch):
    """首次采样没有基准 → CPU% 与网络速率为 None(前端显示 —),不能瞎报 0。"""
    monkeypatch.setattr(si, "_read", _fake_read(
        {"stat": _CPU0, "dev": _NET0, "meminfo": _MEM, "loadavg": "0.42 0.30 0.20 1/234 5678\n"}))
    monkeypatch.setattr(si, "_last", None)
    monkeypatch.setattr(si.time, "monotonic", lambda: 100.0)

    out = si.snapshot()
    assert out["cpu_percent"] is None and out["net_rx_bps"] is None
    assert out["mem_percent"] == 75.0                     # 内存是瞬时量,首次就有
    assert out["load1"] == 0.42
    assert out["net_rx_bytes"] == 1000


def test_snapshot_rates_from_two_samples(monkeypatch):
    """第二次采样:CPU% 与上下行速率按两次之间的差值算。"""
    state = {"stat": _CPU0, "dev": _NET0}
    monkeypatch.setattr(si, "_read", lambda p: state.get(Path(p).name))
    monkeypatch.setattr(si, "_last", None)
    clock = iter([100.0, 110.0])
    monkeypatch.setattr(si.time, "monotonic", lambda: next(clock))

    assert si.snapshot()["cpu_percent"] is None
    state["stat"], state["dev"] = _CPU1, _NET1
    out = si.snapshot()
    # dt=10s:busy 150→260(+110),total 1000→1220(+220) → 50.0%
    assert out["cpu_percent"] == 50.0
    assert out["net_rx_bps"] == 205        # 2048B / 10s
    assert out["net_tx_bps"] == 20         # 200B / 10s


def test_snapshot_tolerates_missing_proc(monkeypatch):
    """读不到 /proc(Windows 开发机/无权限)不能抛异常,一律 None 交给前端显示 —。"""
    monkeypatch.setattr(si, "_read", lambda p: None)
    monkeypatch.setattr(si, "_last", None)

    out = si.snapshot()
    assert out["cpu_percent"] is None and out["mem_percent"] is None
    assert out["net_rx_bytes"] is None and out["load1"] is None
    assert out["cpu_count"]                            # 这条不依赖 /proc
