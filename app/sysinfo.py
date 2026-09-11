"""本机资源实时检测(CPU / 内存 / 网络):只读 /proc,零依赖。

**口径提醒**:容器里读到的是**承载它的那台机器**(WSL 虚拟机)的资源,不是 Windows 宿主机的
——与磁盘卡同一口径(媒体目录本身也落在宿主盘上)。

CPU% 与网络速率是**差值**:要两次采样才算得出来,所以 `snapshot()` 把上次采样留在模块里,
返回"与上次调用之间"的平均值;首次调用这两项为 None(前端显示 —)。
解析全部做成纯函数(吃文本),这样在 Windows 开发机上也能测。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

_PROC = Path("/proc")

# 上次采样:(单调时钟, CPU busy/total, 网络 rx/tx 累计字节)
_last: tuple[float, tuple[int, int] | None, tuple[int, int] | None] | None = None


def parse_cpu(stat_text: str) -> tuple[int, int] | None:
    """解析 /proc/stat 的 `cpu ` 汇总行 → (busy_ticks, total_ticks)。

    字段序:user nice system idle iowait irq softirq steal guest guest_nice。
    `guest`/`guest_nice` 已经计入 user/nice,**不能重复加**;`idle+iowait` 才算空闲
    (iowait 是"没活干在等 IO",算空闲才与 top 的口径一致)。
    """
    for line in stat_text.splitlines():
        if line.startswith("cpu "):
            parts = [int(x) for x in line.split()[1:] if x.isdigit()]
            if len(parts) < 5:
                return None
            idle = parts[3] + parts[4]
            total = sum(parts[:8])
            return max(0, total - idle), total
    return None


def parse_mem(meminfo_text: str) -> dict | None:
    """解析 /proc/meminfo → 内存使用。用 `MemAvailable` 算可用(比 MemFree 准)。"""
    vals: dict[str, int] = {}
    for line in meminfo_text.splitlines():
        key, _, rest = line.partition(":")
        cols = rest.split()
        if cols and cols[0].isdigit():
            vals[key.strip()] = int(cols[0]) * 1024      # /proc 以 kB 计
    total = vals.get("MemTotal")
    if not total:
        return None
    avail = vals.get("MemAvailable", vals.get("MemFree", 0))
    used = max(0, total - avail)
    return {"total_bytes": total, "available_bytes": avail, "used_bytes": used,
            "percent": round(used / total * 100, 1)}


def parse_net(dev_text: str) -> tuple[int, int]:
    """解析 /proc/net/dev → (累计收字节, 累计发字节);**排除 lo**(回环不算网络流量)。"""
    rx = tx = 0
    for line in dev_text.splitlines()[2:]:               # 前两行是表头
        name, _, rest = line.partition(":")
        if name.strip() == "lo" or not rest.strip():
            continue
        cols = rest.split()
        if len(cols) >= 9:
            rx += int(cols[0])
            tx += int(cols[8])
    return rx, tx


def parse_loadavg(text: str) -> float | None:
    parts = text.split()
    try:
        return float(parts[0])
    except (IndexError, ValueError):
        return None


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def snapshot() -> dict:
    """本机实时指标(读不到的一律给 None,前端显示 —;这函数不抛异常)。

    CPU% / 网络速率按"与上次调用之间"的差值算 —— 页面每 10 秒轮询,正好当采样间隔。
    """
    global _last
    now = time.monotonic()
    cpu = net = None
    if (txt := _read(_PROC / "stat")) is not None:
        cpu = parse_cpu(txt)
    if (txt := _read(_PROC / "net" / "dev")) is not None:
        net = parse_net(txt)
    mem = parse_mem(txt) if (txt := _read(_PROC / "meminfo")) is not None else None
    load1 = parse_loadavg(txt) if (txt := _read(_PROC / "loadavg")) is not None else None

    out: dict = {
        "cpu_count": os.cpu_count(),
        "load1": load1,
        "cpu_percent": None,
        "mem_percent": (mem or {}).get("percent"),
        "mem_used_bytes": (mem or {}).get("used_bytes"),
        "mem_total_bytes": (mem or {}).get("total_bytes"),
        "net_rx_bps": None,
        "net_tx_bps": None,
        "net_rx_bytes": net[0] if net else None,
        "net_tx_bytes": net[1] if net else None,
    }
    if _last is not None:
        at0, cpu0, net0 = _last
        dt = now - at0
        if dt > 0.5:
            if cpu and cpu0 and cpu[1] > cpu0[1]:
                busy, total = cpu[0] - cpu0[0], cpu[1] - cpu0[1]
                out["cpu_percent"] = round(max(0.0, busy / total * 100), 1)
            if net and net0 and dt > 0:
                out["net_rx_bps"] = max(0, round((net[0] - net0[0]) / dt))
                out["net_tx_bps"] = max(0, round((net[1] - net0[1]) / dt))
    _last = (now, cpu, net)
    return out
