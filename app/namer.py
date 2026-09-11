"""目标文件名生成(处理段):用**探测到的画质事实** + TMDB 元数据拼规范名。

模板沿用旧项目(用户的既有习惯):
- 剧集:`{title}.{year}.SxxEyy.第N集.{quality} {tmdb-ID}{ext}`
- 电影:`{title} ({year}) - {quality} {tmdb-ID}{ext}`

quality 由探测标签 + 原文件名里的平台/来源 token 组成(缺失项连同分隔点一起折叠),
**分辨率/编码/音频一律以探测为准**——这正是"探测"存在的意义:不信原始文件名。

`{tmdb-NNN}` 写进文件名,Emby/飞牛可 100% 刮削;净化规则保证跨平台可用。
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_MAX_STEM = 180          # stem 上限(留出扩展名与目录余量,规避 NTFS 260 限制)
_BAD_CHARS = re.compile(r'[<>"/\\|?*\x00-\x1f]')


def sanitize_name(name: str) -> str:
    """文件名净化:冒号→全角、去掉非法字符、剥尾部点空格、超长截断保扩展名。"""
    stem, dot, ext = name.rpartition(".")
    if not dot:                      # 没有扩展名
        stem, ext = name, ""
    stem = stem.replace(":", "：")
    stem = _BAD_CHARS.sub("", stem).rstrip(" .")
    if len(stem) > _MAX_STEM:
        stem = stem[:_MAX_STEM].rstrip(" .")
    return f"{stem}.{ext}" if ext else stem


def _dedup(parts: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        key = p.lower()
        if p and key not in seen:
            seen.add(key)
            out.append(p)
    return out


def quality_label(probe, media=None) -> str:
    """画质标签:探测标签打头(权威),再补原文件名里的平台/来源 token(不重复)。"""
    parts: list[str] = list(getattr(probe, "quality_parts", []) or [])
    if media is not None:
        for tok in list(getattr(media, "quality_info", []) or []):
            parts.append(tok)
    return ".".join(_dedup([p.strip() for p in parts if p and p.strip()]))


def render_name(media, details: dict | None, probe, ext: str = ".mkv") -> str:
    """生成目标文件名(不含目录)。识别不出标题时返回空串,由调用方拦下。"""
    title = (details or {}).get("title") or getattr(media, "title", "") or ""
    title = sanitize_name(str(title).strip())
    if not title:
        return ""
    year = (details or {}).get("year") or getattr(media, "year", None)
    quality = quality_label(probe, media)
    tmdb_id = (details or {}).get("tmdb_id") or getattr(media, "tmdb_id", None)
    tmdb_tag = f" {{tmdb-{tmdb_id}}}" if tmdb_id else ""

    is_tv = getattr(media, "media_type", "movie") == "tv" or getattr(media, "season", None)
    if is_tv:
        season = getattr(media, "season", None) or (getattr(media, "seasons", None) or [None])[0]
        episode = getattr(media, "episode_start", None)
        if season is None or episode is None:
            return ""                       # 剧集缺季集号:不猜(交人工)
        bits = [f"{title}.{year}" if year else title, f"S{int(season):02d}E{int(episode):02d}",
                f"第{int(episode)}集"]
        if quality:
            bits.append(quality)
        return sanitize_name(".".join(bits) + tmdb_tag + ext)

    head = f"{title} ({year})" if year else title
    tail = f" - {quality}" if quality else ""
    return sanitize_name(f"{head}{tail}{tmdb_tag}" + ext)
