"""End-to-end recording test against the project's test_form.html.

Drives a fresh Chromium with the recorder overlay, simulates a typical
user filling out the demo form (text + textarea + select + checkbox +
radio + submit click), then asserts the captured config has every
expected action with the right value.

This is the smoke test for the user's stated goal: 'tick box, submit
form, chuẩn xác'.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from playwright.async_api import async_playwright  # noqa: E402
from recorder_v2 import OVERLAY_JS, _Session, build_config  # noqa: E402


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
        # Simulate Tab to commit value (change event fires on blur for fill).
        await page.press("#email", "Tab")
        await page.fill("#full_name", "Alice")
        await page.press("#full_name", "Tab")
        await page.fill("#message", "hello world")
        await page.press("#message", "Tab")
        await page.select_option("#country", "vn")
        await page.check("#agree")
        await page.check('input[name="plan"][value="pro"]')
        # Click submit explicitly.
        await page.click("#submit")
        await asyncio.sleep(0.3)

        await ctx.close()
        await browser.close()

    return build_config(
        sess, target_url=_form_url(), wait_for_selector=None, auto_template=False,
    )


async def main() -> None:
    cfg = await _capture()
    actions = cfg["actions"]
    by_field = {}
    for a in actions:
        fid = a.get("field_id") or a.get("kind")
        # Latest action per field wins (after dedup; test_form fields are
        # filled exactly once, so this is a stable lookup).
        by_field[fid] = a
    print(f"[capture] {len(actions)} actions, fields={list(by_field.keys())}")

    # Email — the user's primary "chuẩn xác" check.
    e = by_field.get("email")
    assert e is not None, f"no email action: {by_field!r}"
    assert e["kind"] == "fill"
    assert e["value"] == "alice@example.com"

    # Full name.
    n = by_field.get("full_name")
    assert n is not None and n["value"] == "Alice"

    # Message (textarea).
    m = by_field.get("message")
    assert m is not None and m["value"] == "hello world"

    # Country (select).
    c = by_field.get("country")
    assert c is not None
    assert c["kind"] == "select"
    assert c["value"] == "vn"

    # Agree checkbox.
    a = by_field.get("agree")
    assert a is not None
    assert a["kind"] == "check"
    assert a["checked"] is True

    # Plan radio — must capture which one (pro), not just the group.
    p = by_field.get("plan")
    assert p is not None
    assert p["kind"] == "check"
    # radio_value is the per-checkbox-group disambiguator we added.
    assert p.get("radio_value") == "pro", (
        f"radio value lost for plan group: {p!r}"
    )

    # Submit must be captured at top level.
    submit = cfg.get("submit")
    assert submit is not None, "submit lost"
    assert submit.get("selectors"), f"submit has no selectors: {submit!r}"

    print("[ok] e2e: every form field + radio + checkbox + submit captured")


if __name__ == "__main__":
    asyncio.run(main())
