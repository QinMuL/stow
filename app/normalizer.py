"""分享前目录结构标准化(移植自旧项目 share_normalizer,适配 stow 接口)。

把待分享资源整理成 Emby/Jellyfin 标准目录结构:

    片名 (年份) {tmdb-ID}/
    ├── 01/          ← 季目录(零填充两位)
    │   └── S01E01.mkv
    └── 02/

四种场景:
  A. 已有季子目录 → 重命名根目录 + 季子目录
  B. 剧集散集文件 → 重命名根目录 + 按季号建子目录 + 移入文件
  C. 电影文件夹   → 重命名根目录
  D. 散文件       → 建资源根目录 + 季目录 + 移入文件

幂等:已是标准结构的目录不重复处理。
失败容错:标准化失败不阻断分享创建,降级为用原结构建分享。
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field

from app.media import AggregatedMedia, ShareFile, analyze_share, extract_tmdb_id, parse_filename
from app.pan115 import Pan115Reader, ShareError

logger = logging.getLogger(__name__)

# 季目录名匹配(Season 1 / S01 / 第1季 / 第一季 / 01)
_SEASON_DIR_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^Season\s*(\d{1,2})$", re.IGNORECASE), "en"),
    (re.compile(r"^S(\d{1,2})$", re.IGNORECASE), "en"),
    (re.compile(r"^第\s*(\d{1,2})\s*季$"), "zh"),
    (re.compile(r"^第([一二三四五六七八九十]+)季$"), "zh_cn"),
]
_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_VIDEO_EXTS = (".mkv", ".mp4", ".avi", ".ts", ".mov", ".wmv", ".flv", ".webm")

# 疑似垃圾标题:纯小写字母/数字/下划线组合(无大写、无 CJK)且含长数字串
# (分享码、时间戳形态);区分大小写,"The Movie 2023" 含大写不判垃圾
_SUSPECT_TITLE_RE = re.compile(r"^[a-z0-9_\- ]+$")


def _suspect_title(title: str) -> bool:
    return bool(title) and bool(
        _SUSPECT_TITLE_RE.fullmatch(title) and re.search(r"\d{4,}", title)
    )


def _cn_to_int(s: str) -> int | None:
    """中文数字转 int(支持一~九十九)。"""
    if not s:
        return None
    if len(s) == 1:
        return _CN_NUM.get(s)
    if s.startswith("十"):
        rest = s[1:]
        return 10 + (_CN_NUM.get(rest, 0) if rest else 0)
    if "十" in s:
        tens, _, ones = s.partition("十")
        return _CN_NUM.get(tens, 0) * 10 + (_CN_NUM.get(ones, 0) if ones else 0)
    return None


def parse_season_dir(name: str) -> int | None:
    """从目录名解析季号(01/Season 1/S02/第3季/第一季);非季目录 None。"""
    m = re.match(r"^0*(\d{1,2})$", name)
    if m and 1 <= int(m.group(1)) <= 99:
        return int(m.group(1))
    for pattern, kind in _SEASON_DIR_PATTERNS:
        m = pattern.match(name)
        if m:
            raw = m.group(1)
            return _cn_to_int(raw) if kind == "zh_cn" else int(raw)
    return None


def format_season_dir(season: int) -> str:
    return f"{season:02d}"


def build_resource_name(title: str, year: int | None, tmdb_id: int | None) -> str:
    """标准资源目录名:片名 (年份) {tmdb-ID}。"""
    parts = [title.strip()]
    if year:
        parts.append(f"({year})")
    if tmdb_id:
        parts.append(f"{{tmdb-{tmdb_id}}}")
    return " ".join(parts)


def _has_tmdb_tag(name: str) -> bool:
    return bool(re.search(r"\{tmdb-\d+\}", name, re.IGNORECASE))


@dataclass
class NormalizeResult:
    fid: int              # 标准化后的 fid(可能变了:散文件时建了新资源目录)
    name: str             # 标准化后的名称
    changed: bool
    recognized: bool = True  # 媒体信息是否识别成功(TMDB 命中);未识别不应建分享
    actions: list[str] = field(default_factory=list)


class ShareNormalizer:
    """目录标准化器;依赖 Pan115Reader(list_dir/fs_rename/fs_move)与 TmdbClient。"""

    def __init__(self, pan115: Pan115Reader, tmdb=None, *, dry_run: bool = False) -> None:
        self.pan115 = pan115
        self.tmdb = tmdb
        self.dry_run = dry_run

    async def normalize(self, fid: int, name: str, is_dir: bool,
                        parent_cid: int) -> NormalizeResult:
        """标准化单个资源;失败降级用原结构(不阻断分享创建)。"""
        try:
            if is_dir:
                return await self._normalize_folder(fid, name)
            return await self._wrap_single_file(fid, name, parent_cid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("目录标准化失败(%s),降级用原结构:%s", name, exc)
            return NormalizeResult(fid=fid, name=name, changed=False, recognized=False)

    # ── 场景 A/B/C:已有目录 ────────────────────────────────
    async def _normalize_folder(self, fid: int, name: str) -> NormalizeResult:
        actions: list[str] = []
        items = await self.pan115.list_dir(fid, nf=0)
        subdirs = [it for it in items if it["is_dir"]]
        files = [it for it in items if not it["is_dir"]]

        media = self._detect_media(name, items)
        if media is None:
            logger.warning("目录标准化:无法识别媒体信息:%s", name)
            return NormalizeResult(fid=fid, name=name, changed=False, recognized=False)

        # 与推送卡片同一识别入口:tmdb.match(两轮搜索+别名+年份门槛)
        details = await self._match_details(media)
        if details is None:
            logger.warning("目录标准化:TMDB 未命中(%s),资源保留原结构待人工处理", media.title)
            return NormalizeResult(fid=fid, name=name, changed=False, recognized=False)
        title, year = details["title"], details["year"]
        tmdb_id = int(details["tmdb_id"])

        new_name = build_resource_name(title, year, tmdb_id)
        root_changed = False
        if new_name != name and not _has_tmdb_tag(name):
            if not self.dry_run:
                await self.pan115.fs_rename(fid, new_name)
            root_changed = True
            actions.append(f"重命名目录:{name} → {new_name}")

        season_changed = await self._handle_seasons(fid, subdirs, files, actions)
        return NormalizeResult(
            fid=fid, name=new_name if root_changed else name,
            changed=root_changed or season_changed, recognized=True, actions=actions,
        )

    # ── 季目录处理(A 重命名 / B 散集归季) ────────────────────
    async def _handle_seasons(self, parent_fid: int, subdirs: list[dict],
                              files: list[dict], actions: list[str]) -> bool:
        changed = False

        # A. 已有季子目录 → 重命名为零填充
        season_dirs: list[tuple[dict, int]] = []
        for d in subdirs:
            sn = parse_season_dir(d["name"])
            if sn is not None:
                season_dirs.append((d, sn))
        for item, sn in season_dirs:
            target = format_season_dir(sn)
            if item["name"] == target:
                continue
            if not self.dry_run:
                await self.pan115.fs_rename(item["fid"], target)
            changed = True
            actions.append(f"季目录重命名:{item['name']} → {target}")

        # B. 无季子目录但有 SxxExx 散集 → 按季建目录移入
        if not season_dirs and files:
            season_files: dict[int, list[dict]] = {}
            for f in files:
                sn = parse_filename(f["name"]).season
                if sn is not None:
                    season_files.setdefault(sn, []).append(f)
            for sn, sfiles in sorted(season_files.items()):
                target = format_season_dir(sn)
                season_cid = await self._makedirs_under(parent_fid, target)
                for f in sfiles:
                    await self.pan115.fs_move(f["fid"], season_cid)
                    await asyncio.sleep(0.5)  # 115 移动服务端异步,留间隔
                actions.append(f"建季目录 {target}/,移入 {len(sfiles)} 个文件")
                changed = True
        return changed

    # ── 场景 D:散文件包裹 ──────────────────────────────────
    async def _wrap_single_file(self, fid: int, name: str,
                                parent_cid: int) -> NormalizeResult:
        actions: list[str] = []
        media = self._detect_media(name, [dict(name=name, size=0, is_dir=False)])
        if media is None:
            return NormalizeResult(fid=fid, name=name, changed=False, recognized=False)
        details = await self._match_details(media)
        if details is None:
            return NormalizeResult(fid=fid, name=name, changed=False, recognized=False)
        title, year = details["title"], details["year"]
        root_name = build_resource_name(title, year, int(details["tmdb_id"]))

        root_cid = await self._makedirs_under(parent_cid, root_name)
        if media.season is not None:
            season_cid = await self._makedirs_under(root_cid, format_season_dir(media.season))
            await self.pan115.fs_move(fid, season_cid)
            actions.append(f"建资源目录 {root_name}/{format_season_dir(media.season)}/,移入 {name}")
        else:
            await self.pan115.fs_move(fid, root_cid)
            actions.append(f"建资源目录 {root_name}/,移入 {name}")
        return NormalizeResult(fid=root_cid, name=root_name, changed=True, actions=actions)

    # ── 媒体信息探测(与推送卡片同一入口:analyze_share) ──
    def _detect_media(self, dir_name: str, items: list[dict]) -> AggregatedMedia | None:
        """复用 analyze_share 聚合(标题多数票/类型判定/画质),保证与卡片同规则。"""
        entries = [
            ShareFile(name=it["name"], size=it.get("size") or 0, is_dir=it["is_dir"])
            for it in items
        ]
        media = analyze_share(entries)
        if media is None or not media.title:
            return None
        media.tmdb_id = extract_tmdb_id([dir_name]) or extract_tmdb_id(
            [it["name"] for it in items]
        )
        return media

    async def _match_details(self, media: AggregatedMedia) -> dict | None:
        """目录名带 {tmdb-} 标注时直连详情;否则走与卡片相同的 match 流程。"""
        if self.tmdb is None:
            return None
        if media.tmdb_id:
            details = await self.tmdb.get_details(media.tmdb_id, media.media_type)
            if details:
                return details
        return await self.tmdb.match(media)

    # ── TMDB ────────────────────────────────────────────────
    async def _makedirs_under(self, parent_cid: int, name: str) -> int:
        items = await self.pan115.list_dir(parent_cid, nf=1)
        for it in items:
            if it["name"].lower() == name.lower():
                return it["fid"]
        from p115client.client import check_response

        client = self.pan115._require_login()
        resp = await self.pan115._call(
            client.fs_mkdir, {"cname": name, "pid": parent_cid}, async_=False
        )
        try:
            check_response(resp)
        except Exception as exc:
            raise ShareError(f"建目录失败({name}):{exc}") from exc
        items = await self.pan115.list_dir(parent_cid, nf=1)  # 建后重列最可靠
        for it in items:
            if it["name"].lower() == name.lower():
                return it["fid"]
        raise ShareError(f"建目录后无法获取 cid:{name}")
