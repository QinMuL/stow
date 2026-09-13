"""系统工具:版本检测。

检测:GitHub Releases API 的最新 tag vs 本地 app.__version__,只读、带代理。
带 github_token(配置项)时认证请求,绕开共享出口 IP 的匿名 API 限流(403)。
升级功能已移除(2026-09-13 用户决定:自重建 / sidecar / Watchtower 方案均不采纳)。
"""

from __future__ import annotations

import logging
import re

from app import __version__

logger = logging.getLogger(__name__)

REPO = "qinmul/stow"
_GH_LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"


def parse_ver(s: str) -> tuple[int, ...]:
    """'v1.2.3' / '1.2.3' / 'v1.2.3-beta' → (1,2,3);解析不出返回空元组。"""
    m = re.search(r"(\d+(?:\.\d+){0,3})", s or "")
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def version_report(latest: str, current: str = __version__) -> dict:
    """对比本地与远端版本。latest 取不到数字时 has_update=False(不误报)。"""
    lv, cv = parse_ver(latest), parse_ver(current)
    return {
        "current": current,
        "latest": latest or "",
        "has_update": bool(lv and cv and lv > cv),
    }


def check_version(cfg) -> dict:
    """查 GitHub 最新 release。网络/限流/解析失败一律返回 error,绝不抛出。"""
    import httpx

    headers = {"Accept": "application/vnd.github+json", "User-Agent": "stow"}
    token = (getattr(cfg, "github_token", "") or "").strip()
    if token:  # 带认证:公开仓库匿名限流(共享出口 IP 常被打满 403)
        headers["Authorization"] = f"Bearer {token}"
    try:
        with httpx.Client(proxy=cfg.proxy_url or None, timeout=8, headers=headers) as c:
            r = c.get(_GH_LATEST_URL)
            if r.status_code == 404:
                return {"error": "仓库无 release(仓库私有或尚未发布)"}
            r.raise_for_status()
            latest = (r.json() or {}).get("tag_name", "")
    except Exception as exc:  # noqa: BLE001 - 检测失败仅提示,不打断使用
        return {"error": f"检测失败:{type(exc).__name__} {str(exc)[:80]}"}
    return version_report(latest)
