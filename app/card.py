"""卡片渲染:HTML caption(≤1024,超长按优先级截断)。阶段二将替换为 8 维渲染引擎。"""

from __future__ import annotations

import html

from app.media import AggregatedMedia
from app.pan115 import ShareFile, ShareLink, fmt_size, share_url

_MAX_CAPTION = 1024


def _stars(rating) -> str:
    if not rating:
        return "暂无评分"
    return f"★ {rating}"


def render(
    media: AggregatedMedia,
    details: dict | None,
    link: ShareLink,
    files: list[ShareFile] | None = None,
) -> str:
    """渲染卡片 caption(HTML)。details 为 TMDB 归一化详情,未匹配时 None。"""
    parts: list[str] = []

    # 标题行:标题 (年份) [TMDB 匹配时]
    if details:
        head = f"<b>{html.escape(details['title'])}</b>"
        if details["year"]:
            head += f" ({details['year']})"
        parts.append(head)
        meta: list[str] = [_stars(details["vote_average"])]
        if details["genres"]:
            meta.append(" / ".join(details["genres"][:4]))
        if details["media_type"] == "tv":
            seg = []
            if details["number_of_seasons"]:
                seg.append(f"{details['number_of_seasons']} 季")
            if details["number_of_episodes"]:
                seg.append(f"{details['number_of_episodes']} 集")
            if seg:
                meta.append("·".join(seg))
        elif details["runtime"]:
            meta.append(f"{details['runtime']} 分钟")
        parts.append("  ".join(meta))
    else:
        head = f"<b>{html.escape(media.title or '未识别资源')}</b>"
        if media.year:
            head += f" ({media.year})"
        parts.append(head)
        parts.append("<i>TMDB 未匹配</i>")

    # 规格:画质 + 体积 + 文件数
    spec: list[str] = []
    if media.quality:
        spec.append(media.quality)
    if media.total_size:
        spec.append(fmt_size(media.total_size))
    file_names = [f.name for f in (files or []) if not f.is_dir]
    spec.append(f"{media.file_count} 个视频" if media.file_count else f"{len(files or [])} 个文件")
    if media.media_type == "tv" and media.episode_start is not None:
        if media.episode_end and media.episode_end > media.episode_start:
            rng = f"E{media.episode_start:02d}-E{media.episode_end:02d}"
        else:
            rng = f"E{media.episode_start:02d}"
        spec.append(f"S{media.season or 1:02d}{rng}")
    parts.append("  ".join(spec))

    # 概览
    if details and details["overview"]:
        ov = details["overview"]
        if len(ov) > 180:
            ov = ov[:180].rstrip() + "…"
        parts.append(f"<i>{html.escape(ov)}</i>")

    # 文件清单(可折叠)
    if file_names:
        lines = [html.escape(n) for n in file_names[:15]]
        if len(file_names) > 15:
            lines.append(f"… 共 {len(file_names)} 个")
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
