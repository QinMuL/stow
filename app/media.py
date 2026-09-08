"""文件名解析(纯函数):标题/年份/季集/画质。聚合多文件得分享级媒体信息。"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

_YEAR_RE = re.compile(r"(?:^|[\s.\[(])((?:19|20)\d{2})(?=[\s.\]\)]|$)")
_SE_RE = re.compile(r"S(\d{1,2})\s*E(\d{1,4})", re.IGNORECASE)
_SE_RANGE_RE = re.compile(r"S(\d{1,2})\s*E(\d{1,4})\s*[-~]\s*E?(\d{1,4})", re.IGNORECASE)
_SEASON_ONLY_RE = re.compile(r"(?:^|[\s.\[])S(\d{1,2})(?![\dEe])", re.IGNORECASE)
_RES_RE = re.compile(r"\b(4320p|2160p|1440p|1080[pi]|720p|480p)\b", re.IGNORECASE)
# 噪音词:前置清洗(避免污染标题)
_NOISE_RE = re.compile(
    r"\b(4K|8K|HDR10\+?|DoVi|DV|SDR|60fps|\d{2}fps|10bit|8bit|HQ|HD\b)"
    r"|\b(AAC\s?\d\.\d|AC3|EAC3|DTS-?HD?|FLAC|TrueHD|Atmos|LPCM)\b",
    re.IGNORECASE,
)


@dataclass
class FileInfo:
    title: str
    year: int | None
    season: int | None
    episode: int | None
    resolution: str = ""

    def __init__(self, raw: str):  # noqa: PLR0912 - 分支即解析规则
        name = raw
        # 去扩展名
        if "." in name:
            name = name.rsplit(".", 1)[0]
        # 集数范围:取起始集
        m = _SE_RANGE_RE.search(name) or _SE_RE.search(name)
        season = episode = None
        if m:
            season, episode = int(m.group(1)), int(m.group(2))
        else:
            sm = _SEASON_ONLY_RE.search(name)
            if sm:
                season = int(sm.group(1))
        # 年份:首个独立四位年份
        ym = _YEAR_RE.search(name)
        year = int(ym.group(1)) if ym else None
        # 标题:年份/集数之前的部分,去噪音
        cut = min(
            [p for p in (ym.start() if ym else -1, m.start() if m else -1) if p > 0]
            or [len(name)]
        )
        title = name[:cut]
        title = _NOISE_RE.sub(" ", title)
        title = re.sub(r"[\.\_]+", " ", title)
        title = re.sub(r"\s*[-–]\s*$", "", title.strip())
        title = re.sub(r"\s{2,}", " ", title).strip(" -_.[]()")
        # 画质
        rm = _RES_RE.search(name)
        resolution = rm.group(1).lower().replace("i", "p") if rm else ""
        self.title, self.year, self.season, self.episode, self.resolution = (
            title, year, season, episode, resolution,
        )


@dataclass
class MediaInfo:
    """分享级聚合信息。"""

    title: str = ""
    year: int | None = None
    is_tv: bool = False
    seasons: set[int] = field(default_factory=set)
    episodes: set[int] = field(default_factory=set)
    resolution: str = ""
    video_files: list[str] = field(default_factory=list)
    total_size: int = 0
    file_count: int = 0


def normalize_title(t: str) -> str:
    """比较用:去年份 token + 全半角标点 + 小写。"""
    t = re.sub(r"(?:19|20)\d{2}", "", t)
    return re.sub(r"[^\w\u4e00-\u9fff]", "", t).lower()


def title_match(a: str, b: str) -> bool:
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return False
    return na == nb or (len(na) >= 4 and na in nb) or (len(nb) >= 4 and nb in na)


def aggregate(files: list[tuple[str, int, bool]]) -> MediaInfo:
    """聚合(文件名, 体积, 是否目录)列表为分享级信息。"""
    from app.pan115 import ShareFile  # noqa: F401 - 类型提示用

    info = MediaInfo(file_count=len(files))
    parsed: list[FileInfo] = []
    for name, size, is_dir in files:
        info.total_size += size if not is_dir else 0
        if is_dir:
            continue
        probe = FileInfo(name)
        if _is_video_name(name):
            info.video_files.append(name)
            parsed.append(probe)
    if not parsed:
        # 无视频:用第一个文件名猜标题
        if files:
            p = FileInfo(files[0][0])
            info.title, info.year = p.title, p.year
        return info
    # 标题:多数票
    counter = Counter(p.title for p in parsed if p.title)
    info.title = counter.most_common(1)[0][0] if counter else ""
    years = [p.year for p in parsed if p.year]
    info.year = Counter(years).most_common(1)[0][0] if years else None
    # TV 判定:任一有集数,或视频数 > 1
    info.is_tv = any(p.episode is not None for p in parsed) or len(parsed) > 1
    for p in parsed:
        if p.season is not None:
            info.seasons.add(p.season)
        if p.episode is not None:
            info.episodes.add(p.episode)
    if info.is_tv and not info.seasons:
        info.seasons.add(1)
    # 画质:取最高
    rank = {"480p": 0, "720p": 1, "1080p": 2, "1440p": 3, "2160p": 4, "4320p": 5}
    best = max((p.resolution for p in parsed if p.resolution), key=lambda r: rank.get(r, -1), default="")
    info.resolution = best
    return info


def _is_video_name(name: str) -> bool:
    return name.rsplit(".", 1)[-1].lower() in {
        "mkv", "mp4", "avi", "wmv", "mov", "flv", "ts", "m2ts", "webm", "rmvb",
    }
