"""Probe HLTV's matches page + a live match page for what's server-rendered."""

from __future__ import annotations

import re

from curl_cffi import requests
from selectolax.parser import HTMLParser

H = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.hltv.org/",
}


def get(url: str) -> str:
    r = requests.get(url, headers=H, impersonate="safari17_0", timeout=20)
    assert r.status_code == 200, f"{url} → {r.status_code}"
    return r.text


def find_live_match_urls() -> list[str]:
    html = get("https://www.hltv.org/matches")
    tree = HTMLParser(html)
    # HLTV's matches list contains anchors to /matches/<id>/<slug>
    urls = []
    for a in tree.css("a"):
        href = a.attributes.get("href", "")
        if not href:
            continue
        m = re.match(r"^/matches/(\d+)/", href)
        if m:
            urls.append("https://www.hltv.org" + href)
    # Dedup, keep order
    seen = set()
    out = []
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def inspect_match(url: str) -> None:
    print(f"\n=== {url}")
    html = get(url)
    tree = HTMLParser(html)
    title = tree.css_first("title").text() if tree.css_first("title") else "?"
    print(f"  title: {title!r}")
    # Score lookups
    for sel in [
        ".currentMapScore",
        ".scoreLineBox",
        ".teamsBox",
        ".small-text",
        ".matchScore",
        ".matchTeam",
        ".totalScore",
        ".mapholder",
        ".played .results-stats",
        ".stats-content",
        ".scoreboard",
    ]:
        hits = tree.css(sel)
        if hits:
            t0 = (hits[0].text(strip=True) or "")[:200].replace("\n", " | ")
            print(f"  {sel:<30}  hits={len(hits):>3}  sample: {t0[:180]}")
    # Look for any element that says CT or T sides or "alive"
    txt = tree.body.text(strip=False) if tree.body else ""
    for kw in ["alive", "money", "bomb", "Round ", "CT side", "T side", "/30", "/24"]:
        if kw.lower() in txt.lower():
            idx = txt.lower().find(kw.lower())
            print(f"  found {kw!r}: ...{txt[max(0, idx-40):idx+60]!r}")


if __name__ == "__main__":
    urls = find_live_match_urls()
    print(f"found {len(urls)} match urls in /matches")
    for u in urls[:8]:
        print(" •", u)
    # Inspect the first live-ish match
    if urls:
        inspect_match(urls[0])
        if len(urls) > 1:
            inspect_match(urls[1])
