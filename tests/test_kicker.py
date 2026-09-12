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
