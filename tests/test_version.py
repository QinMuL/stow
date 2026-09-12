"""版本号:单一真源是 `app.__version__`,其余三处元数据必须与它一致。

2026-09-12 发现过漂移(重写仓库后实测:`app` 0.3.0 / `frontend` 0.3.0 /
`pyproject` 0.2.0 / `package-lock` 0.1.0 —— 三处各说各话)。此后版本基线重置为 1.0.0,
并在本文件锁住一致性:只改一处的提交会被测试拦下。
"""

from __future__ import annotations

import json
import pathlib
import re

from app import __version__

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_version_format():
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__), __version__


def test_frontend_package_version_in_sync():
    pkg = json.loads((ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
    assert pkg["version"] == __version__


def test_frontend_lock_version_in_sync():
    lock = json.loads((ROOT / "frontend" / "package-lock.json").read_text(encoding="utf-8"))
    assert lock["version"] == __version__
    assert lock["packages"][""]["version"] == __version__


def test_pyproject_version_in_sync():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert f'version = "{__version__}"' in text
