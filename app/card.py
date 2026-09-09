"""卡片渲染引擎:caption(≤1024 智能截断)/ text(≤4096)的 HTML 组装,无投递逻辑。

caption 结构:标题区 + 画质区块 + 季集区块 + 链接 + 文件清单 + 简介;
超限按优先级截断:简介→二分减文件项数→去简介→去文件清单。
可展开模块用 <blockquote expandable>,默认折叠点击展开。
"""

from __future__ import annotations

import html
import re

from app.media import AggregatedMedia
from app.pan115 import ShareFile, ShareLink, fmt_size, share_url

_CAPTION_LIMIT = 1024
_TEXT_LIMIT = 4096
_ELLIPSIS = "…"

TMDB_SITE = "https://www.themoviedb.org"

# ISO 3166-1 国家代码 → (国旗 emoji, 中文名)
_COUNTRY_MAP = {
    "US": ("🇺🇸", "美国"), "CN": ("🇨🇳", "中国大陆"), "HK": ("🇭🇰", "中国香港"),
    "TW": ("🇹🇼", "中国台湾"), "JP": ("🇯🇵", "日本"), "KR": ("🇰🇷", "韩国"),
    "GB": ("🇬🇧", "英国"), "FR": ("🇫🇷", "法国"), "DE": ("🇩🇪", "德国"),
    "IN": ("🇮🇳", "印度"), "CA": ("🇨🇦", "加拿大"), "AU": ("🇦🇺", "澳大利亚"),
    "ES": ("🇪🇸", "西班牙"), "IT": ("🇮🇹", "意大利"), "RU": ("🇷🇺", "俄罗斯"),
    "TH": ("🇹🇭", "泰国"), "MX": ("🇲🇽", "墨西哥"), "BR": ("🇧🇷", "巴西"),
    "NL": ("🇳🇱", "荷兰"), "SE": ("🇸🇪", "瑞典"), "DK": ("🇩🇰", "丹麦"),
    "NO": ("🇳🇴", "挪威"), "FI": ("🇫🇮", "芬兰"), "PL": ("🇵🇱", "波兰"),
    "TR": ("🇹🇷", "土耳其"), "AE": ("🇦🇪", "阿联酋"), "SA": ("🇸🇦", "沙特"),
    "EG": ("🇪🇬", "埃及"), "ZA": ("🇿🇦", "南非"), "NG": ("🇳🇬", "尼日利亚"),
    "AR": ("🇦🇷", "阿根廷"), "CL": ("🇨🇱", "智利"), "CO": ("🇨🇴", "哥伦比亚"),
    "BE": ("🇧🇪", "比利时"), "CH": ("🇨🇭", "瑞士"), "AT": ("🇦🇹", "奥地利"),
    "IE": ("🇮🇪", "爱尔兰"), "PT": ("🇵🇹", "葡萄牙"), "GR": ("🇬🇷", "希腊"),
    "CZ": ("🇨🇿", "捷克"), "HU": ("🇭🇺", "匈牙利"), "RO": ("🇷🇴", "罗马尼亚"),
    "IL": ("🇮🇱", "以色列"), "ID": ("🇮🇩", "印度尼西亚"), "MY": ("🇲🇾", "马来西亚"),
    "SG": ("🇸🇬", "新加坡"), "PH": ("🇵🇭", "菲律宾"), "VN": ("🇻🇳", "越南"),
    "PK": ("🇵🇰", "巴基斯坦"), "BD": ("🇧🇩", "孟加拉"), "IR": ("🇮🇷", "伊朗"),
    "KZ": ("🇰🇿", "哈萨克斯坦"), "UA": ("🇺🇦", "乌克兰"), "IS": ("🇮🇸", "冰岛"),
    "NZ": ("🇳🇿", "新西兰"), "PE": ("🇵🇪", "秘鲁"), "VE": ("🇻🇪", "委内瑞拉"),
    "JO": ("🇯🇴", "约旦"), "LB": ("🇱🇧", "黎巴嫩"), "IQ": ("🇮🇶", "伊拉克"),
    "KH": ("🇰🇭", "柬埔寨"), "MM": ("🇲🇲", "缅甸"), "LK": ("🇱🇰", "斯里兰卡"),
}

# TMDB 状态 → 中文
_STATUS_MAP = {
    "Returning Series": "连载中",
    "Ended": "已完结",
    "Canceled": "已取消",
    "In Production": "制作中",
    "Planned": "计划中",
    "Pilot": "试播集",
    "Released": "已上映",
    "Rumored": "传闻中",
    "Post Production": "后期制作中",
}


def _esc(s) -> str:
    return html.escape(str(s or ""), quote=False)


def _truncate(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    return s[: max(0, limit - len(_ELLIPSIS))] + _ELLIPSIS


# hashtag 清洗:仅保留单词字符(Telegram 的 # 遇标点会断开)
_TAG_CLEAN_RE = re.compile(r"[^\w]+", re.UNICODE)


def _tag_name(title: str) -> str:
    return _TAG_CLEAN_RE.sub("", title or "")


def tmdb_url(details: dict) -> str:
    kind = "movie" if details.get("media_type") == "movie" else "tv"
    return f"{TMDB_SITE}/{kind}/{details.get('tmdb_id')}"


def _pseudo_details(media: AggregatedMedia) -> dict:
    """未匹配 TMDB 时的占位详情(仅文件名信息)。"""
    return {
        "tmdb_id": None, "media_type": media.media_type, "title": media.title,
        "year": media.year, "release_date": "", "overview": "", "genres": [],
        "vote_average": 0, "vote_count": 0, "cast": [], "countries": [],
        "runtime": None, "status": "", "seasons": [], "number_of_episodes": 0,
    }


# ── 季集范围合并 ───────────────────────────────────────────
def _merge_ranges(eps: list[int]) -> list[tuple[int, int]]:
    """连续集号合并成范围 [(start, end), ...]。"""
    if not eps:
        return []
    eps = sorted(set(eps))
    ranges = [(eps[0], eps[0])]
    for ep in eps[1:]:
        if ep == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], ep)
        else:
            ranges.append((ep, ep))
    return ranges


def _format_ranges(ranges: list[tuple[int, int]]) -> str:
    """E01-E05、E07、E10-E12"""
    parts = []
    for start, end in ranges:
        parts.append(f"E{start:02d}" if start == end else f"E{start:02d}-E{end:02d}")
    return "、".join(parts)


def _format_season_episodes(season_episodes: dict[int, list[int]]) -> str:
    """S01 E01-E61 | S02 E62-E77 | ..."""
    parts = []
    for season in sorted(season_episodes.keys()):
        parts.append(f"S{season:02d} {_format_ranges(_merge_ranges(season_episodes[season]))}")
    return " | ".join(parts)


def _reallocate_by_tmdb(all_eps: list[int], tmdb_seasons: list[dict]) -> dict[int, list[int]]:
    """用 TMDB 季的 episode_count 把全局集号(S01Exxx)分配到对应季。

    适用:文件名只有 S01 全局集号,但 TMDB 有多季(航海王等长篇)。
    """
    season_ranges: dict[int, tuple[int, int]] = {}
    ep_start = 1
    for s in sorted(tmdb_seasons, key=lambda x: x.get("season") or 0):
        season, ec = s.get("season"), s.get("episode_count") or 0
        if season is not None and season > 0 and ec > 0:
            season_ranges[season] = (ep_start, ep_start + ec - 1)
            ep_start += ec
    if not season_ranges:
        return {}
    result: dict[int, list[int]] = {}
    for ep in all_eps:
        for season, (lo, hi) in season_ranges.items():
            if lo <= ep <= hi:
                result.setdefault(season, []).append(ep)
                break
    return result


# ── 内容区块 ───────────────────────────────────────────────
def _render_head(details: dict, media: AggregatedMedia) -> str:
    """标题区:片名 + 评分/年份/地区/类型/主演/体积/标签/日期/时长。"""
    title = _esc(details.get("title") or media.title or "未识别资源")
    is_movie = (details.get("media_type") or media.media_type) == "movie"
    lines = [f"{'📽️ 电影' if is_movie else '🎞️ 剧集'}：<b>{title}</b>"]

    if details.get("tmdb_id"):
        lines.append(f"🆔 TMDB：{details['tmdb_id']}")
    else:
        lines.append("<i>TMDB 未匹配，信息来自文件名</i>")

    vote = details.get("vote_average") or 0
    if vote:
        vote_s = f"✨ 评分：{round(float(vote), 1)}"
        if details.get("vote_count"):
            vote_s += f" · {details['vote_count']}票"
        lines.append(vote_s)

    year = details.get("year") or media.year
    if year:
        lines.append(f"⏳ 年份：{year}")

    countries = details.get("countries") or []
    if countries:
        parts = []
        for code in countries[:3]:
            flag, zh = _COUNTRY_MAP.get(code, ("", code))
            parts.append(f"{flag} {zh}" if flag else zh)
        lines.append(f"🌐 地区：{' / '.join(parts)}")

    genres = " / ".join(details.get("genres", [])[:3])
    if genres:
        lines.append(f"🔮 类型：{_esc(genres)}")

    cast = details.get("cast") or []
    if cast:
        lines.append(f"🎭 主演：{_esc(' / '.join(cast[:5]))}")

    if media.total_size:
        lines.append(f"💾 体积：{fmt_size(media.total_size)}")

    tag = _tag_name(details.get("title") or media.title or "")
    if tag:
        lines.append(f"🏷️ 标签：#{_esc(tag)}")

    rd = details.get("release_date")
    if rd:
        label = "上映" if details.get("media_type") == "movie" else "首播"
        lines.append(f"📅 {label} {_esc(rd)}")

    if details.get("media_type") == "movie" and details.get("runtime"):
        lines.append(f"⏱️ 时长：{_esc(details['runtime'])} 分钟")

    return "\n".join(lines)


def _quality_line(media: AggregatedMedia) -> str:
    """画质信息:优先 8 维 quality_info,兜底 quality/hdr/source。"""
    if media.quality_info:
        return " | ".join(media.quality_info)
    parts = [p for p in (media.quality, media.hdr, media.source) if p]
    return " ".join(parts)


def _render_quality_block(media: AggregatedMedia) -> str:
    """画质模块:💿 行;全空返回空串。"""
    q = _quality_line(media)
    if not q:
        return ""
    return f"<blockquote>💿 画质：{_esc(q)}</blockquote>"


def _render_season_block(details: dict, media: AggregatedMedia) -> str:
    """季集模块(TV):🗂️ 季集 / 📋 按季集数范围 / ⚙️ 状态;电影返回空。"""
    if details.get("media_type") != "tv":
        return ""

    seasons_list = media.seasons or []
    actual = media.file_count
    file_season_eps = media.season_episodes or {}
    tmdb_seasons = details.get("seasons") or []

    if len(seasons_list) > 1:
        season_str = f"S{seasons_list[0]:02d}-S{seasons_list[-1]:02d}"
    elif media.season is not None:
        season_str = "S00 特别篇" if media.season == 0 else f"S{media.season:02d}"
    elif seasons_list:
        season_str = f"S{seasons_list[0]:02d}"
    else:
        season_str = ""

    # TMDB 总集数:只统计分享中包含的季
    if len(seasons_list) > 1:
        season_set = set(seasons_list)
        tmdb_season_nums = {s.get("season") for s in tmdb_seasons}
        if season_set <= tmdb_season_nums:
            tmdb_eps = sum(
                s.get("episode_count") or 0
                for s in tmdb_seasons if s.get("season") in season_set
            )
        else:
            tmdb_eps = details.get("number_of_episodes") or 0
    elif media.season is None:
        tmdb_eps = details.get("number_of_episodes") or 0
    else:
        tmdb_eps = 0
        for s in tmdb_seasons:
            if s.get("season") == media.season:
                tmdb_eps = s.get("episode_count") or 0
                break
        # S00(特别篇)无 TMDB 数据时不回退整剧总集数
        if not tmdb_eps and media.season != 0:
            tmdb_eps = details.get("number_of_episodes") or 0

    lines: list[str] = []
    if season_str:
        if tmdb_eps:
            lines.append(f"🗂️ 季集：{season_str} 共{tmdb_eps}集（{actual}个文件）")
        elif actual:
            lines.append(f"🗂️ 季集：{season_str}（{actual}个文件）")
        else:
            lines.append(f"🗂️ 季集：{season_str}")

    # 集数行:仅 S01(可能默认标注)+ 全局集号时按 TMDB 重分配到各季
    final_eps = file_season_eps
    if (
        len(file_season_eps) == 1
        and 0 not in file_season_eps
        and 1 in file_season_eps
        and len(tmdb_seasons) > 1
    ):
        reallocated = _reallocate_by_tmdb(next(iter(file_season_eps.values())), tmdb_seasons)
        if reallocated:
            final_eps = reallocated

    if final_eps:
        ep_str = _format_season_episodes(final_eps)
        if ep_str:
            lines.append(f"📋 集数：{ep_str}")
    elif media.episode_start is not None:
        if media.episode_end and media.episode_end > media.episode_start:
            ep_range = f"E{media.episode_start:02d}-E{media.episode_end:02d}"
        else:
            ep_range = f"E{media.episode_start:02d}"
        prefix = f"{season_str} " if season_str else ""
        lines.append(f"📋 集数：{prefix}{ep_range}")

    status_raw = details.get("status") or ""
    if status_raw:
        lines.append(f"⚙️ 状态：{_esc(_STATUS_MAP.get(status_raw, status_raw))}")

    if not lines:
        return ""
    return "<blockquote>" + "\n".join(lines) + "</blockquote>"


def _render_footer(link: ShareLink) -> str:
    """链接区:115 网盘模块(明文完整链接,含访问码参数)。"""
    return f"<blockquote>🔗 115 网盘\n<code>{_esc(share_url(link))}</code></blockquote>"


# ── 文件清单自然排序 ───────────────────────────────────────
_NATURAL_SPLIT = re.compile(r"(\d+)")
_SEASON_DIR_RE = re.compile(r"^season\s*(\d+)$", re.IGNORECASE)
_TMDB_DIR_RE = re.compile(r"\{tmdb-\d+\}", re.IGNORECASE)


def _natural_key(name: str) -> list:
    return [int(t) if t.isdigit() else t.lower() for t in _NATURAL_SPLIT.split(name)]


def _file_sort_key(f: ShareFile) -> tuple:
    """目录在前 → {tmdb-} 父目录 → 普通目录 → Season 目录按季号 → 文件自然序。"""
    name = f.name.strip()
    dir_first = 0 if f.is_dir else 1
    if _TMDB_DIR_RE.search(name):
        return (dir_first, -1, _natural_key(name))
    m = _SEASON_DIR_RE.match(name)
    if m:
        return (dir_first, 1, int(m.group(1)))
    return (dir_first, 0, _natural_key(name))


def _render_files_block(
    files: list[ShareFile],
    *,
    max_items: int | None = None,
    files_sorted: list[ShareFile] | None = None,
) -> str:
    """文件清单可展开模块。max_items 限制显示前 N 项。"""
    files_sorted = files_sorted if files_sorted is not None else sorted(files, key=_file_sort_key)
    shown = files_sorted if max_items is None else files_sorted[:max_items]
    lines = [f"<blockquote expandable>📁 分享内容（{len(files)} 项） · 点击展开"]
    for f in shown:
        line = f"{'📁' if f.is_dir else '📄'} {_esc(f.name)}"
        if not f.is_dir and f.size:
            line += f"  ·  {fmt_size(f.size)}"
        lines.append(line)
    if max_items is not None and max_items < len(files):
        lines.append(f"… 共 {len(files)} 项，已显示前 {max_items} 项")
    lines.append("</blockquote>")
    return "\n".join(lines)


def _render_overview_block(overview: str, *, limit: int | None = None) -> str:
    """简介可展开模块。"""
    text = _truncate(overview, limit) if limit else overview
    return f"<blockquote expandable>📝 简介 · 点击展开\n{text}\n</blockquote>"


# ── 智能截断(caption ≤1024) ──────────────────────────────
def _fit_caption(
    head: str,
    quality_block: str,
    season_block: str,
    footer: str,
    files: list[ShareFile] | None,
    overview: str,
    limit: int = _CAPTION_LIMIT,
) -> str:
    """组装 caption,超限按优先级截断:简介 → 减文件项数(二分) → 去简介 → 去文件清单。"""
    SEP = "\n\n"

    def build(fb: str, ob: str) -> str:
        parts = [head]
        if quality_block:
            parts.append(quality_block)
        if season_block:
            parts.append(season_block)
        if fb:
            parts.append(fb)
        if ob:
            parts.append(ob)
        parts.append(footer)
        return SEP.join(parts)

    # 排序一次:完整渲染与截断二分复用
    files_sorted = sorted(files, key=_file_sort_key) if files else None

    def render_fb(n: int | None = None) -> str:
        return _render_files_block(files, max_items=n, files_sorted=files_sorted)

    fb = render_fb() if files else ""
    ob = _render_overview_block(overview) if overview else ""

    body = build(fb, ob)
    if len(body) <= limit:
        return body

    if overview:
        ob = _render_overview_block(overview, limit=200)
        body = build(fb, ob)
        if len(body) <= limit:
            return body

    if files:
        fb = ""  # 兜底:n=1 仍超限时去掉整个文件清单
        if len(build(render_fb(1), ob)) <= limit:
            lo, hi = 1, len(files)
            while lo <= hi:  # 二分找最大 n 使 caption ≤ limit
                mid = (lo + hi) // 2
                fb_mid = render_fb(mid)
                if len(build(fb_mid, ob)) <= limit:
                    fb = fb_mid
                    lo = mid + 1
                else:
                    hi = mid - 1
        body = build(fb, ob)
        if len(body) <= limit:
            return body

    if overview:
        body = build(fb, "")
        if len(body) <= limit:
            return body

    # 兜底:head + quality + season + footer
    parts = [head]
    if quality_block:
        parts.append(quality_block)
    if season_block:
        parts.append(season_block)
    parts.append(footer)
    return SEP.join(parts)


# ── 渲染入口 ───────────────────────────────────────────────
def render_caption(
    media: AggregatedMedia,
    details: dict | None,
    link: ShareLink,
    files: list[ShareFile] | None = None,
) -> str:
    """海报下方 caption(≤1024)。details 为 TMDB 归一化详情,未匹配传 None。"""
    details = details or _pseudo_details(media)
    return _fit_caption(
        _render_head(details, media),
        _render_quality_block(media),
        _render_season_block(details, media),
        _render_footer(link),
        files,
        _esc(details.get("overview") or ""),
        _CAPTION_LIMIT,
    )


def render_text(
    media: AggregatedMedia,
    details: dict | None,
    link: ShareLink,
    files: list[ShareFile] | None = None,
) -> str:
    """无海报时的完整消息(≤4096)。与 caption 同一截断阶梯,限额更高。"""
    details = details or _pseudo_details(media)
    return _fit_caption(
        _render_head(details, media),
        _render_quality_block(media),
        _render_season_block(details, media),
        _render_footer(link),
        files,
        _esc(details.get("overview") or ""),
        _TEXT_LIMIT,
    )
