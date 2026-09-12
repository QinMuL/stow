"""把"尽快跑一轮"的多次请求合并成一次的小工具(三段链通知式衔接用,2026-09-12 用户确认)。

设计取向:
- **事件驱动只做延迟优化,轮询继续当兜底** —— 漏一次通知不会让文件卡死,只是退回"等下一轮"。
- **合并**:一批文件同时完成 → 只跑一轮;正在跑的时候来的请求 → 跑完**补一轮**(不丢)。
- **可延迟**:处理段撞上守门(文件还没到静默年龄)时,按"还差几秒"排一次延迟触发,
  而不是干等下一轮 5 分钟。

同一时刻只会有一轮在跑 —— 这也是处理段原先缺的那道并发保护
(手动 `/process` + 定时轮 + 通知三条路本来可能同时进来)。

⚠️ **2026-09-12 线上事故(自激热循环)**,两道防线都是这次事故换来的,别删:

① 延迟必须记在"待跑的那一轮"上。第一版把 `delay` 只当**创建任务时**的参数,而"一轮扫描在
   自己内部申请延迟重跑"是常态(处理段撞上 `moving` 守门就会 `kick(5.0)`)——那次请求被记成
   `_pending=True` 但延迟丢了,`while self._pending` 立刻又跑一轮、再 re-arm、再立刻跑……
   表现为:日志 5 分钟写满 5MB(每 20ms 两条)、每轮 `path.stat()` 都走 9p 到 Windows(单次
   ~38ms),**事件循环被同步 I/O 淹死** → 心跳停跳(页面报"bot 心跳异常")、获取段结算停摆
   (文件传完不落账)、进度定格。
② 每轮开跑前必须 `await asyncio.sleep(0)` 让出控制权。若某一轮**同步**再 `kick(0)`,
   不让出的 `while` 会连成一条死循环,**整个事件循环彻底饿死**(连心跳、HTTP 都停)——
   比①更致命。加上让出后最坏也只是"忙但活着"。
   另配刹车:密集触发时告警并强制降速,把自激的伤害限成"慢轮询"。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

logger = logging.getLogger(__name__)

_BURST_WINDOW = 60.0       # 观察窗口(秒)
_BURST_LIMIT = 60          # 窗口内超过这么多轮 = 平均不到 1 秒一轮 → 疑似自激
_BRAKE_SECONDS = 1.0       # 疑似自激时强制降速到每轮间隔(兜底,正常链路远达不到这个密度)


class Kicker:
    """合并"尽快跑一轮"的请求;同一时刻只会有一轮在跑。"""

    def __init__(self, run_once, *, name: str = "") -> None:
        self._run_once = run_once          # async callable,无参
        self._name = name or "该段"
        self._task: asyncio.Task | None = None
        self._pending = False
        self._delay = 0.0                  # 待跑那一轮要求的延迟(多次请求取最小)
        self._rounds: deque[float] = deque()   # 近期开跑时间(自激告警用)
        self._warned_at = 0.0
        self._brake_until = 0.0

    def kick(self, delay: float = 0.0) -> None:
        """请求跑一轮;`delay` 秒后触发(默认立刻)。多次请求合并,延迟取最小。"""
        delay = max(float(delay or 0.0), 0.0)
        self._delay = min(self._delay, delay) if self._pending else delay
        self._pending = True
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        while self._pending:
            delay, self._delay = self._delay, 0.0
            if delay:
                await asyncio.sleep(delay)     # 等待期间来的请求合并进这一轮(_pending 仍为真)
            else:
                await asyncio.sleep(0)         # 防线②:每轮必须让出控制权
            brake = self._brake_until - time.monotonic()
            if brake > 0:
                await asyncio.sleep(brake)     # 防线③:疑似自激,强制降速
            self._pending = False              # 到这一轮真正开跑,才算把请求都接下了
            self._note_round()
            try:
                await self._run_once()
            except Exception as exc:  # noqa: BLE001 - 单轮异常不影响后续触发
                logger.warning("%s通知式触发的那一轮异常:%s", self._name, exc, exc_info=exc)

    def _note_round(self) -> None:
        now = time.monotonic()
        self._rounds.append(now)
        while self._rounds and now - self._rounds[0] > _BURST_WINDOW:
            self._rounds.popleft()
        if len(self._rounds) <= _BURST_LIMIT:
            return
        self._brake_until = now + _BRAKE_SECONDS
        if now - self._warned_at > _BURST_WINDOW:
            self._warned_at = now
            logger.warning("%s通知式触发过密(%.0f 秒内 %d 轮),疑似自激循环:降速到 %.1f 秒一轮,"
                           "请检查延迟合并", self._name, _BURST_WINDOW, len(self._rounds),
                           _BRAKE_SECONDS)

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()
