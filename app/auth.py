"""Web 认证:PBKDF2 密码哈希 + HMAC 签名 token(仅标准库)。"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

_PBKDF2_ITER = 120_000
_TOKEN_TTL = 12 * 3600

DEFAULT_ADMIN_USER = "admin"
DEFAULT_ADMIN_PASSWORD = "admin"


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


def _secret() -> bytes:
    """签名密钥:进程级随机(重启后 token 失效,重新登录即可)。"""
    global _SECRET
    try:
        return _SECRET
    except NameError:
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
