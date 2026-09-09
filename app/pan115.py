"""115 分享读取:链接解析 + 匿名接口读清单 + 风控/失效分类。

语义参考旧项目踩坑结论(实现全新):
- margin 响应 {"margin": N}:等待 min(N,30)s 重试
- errno 4100009/4100010 分享失效;4100012 需访问码;4100008 访问码被改
- "正在生成文件快照":渐进重试
- 读取分享匿名即可,无需 cookie
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from functools import partial

logger = logging.getLogger(__name__)

# ── 链接解析 ────────────────────────────────────────────────
_115_URL_RE = re.compile(
    r"115(?:cdn)?\.com/s/([A-Za-z0-9_-]+)(?:\?(?P<query>[^ \n]*))?", re.IGNORECASE
)
_PWD_RE = re.compile(r"password=([A-Za-z0-9_-]+)", re.IGNORECASE)
_TAIL_TOKEN_RE = re.compile(r"^[A-Za-z0-9]{4,12}$")
_BODY_CODE_RE = re.compile(r"(?:访问码|提取码|密码)\s*[：:=＝]\s*([A-Za-z0-9]{4,12})")
_BARE_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{8,}$")


@dataclass
class ShareLink:
    code: str
    password: str | None = None


def parse_links(text: str) -> list[ShareLink]:
    """提取文本中全部 115 分享(URL 形态);链接缺访问码时从正文提取。"""
    found: list[ShareLink] = []
    seen: set[str] = set()
    for m in _115_URL_RE.finditer(text):
        code, query = m.group(1), m.group("query") or ""
        if code in seen:
            continue
        seen.add(code)
        pwd = None
        pm = _PWD_RE.search(query)
        if pm:
            pwd = pm.group(1)
        elif query and _TAIL_TOKEN_RE.fullmatch(query):
            pwd = query
        found.append(ShareLink(code, pwd))
    if any(lk.password is None for lk in found):
        bm = _BODY_CODE_RE.search(text)
        if bm:
            for lk in found:
                if lk.password is None:
                    lk.password = bm.group(1)
    return found


def parse_single(text: str) -> ShareLink | None:
    """单链接场景:URL 优先,其次 8+ 位裸码。"""
    links = parse_links(text)
    if links:
        return links[0]
    token = text.strip()
    if _BARE_CODE_RE.fullmatch(token):
        m = _BODY_CODE_RE.search(text)
        return ShareLink(token, m.group(1) if m else None)
    return None


# ── 错误分类 ────────────────────────────────────────────────
class ShareError(Exception):
    """分享读取失败(基类)。"""


class ShareDead(ShareError):
    """分享已失效/取消/不存在。"""


class ShareNeedCode(ShareError):
    """缺访问码或访问码错误。"""

    def __init__(self, code_changed: bool = False):
        self.code_changed = code_changed
        super().__init__("访问码错误(分享者已改码)" if code_changed else "需要访问码")


class ShareRateLimited(ShareError):
    """115 风控限速,重试后仍失败。"""


class ShareSnapshotting(ShareError):
    """分享快照生成中。"""


# ── 读取 ────────────────────────────────────────────────────
_MARGIN_CAP = 30.0
_MAX_RETRY = 3
_MIN_INTERVAL = 1.0  # 两次 115 请求最小间隔(防风控)
_HTTP_RETRY_WAIT = 5.0  # HTTP 层错误(405 风控等)重试基础间隔


@dataclass
class ShareFile:
    name: str
    size: int
    is_dir: bool

    @property
    def is_video(self) -> bool:
        return not self.is_dir and self.name.rsplit(".", 1)[-1].lower() in {
            "mkv", "mp4", "avi", "wmv", "mov", "flv", "ts", "m2ts", "webm", "rmvb",
        }


def fmt_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.2f}{unit}" if unit not in ("B",) else f"{int(size)}B"
        size /= 1024
    return f"{size:.2f}TB"


def share_url(link: ShareLink) -> str:
    url = f"https://115.com/s/{link.code}"
    return f"{url}?password={link.password}" if link.password else url


class Pan115Reader:
    """读 115 分享(双通道;同步调用 to_thread 包裹)。

    - 有 cookie:android/proapi 通道(share_snap_app)。115 已按请求指纹封锁匿名
      webapi share_snap(同参数 urllib 200/库请求 405,2026-08 旧项目实测),
      proapi 不受影响;代价:读他人分享每目录最多 1000 条(结构化分享不受影响)。
    - 无 cookie:匿名 web 通道兜底(易触发 405/margin 风控)。
    """

    def __init__(self, cookie: str = "") -> None:
        self._cookie = (cookie or "").strip()
        self._client = None
        self._last_request = 0.0

    @property
    def logged_in(self) -> bool:
        return bool(self._cookie)

    def _get_client(self):
        if self._client is None:
            from p115client import P115Client

            self._client = (
                P115Client(self._cookie, app="android")
                if self._cookie
                else P115Client("", app="web")
            )
        return self._client

    async def _call(self, fn, *args, **kwargs):
        """限速 + to_thread 执行同步 p115client 调用。"""
        gap = time.monotonic() - self._last_request
        if gap < _MIN_INTERVAL:
            await asyncio.sleep(_MIN_INTERVAL - gap)
        self._last_request = time.monotonic()
        return await asyncio.to_thread(fn, *args, **kwargs)

    # -- 原始快照(带 margin/快照/HTTP 层重试) --------------------------
    async def _snap(self, code: str, password: str) -> dict | None:
        """取分享快照做校验(死链/访问码/状态)。

        返回 None = share_snap 端点不可用(HTTP 层失败,如 405 风控),
        调用方降级为直接枚举,校验交给枚举结果兜底。
        """
        from p115client.util import share_extract_payload

        payload = dict(share_extract_payload(code))
        payload["receive_code"] = password or payload.get("receive_code") or ""
        client = self._get_client()
        snap_fn = (
            partial(client.share_snap_app, app="android")
            if self._cookie
            else client.share_snap
        )
        for attempt in range(1, _MAX_RETRY + 1):
            try:
                resp = await self._call(snap_fn, payload, async_=False)
            except Exception as exc:  # noqa: BLE001 - HTTP 层错误归一重试
                if attempt == _MAX_RETRY:
                    logger.warning("share_snap 不可用(%s),降级直接枚举", str(exc)[:80])
                    return None
                logger.warning("115 接口异常(%s),重试 %d/%d", exc, attempt, _MAX_RETRY)
                await asyncio.sleep(_HTTP_RETRY_WAIT * attempt)
                continue
            # margin 限流:{"margin": N} 且无 state
            if "state" not in resp and "margin" in resp:
                wait = min(float(resp.get("margin") or 5), _MARGIN_CAP)
                logger.warning("115 限速,等待 %.0fs 重试(%d/%d)", wait, attempt, _MAX_RETRY)
                await asyncio.sleep(wait)
                continue
            if "正在生成文件快照" in str(resp):
                await asyncio.sleep(3 * attempt)
                continue
            return resp
        raise ShareRateLimited("115 限速或快照生成中,多次重试后仍失败")

    async def read_share(self, link: ShareLink) -> list[ShareFile]:
        """读取分享全部文件(递归扁平化)。失败抛分类异常。"""
        resp = await self._snap(link.code, link.password or "")
        if resp is not None:
            errno = resp.get("errno") or resp.get("errNo")
            if resp.get("state") is False:
                if errno in (4100008, 4100012):
                    raise ShareNeedCode(code_changed=(errno == 4100008))
                msg = str(resp.get("error") or resp)
                if "正在生成文件快照" in msg:
                    raise ShareSnapshotting("分享正在生成文件快照,稍后重试")
                if errno in (4100009, 4100010) or "失效" in msg or "取消" in msg:
                    raise ShareDead("分享已失效或被取消")
                raise ShareError(f"分享读取失败:{msg[:120]}")

            data = resp.get("data") or {}
            if data.get("share_state") == 7:
                raise ShareDead("分享已失效")

        from p115client.tool import share_iterdir_walk

        client = self._get_client()

        def _walk_files() -> list[ShareFile]:
            """share_iterdir_walk 为 os.walk 风格:yield (目录路径, 子目录列表, 文件字典列表)。

            cooldown 限速分页请求(默认无限速,千集分享海量请求易触发 115 IP 风控);
            子目录也收集(Season N 目录是季号聚合的来源)。            """
            out: list[ShareFile] = []
            for entry in share_iterdir_walk(
                client, link.code, link.password or "",
                app="android" if self._cookie else "web",
                cooldown=1.0,
                onerror=True,  # 默认 False 会静默跳过列表失败的目录 → 漏季/漏文件
            ):
                if isinstance(entry, tuple):
                    dirnames = list(entry[1]) if len(entry) >= 2 else []
                    file_dicts = entry[2] if len(entry) >= 3 else []
                elif isinstance(entry, dict):
                    dirnames, file_dicts = [], [entry]
                else:
                    continue
                for d in dirnames:
                    name = d if isinstance(d, str) else str(getattr(d, "name", "") or "")
                    if name:
                        out.append(ShareFile(name, 0, True))
                for item in file_dicts or []:
                    name = str(item.get("n") or item.get("name") or "")
                    if not name:
                        continue
                    size = int(item.get("s") or item.get("size") or 0)
                    out.append(ShareFile(name, size, False))
            return out

        try:
            # 大分享分页多(cooldown 0.5s/页),超时按分享规模放宽
            files = await asyncio.wait_for(asyncio.to_thread(_walk_files), timeout=180)
        except TimeoutError as exc:
            raise ShareError("读取分享超时(180s),稍后重试") from exc
        except KeyError as exc:
            # 读取途中被 margin 限速:响应无 data,check_response 放行后取值炸
            if str(exc) == "'data'":
                raise ShareRateLimited("115 限速,读取中断,请稍后重试") from exc
            raise ShareError(f"读取分享列表失败:{exc!r}") from exc
        except Exception as exc:  # noqa: BLE001 - p115client 各种异常归一
            msg = str(exc)
            if "4100008" in msg or "4100012" in msg or "访问码" in msg:
                raise ShareNeedCode() from exc
            if "4100009" in msg or "4100010" in msg or "失效" in msg:
                raise ShareDead("分享已失效") from exc
            raise ShareError(f"读取分享列表失败:{msg[:120]}") from exc
        if not files:
            raise ShareError("分享内容为空(可能仍在生成快照,或 115 风控限流,稍后重试)")
        return files
