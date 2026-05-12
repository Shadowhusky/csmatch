"""Closer look at HLTV match-page structure for the parser."""

from __future__ import annotations

from curl_cffi import requests
from selectolax.parser import HTMLParser

H = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.hltv.org/",
}

# Use a known live match URL (CHange The Game vs Last Bullet has live data).
URL = "https://www.hltv.org/matches/2394258/change-the-game-vs-last-bullet-hero-esports-asian-champions-league-2026"

r = requests.get(URL, headers=H, impersonate="safari17_0", timeout=20)
html = r.text
tree = HTMLParser(html)

print("=== .teamsBox (team names + score + status)")
tb = tree.css_first(".teamsBox")
if tb:
    print(tb.html[:2000])

print("\n=== .mapholder (per-map score with half splits)")
for mh in tree.css(".mapholder"):
    print("---")
    print(mh.text(separator=" | ", strip=True)[:300])

print("\n=== .stats-content (per-player table)")
sc = tree.css_first(".stats-content")
if sc:
    # Find the rows
    rows = sc.css("tr")
    print(f"  rows: {len(rows)}")
    for row in rows[:14]:
        cells = [c.text(strip=True) for c in row.css("td, th")]
        print(f"   {cells}")

print("\n=== match detail box (event, BO, format)")
for sel in [".matchInfoBox", ".veto-box", ".event a", ".timeAndEvent"]:
    el = tree.css_first(sel)
    if el:
        print(f"  {sel}: {el.text(strip=True)[:200]}")

print("\n=== match status / LIVE indicator")
for sel in [".matchHeader .countdown", ".matchHeader .live", ".match-info-row .live", ".match-page-header"]:
    el = tree.css_first(sel)
    if el:
        print(f"  {sel}: {el.text(strip=True)[:200]}")
