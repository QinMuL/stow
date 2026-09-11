"""媒体探测(ffprobe):只取"命名需要的画质标签"。

照旧项目语义(不做全量元数据清点),也顺便取清洗阶段要用的校验字段:
- 分辨率:按**最大边**归一(宽幅电影高度不达标按宽度判):≥3200→2160p / ≥1700→1080p / ≥1100→720p
- 效果:Dolby Vision 优先 → `DoVi P{n}`;否则 smpte2084→HDR10、arib-std-b67→HDR Vivid;其余 SDR(不标)
- 色深:pix_fmt 里 **≥10 才标**(10bit/12bit)
- 视频编码:h264→H.264 / hevc→H.265 / av1→AV1 / vp9 / mpeg2 / vc1 …
- 音频:eac3→DDP / ac3→DD / truehd→TrueHD / dts→DTS / aac / flac / opus …

探测失败(ffprobe 缺失/文件损坏)→ 返回空标签,**不阻塞链路**(与旧项目一致)。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_VIDEO_CODEC = {
    "h264": "H.264", "hevc": "H.265", "h265": "H.265", "av1": "AV1", "vp9": "VP9",
    "mpeg2video": "MPEG2", "mpeg4": "MPEG4", "vc1": "VC-1", "wmv3": "WMV3",
}
_AUDIO_CODEC = {
    "eac3": "DDP", "ac3": "DD", "truehd": "TrueHD", "dts": "DTS", "aac": "AAC",
    "flac": "FLAC", "opus": "Opus", "mp3": "MP3", "pcm_s16le": "PCM", "pcm_bluray": "PCM",
}


@dataclass
class ProbeTags:
    """画质标签 + 清洗校验字段。"""

    resolution: str = ""
    effect: str = ""
    bit_depth: str = ""
    video_codec: str = ""
    audio_codec: str = ""
    video_tracks: int = 0
    audio_tracks: int = 0
    duration: float = 0.0
    raw: dict = field(default_factory=dict)   # ffprobe 原始 JSON(清洗阶段复用)

    @property
    def quality_parts(self) -> list[str]:
        return [p for p in (self.resolution, self.effect, self.bit_depth,
                            self.video_codec, self.audio_codec) if p]

    def ok(self) -> bool:
        """探测是否拿到了可用信息(至少认出分辨率或编码)。"""
        return bool(self.resolution or self.video_codec or self.audio_codec)


def tags_from_ffprobe(data: dict) -> ProbeTags:
    """ffprobe JSON → ProbeTags(纯函数,便于单测)。"""
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    videos = [s for s in streams if s.get("codec_type") == "video"]
    audios = [s for s in streams if s.get("codec_type") == "audio"]

    t = ProbeTags(
        video_tracks=len(videos),
        audio_tracks=len(audios),
        duration=float(fmt.get("duration") or 0.0),
        raw=data,
    )
    if not videos:
        return t
    v = videos[0]

    # 分辨率:最大边归一
    width, height = int(v.get("width") or 0), int(v.get("height") or 0)
    longest = max(width, height)
    if longest >= 3200:
        t.resolution = "2160p"
    elif longest >= 1700:
        t.resolution = "1080p"
    elif longest >= 1100:
        t.resolution = "720p"

    # 效果:DV 优先(DV 不叠 HDR10 后缀)
    has_dv = any(
        str(sd.get("side_data_type", "")).lower().startswith("dovi")
        for sd in (v.get("side_data_list") or [])
    )
    transfer = str(v.get("color_transfer") or "").lower()
    if has_dv:
        profile = next(
            (sd.get("dv_profile") for sd in (v.get("side_data_list") or [])
             if str(sd.get("side_data_type", "")).lower().startswith("dovi")
             and sd.get("dv_profile")), None,
        )
        t.effect = f"DoVi P{profile}" if profile else "DoVi"
    elif transfer == "smpte2084":
        t.effect = "HDR10"
    elif transfer == "arib-std-b67":
        t.effect = "HDR Vivid"

    # 色深:≥10 才标
    pix_fmt = str(v.get("pix_fmt") or "")
    if pix_fmt:
        import re

        m = re.match(r"^yuv[a-z0-9]*?(\d{2})[bl]e?$", pix_fmt)
        if m and int(m.group(1)) >= 10:
            t.bit_depth = f"{int(m.group(1))}bit"
        elif pix_fmt.startswith(("p10", "yuv420p10", "yuv422p10", "yuv444p10")):
            t.bit_depth = "10bit"

    t.video_codec = _VIDEO_CODEC.get(str(v.get("codec_name") or "").lower(), "")
    if audios:
        t.audio_codec = _AUDIO_CODEC.get(str(audios[0].get("codec_name") or "").lower(), "")
    return t


async def probe_file(path: str, *, timeout: float = 60.0) -> ProbeTags:
    """跑 ffprobe 取标签;失败返回空标签(调用方降级,不阻塞)。"""
    args = ["ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_streams", "-show_format", "-show_chapters", str(path)]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except FileNotFoundError:
        logger.warning("找不到 ffprobe,跳过媒体探测(容器需装 ffmpeg)")
        return ProbeTags()
    except TimeoutError:
        logger.warning("ffprobe 超时(%ss):%s", timeout, path)
        return ProbeTags()
    except Exception as exc:  # noqa: BLE001
        logger.warning("ffprobe 执行失败(%s):%s", path, exc)
        return ProbeTags()
    if proc.returncode != 0:
        logger.warning("ffprobe 返回 %s(%s):%s", proc.returncode, path,
                       (err or b"")[:120].decode("utf-8", "replace"))
        return ProbeTags()
    try:
        return tags_from_ffprobe(json.loads(out or b"{}"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("ffprobe 输出解析失败(%s):%s", path, exc)
        return ProbeTags()
