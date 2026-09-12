"""Bot 快捷命令菜单:菜单项必须与实际注册的命令处理器一一对应。

用户要求(2026-09-12):"以后如果有新设计的给 Bot 机器人使用的快捷命令,都要注册到
快捷命令菜单里,不要每次都需要人为提醒"。所以这里用测试把两条集合锁死 ——
新增命令只加处理器、忘了加菜单(或反之)会直接红,不需要人来提醒。
"""

from __future__ import annotations

import ast
import pathlib
import re

import app.bot as bot

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _handler_command_names() -> set[str]:
    """从源码里取出所有 `CommandHandler("<name>", ...)` 注册的命令名。"""
    tree = ast.parse((ROOT / "app" / "bot.py").read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "id", "") != "CommandHandler":
            continue
        if node.args and isinstance(node.args[0], ast.Constant):
            names.add(str(node.args[0].value))
    return names


def test_menu_covers_every_registered_handler():
    registered = _handler_command_names()
    in_menu = {name for name, _ in bot._COMMANDS}
    missing = registered - in_menu
    stale = in_menu - registered
    assert not missing, f"这些命令注册了处理器却没进快捷菜单:{sorted(missing)}"
    assert not stale, f"这些菜单项没有对应的处理器(命令无效):{sorted(stale)}"


def test_menu_entries_meet_telegram_limits():
    """名称与描述要满足 Bot API 的限制,否则 set_my_commands 会被拒。"""
    assert bot._COMMANDS, "菜单不能为空"
    for name, desc in bot._COMMANDS:
        assert re.fullmatch(r"[a-z0-9_]{1,32}", name), name
        assert 3 <= len(desc) <= 256, (name, desc)
    names = [name for name, _ in bot._COMMANDS]
    assert len(names) == len(set(names)), f"菜单里有重名命令:{names}"


def test_help_text_mentions_menu_commands():
    """帮助文案里出现的命令名,应当都在菜单里(别让帮助教用户敲不存在的命令)。"""
    in_menu = {name for name, _ in bot._COMMANDS}
    mentioned = set(re.findall(r"/([a-z]{3,})", bot._HELP))
    unknown = mentioned - in_menu
    assert not unknown, f"帮助文案提到了菜单里没有的命令:{sorted(unknown)}"
