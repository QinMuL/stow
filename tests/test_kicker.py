"""通知式衔接的小工具 Kicker:合并、不重叠、异常不中断(2026-09-12)。"""

from __future__ import annotations

import asyncio

from app.kicker import Kicker


def test_kicks_merge_into_one_run():
    """同一批完成 → 多次 kick 只跑一轮(12 个文件同时完成不会踢 12 轮)。"""
    runs: list[str] = []

    async def run_once():
        runs.append("run")

    async def main():
        k = Kicker(run_once, name="测试段")
        for _ in range(12):
            k.kick()
        await asyncio.sleep(0.05)

    asyncio.run(main())
    assert runs == ["run"]


def test_kick_during_run_schedules_exactly_one_more():
    """跑的过程中来的请求:跑完**补一轮**(不丢),多次仍合并成一轮。"""
    runs: list[str] = []

    async def main():
        started = asyncio.Event()
        release = asyncio.Event()

        async def run_once():
            runs.append("run")
            started.set()
            await release.wait()          # 第一轮卡住,好在这期间再 kick

        k = Kicker(run_once)
        k.kick()
        await started.wait()
        k.kick()                          # 跑的时候又来一次
        k.kick()                          # 再来一次(仍只补一轮)
        release.set()
        await asyncio.sleep(0.05)

    asyncio.run(main())
    assert runs == ["run", "run"]


def test_delayed_kick_waits_before_running():
    """可按"还差几秒"延迟触发(处理段撞守门时用,免得干等下一轮轮询)。"""
    n = {"v": 0}

    async def run_once():
        n["v"] += 1

    async def main():
        k = Kicker(run_once)
        k.kick(delay=0.06)
        await asyncio.sleep(0.02)
        assert n["v"] == 0, "还没到点就跑了"
        await asyncio.sleep(0.08)

    asyncio.run(main())
    assert n["v"] == 1


def test_run_exception_does_not_break_later_kicks():
    """一轮抛异常不能把这条触发通道打死(下一段/下一轮照常)。"""
    calls = {"n": 0}

    async def run_once():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("炸了")

    async def main():
        k = Kicker(run_once)
        k.kick()
        await asyncio.sleep(0.05)
        k.kick()
        await asyncio.sleep(0.05)

    asyncio.run(main())
    assert calls["n"] == 2


def test_self_rearm_does_not_hot_loop():
    """回归(2026-09-12 线上事故):一轮**在自己内部**申请延迟重跑时,延迟必须生效。

    处理段撞上 `moving` 守门就会 `kick(5.0)`;第一版丢了延迟 → `while _pending` 立刻再跑
    → 无限热循环(日志 5 分钟 5MB、9p stat 淹死事件循环、心跳停跳、获取段不再结算)。
    """
    calls = {"n": 0}

    async def run_once():
        calls["n"] += 1
        k.kick(delay=0.1)                 # 模拟"文件还没就绪,5 秒后再看"

    async def main():
        k = Kicker(run_once)
        k.kick()
        await asyncio.sleep(0.55)         # 0.55 秒内:正常只该跑 5~6 轮

    asyncio.run(main())
    assert 1 <= calls["n"] <= 8, f"疑似自激热循环:{calls['n']} 轮/0.55 秒"


def test_self_rearm_plus_immediate_kick_merges_into_that_round():
    """延迟等待期间来的请求合并进那一轮(不丢、也不多跑一轮)。"""
    calls = {"n": 0}
    box: dict = {}

    async def run_once():
        calls["n"] += 1
        if calls["n"] == 1:
            box["k"].kick(delay=0.3)      # 排一轮 0.3 秒后的

    async def main():
        box["k"] = Kicker(run_once)
        box["k"].kick()
        await asyncio.sleep(0.05)
        box["k"].kick()                   # 通知来了:合并进已排的那一轮
        await asyncio.sleep(0.4)

    asyncio.run(main())
    assert calls["n"] == 2, f"应合并成 2 轮,实际 {calls['n']} 轮"
    assert not box["k"].busy and not box["k"]._pending, "请求不能被丢掉(还有待跑的轮次)"


def test_burst_warning_fires_on_hot_loop(caplog):
    """兜底告警:真的出现密集触发时要留下告警(只看不拦)。"""
    import logging

    box: dict = {}

    async def run_once():
        box["k"].kick()                   # 无视延迟、立刻再踢 = 自激

    async def main():
        box["k"] = Kicker(run_once, name="测试段")
        box["k"].kick()
        await asyncio.sleep(0.2)

    with caplog.at_level(logging.WARNING, logger="app.kicker"):
        asyncio.run(main())
    assert any("过密" in r.message for r in caplog.records)
