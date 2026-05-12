"""Open an HLTV live match page in headless Chromium and intercept
the scorebot WebSocket frames. Dumps raw frames + parsed Socket.IO
event names so we can see what the scorebot actually emits.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime

from playwright.async_api import WebSocket, async_playwright


async def run(match_id: str, slug_hint: str, duration: int = 90) -> None:
    url = f"https://www.hltv.org/matches/{match_id}/{slug_hint}"
    print(f"opening {url}  for {duration}s")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )
        page = await context.new_page()

        ws_count = [0]
        scorebot_events: dict[str, int] = {}

        def on_ws(ws: WebSocket) -> None:
            ws_count[0] += 1
            wid = ws_count[0]
            print(f"[WS#{wid}] OPEN {ws.url}")

            def on_recv(payload: str | bytes) -> None:
                # payload may be str or bytes
                if isinstance(payload, bytes):
                    try:
                        s = payload.decode("utf-8", "replace")
                    except Exception:
                        s = repr(payload)
                else:
                    s = payload
                ts = datetime.now().strftime("%H:%M:%S")
                # Socket.IO event frames look like "42[event,...]"
                evt_name: str | None = None
                if s.startswith("42"):
                    try:
                        parsed = json.loads(s[2:])
                        if isinstance(parsed, list) and parsed:
                            evt_name = str(parsed[0])
                    except Exception:
                        pass
                tag = f"[{evt_name}]" if evt_name else ""
                if evt_name:
                    scorebot_events[evt_name] = scorebot_events.get(evt_name, 0) + 1
                snippet = s[:240].replace("\n", " ")
                print(f"[WS#{wid} ← {ts}] {tag} {snippet}")

            def on_sent(payload: str | bytes) -> None:
                s = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else payload
                print(f"[WS#{wid} → ] {s[:180]}")

            def on_close() -> None:
                print(f"[WS#{wid}] CLOSED")

            ws.on("framereceived", on_recv)
            ws.on("framesent", on_sent)
            ws.on("close", on_close)

        page.on("websocket", on_ws)

        # Also dump XHR calls that hit the scorebot host
        async def on_response(resp):
            if "scorebot" in resp.url:
                print(f"[XHR] {resp.status} {resp.url[:120]}")

        page.on("response", on_response)

        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        print("page loaded; listening …")

        await asyncio.sleep(duration)

        await context.close()
        await browser.close()

    print("\n=== event-name counts ===")
    for name, n in sorted(scorebot_events.items(), key=lambda kv: -kv[1]):
        print(f"  {name:<30s} {n}")


if __name__ == "__main__":
    mid = sys.argv[1] if len(sys.argv) > 1 else "2394323"
    slug = sys.argv[2] if len(sys.argv) > 2 else "procyon-vs-guara-cct-2026-south-america-series-2"
    secs = int(sys.argv[3]) if len(sys.argv) > 3 else 60
    asyncio.run(run(mid, slug, secs))
