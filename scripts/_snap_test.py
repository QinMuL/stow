from p115client import P115Client
from p115client.util import share_extract_payload
c = P115Client("", app="web")
payload = dict(share_extract_payload("swsyb7o3hib"))
payload["receive_code"] = "nfe7"
try:
    r = c.share_snap(payload, async_=False)
    print("OK:", str(r)[:150])
except Exception as e:
    print("FAIL:", type(e).__name__, e)
