"""Probe bo3.gg for per-player and per-round live data: kills, utility, bomb."""

from __future__ import annotations

import json

from curl_cffi import requests

H = {"Accept": "application/json", "Referer": "https://bo3.gg/"}

# Pick a match that's mid-round so live data is populated.
LIVE_MATCH_ID = 119879  # Alter Ego vs THUNDER, dust2 m2 mid-round (from earlier probe)
LIVE_GAME_ID = 172763   # The current game (de_dust2)
TEAM1_ID = 22191
TEAM2_ID = 23286

CANDIDATES = [
    # Standard per-game endpoints worth trying.
    f"https://api.bo3.gg/api/v1/games/{LIVE_GAME_ID}",
    f"https://api.bo3.gg/api/v1/games/{LIVE_GAME_ID}/players",
    f"https://api.bo3.gg/api/v1/games/{LIVE_GAME_ID}/results",
    f"https://api.bo3.gg/api/v1/games/{LIVE_GAME_ID}/rounds",
    f"https://api.bo3.gg/api/v1/games/{LIVE_GAME_ID}/stats",
    f"https://api.bo3.gg/api/v1/games/{LIVE_GAME_ID}/playerstats",
    f"https://api.bo3.gg/api/v1/games/{LIVE_GAME_ID}/scoreboard",
    # Player-related list endpoints
    f"https://api.bo3.gg/api/v1/players?filter[players.team_id][eq]={TEAM1_ID}",
    f"https://api.bo3.gg/api/v1/players?filter[players.match_id][eq]={LIVE_MATCH_ID}",
    f"https://api.bo3.gg/api/v1/match_results?filter[match_results.match_id][eq]={LIVE_MATCH_ID}",
    f"https://api.bo3.gg/api/v1/game_results?filter[game_results.game_id][eq]={LIVE_GAME_ID}",
    f"https://api.bo3.gg/api/v1/player_results?filter[player_results.game_id][eq]={LIVE_GAME_ID}",
    f"https://api.bo3.gg/api/v1/player_stats?filter[player_stats.match_id][eq]={LIVE_MATCH_ID}",
    f"https://api.bo3.gg/api/v1/rounds?filter[rounds.game_id][eq]={LIVE_GAME_ID}&sort=number",
    # Match-level: maybe whole match has nested details
    f"https://api.bo3.gg/api/v1/matches/{LIVE_MATCH_ID}/results",
    f"https://api.bo3.gg/api/v1/matches/{LIVE_MATCH_ID}/players",
    f"https://api.bo3.gg/api/v1/matches/{LIVE_MATCH_ID}/rounds",
    # Lineups
    f"https://api.bo3.gg/api/v1/lineups?filter[lineups.match_id][eq]={LIVE_MATCH_ID}",
    f"https://api.bo3.gg/api/v1/match_lineups?filter[match_lineups.match_id][eq]={LIVE_MATCH_ID}",
    # Bomb/event log?
    f"https://api.bo3.gg/api/v1/events?filter[events.game_id][eq]={LIVE_GAME_ID}",
    f"https://api.bo3.gg/api/v1/round_events?filter[round_events.game_id][eq]={LIVE_GAME_ID}",
]


def probe(url: str) -> None:
    try:
        r = requests.get(url, headers=H, impersonate="safari17_0", timeout=12)
    except Exception as e:
        print(f"  ERR  {type(e).__name__}: {e}\n  {url}")
        return
    sc = r.status_code
    ct = r.headers.get("content-type", "")[:30]
    blen = len(r.content)
    print(f"  {sc:3d}  len={blen:>6}  ct={ct:<30}  {url}")
    if sc == 200 and "json" in ct and blen > 60:
        try:
            data = r.json()
        except Exception:
            return
        # Just preview first 300 chars of pretty JSON.
        print("        " + json.dumps(data, indent=2)[:400].replace("\n", "\n        "))


if __name__ == "__main__":
    for u in CANDIDATES:
        probe(u)
