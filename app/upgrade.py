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
_GH_LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
_CONTAINER = "stow"
_STATE_FILE = "upgrade_state.json"   # 放 data/ 下;升级结果落盘,前端轮询有反馈


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


def upgrade(cfg, data_dir: str = "") -> tuple[bool, str]:
    """执行升级:拉最新镜像 → 重建本容器(复制原容器配置)。返回 (成功, 消息)。

    data_dir 非空时结果落盘到 {data_dir}/{_STATE_FILE}:重建会杀掉本进程,
    只有落盘的状态能在新进程里被读回,前端据此轮询升级成败(2026-09-13 加)。
    """
    ok, msg = _upgrade_impl(cfg)
    _write_state(data_dir, ok, msg)
    return ok, msg


def _upgrade_impl(cfg) -> tuple[bool, str]:
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


def _write_state(data_dir: str, ok: bool, msg: str) -> None:
    """升级结果落盘(data/upgrade_state.json):时间/成败/消息/目标版本。"""
    import json as _json
    import time as _time
    from pathlib import Path

    if not data_dir:
        return
    try:
        p = Path(data_dir) / _STATE_FILE
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        p.write_text(
            _json.dumps({
                "ts": _time.time(), "ok": bool(ok), "message": msg,
                "to_version": __version__,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001 - 状态写失败不影响升级结果本身
        logger.warning("升级状态落盘失败:%s", exc)


def read_upgrade_state(data_dir: str = "") -> dict:
    """读最近一次升级结果;无记录返回空 dict。"""
    import json as _json
    from pathlib import Path

    if not data_dir:
        return {}
    p = Path(data_dir) / _STATE_FILE
    try:
        return _json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取升级状态失败:%s", exc)
        return {}


def _recreate(client, container) -> None:
    """按原容器配置重建(镜像换成已拉取的新版本)。

    从 inspect attrs 复制关键字段:environment/cmd/entrypoint/working_dir/labels +
    host 侧(binds/network_mode/restart_policy/init/privileged/extra_hosts/dns)。
    docker SDK 的 containers.create(**kw) 会把 host 参数自动装进 HostConfig,
    所以这些参数**平铺直传**,不要手动 create_host_config 再塞 host_config= 进去
    (SDK 会把未消费的 kwargs 直接 reject,报 "run() got unexpected keyword arguments";
    2026-09-13 实测翻车)。重建必然杀掉当前进程,调用方需先回响应、再延时执行。
    """
    attrs = container.attrs
    cfg = attrs.get("Config") or {}
    hc = attrs.get("HostConfig") or {}
    rp = hc.get("RestartPolicy") or {}
    restart = {"Name": rp.get("Name") or "no"}
    if restart["Name"] == "on-failure":
        restart["MaximumRetryCount"] = rp.get("MaximumRetryCount") or 0
    new = client.containers.create(
        IMAGE,
        name=container.name,
        command=cfg.get("Cmd"),
        entrypoint=cfg.get("Entrypoint"),
        working_dir=cfg.get("WorkingDir"),
        environment=cfg.get("Env"),
        labels=cfg.get("Labels"),
        # host 侧配置:create() 内部自动组装 HostConfig。
        # 注意挂载卷的参数名是 **volumes**(内部转成 HostConfig.Binds),
        # 不是 binds —— docker SDK 7.x 的 create() 只认 RUN_HOST_CONFIG_KWARGS
        # 里的键,未知键直接 reject(2026-09-13 两次实测翻车)。
        volumes=hc.get("Binds"),
        network_mode=hc.get("NetworkMode") or "default",
        restart_policy=restart,
        init=hc.get("Init"),
        privileged=hc.get("Privileged"),
        extra_hosts=hc.get("ExtraHosts"),
        dns=hc.get("Dns"),
    )
    container.stop()
    container.remove()
    new.start()
    logger.info("升级:容器 %s 已用新镜像重建", container.name)
