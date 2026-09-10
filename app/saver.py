"""115 转存器:把他人分享内容转存到自己网盘(cookie 登录态,android/proapi 通道)。

链路语义参考 P115-Share 1.1.7(多方比对后的取舍):
- share_snap 拿根目录条目 fid/cid → fs_makedirs 确保目录 → 任务独立子目录 →
  share_receive_app 接收
- 子目录递归 + 单批 ≤450 分批(绕非会员单次 500 文件限制 errno 4200044)
- errno 4200045 = 已接收过 → 视为成功(提示)
- 失败分类返回,由调用方决定提示;不吞异常栈(日志记全)
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field

from p115client.client import check_response
from p115client.tool import share_iterdir_walk
from p115client.util import share_extract_payload

logger = logging.getLogger(__name__)

# 非会员单次接收条数上限(官方 500,留余量)
_BATCH_SIZE = 450
_ERR_ALREADY = 4200045


@dataclass
class SaveResult:
    ok: bool
    message: str
    task_cid: int = 0       # 资源根(单文件夹)或资源文件 CID,流水线定位用
    task_name: str = ""     # 资源名
    is_dir: bool = True     # 资源是否目录(单文件 False → 标准化走散文件包裹)
    saved_items: int = 0    # 接收的条目数
    already: bool = False   # 内容本就已在网盘中
    errors: list[str] = field(default_factory=list)


class Pan115Saver:
    """复用 Pan115Reader 的 cookie 客户端做转存;必须在 reader.logged_in 时使用。"""

    def __init__(self, reader) -> None:
        self._reader = reader

    @property
    def client(self):
        return self._reader._get_client()

    async def _call(self, fn, *args, **kwargs):
        return await self._reader._call(fn, *args, **kwargs)

    async def _share_title(self, share_code: str, receive_code: str) -> str:
        """取分享标题(资源目录命名用);失败返回空。"""
        try:
            st = await self._reader.share_status(share_code, receive_code)
            return st.get("title") or ""
        except Exception:  # noqa: BLE001
            return ""

    # ── 目录 ───────────────────────────────────────────────
    async def ensure_dir(self, path: str, parent_cid: int = 0) -> int:
        """确保目录存在(逐级下钻,fs_makedirs_app 幂等),返回 CID。"""
        parts = [p for p in re.split(r"[\\/]+", path or "") if p and p not in (".", "/")]
        cid = parent_cid
        for name in parts:
            resp = await self._call(
                self.client.fs_makedirs_app, name, pid=cid, async_=False
            )
            check_response(resp)
            cid = int(resp.get("cid") or resp.get("id") or (resp.get("data") or {}).get("cid") or 0)
            if not cid:
                raise RuntimeError(f"创建目录 {name} 未返回有效 CID: {resp}")
        return cid

    # ── 接收 ───────────────────────────────────────────────
    async def _receive(self, payload: dict) -> dict:
        """单次接收调用;errno 4200045(已接收)视为成功。"""
        resp = await self._call(self.client.share_receive_app, payload, async_=False)
        state = resp.get("state")
        errno = resp.get("errno") or resp.get("errNo")
        if state is False and errno in (_ERR_ALREADY,):
            return {"already": True}
        check_response(resp)
        return {"already": False}

    async def _receive_batched(self, share_code: str, receive_code: str,
                               ids: list[str], cid: int) -> tuple[int, bool]:
        """分批接收一批条目到目标目录;返回 (已接收批次数, 是否含已接收)。"""
        already = False
        for i in range(0, len(ids), _BATCH_SIZE):
            batch = ids[i:i + _BATCH_SIZE]
            payload = {
                "share_code": share_code,
                "receive_code": receive_code,
                "file_id": ",".join(batch),
                "cid": cid,
            }
            r = await self._receive(payload)
            already = already or r["already"]
            await asyncio.sleep(1.0)  # 接收指令间隔,防风控
        return len(ids), already

    async def save_share(self, link, parent_cid: int) -> SaveResult:
        """转存一个 115 分享到 parent_cid,**内容原样落盘**(无包装目录)。

        - 分享根只有一个条目(文件夹/文件)→ 原样落在 parent 下,零包装
        - 分享根有多个条目 → 收进一个资源目录(分享标题命名)——多个散项
          需要一个资源根才能标准化与建分享
        返回 SaveResult.task_cid(资源根/文件的 CID)+ is_dir 供流水线定位。
        """
        if not self._reader.logged_in:
            return SaveResult(False, "未配置 115 Cookie,无法转存(匿名接口只能读,不能转存)")
        t0 = time.monotonic()
        payload = dict(share_extract_payload(link.code))
        receive_code = link.password or payload.get("receive_code") or ""
        share_code = payload["share_code"]

        try:
            total = 0
            any_already = False
            errors: list[str] = []

            # 遍历分享树:目录建网盘侧对应结构(不接收——接收目录=整树复制会双份),
            # 文件按层分批接收;topdown 父先于子
            cid_map = {0: parent_cid}
            root_dirs = root_files = None

            def _walk_sync():
                out = []
                for pid, dirs, files in share_iterdir_walk(
                    self.client, share_code, receive_code,
                    app="android", cooldown=1.0, onerror=True,
                ):
                    out.append((pid, dirs, files))
                return out

            tree = await asyncio.wait_for(asyncio.to_thread(_walk_sync), timeout=600)
            if tree:
                root_dirs, root_files = tree[0][1], tree[0][2]

            for pid, dirs, files in tree:
                if pid not in cid_map:
                    errors.append(f"目录映射缺失 pid={pid},跳过 {len(dirs) + len(files)} 项")
                    continue
                dest = cid_map[pid]
                for d in dirs:
                    sub = await self.ensure_dir(d["name"], parent_cid=dest)
                    cid_map[d["id"]] = sub
                ids = [str(f["id"]) for f in files]
                if not ids:
                    continue
                n, already = await self._receive_batched(share_code, receive_code, ids, dest)
                total += n
                any_already = any_already or already

            # 资源根定位:单根条目原样;多根条目收进资源目录(分享标题命名)
            n_root = (len(root_dirs or []) + len(root_files or [])) if tree else 0
            is_dir = bool(root_dirs)
            if n_root > 1:
                title = await self._share_title(share_code, receive_code)
                clean = re.sub(r'[\\/:*?"<>|]', "", title or "").strip()[:40]
                task_name = f"{clean}_{time.strftime('%Y%m%d_%H%M%S')}" if clean else \
                    f"未整理_{time.strftime('%Y%m%d_%H%M%S')}"
                root_cid = await self.ensure_dir(task_name, parent_cid=parent_cid)
                moved = 0
                for it in await self._reader.list_dir(parent_cid, nf=0):
                    if it["name"] in {d["name"] for d in (root_dirs or [])} | \
                       {f["name"] for f in (root_files or [])}:
                        await self._reader.fs_move(it["fid"], root_cid)
                        await asyncio.sleep(1)
                        moved += 1
                task_cid, is_dir = root_cid, True
                logger.info("多根条目收拢:%d/%d 项 → %s", moved, n_root, task_name)
            elif is_dir:
                # 单文件夹:名字匹配拿 fid(接收时 115 保留原名)
                want = root_dirs[0]["name"]
                items = await self._reader.list_dir(parent_cid, nf=1)
                match = next((it for it in items if it["name"] == want), None)
                if match is None:
                    raise RuntimeError(f"转存后未找到资源目录 {want}")
                task_cid, task_name = match["fid"], match["name"]
            else:
                # 单文件
                want = root_files[0]["name"]
                items = await self._reader.list_dir(parent_cid, nf=0)
                match = next((it for it in items if it["name"] == want), None)
                if match is None:
                    raise RuntimeError(f"转存后未找到文件 {want}")
                task_cid, task_name, is_dir = match["fid"], match["name"], False

            msg = (
                "已在网盘中(重复转存)"
                if any_already and not total
                else f"已转存 {total} 个条目 → {task_name}"
            )
            logger.info("转存完成:%s(耗时 %.1fs)", msg, time.monotonic() - t0)
            return SaveResult(True, msg, task_cid=task_cid, task_name=task_name,
                              is_dir=is_dir, saved_items=total,
                              already=any_already and not total, errors=errors)
        except TimeoutError:
            logger.error("转存超时(遍历分享树 600s):%s", link.code)
            return SaveResult(False, "转存超时(分享过大或接口缓慢),稍后重试")
        except Exception as exc:  # noqa: BLE001 - 统一分类回报
            logger.error("转存失败 %s: %s", link.code, exc, exc_info=exc)
            text = str(exc)
            if "4200044" in text:
                return SaveResult(False, "触发 115 接收数量限制,转存中止")
            if "99" in text or "请重新登录" in text:
                return SaveResult(False, "115 登录态失效,请更新 Cookie 后重试")
            return SaveResult(False, f"转存失败:{text[:100]}")
