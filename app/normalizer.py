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

from app.media import AggregatedMedia, extract_tmdb_id, parse_filename
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
            return NormalizeResult(fid=fid, name=name, changed=False)

    # ── 场景 A/B/C:已有目录 ────────────────────────────────
    async def _normalize_folder(self, fid: int, name: str) -> NormalizeResult:
        actions: list[str] = []
        items = await self.pan115.list_dir(fid, nf=0)
        subdirs = [it for it in items if it["is_dir"]]
        files = [it for it in items if not it["is_dir"]]

        media = self._detect_media(name, items)
        if media is None:
            logger.info("目录标准化跳过(无法识别媒体信息):%s", name)
            return NormalizeResult(fid=fid, name=name, changed=False)
        title, year, media_type, tmdb_id = media

        if tmdb_id is None:
            tmdb_id = await self._tmdb_match(title, year, media_type)
            if tmdb_id is not None:
                details = await self._tmdb_details(tmdb_id, media_type)
                if details:
                    title = details.get("title") or title
                    year = details.get("year") or year

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
            changed=root_changed or season_changed, actions=actions,
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
        parsed = parse_filename(name)
        title, year, media_type = parsed.title, parsed.year, parsed.media_type
        tmdb_id = await self._tmdb_match(title, year, media_type)
        if tmdb_id is not None:
            details = await self._tmdb_details(tmdb_id, media_type)
            if details:
                title = details.get("title") or title
                year = details.get("year") or year
        root_name = build_resource_name(title, year, tmdb_id)

        root_cid = await self._makedirs_under(parent_cid, root_name)
        if parsed.season is not None:
            season_cid = await self._makedirs_under(root_cid, format_season_dir(parsed.season))
            await self.pan115.fs_move(fid, season_cid)
            actions.append(f"建资源目录 {root_name}/{format_season_dir(parsed.season)}/,移入 {name}")
        else:
            await self.pan115.fs_move(fid, root_cid)
            actions.append(f"建资源目录 {root_name}/,移入 {name}")
        return NormalizeResult(fid=root_cid, name=root_name, changed=True, actions=actions)

    # ── 媒体信息探测 ────────────────────────────────────────
    def _detect_media(self, dir_name: str, items: list[dict]):
        tmdb_id = extract_tmdb_id([dir_name]) or extract_tmdb_id(
            [it["name"] for it in items]
        )
        parsed = parse_filename(dir_name)
        title, year, media_type = parsed.title, parsed.year, parsed.media_type

        if media_type == "movie":
            for it in items:
                if parse_filename(it["name"]).season is not None:
                    media_type = "tv"
                    break
            video_count = sum(
                1 for it in items
                if not it["is_dir"] and it["name"].lower().endswith(_VIDEO_EXTS)
            )
            if video_count > 1:
                media_type = "tv"

        if not title or title == dir_name:
            for it in items:
                p = parse_filename(it["name"])
                if p.title and p.title != it["name"]:
                    title, year = p.title, (p.year or year)
                    if p.media_type == "tv":
                        media_type = "tv"
                    break

        return (title, year, media_type, tmdb_id) if title else None

    # ── TMDB ────────────────────────────────────────────────
    async def _tmdb_match(self, title: str, year: int | None,
                          media_type: str) -> int | None:
        if self.tmdb is None:
            return None
        try:
            details = await self.tmdb.match(
                AggregatedMedia(title=title, year=year, media_type=media_type)
            )
            return int(details["tmdb_id"]) if details else None
        except Exception as exc:  # noqa: BLE001
            logger.info("TMDB 匹配失败(%s):%s", title, exc)
            return None

    async def _tmdb_details(self, tmdb_id: int, media_type: str) -> dict | None:
        if self.tmdb is None:
            return None
        try:
            return await self.tmdb.get_details(tmdb_id, media_type)
        except Exception:  # noqa: BLE001
            return None

    # ── 目录创建辅助(fs_mkdir 幂等:先查后建,建后重列取 cid) ──
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
