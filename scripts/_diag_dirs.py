import asyncio
import re
import sys

sys.path.insert(0, "/app")

from app.config import load_config
from app.pan115 import Pan115Reader, ShareLink


async def main() -> None:
    cfg = load_config("/app/data/config.json", strict=False)
    files = await Pan115Reader(cfg.pan115_cookie).read_share(ShareLink("swsyb7o3hib", "nfe7"))
    dirs = [f.name for f in files if f.is_dir]
    vids = [f for f in files if f.is_video]
    others = [f.name for f in files if not f.is_dir and not f.is_video]
    print(f"RESULT 总项 {len(files)} = 目录 {len(dirs)} + 视频 {len(vids)} + 非视频文件 {len(others)}")
    print("RESULT 目录名:", dirs)
    print("RESULT 非视频文件:", others)
    no_ep = [v.name for v in vids if not re.search(r"S\d{1,2}\s*E\d{1,4}", v.name, re.I)]
    print(f"RESULT 无 SxxExx 命名的视频 {len(no_ep)} 个: {no_ep[:6]}")

asyncio.run(main())
