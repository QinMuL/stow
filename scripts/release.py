#!/usr/bin/env python
"""发版一步到位:改版本号 → 跑测试 → 提交 → 打 tag → 推送。

用法(在仓库根目录跑):
    python scripts/release.py 1.0.1             # 完整发版
    python scripts/release.py 1.0.1 --no-push    # 只改版本并提交、打本地 tag,不推送(演练)
    python scripts/release.py --tag-only         # 版本号不动,只给当前版本补打 tag 并推送

它只 `git add` 四个版本文件,不会把你在改的其它代码卷进发版提交。
推送 tag 后 GitHub Actions 会构建不可变的版本镜像,并校验 tag 与代码版本一致
(见 .github/workflows/docker.yml)。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
INIT = ROOT / "app" / "__init__.py"
PKG = ROOT / "frontend" / "package.json"
LOCK = ROOT / "frontend" / "package-lock.json"
PYPROJECT = ROOT / "pyproject.toml"
VERSION_FILES = [INIT, PKG, LOCK, PYPROJECT]


def sh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    print("  $", " ".join(args))
    return subprocess.run(args, cwd=ROOT, check=check, text=True,
                          encoding="utf-8", errors="replace")


def current_version() -> str:
    m = re.search(r'__version__\s*=\s*"([^"]+)"', INIT.read_text(encoding="utf-8"))
    if not m:
        sys.exit("✗ 读不到 app/__init__.py 里的 __version__")
    return m.group(1)


def write_version(new: str) -> None:
    """四处一起改:app 是单一真源,其余三处有 tests/test_version.py 盯着。"""
    text = INIT.read_text(encoding="utf-8")
    INIT.write_text(re.sub(r'__version__\s*=\s*"[^"]+"', f'__version__ = "{new}"', text),
                    encoding="utf-8")
    for path in (PKG, LOCK):
        data = json.loads(path.read_text(encoding="utf-8"))
        data["version"] = new
        if path is LOCK and isinstance(data.get("packages"), dict) and "" in data["packages"]:
            data["packages"][""]["version"] = new
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    PYPROJECT.write_text(
        re.sub(r'^version = "[^"]+"', f'version = "{new}"',
               PYPROJECT.read_text(encoding="utf-8"), count=1, flags=re.M),
        encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("version", nargs="?", help="新版本号,如 1.0.1")
    ap.add_argument("--tag-only", action="store_true", help="不改版本,只补打当前版本的 tag")
    ap.add_argument("--no-push", action="store_true", help="不推送(演练)")
    ap.add_argument("--skip-tests", action="store_true", help="跳过测试(不推荐)")
    args = ap.parse_args()

    cur = current_version()
    if args.tag_only:
        new = cur
        print(f"只补 tag:当前版本 {cur}")
    else:
        if not args.version:
            sys.exit("✗ 请给新版本号,如:python scripts/release.py 1.0.1")
        if not re.fullmatch(r"\d+\.\d+\.\d+", args.version):
            sys.exit(f"✗ 版本号要形如 x.y.z,收到 {args.version!r}")
        new = args.version
        if new == cur:
            print(f"版本已是 {cur}(不修改文件,继续打 tag)")
        else:
            print(f"版本 {cur} → {new}")
            write_version(new)

    if not args.skip_tests:
        print("跑测试(不过就中止发版)…")
        proc = sh(sys.executable, "-m", "pytest", "-q", check=False)
        if proc.returncode != 0:
            sys.exit("✗ 测试没通过,已中止(版本文件可自行回退:git checkout -- app frontend pyproject.toml)")

    tag = f"v{new}"
    existing = sh("git", "tag", "-l", tag, check=False).stdout.strip()
    if existing:
        sys.exit(f"✗ tag {tag} 已存在,换版本号或先删掉它")

    if new != cur:
        sh("git", "add", *(str(p.relative_to(ROOT)) for p in VERSION_FILES))
        sh("git", "commit", "-m", f"chore(release): v{new}")
    sh("git", "tag", "-a", tag, "-m", f"release v{new}")

    if args.no_push:
        print(f"\n✅ 本地完成:{tag}(未推送,演练模式)")
        return
    sh("git", "push", "origin", "HEAD")
    sh("git", "push", "origin", tag)
    print(f"""
✅ 已推送 {tag}
   · main 推送 → 构建 :latest
   · tag 推送  → 构建不可变的 :{new},并校验 tag 与 __version__ 一致
   部署(容器跟版本走时):
     docker pull ghcr.io/qinmul/stow:{new}
     docker compose up -d --force-recreate""")


if __name__ == "__main__":
    main()
