"""Compare each .gamelog row's raw text vs how our extractor parses it.

Goal: surface any row where the parsed (killer, victim, side, weapon,
assist, headshot) doesn't match the visible HLTV log.
"""

from __future__ import annotations

import asyncio
import json
import sys

from playwright.async_api import async_playwright

from csmatch.scorebot import _EXTRACT_JS


async def run(url: str) -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--window-position=-2400,-2400", "--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        )
        page = await ctx.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(15)

        # 1) Pull each gamelog row's raw outerHTML so we can inspect.
        raw_rows = await page.evaluate(r"""
            () => {
              const out = [];
              document.querySelectorAll('.gamelog .gamelogBox').forEach((box, i) => {
                out.push({
                  index: i,
                  classes: box.className,
                  text: box.innerText.trim(),
                  html: box.outerHTML.slice(0, 1500),
                });
              });
              return out;
            }
        """)

        # 2) Run our extractor.
        parsed_doc = await page.evaluate(_EXTRACT_JS)
        parsed_rows = parsed_doc.get("rows", [])
        sb_state = parsed_doc.get("scoreboard")

        print(f"scoreboard: {sb_state}")
        print(f"gamelog rows: {len(raw_rows)}  parsed: {len(parsed_rows)}")
        print()

        for raw, parsed in zip(raw_rows, parsed_rows):
            print(f"── row {raw['index']}  classes={raw['classes']!r}")
            print(f"   text   : {raw['text']!r}")
            print(f"   parsed : {json.dumps({k: v for k, v in parsed.items() if k not in ('classes','text')}, default=str)}")
            print()

        await ctx.close(); await browser.close()


if __name__ == "__main__":
    mid = sys.argv[1] if len(sys.argv) > 1 else "2394161"
    asyncio.run(run(f"https://www.hltv.org/matches/{mid}/x"))
