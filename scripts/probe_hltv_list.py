"""Probe HLTV /matches list page for live vs upcoming structure."""

from __future__ import annotations

from curl_cffi import requests
from selectolax.parser import HTMLParser

H = {"Accept": "text/html", "Referer": "https://www.hltv.org/"}

r = requests.get("https://www.hltv.org/matches", headers=H, impersonate="safari17_0", timeout=20)
tree = HTMLParser(r.text)

print("=== top-level containers")
for sel in [
    ".liveMatches",
    ".upcomingMatchesSection",
    ".upcomingMatches",
    ".upcomingMatchesContainer",
    ".upcomingMatchesAllToday",
    ".match-wrapper",
    ".live-match",
    ".matches-grid",
]:
    hits = tree.css(sel)
    print(f"  {sel}: {len(hits)} hits")

print("\n=== first live match block")
live = tree.css(".liveMatches .match-wrapper") or tree.css(".live-match-wrapper") or tree.css(".liveMatch") or tree.css(".liveMatch-container")
print(f"  found {len(live)}")
if live:
    print(live[0].html[:2500])

print("\n=== alt: anchors with 'matches/' that look live")
for a in tree.css(".liveMatches a, .live-match a"):
    href = a.attributes.get("href", "")
    if "/matches/" in href:
        text = a.text(separator=" | ", strip=True)[:200]
        print(f"  {href}\n    {text}")

print("\n=== upcomingMatches first block")
up = tree.css(".upcomingMatch") or tree.css(".upcoming-match-wrapper") or tree.css(".upcomingMatches a")
print(f"  found {len(up)}")
if up:
    print(up[0].html[:1500])
