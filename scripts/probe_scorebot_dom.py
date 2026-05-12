"""Open an HLTV match page in headless Chromium, wait for scorebot DOM
to populate, then dump the relevant elements + take a screenshot."""

from __future__ import annotations

import asyncio
import sys

from playwright.async_api import async_playwright


async def run(match_id: str, slug: str) -> None:
    url = f"https://www.hltv.org/matches/{match_id}/{slug}"
    print(f"opening {url}")

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
            viewport={"width": 1400, "height": 900},
            locale="en-US",
        )
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        # Give scorebot 10 seconds to populate
        await asyncio.sleep(10)

        # Dump #scoreboardElement and the live area
        sel_dump = await page.evaluate(r"""
        () => {
            const out = {};
            const sb = document.querySelector('#scoreboardElement');
            out.scoreboardEl_html = sb ? sb.innerHTML.slice(0, 4000) : null;
            out.scoreboardEl_text = sb ? sb.innerText.slice(0, 2000) : null;
            // Any element with 'live' in className?
            const liveEls = Array.from(document.querySelectorAll('[class*=live i], .currentMapScore'));
            out.liveText = liveEls.map(el => el.innerText.trim()).filter(Boolean).slice(0, 30);
            // Inspect data attrs
            const attrs = {};
            document.querySelectorAll('[data-scorebot-id], [data-livescore-team], [data-livescore-current-map-score]').forEach(el => {
                const k = el.tagName + '.' + el.className.slice(0, 30);
                attrs[k] = (attrs[k] || 0) + 1;
            });
            out.scorebot_attr_counts = attrs;
            return out;
        }
        """)
        print("\n=== scoreboardElement.innerText (first 2k chars) ===")
        print(sel_dump.get("scoreboardEl_text") or "(none)")
        print("\n=== liveText elements ===")
        for s in sel_dump.get("liveText") or []:
            print(f"  {s!r}")
        print("\n=== scorebot-related element counts ===")
        for k, v in (sel_dump.get("scorebot_attr_counts") or {}).items():
            print(f"  {v:>3}  {k}")

        await page.screenshot(path="/tmp/hltv_match_headless.png", full_page=False)
        print("\nscreenshot → /tmp/hltv_match_headless.png")

        await context.close()
        await browser.close()


if __name__ == "__main__":
    mid = sys.argv[1] if len(sys.argv) > 1 else "2393950"
    slug = sys.argv[2] if len(sys.argv) > 2 else "tdk-vs-sashi-nodwin-clutch-series-8"
    asyncio.run(run(mid, slug))
