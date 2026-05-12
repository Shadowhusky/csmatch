"""Dig into one match-wrapper to find score elements."""

from curl_cffi import requests
from selectolax.parser import HTMLParser

H = {"Accept": "text/html", "Referer": "https://www.hltv.org/"}
r = requests.get("https://www.hltv.org/matches", headers=H, impersonate="safari17_0", timeout=20)
tree = HTMLParser(r.text)

live = tree.css(".match-wrapper.live-match-container, .match-wrapper[live='true']")
print(f"Live wrappers: {len(live)}")
if not live:
    # Try all wrappers with class containing 'live'
    live = [w for w in tree.css(".match-wrapper") if "live-match" in (w.attributes.get("class") or "")]
    print(f"After fallback: {len(live)}")

if live:
    w = live[0]
    print(f"\ndata-match-id: {w.attributes.get('data-match-id')}")
    print(f"data-stars: {w.attributes.get('data-stars')}")
    print(f"\nFull HTML:")
    print(w.html[:4500])

print("\n=== Looking for upcoming (non-live)")
upcoming = [
    w for w in tree.css(".match-wrapper")
    if not w.attributes.get("live") and "live-match" not in (w.attributes.get("class") or "")
]
print(f"Upcoming-ish: {len(upcoming)}")
if upcoming:
    w = upcoming[0]
    print(f"data-match-id: {w.attributes.get('data-match-id')}")
    print(f"data attrs: {dict(w.attributes)}")
    print(w.html[:1500])
