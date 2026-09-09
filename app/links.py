"""统一链接解析:115 分享 + ed2k,按 provider 路由(语义参考旧项目 link_parser)。

- 115 三形态:URL(带码/尾 token)/ 裸码 / 正文访问码(详见 pan115)
- ed2k:ed2k://|file|<文件名>|<字节数>|<hash>|...|/ —— 名称/大小全内嵌,零网络
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.pan115 import _115_URL_RE, _BARE_CODE_RE, _BODY_CODE_RE, _PWD_RE, _TAIL_TOKEN_RE

# ed2k 单文件链接:文件名 / 字节数 / 32 位 hash
_ED2K_RE = re.compile(
    r"ed2k://\|file\|([^|]+)\|(\d+)\|([0-9A-Fa-f]{32})\|[^ ]*?/",
    re.IGNORECASE,
)


@dataclass
class ParsedLink:
    provider: str  # "115" | "ed2k"
    code: str      # 115: 分享码;ed2k: 完整链接 URL
    url: str       # 展示/投递用完整链接
    password: str | None = None
    file_hash: str = ""  # ed2k 文件 hash(去重 key)

    @property
    def key(self) -> str:
        """去重 key:115 用分享码,ed2k 用文件 hash(链接尾参可变,hash 才稳定)。"""
        return self.file_hash if self.provider == "ed2k" else self.code

    @property
    def dedup_display(self) -> str:
        return self.file_hash[:16] if self.provider == "ed2k" else self.code


def ed2k_file(url: str) -> tuple[str, int, str] | None:
    """解析 ed2k 链接 → (文件名, 字节数, hash);格式无效返回 None。"""
    m = _ED2K_RE.search(url)
    if not m:
        return None
    return m.group(1), int(m.group(2)), m.group(3)


def _from_115_match(m: re.Match) -> ParsedLink:
    code = m.group(1)
    query = m.group("query") or ""
    pwd: str | None = None
    if pm := _PWD_RE.search(query):
        pwd = pm.group(1)
    elif query and _TAIL_TOKEN_RE.fullmatch(query):
        pwd = query  # 尾 token 形式:?abcd1234(无 = 的短访问码)
    url = f"https://115.com/s/{code}" + (f"?password={pwd}" if pwd else "")
    return ParsedLink("115", code, url, pwd)


def _fill_body_password(links: list[ParsedLink], text: str) -> None:
    """115 链接未带访问码时,用正文提取的访问码填充(懒提取,已有码不覆盖)。"""
    if not any(p.provider == "115" and p.password is None for p in links):
        return
    m = _BODY_CODE_RE.search(text)
    if not m:
        return
    for p in links:
        if p.provider == "115" and p.password is None:
            p.password = m.group(1)
            p.url = f"https://115.com/s/{p.code}?password={p.password}"


def parse_all(text: str) -> list[ParsedLink]:
    """提取文本中全部 115 + ed2k 链接,按出现顺序返回(按去重 key 去重)。"""
    if not text:
        return []
    found: list[tuple[int, ParsedLink]] = []
    for m in _ED2K_RE.finditer(text):
        found.append((m.start(), ParsedLink("ed2k", m.group(0), m.group(0), file_hash=m.group(3))))
    for m in _115_URL_RE.finditer(text):
        found.append((m.start(), _from_115_match(m)))
    found.sort(key=lambda x: x[0])
    seen: set[str] = set()
    result: list[ParsedLink] = []
    for _, p in found:
        if p.key in seen:
            continue
        seen.add(p.key)
        result.append(p)
    _fill_body_password(result, text)
    return result


def parse_one(text: str) -> ParsedLink | None:
    """解析单链接(/push 用):ed2k 优先于裸码;含裸码 115。"""
    if not text:
        return None
    if (m := _ED2K_RE.search(text)):
        return ParsedLink("ed2k", m.group(0), m.group(0), file_hash=m.group(3))
    if (m := _115_URL_RE.search(text)):
        p = _from_115_match(m)
        _fill_body_password([p], text)
        return p
    first = text.strip().split()[0] if text.strip() else ""
    if _BARE_CODE_RE.fullmatch(first):
        p = ParsedLink("115", first, f"https://115.com/s/{first}")
        _fill_body_password([p], text)
        return p
    return None
