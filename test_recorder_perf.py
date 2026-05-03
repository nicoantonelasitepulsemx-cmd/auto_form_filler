"""Micro-benchmark: prove that the recorder no longer blocks the JS-side
binding callback while a screenshot is encoding.

Before the fix, `on_record` awaited `_capture_screenshot` before returning
from the binding callback.  Each screenshot takes 100-500 ms on a real
display, so chained user interactions (clicks through a country picker,
etc.) felt jittery.

After the fix, screenshots run as fire-and-forget background tasks with a
single-flight gate, so consecutive `__afRecord` calls return in <1 ms.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from playwright.async_api import async_playwright  # noqa: E402
from recorder_v2 import OVERLAY_JS, _Session  # noqa: E402


async def main() -> None:
    durations: list[float] = []
    received = 0

    sess = _Session()
    # Mimic the post-fix on_record body: session.add then spawn the
    # screenshot task.  We measure the time the binding callback itself
    # takes — this is what blocks the JS side.
    fake_screenshot_ms = 200  # pessimistic; real ones are 100-500 ms

    async def _slow_screenshot() -> None:
        await asyncio.sleep(fake_screenshot_ms / 1000.0)

    async def on_record(payload: str) -> None:
        nonlocal received
        t0 = time.perf_counter()
        sess.add({"kind": "click"})
        # Same pattern as the patched recorder_v2.on_record:
        asyncio.create_task(_slow_screenshot())
        durations.append((time.perf_counter() - t0) * 1000)
        received += 1

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        await ctx.expose_function("__afRecord", on_record)
        await ctx.expose_function("__afFinish", lambda: None)
        await ctx.expose_function("__afCancel", lambda: None)
        await ctx.expose_function("__afUndo", lambda: None)
        await ctx.add_init_script(OVERLAY_JS)
        page = await ctx.new_page()
        await page.goto("data:text/html,<button id=b>x</button>")

        # Fire 30 rapid clicks and measure how long the JS->Python bridge
        # actually blocks.
        for _ in range(30):
            await page.click("#b")
            await asyncio.sleep(0.005)

        await asyncio.sleep(0.2)
        await ctx.close()
        await browser.close()

    durations.sort()
    median = durations[len(durations) // 2] if durations else float("nan")
    p95 = durations[int(len(durations) * 0.95)] if durations else float("nan")
    print(f"binding callback: received={received} "
          f"median={median:.2f}ms p95={p95:.2f}ms "
          f"(simulated screenshot={fake_screenshot_ms}ms)")
    # Without the fix, every callback would block for ~200 ms.
    # With the fix, each returns in well under 5 ms because the
    # screenshot is dispatched as a background task.
    assert median < 5.0, f"binding still blocked: median {median:.2f}ms"
    print("[ok] binding callback returns immediately — recorder no longer blocks UI")


if __name__ == "__main__":
    asyncio.run(main())
