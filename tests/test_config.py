"""配置归一化:HTTP 地址补 scheme、gRPC 地址剥 scheme(2026-09-14)。

用户填 IP 地址时协议头写不写都能用:proxy_url / openlist_base_url 自动补 http://,
cd2_address 自动剥 http(s)://(否则 gRPC 把整串当域名解析报 Misformatted domain name)。
"""

from __future__ import annotations

import json

from app.config import _grpc_address, _http_url, load_config


# ── HTTP 类地址(proxy_url / openlist_base_url) ─────────────
def test_http_url_adds_scheme_when_missing():
    assert _http_url("127.0.0.1:5244") == "http://127.0.0.1:5244"


def test_http_url_keeps_existing_scheme():
    assert _http_url("http://127.0.0.1:5244") == "http://127.0.0.1:5244"
    assert _http_url("https://127.0.0.1:5244") == "https://127.0.0.1:5244"
    assert _http_url("socks5://127.0.0.1:1080") == "socks5://127.0.0.1:1080"


def test_http_url_strips_trailing_slash():
    assert _http_url("http://127.0.0.1:5244/") == "http://127.0.0.1:5244"
    assert _http_url("127.0.0.1:5244/") == "http://127.0.0.1:5244"


def test_http_url_empty():
    assert _http_url("") == ""
    assert _http_url("   ") == ""


# ── gRPC 地址(cd2_address) ─────────────────────────────────
def test_grpc_address_strips_scheme():
    assert _grpc_address("http://192.168.1.202:19798") == "192.168.1.202:19798"
    assert _grpc_address("https://192.168.1.202:19798") == "192.168.1.202:19798"
    assert _grpc_address("192.168.1.202:19798") == "192.168.1.202:19798"
    assert _grpc_address("http://192.168.1.202:19798/") == "192.168.1.202:19798"
    assert _grpc_address("") == ""


# ── load_config 集成 ───────────────────────────────────────
def test_load_config_normalizes_endpoints(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({
        "proxy_url": "127.0.0.1:7897",
        "openlist_base_url": "http://127.0.0.1:5244/",
        "cd2_address": "http://192.168.1.202:19798",
    }), encoding="utf-8")
    cfg = load_config(p)
    assert cfg.proxy_url == "http://127.0.0.1:7897"
    assert cfg.openlist_base_url == "http://127.0.0.1:5244"
    assert cfg.cd2_address == "192.168.1.202:19798"


def test_load_config_keeps_already_normalized(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({
        "proxy_url": "http://127.0.0.1:7897",
        "openlist_base_url": "http://127.0.0.1:5244",
        "cd2_address": "192.168.1.202:19798",
    }), encoding="utf-8")
    cfg = load_config(p)
    assert cfg.proxy_url == "http://127.0.0.1:7897"
    assert cfg.openlist_base_url == "http://127.0.0.1:5244"
    assert cfg.cd2_address == "192.168.1.202:19798"
