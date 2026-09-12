"""把"尽快跑一轮"的多次请求合并成一次的小工具(三段链通知式衔接用,2026-09-12 用户确认)。

设计取向:
- **事件驱动只做延迟优化,轮询继续当兜底** —— 漏一次通知不会让文件卡死,只是退回"等下一轮"。
- **合并**:一批文件同时完成 → 只跑一轮;正在跑的时候来的请求 → 跑完**补一轮**(不丢)。
- **可延迟**:处理段撞上守门(文件还没到静默年龄)时,按"还差几秒"排一次延迟触发,
  而不是干等下一轮 5 分钟。

同一时刻只会有一轮在跑 —— 这也是处理段原先缺的那道并发保护
(手动 `/process` + 定时轮 + 通知三条路本来可能同时进来)。
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class Kicker:
    """合并"尽快跑一轮"的请求;同一时刻只会有一轮在跑。"""

    def __init__(self, run_once, *, name: str = "") -> None:
        self._run_once = run_once          # async callable,无参
        self._name = name or "该段"
        self._task: asyncio.Task | None = None
        self._pending = False

    def kick(self, delay: float = 0.0) -> None:
        """请求尽快跑一轮;`delay` 秒后触发(默认立刻)。"""

        self._pending = True
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._drain(max(delay, 0.0)))

    async def _drain(self, delay: float) -> None:
        if delay:
            await asyncio.sleep(delay)
        while self._pending:
            self._pending = False
            try:
                await self._run_once()
            except Exception as exc:  # noqa: BLE001 - 单轮异常不影响后续触发
                logger.warning("%s通知式触发的那一轮异常:%s", self._name, exc, exc_info=exc)

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()
