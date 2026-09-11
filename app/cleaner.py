"""视频元数据清洗(L1 保守档):检测 + ffmpeg remux,零重编码。

**只清三类广告类脏数据**(title/名称命中关键词才动,其余一律不碰):
1. 容器全局 tags:如 title 塞"更多资源访问 xxx.com" → ffmetadata 过滤回灌
2. 垃圾章节:章节 title 含广告/网址 → 剔除该章节,正常章节保留
3. 广告音轨/字幕轨:流 title 含广告/网址 → remux 时不映射该轨

**用户规则(2026-09-11)**:不是每个资源都清洗——只有检测到上述脏数据才清洗,
干净文件**原样不动**(连 remux 都不做)。清洗发生在**重命名之前**。

不做(明确边界,与旧项目一致):
- 画面水印/角标:烧在像素里,remux 洗不掉(重编码伤画质)
- 按语言删轨:误删风险高,仅按 title 关键词
- 附件(字体/图片):全部保留(`-map 0:t?`,字体是字幕渲染依赖)

安全:
- 清洗后校验"视频轨数一致 + 时长差 < 1.5s",不过则**删半成品、原件不动**
- remux 前后双 stat 比对:大小变了说明源文件仍在被写入 → **作废本次清洗**
- 不用 `-map_metadata -1`:实测它会连流级 title 一并清除(改用 ffmetadata 过滤回灌)
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# 广告关键词(大小写不敏感子串;保守集:只收"明确推广信号",不收"压制/字幕组"等可能正常出现的词)
DEFAULT_JUNK_KEYWORDS: tuple[str, ...] = (
    "http://", "https://", "www.", ".com", ".net", ".org", ".cc",
    ".xyz", ".top", ".icu", "promo", "trailer",
    "广告", "官网", "更多资源", "资源站", "ed2k://", "magnet:?",
    "点击", "订阅", "频道推广", "群号", "qq群",
)


class CleanError(Exception):
    """清洗失败(半成品已清理,原件未动)。"""


@dataclass
class CleanReport:
    """单文件的垃圾元数据检测结果。"""

    junk_tags: list[str] = field(default_factory=list)
    junk_chapters: list[str] = field(default_factory=list)
    junk_tracks: list[dict] = field(default_factory=list)
    kept_track_indexes: list[int] = field(default_factory=list)   # 构造 -map 用

    @property
    def has_junk(self) -> bool:
        return bool(self.junk_tags or self.junk_chapters or self.junk_tracks)

    def summary(self) -> str:
        parts: list[str] = []
        if self.junk_tags:
            parts.append(f"容器标签×{len(self.junk_tags)}")
        if self.junk_chapters:
            parts.append(f"垃圾章节×{len(self.junk_chapters)}")
        if self.junk_tracks:
            parts.append("广告轨 " + "、".join(
                f"{t['kind']}#{t['index']}" for t in self.junk_tracks))
        return ";".join(parts)


def _hit(text: str, keywords: tuple[str, ...] = DEFAULT_JUNK_KEYWORDS) -> str | None:
    low = (text or "").lower()
    for kw in keywords:
        if kw in low:
            return kw
    return None


def report_from_ffprobe(data: dict,
                        keywords: tuple[str, ...] = DEFAULT_JUNK_KEYWORDS) -> CleanReport:
    """ffprobe JSON(-show_streams -show_chapters -show_format)→ 垃圾报告(纯函数)。"""
    rpt = CleanReport()
    for key, val in (data.get("format", {}).get("tags") or {}).items():
        if _hit(str(val), keywords):
            rpt.junk_tags.append(f"{key}={str(val)[:60]}")
    for ch in data.get("chapters") or []:
        title = (ch.get("tags") or {}).get("title", "")
        if _hit(title, keywords):
            rpt.junk_chapters.append(title[:60])
    for st in data.get("streams") or []:
        if st.get("codec_type") not in ("audio", "subtitle"):
            continue
        title = (st.get("tags") or {}).get("title", "")
        if _hit(title, keywords):
            rpt.junk_tracks.append({
                "index": st.get("index", -1),
                "kind": "音轨" if st.get("codec_type") == "audio" else "字幕",
                "title": title[:60],
            })
    return rpt


def analyze(probe_raw: dict) -> CleanReport:
    """从探测结果(ProbeTags.raw)判脏——复用同一次 ffprobe 输出,不重复探测。"""
    return report_from_ffprobe(probe_raw or {})


def filter_ffmetadata(text: str, rpt: CleanReport) -> str:
    """按报告重写 ffmetadata:有垃圾容器标签则清空全局段;垃圾章节段丢弃,其余原样。"""
    lines = (text or "").splitlines()
    out = [";FFMETADATA1"]
    if not rpt.junk_tags:
        i = 1
        while i < len(lines) and lines[i].strip() not in ("[CHAPTER]", "[STREAM]"):
            if lines[i].strip():
                out.append(lines[i])
            i += 1
    i = 0
    while i < len(lines):
        if lines[i].strip() == "[CHAPTER]":
            j, block = i + 1, []
            while j < len(lines) and lines[j].strip() not in ("[CHAPTER]", "[STREAM]"):
                block.append(lines[j])
                j += 1
            title = next((ln.split("=", 1)[1] for ln in block
                          if ln.strip().lower().startswith("title=")), "")
            if not _hit(title):
                out.append("")
                out.append("[CHAPTER]")
                out.extend(block)
            i = j
        else:
            i += 1
    return "\n".join(out) + "\n"


# ── 子进程封装 ──────────────────────────────────────────────
async def _run(cmd: list[str], *, quiet: bool = True) -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL if quiet else asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
        return proc.returncode == 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("命令执行失败(%s):%s", cmd[0], exc)
        return False


async def _ffprobe_json(path: str) -> dict | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_streams", "-show_chapters", "-show_format", path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
    except Exception as exc:  # noqa: BLE001
        logger.warning("ffprobe 执行失败(%s):%s", path, exc)
        return None
    if proc.returncode != 0:
        return None
    import json

    try:
        return json.loads(out.decode("utf-8", "replace"))
    except (json.JSONDecodeError, ValueError):
        return None


def _remove(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _build_args(src: str, dst: str, rpt: CleanReport, chapter_meta: str | None) -> list[str]:
    """remux 参数(-c copy 零重编码);输出选项必须都在 -i 之后、输出文件之前。"""
    args = ["ffmpeg", "-y", "-i", src]
    if chapter_meta is not None:
        args += ["-i", chapter_meta]
    if rpt.junk_tracks:
        args += ["-map", "0:v"]
        for idx in rpt.kept_track_indexes:
            args += ["-map", f"0:{idx}"]
        args += ["-map", "0:t?"]          # 附件全保留
    else:
        args += ["-map", "0"]
    args += ["-c", "copy"]
    if chapter_meta is not None:
        # 全局 tags 与章节统一由过滤文件回灌(输入 1);流级 title 不受影响
        args += ["-map_metadata", "1", "-map_chapters", "1"]
    args.append(dst)
    return args


async def _export_filtered_meta(src: str, rpt: CleanReport, tmp_meta: str) -> str | None:
    if not await _run(["ffmpeg", "-y", "-i", src, "-f", "ffmetadata", tmp_meta]):
        return None
    try:
        raw = Path(tmp_meta).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        Path(tmp_meta).write_text(filter_ffmetadata(raw, rpt), encoding="utf-8")
    except OSError:
        return None
    return tmp_meta


async def clean_file(path: str | Path) -> tuple[bool, str]:
    """按需清洗单个文件。

    返回 (是否清洗过, 说明)。**无脏数据 → (False, "干净"):文件一个字节都不动**。
    清洗成功 → 原文件被**同名替换**(调用方随后照常重命名/算哈希)。
    失败 → 清理半成品后抛 CleanError,原文件不动。
    """
    src = str(path)
    size_before = os.path.getsize(src)
    data = await _ffprobe_json(src)
    if data is None:
        return False, "探测失败(按不清洗处理)"
    rpt = report_from_ffprobe(data)
    if not rpt.has_junk:
        return False, "干净"
    if rpt.junk_tracks:
        junk_idx = {t["index"] for t in rpt.junk_tracks}
        rpt.kept_track_indexes = [
            st["index"] for st in (data.get("streams") or [])
            if st.get("codec_type") in ("audio", "subtitle")
            and st.get("index") not in junk_idx
        ]

    p = Path(src)
    dst = str(p.with_name(f"{p.stem}.cleaned{p.suffix}"))
    tmp_meta = dst + ".chapters.meta"
    try:
        chapter_meta: str | None = None
        if rpt.junk_tags or rpt.junk_chapters:
            chapter_meta = await _export_filtered_meta(src, rpt, tmp_meta)
            if chapter_meta is None:
                logger.warning("ffmetadata 过滤失败(%s)→ 全局标签/章节原样保留兜底", src)
        if not await _run(_build_args(src, dst, rpt, chapter_meta)):
            raise CleanError("ffmpeg remux 失败")
        if os.path.getsize(src) != size_before:
            # 源文件在 remux 期间被写入(半截文件):作废本次清洗
            raise CleanError(f"源文件在清洗期间仍在变化({size_before}→{os.path.getsize(src)})")
        src_data, dst_data = await _ffprobe_json(src), await _ffprobe_json(dst)
        try:
            src_v = sum(1 for s in src_data["streams"] if s["codec_type"] == "video")
            dst_v = sum(1 for s in dst_data["streams"] if s["codec_type"] == "video")
            src_d = float(src_data["format"]["duration"])
            dst_d = float(dst_data["format"]["duration"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CleanError(f"校验探测失败:{exc}") from exc
        if src_v != dst_v or abs(src_d - dst_d) > 1.5:
            raise CleanError(
                f"校验不过(视频轨 {src_v}→{dst_v},时长 {src_d:.1f}→{dst_d:.1f}s)")
        # 校验通过:同名替换(原件删除由我们负责,保证"要么干净的新文件,要么原文件")
        os.replace(dst, src)
        logger.info("元数据清洗完成:%s(%s)", Path(src).name, rpt.summary())
        return True, rpt.summary()
    except Exception:
        _remove(dst)
        raise
    finally:
        _remove(tmp_meta)
