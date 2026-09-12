"""Web 认证:PBKDF2 密码哈希 + HMAC 签名 token(仅标准库)。"""

from __future__ import annotations

import hashlib
import hmac
import logging
import pathlib
import secrets
import time

logger = logging.getLogger(__name__)

_PBKDF2_ITER = 120_000
# 有效期:自托管单人面板,默认给足 90 天(免去频繁登录)。想收紧就改这一个常量。
_TOKEN_TTL = 90 * 24 * 3600
_SECRET_FILE = "web_secret"

DEFAULT_ADMIN_USER = "admin"
DEFAULT_ADMIN_PASSWORD = "admin"

_SECRET: bytes | None = None


def hash_password(plain: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", plain.encode(), bytes.fromhex(salt), _PBKDF2_ITER)
    return f"{salt}${_PBKDF2_ITER}${digest.hex()}"


def verify_password(plain: str, stored: str) -> bool:
    try:
        salt, iters, digest = stored.split("$")
        calc = hashlib.pbkdf2_hmac(
            "sha256", plain.encode(), bytes.fromhex(salt), int(iters)
        )
        return hmac.compare_digest(calc.hex(), digest)
    except (ValueError, AttributeError):
        return False


def init_secret(data_dir) -> bool:
    """把签名密钥落到 `data_dir/web_secret`,**重启/重建容器后不再踢用户下线**。

    以前密钥是进程级随机 → 每次重启旧 token 全失效,用户天天被迫重登(2026-09-12 用户提)。
    落盘后 token 跨重启有效(浏览器侧本来就存在 localStorage,不受重启影响)。
    返回 True 表示复用了磁盘上的既有密钥。

    - 文件损坏/读写失败 → 退回进程随机(**不阻塞启动**),这次重启仍会掉线
    - 想「一键踢掉所有会话」:删掉该文件再重启即可(密钥变了,旧 token 全失效)
    """
    global _SECRET
    path = pathlib.Path(data_dir) / _SECRET_FILE
    try:
        if path.exists():
            data = bytes.fromhex(path.read_text(encoding="utf-8").strip())
            if len(data) >= 32:
                _SECRET = data
                return True
            logger.warning("会话密钥文件内容异常(%s),将重新生成", path)
        data = secrets.token_bytes(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data.hex(), encoding="utf-8")
        try:                                  # 尽量收紧权限(9p/Windows 上可能不支持)
            path.chmod(0o600)
        except OSError:
            pass
        _SECRET = data
        return False
    except (OSError, ValueError) as exc:
        logger.warning("会话密钥落盘失败(%s),本次运行改用进程级随机密钥:重启后需重新登录", exc)
        _SECRET = None
        return False


def _secret() -> bytes:
    """签名密钥:优先用落盘的(init_secret 载入),没有才用进程级随机。"""
    global _SECRET
    if _SECRET is None:
        _SECRET = secrets.token_bytes(32)
    return _SECRET


def create_token(username: str) -> str:
    payload = f"{username}.{int(time.time()) + _TOKEN_TTL}"
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_token(token: str) -> str | None:
    try:
        username, expires, sig = token.rsplit(".", 2)
        payload = f"{username}.{expires}"
        expect = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expect):
            return None
        if time.time() > int(expires):
            return None
        return username
    except (ValueError, AttributeError):
        return None


def is_default_password(plain_check: str, stored: str) -> bool:
    """stored 哈希是否对应出厂默认密码。"""
    return verify_password(plain_check, stored)
