"""卡片渲染:HTML caption(≤1024,超长按优先级截断)。"""

from __future__ import annotations

import html

from app.media import MediaInfo
from app.pan115 import ShareLink, fmt_size, share_url
from app.tmdb import TmdbMatch

_MAX_CAPTION = 1024


def _stars(rating: float | None) -> str:
    if not rating:
        return "暂无评分"
    return f"★ {rating}"


def render(media: MediaInfo, match: TmdbMatch | None, link: ShareLink) -> str:
    """渲染卡片 caption(HTML)。"""
    parts: list[str] = []

    # 标题行:标题 (年份) [TMDB 匹配时]
    if match:
        head = f"<b>{html.escape(match.title)}</b>"
        if match.year:
            head += f" ({match.year})"
        parts.append(head)
        meta: list[str] = [_stars(match.rating)]
        if match.genres:
            meta.append(" / ".join(match.genres[:4]))
        if match.media_type == "tv":
            seg = []
            if match.seasons:
                seg.append(f"{match.seasons} 季")
            if match.episodes:
                seg.append(f"{match.episodes} 集")
            if seg:
                meta.append("·".join(seg))
        elif match.runtime_min:
            meta.append(f"{match.runtime_min} 分钟")
        parts.append("  ".join(meta))
    else:
        head = f"<b>{html.escape(media.title or '未识别资源')}</b>"
        if media.year:
            head += f" ({media.year})"
        parts.append(head)
        parts.append("<i>TMDB 未匹配</i>")

    # 规格:画质 + 体积 + 文件数
    spec: list[str] = []
    if media.resolution:
        spec.append(media.resolution.upper())
    if media.total_size:
        spec.append(fmt_size(media.total_size))
    spec.append(f"{len(media.video_files)} 个视频" if media.video_files else f"{media.file_count} 个文件")
    if media.is_tv and media.episodes:
        eps = sorted(media.episodes)
        rng = f"E{eps[0]:02d}-E{eps[-1]:02d}" if len(eps) > 1 else f"E{eps[0]:02d}"
        spec.append(f"S{min(media.seasons or [1]):02d}{rng}")
    parts.append("  ".join(spec))

    # 概览
    if match and match.overview:
        ov = match.overview
        if len(ov) > 180:
            ov = ov[:180].rstrip() + "…"
        parts.append(f"<i>{html.escape(ov)}</i>")

    # 文件清单(可折叠)
    if media.video_files:
        lines = [html.escape(n) for n in media.video_files[:15]]
        if len(media.video_files) > 15:
            lines.append(f"… 共 {len(media.video_files)} 个")
        parts.append(
            '<tg-spoiler>' + "\n".join(lines) + "</tg-spoiler>"
        )

    # 链接与访问码
    parts.append(f"\n🔗 {share_url(link)}")
    if link.password:
        parts.append(f"访问码 <code>{html.escape(link.password)}</code>")

    text = "\n".join(parts)
    # 超长截断:优先保头部与链接
    if len(text) > _MAX_CAPTION:
        tail = f"\n🔗 {share_url(link)}"
        keep = _MAX_CAPTION - len(tail) - 20
        text = text[:keep].rsplit("\n", 1)[0] + "\n…" + tail
    return text
