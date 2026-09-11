"""ed2k 哈希与链接生成(处理段产出)。

规范(旧项目实测结论,详见 HANDOFF):
- 分块 **9_728_000 字节**(eMule 标准,不是 9500KB);**无 AICH**,只算 eD2K 根哈希
- 哈希算法 MD4:块哈希拼接后再 MD4;单块文件直接取该块哈希;空文件取 MD4("")
- 性能靠 pycryptodome 的 C 实现(纯 Python 回退慢两个数量级)→ 缺失时明确报错,不静默降级
- 链接:`ed2k://|file|<文件名>|<字节数>|<32位小写hex>|/`;**文件名里含 `|` 或换行会破坏链接语义**
  → 生成前净化(替换为空格),并把净化动作记日志

大文件走线程池单线程执行(核心是 I/O + MD4,逐块丢 executor 的调度开销比哈希本身还大)。
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

ED2K_CHUNK = 9_728_000   # eMule 标准分块
_READ_BUF = 32 * 1024 * 1024

try:  # C 实现(必需:纯 Python 对 GB 级文件不可用)
    from Crypto.Hash import MD4 as _MD4

    def _md4(data: bytes) -> bytes:
        return _MD4.new(data).digest()

except ImportError:  # pragma: no cover - 部署里必须装 pycryptodome
    _MD4 = None

    def _md4(data: bytes) -> bytes:
        raise RuntimeError("缺少 pycryptodome,无法计算 ed2k 哈希(纯 Python 实现对大文件不可用)")


class Ed2kError(Exception):
    """哈希计算失败。"""


def _hash_file_sync(path: str) -> tuple[int, str]:
    """同步计算 (文件大小, 根哈希 hex);流式读取,不整文件入内存。

    根哈希语义(eMule):分块 MD4 → **块哈希列表再 MD4**;
    空文件取 MD4("");**单块文件直接取该块哈希**(不要再套一层)。
    """
    if _MD4 is None:  # pragma: no cover
        raise Ed2kError("缺少 pycryptodome,无法计算 ed2k 哈希")
    digests: list[bytes] = []
    chunk = _MD4.new()
    chunk_len = 0
    size = 0
    with open(path, "rb") as f:
        while True:
            block = f.read(_READ_BUF)
            if not block:
                break
            size += len(block)
            pos = 0
            while pos < len(block):
                take = min(ED2K_CHUNK - chunk_len, len(block) - pos)
                chunk.update(block[pos:pos + take])
                chunk_len += take
                pos += take
                if chunk_len == ED2K_CHUNK:
                    digests.append(chunk.digest())
                    chunk = _MD4.new()
                    chunk_len = 0
    if chunk_len:
        digests.append(chunk.digest())
    if not digests:
        return 0, _MD4.new(b"").digest().hex()
    if len(digests) == 1:
        return size, digests[0].hex()
    return size, _MD4.new(b"".join(digests)).digest().hex()


async def ed2k_hash_file(path: str, *, pool=None) -> tuple[int, str]:
    """异步算哈希:丢线程池执行,避免阻塞事件循环。返回 (大小, 根哈希)。"""
    import asyncio

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(pool, _hash_file_sync, path)


_BAD_NAME_CHARS = re.compile(r"[|\r\n]")


def sanitize_ed2k_name(name: str) -> str:
    """ed2k 链接里的文件名净化:去掉会破坏链接语义的字符(`|` 与换行)。"""
    cleaned = _BAD_NAME_CHARS.sub(" ", name).strip()
    if cleaned != name:
        logger.warning("ed2k 文件名含非法字符(`|`/换行),已净化:%r → %r", name, cleaned)
    return cleaned


def ed2k_uri(file_name: str, size_bytes: int, root_hash_hex: str) -> str:
    """拼 ed2k 链接(文件名净化后原样拼接,不做 URL 编码 —— 与解析端正则一致)。"""
    return f"ed2k://|file|{sanitize_ed2k_name(file_name)}|{size_bytes}|{root_hash_hex.lower()}|/"


