"""系统工具:版本检测与一键升级(容器内经 docker socket 自升级)。

- 检测:GitHub Releases API 的最新 tag vs 本地 app.__version__,只读、带代理
- 升级:docker SDK 连 unix socket → pull 最新镜像 → 复制本容器配置重建
  (镜像换成新拉的层),旧进程随之消亡,restart: unless-stopped 的新容器接管
- 安全:挂载 /var/run/docker.sock 后容器即拥有宿主机 docker 权限(等同 root),
  仅推荐自部署单机使用;不挂 socket 时升级接口返回明确错误,不影响其余功能

镜像/容器名与 docker-compose.yml 保持一致(发布与升级共用同一标识)。
"""

from __future__ import annotations

import logging
import re

from app import __version__

logger = logging.getLogger(__name__)

IMAGE = "ghcr.io/qinmul/stow:latest"
REPO = "qinmul/stow"
# 版本口径看 git tags 而非 releases:我们发版只打 tag(如 v1.0.3),从未创建 GitHub
# Release 页,releases/latest 恒 404;tags 接口按 tag 列表取版本号最大者,与镜像 tag 一致
_GH_TAGS_URL = f"https://api.github.com/repos/{REPO}/tags?per_page=10"
_CONTAINER = "stow"


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
    """查 GitHub 最新 tag(列表里版本号最大者)。网络/限流/解析失败一律返回 error,绝不抛出。"""
    import httpx

    try:
        with httpx.Client(
            proxy=cfg.proxy_url or None, timeout=8,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "stow"},
        ) as c:
            r = c.get(_GH_TAGS_URL)
            if r.status_code == 404:
                return {"error": "仓库不存在或无权访问(私有?)"}
            r.raise_for_status()
            names = [(t or {}).get("name", "") for t in (r.json() or [])]
            latest = max(
                ((parse_ver(n), n) for n in names if parse_ver(n)),
                default=((), ""),
            )[1]
    except Exception as exc:  # noqa: BLE001 - 检测失败仅提示,不打断使用
        return {"error": f"检测失败:{type(exc).__name__} {str(exc)[:80]}"}
    return version_report(latest)


def upgrade(cfg) -> tuple[bool, str]:
    """执行升级:拉最新镜像 → 重建本容器(复制原容器配置)。返回 (成功, 消息)。"""
    try:
        import docker
    except ImportError:
        return False, "缺少 docker SDK——容器未安装/未挂载 docker socket 时不可用"
    try:
        client = docker.from_env()
    except Exception as exc:  # noqa: BLE001
        return False, f"连接 docker daemon 失败(未挂载 socket?):{str(exc)[:80]}"
    try:
        logger.info("升级:拉取镜像 %s", IMAGE)
        client.images.pull(IMAGE)
    except Exception as exc:  # noqa: BLE001
        return False, f"拉取镜像失败:{str(exc)[:120]}"
    try:
        container = client.containers.get(_CONTAINER)
    except Exception as exc:  # noqa: BLE001
        return False, f"找不到容器 {_CONTAINER}({str(exc)[:80]})"
    try:
        _recreate(client, container)
    except Exception as exc:  # noqa: BLE001
        return False, f"重建容器失败:{str(exc)[:120]}"
    return True, "升级完成,已用新镜像重建容器"


def _recreate(client, container) -> None:
    """按原容器配置重建(镜像换成已拉取的新版本)。

    从 inspect attrs 复制关键字段:env/cmd/entrypoint/working_dir/labels +
    host 侧(binds/network_mode/restart_policy/init/privileged)。
    重建必然杀掉当前进程,调用方需先回响应、再延时执行。
    """
    attrs = container.attrs
    cfg = attrs.get("Config") or {}
    hc = attrs.get("HostConfig") or {}
    rp = hc.get("RestartPolicy") or {}
    restart = {"Name": rp.get("Name") or "no"}
    if restart["Name"] == "on-failure":
        restart["MaximumRetryCount"] = rp.get("MaximumRetryCount") or 0
    host_config = client.api.create_host_config(
        binds=hc.get("Binds"),
        network_mode=hc.get("NetworkMode") or "default",
        restart_policy=restart,
        init=hc.get("Init"),
        privileged=hc.get("Privileged"),
        extra_hosts=hc.get("ExtraHosts"),
        dns=hc.get("Dns"),
    )
    new = client.containers.create(
        IMAGE,
        name=container.name,
        command=cfg.get("Cmd"),
        entrypoint=cfg.get("Entrypoint"),
        working_dir=cfg.get("WorkingDir"),
        env=cfg.get("Env"),
        labels=cfg.get("Labels"),
        host_config=host_config,
    )
    container.stop()
    container.remove()
    new.start()
    logger.info("升级:容器 %s 已用新镜像重建", container.name)
