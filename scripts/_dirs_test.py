import asyncio
import sys

sys.path.insert(0, "/app")

from app.config import load_config
from app.pan115 import Pan115Reader


async def main() -> None:
    cfg = load_config("/app/data/config.json", strict=False)
    r = Pan115Reader(cfg.pan115_cookie)
    for cid in (0,):
        items = await r.list_dir(cid, nf=1)
        print(f"SMOKE cid={cid} 子目录数={len(items)}")
        for it in items[:8]:
            print(f"SMOKE   {it['fid']} {it['name']}")
        # 取第一个子目录再列一层
        if items:
            sub = items[0]["fid"]
            items2 = await r.list_dir(sub, nf=1)
            print(f"SMOKE cid={sub} 子目录数={len(items2)}")
            for it in items2[:5]:
                print(f"SMOKE   {it['fid']} {it['name']}")

asyncio.run(main())
