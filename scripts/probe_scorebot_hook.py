"""Inject JS that hooks the in-page socket.io client and forwards every
received event to our Python side via a Playwright bridge."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime

from playwright.async_api import async_playwright


PATCH_JS = r"""
() => {
  if (window.__csmatch_wsHooked) return;
  window.__csmatch_wsHooked = true;

  const Orig = window.WebSocket;
  function Patched(url, protocols) {
    const ws = protocols === undefined
      ? new Orig(url)
      : new Orig(url, protocols);
    let id = (window.__csmatch_wsCounter = (window.__csmatch_wsCounter || 0) + 1);
    try {
      window.__csmatch_emit({ev: "_ws_open", id, url: String(url)});
    } catch(e) {}

    ws.addEventListener("message", (e) => {
      let body = e.data;
      let isBin = false;
      if (body instanceof ArrayBuffer) {
        isBin = true;
        try {
          const bytes = new Uint8Array(body);
          // Best-effort utf-8 decode for engine.io text frames hidden inside binary
          body = new TextDecoder("utf-8", {fatal: false}).decode(bytes);
        } catch (err) {
          body = "<binary " + body.byteLength + " bytes>";
        }
      } else if (body instanceof Blob) {
        // We won't await this — just label it
        body = "<blob " + body.size + " bytes>";
      }
      // Try to parse Socket.IO event frames "42[event, data]"
      let evName = null, evArgs = null;
      if (typeof body === "string") {
        const m = body.match(/^4?42(.*)$/);  // tolerate "42..." and engine.io "442..." nested
        if (m) {
          try {
            const arr = JSON.parse(m[1]);
            if (Array.isArray(arr) && arr.length) {
              evName = String(arr[0]);
              evArgs = arr.slice(1);
            }
          } catch (err) {}
        }
      }
      try {
        window.__csmatch_emit({
          ev: evName || "_raw",
          id,
          isBin,
          raw: typeof body === "string" ? body.slice(0, 400) : String(body),
          args: evArgs,
        });
      } catch (err) {}
    });
    ws.addEventListener("close", (e) => {
      try { window.__csmatch_emit({ev: "_ws_close", id, code: e.code, reason: e.reason}); } catch(_) {}
    });
    // Capture outgoing sends too
    const origSend = ws.send.bind(ws);
    ws.send = function(data) {
      try {
        let s = data;
        if (data instanceof ArrayBuffer) s = "<bin " + data.byteLength + " bytes>";
        else if (typeof data !== "string") s = String(data);
        window.__csmatch_emit({ev: "_ws_send", id, body: s.slice(0, 240)});
      } catch(_) {}
      return origSend(data);
    };
    return ws;
  }
  Patched.prototype = Orig.prototype;
  Patched.CONNECTING = Orig.CONNECTING;
  Patched.OPEN = Orig.OPEN;
  Patched.CLOSING = Orig.CLOSING;
  Patched.CLOSED = Orig.CLOSED;
  window.WebSocket = Patched;
  try { window.__csmatch_emit({ev: "_hooked", note: "WebSocket patched"}); } catch(_) {}
}
"""


async def run(match_id: str, slug: str, secs: int = 90, headless: bool = True) -> None:
    url = f"https://www.hltv.org/matches/{match_id}/{slug}"
    print(f"opening {url}  for {secs}s")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        page = await context.new_page()

        events: list[dict] = []
        evt_counts: dict[str, int] = {}

        async def collect(payload: dict) -> None:
            events.append(payload)
            ev = str(payload.get("ev"))
            evt_counts[ev] = evt_counts.get(ev, 0) + 1
            ts = datetime.now().strftime("%H:%M:%S")
            args_snip = json.dumps(payload.get("args"), default=str)[:240]
            print(f"[{ts}] {ev:<20s} {args_snip}")

        await page.expose_function("__csmatch_emit", collect)
        # Add the init script BEFORE navigation so io is hooked when it loads.
        await page.add_init_script(f"({PATCH_JS})()")

        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        print("page loaded; listening for socket.io events …")

        # Scroll to the scoreboard so it's in view (lazy hookup safety).
        try:
            await page.evaluate("document.querySelector('#scoreboardElement')?.scrollIntoView()")
        except Exception:
            pass
        await asyncio.sleep(secs)

        await context.close()
        await browser.close()

    print("\n=== event counts ===")
    for k, v in sorted(evt_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {k:<30s} {v}")


if __name__ == "__main__":
    mid = sys.argv[1] if len(sys.argv) > 1 else "2393950"
    slug = sys.argv[2] if len(sys.argv) > 2 else "tdk-vs-sashi-nodwin-clutch-series-8"
    secs = int(sys.argv[3]) if len(sys.argv) > 3 else 75
    asyncio.run(run(mid, slug, secs))
