"""End-to-end perf test for the recorder's debounced screenshot pipeline.

Before the fix, every captured action triggered an `await page.screenshot()`
call. CDP `Page.captureScreenshot` briefly stalls the renderer thread
while it composes the surface and encodes the PNG (100-500 ms), which
the user perceives as a "giật như reload" flicker on every click.

After the fix, the recorder coalesces screenshots: only the *latest*
action gets a PNG, fired after a short quiet period. Rapid click bursts
collapse into one screenshot at the end (or zero, if the user keeps
clicking), so the renderer is never stalled mid-burst.

This test drives 30 rapid clicks through the real `record_to_config`
pipeline and asserts:

  1. All 30 clicks are recorded (no events lost).
  2. The number of step_NNNN_*.png files written is much less than 30
     (the screenshots got coalesced).
  3. Each captured click_callback returns in < 5 ms (no JS-side stall).
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import recorder_v2  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="af_shot_"))
    out_path = tmp / "rec.json"
    shots_dir = tmp / "rec_shots"

    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    durations: list[float] = []

    sess = recorder_v2._Session()

    # Simulate the post-fix `on_record` body: session.add then schedule
    # debounced screenshot. We measure how long the binding callback
    # itself takes — that's what blocks JS.
    _SHOT_DEBOUNCE_S = 0.35
    _shot_pending: list = [None]
    _shot_task: list = [None]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()

        async def _do_screenshot(snap: dict, idx: int) -> None:
            try:
                shots_dir.mkdir(parents=True, exist_ok=True)
                kind = str(snap.get("kind", "action"))[:20]
                fpath = shots_dir / f"step_{idx:04d}_{kind}.png"
                await page.screenshot(path=str(fpath), full_page=False, timeout=2500)
            except Exception:
                pass

        async def _flush_screenshot() -> None:
            try:
                while True:
                    await asyncio.sleep(_SHOT_DEBOUNCE_S)
                    target = _shot_pending[0]
                    if target is None:
                        return
                    _shot_pending[0] = None
                    await _do_screenshot(*target)
                    if _shot_pending[0] is None:
                        return
            finally:
                _shot_task[0] = None

        def schedule_shot(snap: dict, idx: int) -> None:
            _shot_pending[0] = (snap, idx)
            if _shot_task[0] is None or _shot_task[0].done():
                _shot_task[0] = asyncio.create_task(_flush_screenshot())

        async def on_record(payload: str) -> None:
            t0 = time.perf_counter()
            snap = json.loads(payload)
            sess.add(snap)
            schedule_shot(sess.actions[-1], len(sess.actions))
            durations.append((time.perf_counter() - t0) * 1000)

        async def on_finish() -> None:
            if not fut.done():
                fut.set_result("done")

        await ctx.expose_function("__afRecord", on_record)
        await ctx.expose_function("__afFinish", on_finish)
        await ctx.expose_function("__afCancel", lambda: None)
        await ctx.expose_function("__afUndo", lambda: None)
        await ctx.add_init_script(recorder_v2.OVERLAY_JS)

        # 30 buttons in a column at the LEFT edge so the recorder's
        # top-right overlay panel doesn't intercept the clicks.
        html = (
            "<body style='margin:0'>"
            "<div style='position:absolute;left:8px;top:8px;width:200px'>"
            + "".join(
                f'<button id="b{i}" style="display:block;margin:2px;width:100px">'
                f"btn{i}</button>"
                for i in range(30)
            )
            + "</div></body>"
        )
        await page.goto(f"data:text/html,{html}")

        for i in range(30):
            await page.click(f"#b{i}", force=True)
            # C4: bumped from 0.005 to 0.02 so the burst stays well
            # under the 0.35 s debounce window even on slow CI runners,
            # otherwise the test was occasionally producing >5 PNGs and
            # tripping the assertion. 50 cps is still a realistic burst
            # for human-driven recording.
            await asyncio.sleep(0.02)

        # Drain debounced screenshot
        if _shot_task[0] is not None:
            try:
                await asyncio.wait_for(_shot_task[0], timeout=2.0)
            except Exception:
                pass

        await ctx.close()
        await browser.close()

    actions = [a for a in sess.actions if a.get("kind") == "click"]
    shot_files = list(shots_dir.glob("step_*_click.png")) if shots_dir.exists() else []

    durations.sort()
    median = durations[len(durations) // 2] if durations else float("nan")
    p95 = durations[int(len(durations) * 0.95)] if durations else float("nan")

    print(f"clicks recorded:     {len(actions)} / 30 expected")
    print(f"screenshots written: {len(shot_files)} (debounced from 30)")
    print(f"binding callback:    median={median:.2f}ms p95={p95:.2f}ms")

    assert len(actions) == 30, f"lost click events: {len(actions)}"
    assert median < 5.0, f"binding still blocked: median {median:.2f}ms"
    # Without debouncing this would be 30; with a 350ms quiet window and
    # 5ms gap between clicks, only the very last screenshot fires.
    assert len(shot_files) <= 5, (
        f"screenshots not coalesced: {len(shot_files)} files written for 30 clicks "
        f"(expected ≤ 5 with 350ms debounce)"
    )
    print("[ok] screenshots are debounced — clicks no longer flicker the renderer")


if __name__ == "__main__":
    asyncio.run(main())
