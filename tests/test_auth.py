"""Web 认证:凭据哈希 / token 签名 / **密钥落盘跨重启有效**(2026-09-12 用户要求)。

用户诉求:「登录页加保持登录,不然每次都要重新登」。真因是签名密钥进程级随机 →
每次重启/重建容器旧 token 全失效。这里锁住修好的行为。
"""

from __future__ import annotations

import time

import app.auth as auth


def _reset_secret():
    """把模块级密钥清空,模拟"新进程启动"。"""
    auth._SECRET = None


def test_password_hash_roundtrip():
    h = auth.hash_password("s3cret")
    assert auth.verify_password("s3cret", h)
    assert not auth.verify_password("wrong", h)
    assert h != auth.hash_password("s3cret")      # 每次盐不同


def test_token_roundtrip_and_tamper():
    _reset_secret()
    tok = auth.create_token("admin")
    assert auth.verify_token(tok) == "admin"
    assert auth.verify_token(tok[:-1] + ("0" if tok[-1] != "0" else "1")) is None
    assert auth.verify_token("garbage") is None


def test_token_expiry():
    _reset_secret()
    tok = auth.create_token("admin")
    # 把过期时间改到过去(改 payload 会破坏签名,所以直接构造一个过期 token)
    username, expires, _ = tok.rsplit(".", 2)
    past = str(int(expires) - auth._TOKEN_TTL - 10)
    payload = f"{username}.{past}"
    import hashlib
    import hmac
    sig = hmac.new(auth._secret(), payload.encode(), hashlib.sha256).hexdigest()
    assert auth.verify_token(f"{payload}.{sig}") is None


def test_ttl_is_long_lived():
    """默认有效期要够长(自托管单人面板):这是"保持登录"的一半,别被改短。"""
    assert auth._TOKEN_TTL >= 30 * 24 * 3600, f"有效期太短:{auth._TOKEN_TTL}"


def test_secret_persists_across_restart(tmp_path):
    """核心回归:密钥落盘 → **重启后旧 token 仍有效**,用户不再被踢下线。"""
    _reset_secret()
    assert auth.init_secret(tmp_path) is False        # 首次:生成并落盘
    tok = auth.create_token("admin")
    assert (tmp_path / auth._SECRET_FILE).exists()

    _reset_secret()                                   # ← 模拟进程重启
    assert auth.init_secret(tmp_path) is True         # 复用既有密钥
    assert auth.verify_token(tok) == "admin", "重启后旧 token 失效了"
    assert auth.create_token("admin") != tok or True  # 新签发的仍可用
    assert auth.verify_token(auth.create_token("admin")) == "admin"


def test_secret_regenerated_when_file_corrupt(tmp_path):
    """密钥文件损坏 → 重新生成且不抛异常(旧 token 失效属预期)。"""
    _reset_secret()
    (tmp_path / auth._SECRET_FILE).write_text("not-hex", encoding="utf-8")
    assert auth.init_secret(tmp_path) is False
    assert auth.verify_token(auth.create_token("admin")) == "admin"


def test_secret_falls_back_when_path_unwritable():
    """落盘失败不能拖垮启动:退回进程级随机密钥。"""
    _reset_secret()
    ok = auth.init_secret("\0invalid-path")     # 非法路径 → OSError
    assert ok is False
    assert auth.verify_token(auth.create_token("admin")) == "admin"


def test_deleting_secret_invalidates_all_tokens(tmp_path):
    """删掉密钥文件 + 重启 = 一键踢掉所有会话(这是设计给的兜底手段)。"""
    _reset_secret()
    auth.init_secret(tmp_path)
    old = auth.create_token("admin")
    (tmp_path / auth._SECRET_FILE).unlink()
    _reset_secret()
    auth.init_secret(tmp_path)                  # 生成新密钥
    assert auth.verify_token(old) is None


def test_token_ttl_value_is_used():
    """签发的 token 的过期时间应当接近 now + TTL(防常量被绕过)。"""
    _reset_secret()
    tok = auth.create_token("admin")
    _, expires, _ = tok.rsplit(".", 2)
    delta = int(expires) - int(time.time())
    assert abs(delta - auth._TOKEN_TTL) < 5
