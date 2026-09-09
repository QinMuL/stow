import inspect
from p115client.tool import share_iterdir_walk
sig = inspect.signature(share_iterdir_walk)
for name, p in sig.parameters.items():
    print(f"{name} = {p.default!r}" if p.default is not inspect.Parameter.empty else name)
