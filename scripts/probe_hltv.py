"""Can curl_cffi + Safari impersonation reach HLTV's match pages?"""

from __future__ import annotations

import re

from curl_cffi import requests

H = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.hltv.org/",
}

URLS = [
    "https://www.hltv.org/matches",
    "https://www.hltv.org/matches/all",
    "https://www.hltv.org/",
    # The CDN often has a more relaxed JSON endpoint
    "https://www.hltv.org/fetch/live-tv",
    "https://www.hltv.org/fetch/matches",
    "https://www.hltv.org/fetch/livematches",
    "https://scorebot-lb.hltv.org/socket.io/?EIO=4&transport=polling",
]


def probe(url: str) -> None:
    try:
        r = requests.get(url, headers=H, impersonate="safari17_0", timeout=15)
    except Exception as e:
        print(f"  ERR  {type(e).__name__}: {e}\n  {url}")
        return
    sc = r.status_code
    ct = r.headers.get("content-type", "")[:30]
    blen = len(r.content)
    body = r.text[:500].replace("\n", " ")[:300]
    cf = r.headers.get("cf-mitigated") or r.headers.get("cf-ray", "")[:8]
    print(f"  {sc:3d}  len={blen:>7}  ct={ct:<30}  cf={cf:<14}  {url}")
    # Hint of cloudflare challenge?
    if "Just a moment" in body or "challenge-platform" in body:
        print("        ⚠ Cloudflare challenge")
    elif sc == 200:
        # Sample of body
        print(f"        body: {body[:200]}")


if __name__ == "__main__":
    for u in URLS:
        probe(u)
