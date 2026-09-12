"""媒体探测(ffprobe):只取"命名需要的画质标签"。

照旧项目语义(不做全量元数据清点),也顺便取清洗阶段要用的校验字段:
- 分辨率:按**最大边**归一(宽幅电影高度不达标按宽度判):≥3200→2160p / ≥1700→1080p / ≥1100→720p
- 效果:Dolby Vision 优先 → `DoVi P{n}`;否则 smpte2084→HDR10、arib-std-b67→HDR Vivid;
  其余 → `SDR`(原项目口径:SDR 也显式写进文件名);**没有视频轨**(探测失败/纯音频)时留空不标
- 色深:pix_fmt 里 **≥10 才标**(10bit/12bit)
- 视频编码:h264→H.264 / hevc→H.265 / av1→AV1 / vp9 / mpeg2 / vc1 …
- 音频:**编码 + 声道数**(照搬原项目 `normalize_audio`):eac3→DDP / ac3→DD / truehd→TrueHD /
  aac / flac / opus …;**DTS 的 profile 优先于编码名**(DTS-HD MA / DTS-HD HRA / DTS Express /
  DTS 96/24);`pcm_*` 一律写 LPCM;声道数按 1.0/2.0/2.1/5.1/6.1/7.1 拼在后面(如 `DDP 5.1`)
  —— 漏了它,文件名里本来写着的 5.1 会被"技术标签以 ffprobe 为准"这条规则覆盖掉

探测失败(ffprobe 缺失/文件损坏)→ 返回空标签,**不阻塞链路**(与旧项目一致)。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_VIDEO_CODEC = {
    "h264": "H.264", "hevc": "H.265", "h265": "H.265", "av1": "AV1", "vp9": "VP9",
    "mpeg2video": "MPEG2", "mpeg4": "MPEG4", "vc1": "VC-1", "wmv3": "WMV3",
}
_AUDIO_CODEC = {
    "eac3": "DDP", "ac3": "DD", "truehd": "TrueHD", "dts": "DTS", "aac": "AAC",
    "flac": "FLAC", "opus": "Opus", "mp3": "MP3",
}
_PCM_RE = re.compile(r"^pcm_")          # pcm_* 一律 LPCM(不要写死几个具体格式)
# 声道数 → 标签(原项目原样;没收录的原样写数字)
_CHANNEL_LABEL = {1: "1.0", 2: "2.0", 3: "2.1", 6: "5.1", 7: "6.1", 8: "7.1"}


def normalize_audio(codec: str, profile: str, channels: int) -> str:
    """音频 codec + profile + 声道数 → 标签(**照搬原项目 `normalize_audio`**)。

    - 声道数拼在后面:`DDP 5.1` / `TrueHD 7.1` / `AAC 2.0`
    - DTS 的 profile 优先于编码名:`DTS-HD MA` / `DTS-HD HRA` / `DTS Express` / `DTS 96/24`
    - `pcm_*` 一律 `LPCM`
    - 认不出的 codec 回退**原始名**(而不是把这一段丢掉)
    """
    codec = (codec or "").strip()
    if not codec:
        return ""
    name = _AUDIO_CODEC.get(codec)
    if codec == "dts" and profile in ("DTS-HD MA", "DTS-HD HRA", "DTS Express", "DTS 96/24"):
        name = profile
    if _PCM_RE.match(codec):
        name = "LPCM"
    if not name:
        name = codec
    ch = _CHANNEL_LABEL.get(channels, str(channels) if channels else "")
    return f"{name} {ch}" if ch else name


@dataclass
class ProbeTags:
    """画质标签 + 清洗校验字段。"""

    resolution: str = ""
    effect: str = ""
    bit_depth: str = ""
    video_codec: str = ""
    audio_codec: str = ""
    frame_rate: str = ""      # 如 "25fps"(命名模板用)
    video_tracks: int = 0
    audio_tracks: int = 0
    duration: float = 0.0
    raw: dict = field(default_factory=dict)   # ffprobe 原始 JSON(清洗阶段复用)

    @property
    def quality_parts(self) -> list[str]:
        return [p for p in (self.resolution, self.effect, self.video_codec,
                            self.bit_depth, self.frame_rate, self.audio_codec) if p]

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
    else:
        # 认不出 HDR 一律标 SDR(原项目口径):与"探测失败"区分开——那条路
        # 在 not videos 时已提前返回,不会误标
        t.effect = "SDR"

    # 色深:≥10 才标
    pix_fmt = str(v.get("pix_fmt") or "")
    if pix_fmt:
        m = re.match(r"^yuv[a-z0-9]*?(\d{2})[bl]e?$", pix_fmt)
        if m and int(m.group(1)) >= 10:
            t.bit_depth = f"{int(m.group(1))}bit"
        elif pix_fmt.startswith(("p10", "yuv420p10", "yuv422p10", "yuv444p10")):
            t.bit_depth = "10bit"

    # 帧率:r_frame_rate 形如 "25/1" / "24000/1001"
    fr = str(v.get("r_frame_rate") or v.get("avg_frame_rate") or "")
    if "/" in fr:
        num, _, den = fr.partition("/")
        try:
            fps = float(num) / float(den or 1)
            if fps > 0:
                t.frame_rate = f"{fps:.3f}".rstrip("0").rstrip(".") + "fps"
        except (ValueError, ZeroDivisionError):
            pass

    t.video_codec = _VIDEO_CODEC.get(str(v.get("codec_name") or "").lower(), "")
    if audios:
        a = audios[0]
        t.audio_codec = normalize_audio(
            str(a.get("codec_name") or "").lower(),
            str(a.get("profile") or ""),
            int(a.get("channels") or 0),
        )
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
