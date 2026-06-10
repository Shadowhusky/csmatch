"""Live validation of the redesigned log: stream a real match through the
bridge + DetailPane pipeline, then print all three display modes and the
kill→zone classifications for eyeballing."""

from __future__ import annotations

import asyncio
import sys

from rich.console import Console
from rich.text import Text

from csmatch import locations
from csmatch.scorebot import KillEvent, ScorebotBridge
from csmatch.tui import DetailPane
from csmatch.vocab import Vocab


async def run(match_id: str, secs: int) -> None:
    url = f"https://www.hltv.org/matches/{match_id}/x"
    print(f"streaming {url} for {secs}s …")
    pane = DetailPane()
    bridge = ScorebotBridge(poll_interval=1.0)
    kills: list[KillEvent] = []
    await bridge.start(url)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + secs
    try:
        while loop.time() < deadline:
            try:
                item = await asyncio.wait_for(bridge._queue.get(), timeout=2.0)
            except asyncio.TimeoutError:
                continue
            pane.push_scorebot(item)
            if isinstance(item, KillEvent):
                kills.append(item)
    finally:
        await bridge.stop()

    console = Console(force_terminal=True, width=100)

    for mode in ("default", "narrative", "monitor"):
        pane.apply_vocab(Vocab(monitor=(mode == "monitor")), mode)
        text = Text()
        if mode == "narrative":
            pane._render_narrative_log(text)
        else:
            pane._render_structured_log(text)
        console.print(f"\n══════ {mode} mode ══════", style="bold")
        console.print(text if text.plain else "(no events captured)")

    print("\n══════ kill → zone classifications ══════")
    for k in kills:
        zone = locations.zone_for(k.map, k.victim_x, k.victim_y)
        print(f"  {k.map}  ({k.victim_x:>8} , {k.victim_y:>8})  R{k.round}  "
              f"{k.killer} → {k.victim}: {zone}")
    print(f"\nevents in log: {len(pane._scorebot_log)}  kills: {len(kills)}")


if __name__ == "__main__":
    mid = sys.argv[1] if len(sys.argv) > 1 else "2395120"
    secs = int(sys.argv[2]) if len(sys.argv) > 2 else 200
    asyncio.run(run(mid, secs))
