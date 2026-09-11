"""文件名解析引擎:guessit + 噪音清洗 + 季集/画质/平台/TMDB标注 + 分享聚合。"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from app.pan115 import ShareFile


@dataclass
class MediaData:
    """单文件解析结果。"""

    title: str = ""
    year: int | None = None
    media_type: str = "movie"  # "movie" | "tv"
    season: int | None = None
    episode: int | None = None
    episode_end: int | None = None
    quality: str = ""
    source: str = ""
    hdr: str = ""
    quality_info: list[str] = field(default_factory=list)
    raw: str = ""
    release_group: str = ""


@dataclass
class AggregatedMedia:
    """分享级聚合结果。"""

    title: str = ""
    year: int | None = None
    media_type: str = "movie"
    season: int | None = None
    seasons: list[int] = field(default_factory=list)  # 所有季号(去重排序)
    season_episodes: dict[int, list[int]] = field(default_factory=dict)  # 按季分组的集号
    episode_start: int | None = None
    episode_end: int | None = None
    quality: str = ""
    source: str = ""
    hdr: str = ""
    release_group: str = ""   # 发布组(命名模板 -{team} 用;已过 clean_release_group)
    quality_info: list[str] = field(default_factory=list)
    file_count: int = 0
    total_size: int = 0
    total_episodes: int | None = None
    tmdb_id: int | None = None  # 文件名/目录名标注的 {tmdb-XXX}
    alt_queries: list[str] = field(default_factory=list)  # 原名方括号中文标注等额外查询词


# TMDB ID 标注:{tmdb-1311031}(分享者/媒体管理工具标注,最可靠的匹配来源)
_TMDB_ID_RE = re.compile(r"\{tmdb-(\d+)\}", re.IGNORECASE)


def extract_tmdb_id(names: list[str]) -> int | None:
    """从名称列表提取第一个 {tmdb-XXX} 标注。"""
    for name in names:
        m = _TMDB_ID_RE.search(name)
        if m:
            return int(m.group(1))
    return None


# 原名方括号内的中文标注:"[藏锋].Sharp.Turns.S01E01" → "藏锋"(TMDB 中文搜索词)
_BRACKET_CJK_RE = re.compile(r"[\[【]([\u4e00-\u9fff][\u4e00-\u9fff·0-9]{1,30})[\]】]")


def extract_cjk_annotations(names: list[str]) -> list[str]:
    """收集方括号中文标注(去重保序)。"""
    out: list[str] = []
    for n in names:
        for m in _BRACKET_CJK_RE.finditer(n):
            if m.group(1) not in out:
                out.append(m.group(1))
    return out


# ── 噪音清洗 ────────────────────────────────────────────────
_SEP_RE = re.compile(r"[._\-\(\)\[\]]")
_FPS_RE = re.compile(r"\b\d{1,2}(?:[.,]\d+)?\s*fps\b", re.IGNORECASE)
_BITDEPTH_RE = re.compile(r"\b\d{1,2}\s*-?\s*bit\b", re.IGNORECASE)
_NOISE_TAG_RE = re.compile(r"\b(?:HQ|HD|FINE|高清)\b", re.IGNORECASE)
_AUDIO_TAG_RE = re.compile(
    r"\b(?:AAC|EAC3|AC3|DTS|DTS-?HD|TrueHD|Atmos|FLAC|DDP?|DD)\d*(?:[.,]\d+)?\b",
    re.IGNORECASE,
)
_CHANNEL_RE = re.compile(r"\b\d\.\d\b")


def clean_name(name: str) -> str:
    """去扩展名、分隔符归一为空格、清洗噪音词(声道/帧率/色深/HQ)。"""
    base = name.rsplit(".", 1)[0] if "." in name else name
    s = _SEP_RE.sub(" ", base)
    s = _FPS_RE.sub(" ", s)
    s = _BITDEPTH_RE.sub(" ", s)
    s = _AUDIO_TAG_RE.sub(" ", s)
    s = _CHANNEL_RE.sub(" ", s)
    s = _NOISE_TAG_RE.sub(" ", s)
    return re.sub(r"\s{2,}", " ", s).strip()


# ── 发布组清洗:guessit 会把帧率/色深/音轨数残留误判为 release_group ──
_GROUP_TOKEN_NOISE_RE = re.compile(
    r"^(?:\d+(?:[.,]\d+)?\s*fps|fps|h|hq|p\d+|10-?bit|\d*audios?|\d+)$",
    re.IGNORECASE,
)
# 我们自己的 {tmdb-NNN} 标注会被 guessit 当成发布组("tmdb")→ 命名会多出 "-tmdb"
_GROUP_KNOWN_NOISE = {"tmdb", "tmdbid", "tmdb_id"}


def clean_release_group(group: str) -> str:
    """清洗误判的发布组:噪音 token 过滤后取末 token(需字母开头且 ≥3 字符)。"""
    tokens = [t for t in re.split(r"[\s._\-]+", (group or "").strip()) if t]
    kept = [t for t in tokens if not _GROUP_TOKEN_NOISE_RE.match(t)]
    if not kept:
        return ""
    cand = kept[-1]
    if cand.lower() in _GROUP_KNOWN_NOISE:
        return ""
    return cand if len(cand) >= 3 and cand[0].isalpha() else ""


# ── 季 / 集 ────────────────────────────────────────────────
# 范围要求显式分隔符(S01E01-E12 / ~E12 / E12)避免单集误匹配;集号支持 4 位(航海王)
_EP_RANGE_RE = re.compile(r"S(\d{1,2})\s*E(\d{1,4})\s*[-~E]+\s*(\d{1,4})", re.IGNORECASE)
_SE_EP_RE = re.compile(r"S(\d{1,2})\s*E(\d{1,4})", re.IGNORECASE)
# 前置非字母数字防 "Cars2" 类误判;分隔兼容 "Season 3"/"Season.3"/"S3"
_SEASON_ONLY_RE = re.compile(r"(?:^|[^A-Za-z0-9])S(?:eason[\s._]*)?(\d{1,2})\b", re.IGNORECASE)


def extract_season_episode(text: str) -> tuple[int | None, int | None, int]:
    """返回 (season, ep_start, ep_span);episode_end = ep_start + ep_span - 1。"""
    rng = _EP_RANGE_RE.search(text)
    if rng:
        s, a, b = int(rng.group(1)), int(rng.group(2)), int(rng.group(3))
        return s, a, max(1, b - a + 1)
    m = _SE_EP_RE.search(text)
    if m:
        return int(m.group(1)), int(m.group(2)), 1
    sm = _SEASON_ONLY_RE.search(text)
    if sm:
        return int(sm.group(1)), None, 0
    return None, None, 0


# ── 画质 / 来源 / HDR ─────────────────────────────────────
def get_quality(text: str) -> str:
    t = text.lower()
    if "2160p" in t or "4k" in t or "uhd" in t:
        return "4K / 2160P"
    if "1080p" in t:
        return "1080P"
    if "720p" in t:
        return "720P"
    if "480p" in t:
        return "480P"
    return ""


def get_source(text: str) -> str:
    t = text.lower()
    rules = [
        (r"\bremux\b", "REMUX"),
        (r"\bblu-?ray\b|\bbd\b", "BluRay"),
        (r"\bweb-?dl\b|\bwebrip\b|\bweb\b", "WEB-DL"),
        (r"\bhdtv\b", "HDTV"),
        (r"\bdvdrip\b|\bdvd\b", "DVD"),
    ]
    for pat, label in rules:
        if re.search(pat, t):
            return label
    return ""


def get_hdr(text: str) -> str:
    t = text.lower()
    tags: list[str] = []
    if "dolby vision" in t or "dovi" in t or "dv " in t or ".dv" in t:
        tags.append("Dolby Vision")
    if "hdr10+" in t:
        tags.append("HDR10+")
    elif "hdr10" in t:
        tags.append("HDR10")
    elif "hdr" in t:
        tags.append("HDR")
    if "sdr" in t and not tags:
        tags.append("SDR")
    return " / ".join(tags)


# ── 播放平台:简称自动补全全称(NF→Netflix、DSNP→Disney+…) ──
_PLATFORM_TOKENS = [
    "Disney+", "Disney", "HBO Max", "HBO", "Max", "AppleTV+", "AppleTV",
    "Netflix", "Amazon", "Peacock", "Paramount+", "Paramount",
    "Crunchyroll", "Hulu", "Bilibili", "friDay", "Baha",
    "NF", "AMZN", "DSNP", "ATVP", "PCOK", "PMTP", "HULU", "HMAX", "CR",
]
_PLATFORM_EXPAND = {
    "nf": "Netflix", "amzn": "Prime Video", "dsnp": "Disney+",
    "atvp": "Apple TV+", "pcok": "Peacock", "pmtp": "Paramount+",
    "hulu": "Hulu", "hmax": "Max", "cr": "Crunchyroll",
}


def extract_platform(name: str) -> str:
    """提取独立平台 token(字母数字边界,防 REMUX 误含 NF 类)并补全简称。"""
    best: tuple[int, str] | None = None
    lowered = name.lower()
    for tok in _PLATFORM_TOKENS:
        idx = lowered.find(tok.lower())
        while idx != -1:
            lo, hi = idx, idx + len(tok)
            if (
                (idx == 0 or not name[lo - 1].isalnum())
                and (hi >= len(name) or not name[hi].isalnum())
            ):
                # 平台标注惯例在分辨率附近,取最靠前的命中
                if best is None or idx < best[0]:
                    best = (idx, name[lo:hi])
                break
            idx = lowered.find(tok.lower(), idx + 1)
    if best is None:
        return ""
    return _PLATFORM_EXPAND.get(best[1].lower(), best[1])


def is_platform_token(text: str) -> bool:
    return (text or "").strip().lower() in {p.lower() for p in _PLATFORM_TOKENS}


# ── 画质详细信息(分辨率|平台|来源|DV版本|编码|色深|帧率|音频) ──
_PLATFORM_MAP = {
    "NF": "Netflix", "NETFLIX": "Netflix",
    "MAX": "Max", "HBOMAX": "Max", "HBO": "HBO",
    "DISNEY": "Disney+", "DISNEY+": "Disney+",
    "HULU": "Hulu",
    "AMZN": "Amazon", "AMAZON": "Amazon",
    "ATVP": "Apple TV+", "APPLETV": "Apple TV+", "APPLETV+": "Apple TV+",
    "PCOK": "Peacock", "PEACOCK": "Peacock",
    "PMTP": "Paramount+", "PARAMOUNT": "Paramount+", "PARAMOUNT+": "Paramount+",
    "CR": "Crunchyroll", "CRUNCHYROLL": "Crunchyroll",
}
_PLATFORM_RE = re.compile(
    r"\b(NF|Netflix|Max|HBOMax|HBO|Disney\+?|Hulu|AMZN|Amazon|ATVP|AppleTV\+?"
    r"|PCOK|Peacock|PMTP|Paramount\+?|CR|Crunchyroll)\b",
    re.IGNORECASE,
)
_DOVI_RE = re.compile(r"\b(?:DoVi|Dolby\s*Vision|DV)[\s._]*P(\d+)\b", re.IGNORECASE)
_CODEC_MAP = {
    "H265": "H.265", "H.265": "H.265", "HEVC": "H.265", "X265": "H.265",
    "H264": "H.264", "H.264": "H.264", "AVC": "H.264", "X264": "H.264",
    "H266": "H.266", "H.266": "H.266", "VVC": "H.266",
}
_CODEC_RE = re.compile(r"\b(H\.?26[456]|HEVC|AVC|VVC|X26[456])\b", re.IGNORECASE)
_Q_BITDEPTH_RE = re.compile(r"\b(\d+)[\-\s]?bit\b", re.IGNORECASE)
_Q_FPS_RE = re.compile(r"\b(\d{1,2}(?:\.\d+)?)\s*fps\b", re.IGNORECASE)
_AUDIO_MAP = {
    "TRUEHD": "TrueHD", "DTSHD": "DTS-HD", "DTS": "DTS",
    "DDP": "DDP", "DD+": "DDP", "EAC3": "DDP", "EAC-3": "DDP",
    "AC3": "AC3", "AC-3": "AC3", "AAC": "AAC", "FLAC": "FLAC",
    "ATMOS": "Atmos", "DD": "DD",
}
# 声道可连写(DDP5.1)或点/空格分隔(TrueHD.5.1);(?!\w) 允许紧跟数字
_AUDIO_RE = re.compile(
    r"\b(TrueHD|DTS-HD|DTS|DDP|DD\+|E-?AC-?3|AC-?3|AAC|FLAC|Atmos|DD)"
    r"[\s.]*(\d(?:[.,]\d)?)?(?!\w)",
    re.IGNORECASE,
)


def get_quality_info(text: str) -> list[str]:
    """提取 8 维画质信息列表(卡片用 | 分隔展示)。"""
    parts: list[str] = []
    tl = text.lower()

    if "2160p" in tl or "4k" in tl or "uhd" in tl:
        parts.append("4K")
    elif "1080p" in tl:
        parts.append("1080P")
    elif "720p" in tl:
        parts.append("720P")
    elif "480p" in tl:
        parts.append("480P")

    m = _PLATFORM_RE.search(text)
    if m:
        platform = _PLATFORM_MAP.get(m.group(1).upper(), m.group(1))
        if platform not in parts:
            parts.append(platform)

    if re.search(r"\bremux\b", tl):
        if "BluRay" not in parts:
            parts.append("BluRay")
        parts.append("REMUX")
    elif re.search(r"\bblu-?ray\b|\bbd\b", tl):
        parts.append("BluRay")
    elif re.search(r"\bweb-?dl\b|\bwebrip\b", tl):
        parts.append("WEB-DL")
    elif re.search(r"\bhdtv\b", tl):
        parts.append("HDTV")

    dovi = _DOVI_RE.search(text)
    if dovi:
        parts.append(f"DoVi P{dovi.group(1)}")
    elif "hdr10+" in tl:
        parts.append("HDR10+")
    elif "hdr10" in tl:
        parts.append("HDR10")
    elif "hdr" in tl:
        parts.append("HDR")
    elif "sdr" in tl:
        parts.append("SDR")

    m = _CODEC_RE.search(text)
    if m:
        parts.append(_CODEC_MAP.get(m.group(1).upper().replace(".", ""), m.group(1)))

    m = _Q_BITDEPTH_RE.search(text)
    if m:
        parts.append(f"{m.group(1)}-bit")

    m = _Q_FPS_RE.search(text)
    if m:
        parts.append(f"{m.group(1)}fps")

    m = _AUDIO_RE.search(text)
    if m:
        key = m.group(1).upper().replace("-", "").replace(".", "").replace("+", "")
        audio = _AUDIO_MAP.get(key, m.group(1))
        parts.append(f"{audio} {m.group(2)}" if m.group(2) else audio)

    return parts


# ── 单文件解析 ─────────────────────────────────────────────
def parse_filename(name: str, *, quick: bool = False) -> MediaData:
    """解析单个文件名。

    quick=True 跳过 guessit(标题/年份/发布组置空),只跑正则提季集/画质——
    guessit 单次 ~90ms,千集分享全员解析要分钟级;大分享聚合只对抽样文件跑全量。
    """
    cleaned = clean_name(name)
    g: dict = {}
    if not quick:
        try:
            from guessit import guessit  # 惰性导入:首次解析才加载(~1s)

            g = guessit(cleaned, {"expected_title": [], "type": "auto"})
        except Exception:  # noqa: BLE001 - guessit 失败时退化为清洗名
            g = {}

    def _first(v):
        if isinstance(v, list):
            return v[0] if v else None
        return v

    title = "" if quick else str(_first(g.get("title")) or cleaned).strip()
    year = None if quick else _first(g.get("year"))
    year = int(year) if year else None

    # 季集检测在原始文件名上做(cleaned 已把 '-' 归一为空格,会破坏 E01-E12 范围)
    season, ep, ep_span = extract_season_episode(name)
    g_season = _first(g.get("season"))
    g_ep = _first(g.get("episode"))
    if season is None and g_season:
        season = int(g_season)
    if ep is None and g_ep:
        ep = int(g_ep)
        ep_span = 1

    media_type = "tv" if (season is not None or ep is not None) else "movie"

    group = "" if quick else clean_release_group(str(_first(g.get("release_group")) or ""))
    return MediaData(
        title=title,
        year=year,
        media_type=media_type,
        season=season,
        episode=ep,
        episode_end=(ep + ep_span - 1) if ep is not None else None,
        quality=get_quality(name),
        source=get_source(name),
        hdr=get_hdr(name),
        quality_info=get_quality_info(name),
        raw=name,
        release_group="" if is_platform_token(group) else group,
    )


# ── 分享聚合 ───────────────────────────────────────────────
_QUALITY_RANK = {"4K / 2160P": 4, "1080P": 3, "720P": 2, "480P": 1, "": 0}
# 超过抽样的分享只对前 N 个文件跑 guessit(同一分享文件名结构一致,标题/年份由抽样定),
# 其余文件 quick 正则解析(季集/画质),千集分享聚合从分钟级降到秒级
_GUESSIT_SAMPLE = 8


def analyze_share(files: list[ShareFile]) -> AggregatedMedia | None:
    """聚合扁平文件列表为分享级媒体信息。无视频且无目录返回 None。"""
    videos = [f for f in files if f.is_video]
    dirs = [f for f in files if f.is_dir]
    candidates = videos if videos else dirs[:1]
    if not candidates:
        return None

    sample = len(candidates) > _GUESSIT_SAMPLE
    parsed = [
        parse_filename(f.name, quick=sample and i >= _GUESSIT_SAMPLE)
        for i, f in enumerate(candidates)
    ]

    title = Counter(p.title for p in parsed if p.title).most_common(1)
    title = title[0][0] if title else clean_name(candidates[0].name)

    years = [p.year for p in parsed if p.year]
    year = years[0] if years else None

    has_tv = any(p.media_type == "tv" for p in parsed)
    media_type = "tv" if (has_tv or len(videos) > 1) else "movie"

    # 季号聚合:视频文件名 + 目录名(如 "Season 4");有视频时目录只需季号,quick 即可
    all_seasons: list[int] = [p.season for p in parsed if p.season is not None]
    for d in dirs:
        ds = parse_filename(d.name, quick=bool(videos)).season
        if ds is not None:
            all_seasons.append(ds)
    seasons = sorted(set(all_seasons))
    season = seasons[0] if seasons else None

    ep_starts = [p.episode for p in parsed if p.episode is not None]
    ep_ends = [p.episode_end for p in parsed if p.episode_end is not None]

    # 按文件名季分组集号(按季渲染集数范围用)
    season_episodes: dict[int, list[int]] = {}
    for p in parsed:
        if p.season is not None and p.episode is not None:
            season_episodes.setdefault(p.season, []).append(p.episode)

    best = max(parsed, key=lambda p: _QUALITY_RANK.get(p.quality, 0))

    # TMDB ID 标注:目录名优先(最可靠),再文件名
    tmdb_id = extract_tmdb_id([d.name for d in dirs]) or extract_tmdb_id(
        [f.name for f in files]
    )

    return AggregatedMedia(
        title=title,
        year=year,
        media_type=media_type,
        season=season,
        seasons=seasons,
        season_episodes=season_episodes,
        episode_start=min(ep_starts) if ep_starts else None,
        episode_end=max(ep_ends) if ep_ends else None,
        quality=best.quality,
        source=best.source,
        hdr=best.hdr,
        release_group=best.release_group,
        quality_info=best.quality_info,
        file_count=len(videos),
        total_size=sum(f.size for f in files if not f.is_dir and f.size),
        total_episodes=len({p.episode for p in parsed if p.episode is not None}) or None,
        tmdb_id=tmdb_id,
        alt_queries=extract_cjk_annotations(
            [f.name for f in candidates] + [d.name for d in dirs]
        ),
    )


# ── 标题匹配 ───────────────────────────────────────────────
_TITLE_STRIP_RE = re.compile(r"[\s,，:：;；!！.。·\-_'\'\"()（）\[\]【】<>《》「」]")


def norm_title(s: str) -> str:
    """标题归一化:去年份 token + 剥全部全/半角标点 + 小写。"""
    s = re.sub(r"(?:19|20)\d{2}", "", (s or "").lower())
    return _TITLE_STRIP_RE.sub("", s)


def title_match(query: str, candidate_titles: list[str]) -> bool:
    """归一化后相等或互相包含(≥4 字符防短词误匹配)。"""
    q = norm_title(query)
    if not q:
        return False
    for t in candidate_titles:
        c = norm_title(t)
        if c and (q == c or (len(q) >= 4 and q in c) or (len(c) >= 4 and c in q)):
            return True
    return False
