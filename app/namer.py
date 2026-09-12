"""目标文件名生成(处理段):**沿用原项目(mediapush)的命名规则**。

模板(刮削向,变量缺失时连同分隔点一起折叠):
- 电影:`{title} ({year}) - {quality}{-team} {tmdb-NNN}{ext}`
- 剧集:`{title}.{year}.SxxEyy.第NN集.{quality}{-team} {tmdb-NNN}{ext}`(集号两位补零)

画质段顺序(原项目原样):
`分辨率 · UHD · 播放平台 · WEB来源 · REMUX · HDR效果 · 视频编码 · 色深 · 帧率 · 音频`

变量来源分工(原项目语义):
- 文件名(guessit):title/year/SxxExx/WEB来源/发布组/播放平台/UHD/REMUX
- ffprobe 实测:分辨率/HDR 效果/视频编码/色深/帧率/音频编码
- TMDB(zh-CN):规范 title/year/tmdb_id

- `UHD` 仅在原文件名有独立 `uhd` token 时补
- 原文件名含 `remux` → 来源写 `BluRay` 且加 `REMUX` 段(WEB 无 remux)
- `{tmdb-NNN}` 写进文件名:Emby/飞牛 100% 刮削
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_MAX_STEM = 180          # stem 上限(给扩展名与目录留余量,规避 NTFS 260 限制)
_BAD_CHARS = re.compile(r'[<>"/\\|?*\x00-\x1f]')
_UHD_RE = re.compile(r"(?<![a-z0-9])uhd(?![a-z0-9])", re.IGNORECASE)


def sanitize_name(name: str) -> str:
    """文件名净化:冒号→全角、去掉非法字符、剥尾部点空格、超长截断保扩展名。"""
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    stem = stem.replace(":", "：")
    stem = _BAD_CHARS.sub("", stem).rstrip(" .")
    if len(stem) > _MAX_STEM:
        stem = stem[:_MAX_STEM].rstrip(" .")
    return f"{stem}.{ext}" if ext else stem


def _uhd_tag(raw_name: str) -> str:
    """原名有独立 uhd token 才补 UHD(与旧项目一致)。"""
    return "UHD" if _UHD_RE.search(raw_name or "") else ""


def _web_source(parsed_source: str, raw_name: str) -> str:
    """来源标签:原名含 remux 一律判 BluRay(REMUX 必来自原盘)。"""
    if "remux" in (raw_name or "").lower():
        return "BluRay"
    return parsed_source or ""


def _resource_type(raw_name: str) -> str:
    return "REMUX" if "remux" in (raw_name or "").lower() else ""


def quality_label(probe, media=None, raw_name: str = "") -> str:
    """画质标签:严格按原项目的顺序与来源拼装,缺失项自动折叠。"""
    from app.media import extract_platform

    probe = probe or ProbeTagsLike()
    parts = [
        getattr(probe, "resolution", ""),
        _uhd_tag(raw_name),
        extract_platform(raw_name) if raw_name else "",
        _web_source(getattr(media, "source", "") if media else "", raw_name),
        _resource_type(raw_name),
        getattr(probe, "effect", ""),
        getattr(probe, "video_codec", ""),
        getattr(probe, "bit_depth", ""),
        getattr(probe, "frame_rate", ""),
        getattr(probe, "audio_codec", ""),
    ]
    return ".".join(p for p in parts if p)


class ProbeTagsLike:
    """给 quality_label 的兜底空对象(避免导入循环与 None 判断)。"""

    resolution = effect = video_codec = bit_depth = frame_rate = audio_codec = ""


def render_name(media, details: dict | None, probe, ext: str = "", raw_name: str = "") -> str:
    """渲染目标文件名(不含目录);标题缺失或剧集无集号时返回空串交调用方拦下。"""
    from app.pan115 import strip_dup_suffix   # 惰性导入:避免 media↔namer 循环

    # 浏览器重复下载后缀("片名.mkv (1)")会让 Path(...).suffix 变成 ".mkv (1)",
    # 目标名就会带着它收尾 —— 在这里兜住,调用方不传 ext 也不会脏
    raw_name = strip_dup_suffix(raw_name or getattr(media, "title", "") or "")
    extension = ext or (Path(raw_name).suffix if "." in raw_name else ".mkv")
    probe = probe or ProbeTagsLike()
    title = (details or {}).get("title") or getattr(media, "title", "") or ""
    title = sanitize_name(str(title).strip())
    if not title:
        return ""
    year = (details or {}).get("year") or getattr(media, "year", None)
    quality = quality_label(probe, media, raw_name)
    team = getattr(media, "release_group", "") or ""
    tail = f"-{team}" if team else ""

    if getattr(media, "media_type", "movie") == "movie":
        head = f"{title} ({year})" if year else title
        name = f"{head} - {quality}" if quality else head
    else:
        season = getattr(media, "season", None)
        # episode_start 是分享聚合(AggregatedMedia)的字段;兜 episode 兼容单文件解析
        episode = getattr(media, "episode_start", None)
        if episode is None:
            episode = getattr(media, "episode", None)
        if episode is None:
            return ""                     # 剧集无集号:不猜(交人工)
        if season is None:
            season = 1                    # 有集号无季号 → S1(原项目惯例)
        se = f"S{int(season):02d}E{int(episode):02d}"
        parts = [title] + ([str(year)] if year else []) + [se, f"第{int(episode):02d}集"]
        head = ".".join(parts)
        name = f"{head}.{quality}" if quality else head

    tmdb_id = (details or {}).get("tmdb_id") or getattr(media, "tmdb_id", None)
    tmdb_tag = f" {{tmdb-{tmdb_id}}}" if tmdb_id else ""
    return sanitize_name(f"{name}{tail}{tmdb_tag}{extension}")
