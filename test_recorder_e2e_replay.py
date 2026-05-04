"""End-to-end: record a session against test_form.html, then replay it
back into a fresh page and assert the form ends up in the same state.

This catches regressions where the recorder ships actions that the
replay engine can't reproduce (selector drift, missing radio_value,
submit-button-not-clickable, etc.).
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from logger import get_logger  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402
from recorder_v2 import OVERLAY_JS, _Session, build_config  # noqa: E402
from replay_engine import run_actions  # noqa: E402


def _form_url() -> str:
    return (Path(__file__).parent / "test_form.html").as_uri()


async def _capture() -> dict:
    sess = _Session()

    async def on_record(payload: str) -> None:
        sess.add(json.loads(payload))

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        await ctx.expose_function("__afRecord", on_record)
        await ctx.expose_function("__afFinish", lambda: None)
        await ctx.expose_function("__afCancel", lambda: None)
        await ctx.expose_function("__afUndo", lambda: None)
        await ctx.add_init_script(OVERLAY_JS)
        page = await ctx.new_page()
        await page.goto(_form_url())

        await page.fill("#email", "alice@example.com")
        await page.press("#email", "Tab")
        await page.fill("#full_name", "Alice")
        await page.press("#full_name", "Tab")
        await page.fill("#message", "hello world")
        await page.press("#message", "Tab")
        await page.select_option("#country", "vn")
        await page.check("#agree")
        await page.check('input[name="plan"][value="pro"]')
        await asyncio.sleep(0.3)

        await ctx.close()
        await browser.close()

    return build_config(
        sess, target_url=_form_url(), wait_for_selector=None, auto_template=False,
    )


async def _replay(cfg: dict) -> dict:
    """Run the captured actions against a fresh page; return final form state."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto(_form_url())
        logger = get_logger(debug=False)
        filled, skipped = await run_actions(
            page, cfg["actions"], ctx={}, dry_run=False, logger=logger,
        )
        state = await page.evaluate("""() => ({
            email:     document.getElementById('email').value,
            full_name: document.getElementById('full_name').value,
            message:   document.getElementById('message').value,
            country:   document.getElementById('country').value,
            agree:     document.getElementById('agree').checked,
            plan:      (document.querySelector('input[name="plan"]:checked') || {}).value || null
        })""")
        await ctx.close()
        await browser.close()
    return {"filled": filled, "skipped": skipped, "state": state}


async def main() -> None:
    cfg = await _capture()
    print(f"[capture] {len(cfg['actions'])} actions captured")
    out = await _replay(cfg)
    print(f"[replay] filled={out['filled']} skipped={out['skipped']}")
    print(f"[replay] state={out['state']}")

    state = out["state"]
    assert state["email"] == "alice@example.com", state
    assert state["full_name"] == "Alice", state
    assert state["message"] == "hello world", state
    assert state["country"] == "vn", state
    assert state["agree"] is True, state
    assert state["plan"] == "pro", state
    print("[ok] capture → replay round-trip preserves all field values")


if __name__ == "__main__":
    asyncio.run(main())
