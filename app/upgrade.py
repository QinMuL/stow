"""系统工具:版本检测与一键升级。

方案(2026-09-13 重做,Watchtower 触发式):
- 检测:GitHub Releases API 的最新 tag vs 本地 app.__version__,只读、带代理
- 升级:POST 到 Watchtower 的 HTTP API(/v1/update),由常驻的 Watchtower 容器
  (compose 里的 stow-watchtower)拉取新镜像并原配置重建 stow。
  主容器自己不碰 docker.sock —— 没有宿主机 root 权限,也没有"自杀式重建"
  (容器无法销毁替换自己)或一次性 sidecar 的复杂度,更新流程交给专职工具。
- 安全:docker 权限只在 watchtower 容器;它开 --label-enable,只更新打了
  com.centurylinklabs.watchtower.enable 标签的 stow,不碰宿主机其它容器
- 完成感知:Watchtower 的 API 立即返回(更新在后台跑);前端轮询版本接口,
  重建期间请求中断、恢复后比对版本号即知道升没升上去
"""

from __future__ import annotations

import logging
import re

from app import __version__

logger = logging.getLogger(__name__)

REPO = "qinmul/stow"
_GH_LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
# Watchtower(compose 里的 stow-watchtower):地址与令牌可用配置覆盖,
# 默认与 compose 的 WATCHTOWER_TOKEN 环境变量对齐
_DEFAULT_WT_URL = "http://127.0.0.1:8080"
_DEFAULT_WT_TOKEN = "stow-upgrade"


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


def upgrade(cfg) -> tuple[bool, str]:
    """触发 Watchtower 更新本容器。返回 (成功, 消息)。

    Watchtower 收到 /v1/update 后在后台:拉取新镜像 → 停旧容器 → 原配置重建
    → 启动;API 本身立即返回。配置解析顺序:watchtower_url/watchtower_token
    → 环境变量 WATCHTOWER_TOKEN(与 compose 对齐)→ 内置默认。
    """
    import os

    import httpx

    url = (getattr(cfg, "watchtower_url", "") or _DEFAULT_WT_URL).rstrip("/")
    token = (getattr(cfg, "watchtower_token", "") or os.environ.get("WATCHTOWER_TOKEN", "")
             or _DEFAULT_WT_TOKEN)
    try:
        with httpx.Client(timeout=15) as c:
            r = c.post(f"{url}/v1/update", headers={"Authorization": f"Bearer {token}"})
    except httpx.ConnectError:
        return False, "连不上 Watchtower:确认 compose 里的 stow-watchtower 已部署并启动"
    except Exception as exc:  # noqa: BLE001
        return False, f"触发失败:{type(exc).__name__} {str(exc)[:80]}"
    if r.status_code in (401, 403):
        return False, "Watchtower 拒绝:令牌不匹配(检查 WATCHTOWER_TOKEN 两边是否一致)"
    if r.status_code not in (200, 204):
        return False, f"Watchtower 返回 HTTP {r.status_code}:{str(r.text)[:60]}"
    return True, "升级已触发:Watchtower 正在后台拉取镜像并重建容器"
