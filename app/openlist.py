"""OpenList 客户端(获取段):列目录 / 移动 / 任务状态。

实测要点(2026-09-11,OpenList 4.2.6,详见 HANDOFF):
- 认证:请求头 `Authorization: <token>`(用户 API 令牌)
- 响应包裹 `{code, message, data}`,code != 200 即失败(401=令牌无效/游客禁用)
- **move 任务有自己的类型**:`/api/task/move/undone|done`(copy 任务在 `/api/task/copy/*`,别混查)
- 跨存储 move → 异步流式任务,返回 `data.tasks[{id,name,state,progress}]`;同存储 move 立即完成
- 任务 state:1=运行中,2=成功(实测);失败为更高值,附 error 文本
- **完成判定就看任务**:出现在 `move/done` 且 state==2(别再自造"文件是否稳定"的启发式)
- 探测端点务必区分 JSON 与前端兜底 HTML——非 JSON 一律视为"端点不可用",不要静默当空列表
"""

from __future__ import annotations

import logging
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

TASK_RUNNING = 1
TASK_SUCCEEDED = 2


class OpenListError(Exception):
    """OpenList 调用失败(网络/鉴权/业务码)。"""


class OpenListClient:
    def __init__(self, base_url: str, token: str, timeout: float = 30.0) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.token = token or ""
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    # ── 基础设施 ────────────────────────────────────────────
    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={"Authorization": self.token},
                timeout=self.timeout,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _call(self, method: str, path: str, payload: dict | None = None) -> object:
        """统一调用与错误归类;返回 data 字段。"""
        if not self.base_url or not self.token:
            raise OpenListError("未配置 openlist 地址或令牌")
        client = await self._http()
        try:
            if method == "GET":
                resp = await client.get(path, params=payload)
            else:
                resp = await client.post(path, json=payload)
        except Exception as exc:  # noqa: BLE001 - 网络层
            raise OpenListError(f"请求 {path} 失败:{exc}") from exc

        ctype = resp.headers.get("content-type", "")
        if "application/json" not in ctype:
            # 非 JSON = 前端兜底页,通常是端点不存在(实测过的坑)
            raise OpenListError(f"{path} 非 API 响应(HTTP {resp.status_code}),端点可能不存在")
        body = resp.json()
        code = body.get("code")
        if code != 200:
            msg = str(body.get("message") or body.get("error") or "")[:120]
            if code == 401:
                raise OpenListError(f"openlist 鉴权失败(401):{msg or '令牌无效或已过期'}")
            raise OpenListError(f"{path} 返回 code={code}:{msg}")
        return body.get("data")

    # ── 文件浏览 ────────────────────────────────────────────
    async def list_dir(self, path: str, per_page: int = 200) -> list[dict]:
        """列目录(不递归);返回 [{name,size,is_dir,modified}...]。"""
        data = await self._call("POST", "/api/fs/list",
                                {"path": path, "page": 1, "per_page": per_page, "refresh": False})
        return (data or {}).get("content") or []

    async def stat(self, path: str) -> dict | None:
        """取单个条目的元信息(用父目录列表实现,避免额外端点)。"""
        parent, _, name = path.rstrip("/").rpartition("/")
        for it in await self.list_dir(parent or "/"):
            if it.get("name") == name:
                return it
        return None

    # ── 移动与任务 ──────────────────────────────────────────
    async def move(self, src_dir: str, dst_dir: str, names: list[str]) -> list[dict]:
        """移动条目(跨存储=异步流式任务,同存储=立即完成);返回任务列表(可能为空)。"""
        data = await self._call("POST", "/api/fs/move",
                                {"src_dir": src_dir, "dst_dir": dst_dir, "names": names})
        if isinstance(data, dict):
            return data.get("tasks") or []
        return []

    async def move_tasks(self, *, undone: bool) -> list[dict]:
        """move 类型的任务列表:undone=进行中,done=已结束(含成功与失败)。"""
        data = await self._call("GET", f"/api/task/move/{'undone' if undone else 'done'}")
        return data if isinstance(data, list) else []

    async def clear_done(self) -> None:
        try:
            await self._call("POST", "/api/task/move/clear_done", {})
        except OpenListError as exc:  # 清理失败不影响主流程
            logger.debug("清空已完成任务失败:%s", exc)

    # ── 上传/上传辅助(目前仅 e2e 自造测试文件用) ───────────
    async def put(self, path: str, content: bytes) -> None:
        """上传文件:PUT /api/fs/put,File-Path 头**必须 URL 编码**(中文直发会被拒)。"""
        client = await self._http()
        resp = await client.put("/api/fs/put", headers={"File-Path": quote(path)}, content=content)
        body = resp.json() if "application/json" in resp.headers.get("content-type", "") else {}
        if body.get("code") != 200:
            raise OpenListError(f"上传 {path} 失败:{str(body.get('message'))[:100]}")

    async def remove(self, dir_path: str, names: list[str]) -> None:
        await self._call("POST", "/api/fs/remove", {"dir": dir_path, "names": names})

    async def mkdir(self, path: str) -> None:
        await self._call("POST", "/api/fs/mkdir", {"path": path})

    # ── 设置(用于同步任务线程数,与我们的并发上限一致) ────────
    async def settings(self) -> dict[str, str]:
        """读 openlist 设置列表 → {key: value}。"""
        data = await self._call("GET", "/api/admin/setting/list")
        if not isinstance(data, list):
            return {}
        return {str(it.get("key")): str(it.get("value")) for it in data if it.get("key")}

    async def save_setting(self, key: str, value: str) -> None:
        """写单项设置(payload 必须是**数组**,实测单对象会 400)。"""
        await self._call("POST", "/api/admin/setting/save",
                         [{"key": key, "value": str(value)}])
