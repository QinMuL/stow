"""配置:config.json 唯一真源。Web 与 Bot 共用。

- load_config(strict=False):供 Web 使用,配置不全也返回(带 problems),
  Web 恒活,等待用户在网页上补齐
- load_config(strict=True):供 Bot 使用,必填缺失时 sys.exit 人话报错
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "./data/config.json"
DEFAULT_WEB_PORT = 8686

# 敏感键:Web 展示脱敏;PUT 收到掩码值表示"未修改"
SENSITIVE_KEYS = (
    "tg_bot_token", "tmdb_api_key", "pan115_cookie", "tg_api_hash",
    "openlist_token", "cd2_token", "cd2_password",
)
MASK = "••••••••"


@dataclass
class ChannelConfig:
    """推送频道:归属预设决定哪类链接推到这里。"""

    chat_id: str
    preset: str = "115"  # "115" | "ed2k"
    title: str = ""      # 频道名(Bot 登记时自动带出,展示用)


@dataclass
class Config:
    tg_bot_token: str = ""
    tg_admin_ids: list[int] = field(default_factory=list)
    tmdb_api_key: str = ""
    proxy_url: str = ""
    # 115 登录 cookie(可选):读分享走 android/proapi 通道,绕开 115 对匿名
    # webapi share_snap 的指纹封锁(405);留空则匿名 web 兜底
    pan115_cookie: str = ""
    # 多频道:归属预设分流(115 链接 → preset=115 的频道;ed2k → preset=ed2k)。
    # 未登记某归属时,该类链接不推送,日志与 Bot 明确提示原因
    channels: list[ChannelConfig] = field(default_factory=list)
    # 转存流水线(/save <链接> 触发):暂存 → 整理 → 建分享 → 推送 → 归档/违规
    # 需 115 Cookie;目录相对网盘根,不存在自动创建;子目录由根目录派生
    pipeline_root_dir: str = "stow流水线"
    # 目录监控:这些网盘目录里出现新资源时,自动标准化+建分享+推送(逗号分隔)
    monitor_dirs: str = ""
    # TG 频道监控(Telethon 用户账号):源频道里的 ed2k 链接 → 本项目卡片 → ed2k 归属频道。
    # 频道列表为空 = 监控不启动(无需额外开关);api_id/api_hash 在 my.telegram.org 申请
    tg_api_id: int = 0
    tg_api_hash: str = ""
    monitor_channels: str = ""  # 逗号分隔:@username / t.me 链接 / chat_id
    # 下载/上传闭环的外部工具(v0.2):openlist 挂载其它网盘取资源,CD2 上传到 115
    openlist_base_url: str = ""   # openlist 服务地址,如 http://127.0.0.1:5244
    openlist_token: str = ""      # openlist API token(敏感)
    openlist_path: str = ""       # openlist 侧的挂载路径(资源所在),如 /项目测试
    # 获取段(自动流):监控这些 openlist 目录,新资源自动**移动**到 openlist_dest_path
    openlist_monitor_dirs: str = ""       # 逗号分隔,一栏一项(Web 上填)
    openlist_dest_path: str = "/项目测试"  # 落地点 = 本地 media/openlist 的挂载视图
    openlist_max_tasks: int = 2           # 同时在搬的任务数上限(用户要求 2)
    fetch_interval_minutes: int = 5        # 扫描间隔(分钟)
    # 处理段(media/openlist → 探测/重命名/ed2k/推卡 → media/clouddrive)
    process_interval_minutes: int = 5     # 处理轮询间隔(分钟)
    min_size_mb: int = 50                 # 体积下限(小于此值不当视频处理)
    min_age_seconds: int = 60             # mtime 静默年龄(避免处理半截文件)
    clean_enabled: bool = True            # 元数据清洗开关:仅在探到广告类脏数据时才清洗
    upload_interval_minutes: int = 5       # 上传段轮询间隔(分钟)
    upload_max_tasks: int = 2              # 上传并发上限(CD2 可同时跑多个;用户定 2)
    cd2_address: str = ""         # CD2 gRPC 地址,如 127.0.0.1:19798
    cd2_token: str = ""           # CD2 API token(敏感)
    cd2_username: str = ""        # 无令牌时的兜底登录账号
    cd2_password: str = ""        # 无令牌时的兜底登录密码(敏感)
    cd2_source_path: str = ""     # CD2 侧看到的本地待上传目录,如 /clouddrive
    cd2_dest_path: str = ""       # 上传目标(CD2 侧 115 路径,如 /115open/目标目录;Web 选择器回填)
    # 本地媒体流转目录(容器内 /app/media,与 compose 的 ./media:/app/media 对应)。
    # 首次部署自动在其下建两个子目录:openlist(下载落地) / clouddrive(CD2 上传源)
    media_root: str = "./media"
    data_dir: str = "./data"
    log_level: str = "INFO"
    web_port: int = DEFAULT_WEB_PORT
    # Web 管理员(哈希存储;首启自动建 admin/admin)
    admin_username: str = ""
    admin_password_hash: str = ""

    @property
    def db_path(self) -> Path:
        return Path(self.data_dir) / "stow.db"

    @property
    def log_dir(self) -> Path:
        return Path(self.data_dir) / "logs"

    # ── 本地媒体流转目录(下载落地 / 上传源) ────────────────
    @property
    def openlist_dir(self) -> Path:
        """openlist 下载落地点(获取段的本地入口)。"""
        return Path(self.media_root) / "openlist"

    @property
    def clouddrive_dir(self) -> Path:
        """CD2 上传源(处理完成后待上传的本地出口)。"""
        return Path(self.media_root) / "clouddrive"

    def ensure_media_dirs(self) -> list[Path]:
        """确保媒体目录存在(首次部署自动创建),返回本次**新建**的目录。

        权限放宽到 0777:openlist / CD2 是各自独立的部署(容器 UID 可能不同),
        目录若为 root 私有,对方会写不进来——这个坑排查起来很费时间。
        """
        created: list[Path] = []
        for path in (Path(self.media_root), self.openlist_dir, self.clouddrive_dir):
            if path.is_dir():
                continue
            path.mkdir(parents=True, exist_ok=True)
            try:
                path.chmod(0o777)
            except OSError as exc:  # noqa: PERF203 - 权限位改不了不影响使用(如 drvfs)
                logger.debug("设置目录权限失败(%s):%s", path, exc)
            created.append(path)
        return created

    def problems(self) -> list[str]:
        """必填项缺失清单(空 = 可跑 Bot)。"""
        out = []
        if not self.tg_bot_token:
            out.append("tg_bot_token 未填写(Bot 无法启动)")
        if not self.tg_admin_ids:
            out.append("tg_admin_ids 未填写(无人能使用 Bot)")
        if not self.tmdb_api_key:
            out.append("tmdb_api_key 未填写(卡片将缺少元数据,仍可推送)")
        return out

    def bot_ready(self) -> bool:
        fatal = (p for p in self.problems() if "tmdb" not in p)
        return not next(fatal, None)

    def channel_for(self, provider: str) -> str | None:
        """按链接类型选归属频道;未登记该归属返回 None(调用方提示,不推送)。"""
        for ch in self.channels:
            if ch.preset == provider:
                return ch.chat_id
        return None

    def pipeline_dirs(self) -> tuple[str, str, str]:
        """流水线三目录:(暂存, 已发布, 违规),由根目录派生。"""
        root = self.pipeline_root_dir.rstrip("/")
        return (f"{root}/待整理", f"{root}/已发布", f"{root}/违规")

    def monitor_dir_list(self) -> list[str]:
        """监控目录列表(逗号/换行分隔,去空)。"""
        parts = re.split(r"[,，\n]+", self.monitor_dirs or "")
        return [p.strip() for p in parts if p.strip()]

    @property
    def monitor_session_path(self) -> Path:
        """Telethon 会话文件(随 data 目录持久化,容器重建不丢登录)。"""
        return Path(self.data_dir) / "monitor.session"

    def openlist_monitor_list(self) -> list[str]:
        """获取段监控的 openlist 目录(逗号/换行分隔,去空、按序去重)。"""
        seen: set[str] = set()
        out: list[str] = []
        parts = re.split("[,，" + chr(10) + "]+", self.openlist_monitor_dirs or "")
        for part in parts:
            p = part.strip()
            if p and p not in seen:
                seen.add(p)
                out.append(p)
        return out

    def openlist_ready(self) -> bool:
        """获取段能否启动:需地址 + 令牌 + 至少一个监控目录。"""
        return bool(self.openlist_base_url and self.openlist_token and self.openlist_monitor_list())

    def monitor_channel_list(self) -> list[str]:
        """源频道列表(逗号/换行分隔,去空、按序去重)。"""
        seen: set[str] = set()
        out: list[str] = []
        for part in re.split(r"[,，\n]+", self.monitor_channels or ""):
            ref = part.strip()
            if ref and ref not in seen:
                seen.add(ref)
                out.append(ref)
        return out

    def monitor_ready(self) -> bool:
        """频道监控能否启动:需凭据 + 至少一个源频道。"""
        return bool(self.tg_api_id and self.tg_api_hash and self.monitor_channel_list())

    def channels_summary(self) -> str:
        """启动日志用:归属分流一览。"""
        if not self.channels:
            return "归属频道:未登记(收到链接将提示先登记)"
        return ";".join(
            f"{ch.preset}→{ch.chat_id}" + (f"({ch.title})" if ch.title else "")
            for ch in self.channels
        )


def _clean(value: object) -> str:
    """清洗配置值:去空白;含非 ASCII(如中文占位符)视为未填写。"""
    s = str(value or "").strip()
    if not s or not s.isascii():
        return ""
    return s


def _dir(raw: dict, key: str, default: str) -> str:
    """目录配置:去空白;空值回退默认。"""
    return str(raw.get(key, default)).strip() or default


def _int(value: object) -> int:
    """整数配置:非法/空值回退 0(Web 表单可能传字符串)。"""
    s = str(value or "").strip()
    return int(s) if s.lstrip("-").isdigit() else 0


def fingerprint(raw: dict) -> str:
    """配置指纹:判断"文件里的配置"与"运行中 Bot 生效的配置"是否一致。

    Bot 启动时记下当时的指纹;此后文件被改(网页保存、/bind 写频道)且**未**同步给
    Bot 时,指纹就不同 → 前端据此提示"需保存并重启"。键序无关,值变化即变。
    """
    payload = json.dumps(raw, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def read_raw(path: str | Path | None = None) -> dict:
    path = Path(path or DEFAULT_CONFIG_PATH)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def write_raw(raw: dict, path: str | Path | None = None) -> None:
    path = Path(path or DEFAULT_CONFIG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    tmp.replace(path)


def load_config(path: str | Path | None = None, strict: bool = False) -> Config:
    path = Path(path or DEFAULT_CONFIG_PATH)
    if not path.exists():
        hint = f"请复制 config.example.json 为 {path} 并填写后再启动。"
        if strict:
            sys.exit(f"[stow] 配置文件不存在:{path}\n       {hint}")
        return Config()

    raw = read_raw(path)
    if not raw and path.exists():
        if strict:
            sys.exit(f"[stow] 配置文件不是合法 JSON:{path}")
        return Config()

    channels = []
    for c in raw.get("channels", []):
        if isinstance(c, dict) and str(c.get("chat_id", "")).strip():
            preset = str(c.get("preset", "115")).strip().lower()
            channels.append(ChannelConfig(
                chat_id=str(c["chat_id"]).strip(),
                preset=preset if preset in ("115", "ed2k") else "115",
                title=str(c.get("title", "")).strip(),
            ))

    cfg = Config(
        tg_bot_token=_clean(raw.get("tg_bot_token")),
        tg_admin_ids=[int(x) for x in raw.get("tg_admin_ids", []) if str(x).strip().lstrip("-").isdigit()],
        tmdb_api_key=_clean(raw.get("tmdb_api_key")),
        proxy_url=_clean(raw.get("proxy_url")),
        pan115_cookie=_clean(raw.get("pan115_cookie")),
        channels=channels,
        pipeline_root_dir=_dir(raw, "pipeline_root_dir", "stow流水线"),
        monitor_dirs=str(raw.get("monitor_dirs", "")).strip(),
        tg_api_id=_int(raw.get("tg_api_id")),
        tg_api_hash=_clean(raw.get("tg_api_hash")),
        monitor_channels=str(raw.get("monitor_channels", "")).strip(),
        openlist_base_url=_clean(raw.get("openlist_base_url")),
        openlist_token=_clean(raw.get("openlist_token")),
        openlist_path=str(raw.get("openlist_path", "")).strip(),
        openlist_monitor_dirs=str(raw.get("openlist_monitor_dirs", "")).strip(),
        openlist_dest_path=str(raw.get("openlist_dest_path", "/项目测试")).strip() or "/项目测试",
        openlist_max_tasks=max(1, int(raw.get("openlist_max_tasks", 2) or 2)),
        fetch_interval_minutes=max(1, int(raw.get("fetch_interval_minutes", 5) or 5)),
        process_interval_minutes=max(1, int(raw.get("process_interval_minutes", 5) or 5)),
        min_size_mb=max(0, int(raw.get("min_size_mb", 50) or 0)),
        min_age_seconds=max(0, int(raw.get("min_age_seconds", 60) or 0)),
        clean_enabled=str(raw.get("clean_enabled", True)).strip().lower() not in ("0", "false", "off", "关"),
        upload_interval_minutes=max(1, int(raw.get("upload_interval_minutes", 5) or 5)),
        upload_max_tasks=max(1, int(raw.get("upload_max_tasks", 2) or 2)),
        cd2_address=_clean(raw.get("cd2_address")),
        cd2_token=_clean(raw.get("cd2_token")),
        cd2_username=_clean(raw.get("cd2_username")),
        cd2_password=_clean(raw.get("cd2_password")),
        cd2_source_path=str(raw.get("cd2_source_path", "")).strip(),
        cd2_dest_path=str(raw.get("cd2_dest_path", "")).strip(),
        media_root=_dir(raw, "media_root", "./media"),
        data_dir=str(raw.get("data_dir", "./data")),
        log_level=str(raw.get("log_level", "INFO")).upper(),
        web_port=int(raw.get("web_port", DEFAULT_WEB_PORT) or DEFAULT_WEB_PORT),
        admin_username=str(raw.get("admin_username", "")).strip(),
        admin_password_hash=str(raw.get("admin_password_hash", "")).strip(),
    )

    if strict:
        problems = cfg.problems()
        if problems:
            for p in problems:
                print(f"[stow] 配置缺项:{p}")
            if not cfg.bot_ready():
                sys.exit(f"[stow] 必填项缺失,退出。填写 {path} 后重试(或经 Web 配置页填写)。")
    return cfg


def ensure_admin(cfg_path: str | Path | None = None) -> None:
    """config.json 无管理员凭据时写入默认 admin/admin(幂等)。"""
    path = Path(cfg_path or DEFAULT_CONFIG_PATH)
    from app import auth

    raw = read_raw(path)
    if raw.get("admin_username") and raw.get("admin_password_hash"):
        return
    raw.setdefault("tg_bot_token", "")
    raw.setdefault("tg_admin_ids", [])
    raw.setdefault("tmdb_api_key", "")
    raw.setdefault("proxy_url", "")
    raw.setdefault("pan115_cookie", "")
    raw.setdefault("channels", [])
    raw.setdefault("monitor_dirs", "")
    raw.setdefault("monitor_channels", "")
    raw.pop("tg_chat_id", None)  # 默认频道概念已移除,旧配置顺手清理
    raw["admin_username"] = auth.DEFAULT_ADMIN_USER
    raw["admin_password_hash"] = auth.hash_password(auth.DEFAULT_ADMIN_PASSWORD)
    write_raw(raw, path)
