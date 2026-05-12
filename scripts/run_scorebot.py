"""Standalone test: connect to a live HLTV match and print scorebot events."""

from __future__ import annotations

import asyncio
import sys

from csmatch.scorebot import (
    BombDefuseEvent,
    BombPlantEvent,
    KillEvent,
    RoundOverEvent,
    RoundStartEvent,
    ScoreboardState,
    ScorebotBridge,
)


def fmt(item) -> str:
    if isinstance(item, ScoreboardState):
        return f"[STATE] R{item.round} {item.map}  CT {item.ct_score}-{item.t_score} T  ({item.time}){' BOMB' if item.bomb_planted else ''}"
    if isinstance(item, KillEvent):
        a = f" + {item.assist}" if item.assist else ""
        hs = " (HS)" if item.headshot else ""
        w = f" [{item.weapon}]" if item.weapon else ""
        return f"[KILL]  {item.killer:<14}{a:<14} →  {item.victim:<14}{w}{hs}"
    if isinstance(item, BombPlantEvent):
        return f"[BOMB+] {item.planter} planted on {item.site}  ({item.t_alive}vs{item.ct_alive})"
    if isinstance(item, BombDefuseEvent):
        return f"[BOMB-] {item.defuser} defused"
    if isinstance(item, RoundStartEvent):
        return "[NEW]   round started"
    if isinstance(item, RoundOverEvent):
        return f"[END]   {item.winner_side} win  {item.t_score}-{item.ct_score}  ({item.reason})"
    from csmatch.scorebot import ScorebotEvent
    if isinstance(item, ScorebotEvent) and item.kind == "heartbeat":
        return f"[HB]    tick={getattr(item,'tick',None)} rows_seen={getattr(item,'rows_seen',None)} new_events={getattr(item,'new_events',None)} seen_keys={getattr(item,'seen_keys_total',None)}"
    return f"[??]    {item}"


async def main(url: str, duration: int) -> None:
    bridge = ScorebotBridge(poll_interval=1.0)
    print(f"starting bridge for {url}")
    await bridge.start(url)
    print("listening …\n")
    deadline = asyncio.get_running_loop().time() + duration

    try:
        while asyncio.get_running_loop().time() < deadline:
            try:
                item = await asyncio.wait_for(bridge._queue.get(), timeout=2.0)
            except asyncio.TimeoutError:
                continue
            print(fmt(item))
    finally:
        await bridge.stop()


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "https://www.hltv.org/matches/2393950/tdk-vs-sashi-nodwin-clutch-series-8"
    dur = int(sys.argv[2]) if len(sys.argv) > 2 else 120
    asyncio.run(main(url, dur))
