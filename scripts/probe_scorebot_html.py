"""Dump the full #scoreboardElement HTML + kill-log structure from a
live HLTV match (headless)."""

from __future__ import annotations

import asyncio
import sys

from playwright.async_api import async_playwright


async def run(mid: str, slug: str) -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = await browser.new_context(
            viewport={"width": 1400, "height": 900},
            locale="en-US",
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        )
        page = await ctx.new_page()
        await page.goto(f"https://www.hltv.org/matches/{mid}/{slug}", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(12)

        data = await page.evaluate(r"""
        () => {
            const sb = document.querySelector('#scoreboardElement');
            const out = {};
            if (!sb) return {error: 'no scoreboard'};

            // Round header (e.g., "R: 4 - dust2")
            const header = sb.querySelector('.scoreboard-info, .header, .round-info');
            // Try to find any element with current round
            out.scoreboard_html = sb.innerHTML.slice(0, 8000);

            // Look for the gamelog / killfeed
            const candidates = ['.gamelog', '.event-log', '.live-log', '.gameLog', '.gamelog-entries', '.scorebot-log'];
            for (const sel of candidates) {
                const el = document.querySelector(sel);
                if (el) {
                    out.gamelog_selector = sel;
                    out.gamelog_text = el.innerText.slice(0, 2000);
                    out.gamelog_html = el.outerHTML.slice(0, 4000);
                    break;
                }
            }
            // Search by text for "Game log"
            const all = document.querySelectorAll('div,section');
            for (const el of all) {
                const t = (el.innerText || '').trim();
                if (t.startsWith('Game log') && t.length < 1500) {
                    out.gamelog_by_text_outer = el.outerHTML.slice(0, 3000);
                    out.gamelog_by_text_inner = el.innerText.slice(0, 2000);
                    break;
                }
            }
            return out;
        }
        """)

        if data.get("error"):
            print(data["error"]); return

        print("=== scoreboard innerHTML[:2k] ===")
        print(data.get("scoreboard_html", "")[:2000])

        print("\n=== gamelog (by class) ===")
        print("selector:", data.get("gamelog_selector"))
        print("text[:1500]:", data.get("gamelog_text", "")[:1500] if data.get("gamelog_text") else None)

        print("\n=== gamelog (by 'Game log' text) ===")
        print(data.get("gamelog_by_text_inner", "")[:1500])

        await ctx.close()
        await browser.close()


if __name__ == "__main__":
    mid = sys.argv[1] if len(sys.argv) > 1 else "2393950"
    slug = sys.argv[2] if len(sys.argv) > 2 else "tdk-vs-sashi-nodwin-clutch-series-8"
    asyncio.run(run(mid, slug))
