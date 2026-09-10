"""获取段验收:扫一轮 openlist 监控目录。

用法:
  docker exec -i stow python - < scripts/e2e_fetch.py            # 只报告(默认,不动任何东西)
  docker exec -i stow python - < scripts/e2e_fetch.py --go       # 真提交移动(会从源盘移走!)
  docker exec -i stow python - < scripts/e2e_fetch.py <目录> ...  # 临时指定监控目录(仍只报告)

安全:默认 dry —— 只列出"会提交哪些"与在途情况,不调 move。
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, "/app")

from app.bot import StowBot
from app.config import load_config
from app.store import Store


async def main() -> None:
    go = "--go" in sys.argv
    dirs = [a for a in sys.argv[1:] if not a.startswith("--")]

    cfg = load_config("/app/data/config.json", strict=False)
    store = Store(Path("/app/data/stow.db"))
    bot = StowBot(cfg, store)
    fetcher = bot.fetcher
    if dirs:
        cfg.openlist_monitor_dirs = ",".join(dirs)

    tok = "已配置" if cfg.openlist_token else "未配置"
    print(f"openlist: {cfg.openlist_base_url or '(未配置)'} | 令牌: {tok}")
    print(f"监控目录: {cfg.openlist_monitor_list() or '(空 —— 请在 Web 全局配置页填)'}")
    print(f"落地点: {cfg.openlist_dest_path} | 并发上限: {cfg.openlist_max_tasks}"
          f" | 间隔: {cfg.fetch_interval_minutes} 分钟")
    print(f"在途任务: {len(store.list_fetch('moving'))} 个 | 状态统计: {store.fetch_stats()}")
    if not fetcher.enabled():
        print("[跳过] 未配置地址/令牌或监控目录")
        return

    if go:
        print("\n[--go] 真提交一轮…")
        print(await fetcher.scan_now())
        for r in store.list_fetch("moving"):
            print(f"  在途: {r['src_path']} → 任务 {r['task_id']}")
        await fetcher.aclose()
        return

    print("\n[dry] 本轮将会提交的条目(不移动):")
    total = 0
    for path in cfg.openlist_monitor_list():
        try:
            items = await fetcher.client.list_dir(path)
        except Exception as exc:  # noqa: BLE001
            print(f"  {path} → 列目录失败:{str(exc)[:80]}")
            continue
        print(f"  {path}:{len(items)} 个条目")
        for it in items:
            src = f"{path.rstrip('/')}/{it.get('name')}"
            if fetcher._should_fetch(src, int(it.get("size") or 0), it.get("name", "")):
                total += 1
                kind = "目录" if it.get("is_dir") else f"{int(it.get('size') or 0):,}B"
                print(f"    → 将提交: {it.get('name')} ({kind})")
    print(f"\n[dry] 合计将提交 {total} 个(实际提交需加 --go;受并发上限 {cfg.openlist_max_tasks} 约束)")
    await fetcher.aclose()
    store.close()


asyncio.run(main())
