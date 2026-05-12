"""Probe HLTV's scorebot to see what events stream when we subscribe to a match.

This is a discovery script — connect to scorebot-lb.hltv.org and listen for
~30 seconds to a live match id, dumping all events received.
"""

from __future__ import annotations

import asyncio
import sys

import socketio


async def main(match_id: str) -> None:
    sio = socketio.AsyncClient(logger=False, engineio_logger=False)

    seen_events = set()

    @sio.event
    async def connect() -> None:
        print(f"[connect] sid={sio.sid}")
        # HLTV's classic protocol: emit 'readyForMatch' with the match id.
        # Older clients also tried 'readyForScores' / 'readyForLog'.
        for evt_name in ["readyForMatch", "readyForScores", "readyForLog"]:
            try:
                await sio.emit(evt_name, match_id)
                print(f"  emitted {evt_name}({match_id!r})")
            except Exception as e:
                print(f"  failed to emit {evt_name}: {e}")

    @sio.event
    async def disconnect() -> None:
        print("[disconnect]")

    # Catch-all event handler
    @sio.on("*")
    async def any_event(event: str, *args) -> None:
        if event not in seen_events:
            print(f"[NEW EVENT] {event}")
            seen_events.add(event)
        print(f"[{event}] {str(args)[:280]}")

    targets = [
        ("https://scorebot-lb.hltv.org", "socket.io"),
        ("https://scorebot-secure.hltv.org", "socket.io"),
        ("https://www.hltv.org", "socket.io"),
        ("wss://scorebot-lb.hltv.org", "socket.io"),
    ]
    transports_list = [["websocket"], ["polling"], ["websocket", "polling"]]

    connected = False
    for url, sp in targets:
        for tr in transports_list:
            try:
                print(f"trying {url}  transports={tr}  path={sp}")
                await sio.connect(url, transports=tr, socketio_path=sp, headers={"Origin": "https://www.hltv.org"})
                connected = True
                print(f"  ✓ connected via {url} {tr}")
                break
            except Exception as e:
                print(f"  × {type(e).__name__}: {str(e)[:120]}")
        if connected:
            break
    if not connected:
        print("all attempts failed")
        return

    await asyncio.sleep(45)
    await sio.disconnect()


if __name__ == "__main__":
    mid = sys.argv[1] if len(sys.argv) > 1 else "2394258"
    asyncio.run(main(mid))
