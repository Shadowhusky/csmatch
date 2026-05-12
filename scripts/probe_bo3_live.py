"""Diagnostic: dump bo3.gg's live-match feed so we can spot schema drift.

Run when csmatch parsing breaks — confirms the API still responds, shows
which fields populate, and prints one match's live_updates payload.
"""

from __future__ import annotations

import json

from curl_cffi import requests

HEADERS = {"Accept": "application/json", "Referer": "https://bo3.gg/"}


def main() -> None:
    r = requests.get(
        "https://api.bo3.gg/api/v1/matches?filter[matches.status][eq]=current&sort=-start_date&page[limit]=20",
        headers=HEADERS,
        impersonate="safari17_0",
        timeout=15,
    )
    data = r.json()
    print(f"total live: {data['total']['count']}")
    for m in data["results"]:
        bu = m.get("bet_updates") or {}
        t1 = (bu.get("team_1") or {}).get("name", f"#{m['team1_id']}")
        t2 = (bu.get("team_2") or {}).get("name", f"#{m['team2_id']}")
        print(
            f"  id={m['id']:>7d}  {t1[:14]:<14} vs {t2[:14]:<14}  "
            f"status={m['status']:<10s} parsed={m.get('parsed_status'):<10s}  "
            f"score={m['team1_score']}-{m['team2_score']}  "
            f"live_updates={'YES' if m.get('live_updates') else 'no'}  "
            f"tier={m.get('tier')}"
        )

    # Probe a match with live_updates if any:
    for m in data["results"]:
        if m.get("live_updates"):
            print("\n--- live_updates sample ---")
            print(json.dumps(m["live_updates"], indent=2)[:2000])
            print("\n--- games for this match ---")
            g = requests.get(
                f"https://api.bo3.gg/api/v1/games?filter[games.match_id][eq]={m['id']}&sort=number",
                headers=HEADERS,
                impersonate="safari17_0",
                timeout=15,
            ).json()
            print(json.dumps(g["results"], indent=2)[:2500])
            break


if __name__ == "__main__":
    main()
