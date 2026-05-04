"""Regression test for Devin Review #3182603972.

The recorder explicitly says NOT to default ``_hidden_input_type`` to
``"radio"`` (see ``replay_engine.py`` ~line 571-577 comment), because
ARIA-only checkbox widgets ship without it. Old code paths inside
``_do_check`` still passed ``hidden_input_type or "radio"`` to a JS
selector — which would generate ``input[type="radio"][name="..."]``
for what is actually a checkbox, silently failing to find / heal the
element.

This test exercises the heal path end-to-end against an HTML form
whose hidden input is a checkbox (``type="checkbox"``), with an
action dict that simulates a recorder run that captured
``_hidden_input_name`` but NOT ``_hidden_input_type`` (e.g. an old
recording or a manually-authored config). After the fix, the heal
JS broadens the selector to ``input[name="..."]`` when type is
empty, so the checkbox is healed correctly.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from logger import get_logger  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402
from replay_engine import _do_check  # noqa: E402


HTML = """\
<!DOCTYPE html>
<html><body>
<form id="f">
  <label>
    <input type="checkbox" name="agree" id="cb_agree" style="display:none">
    <span>I agree to the terms</span>
  </label>
</form>
</body></html>
"""


async def _run() -> bool:
    logger = get_logger(debug=False)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.set_content(HTML)
        await page.wait_for_selector("form")

        # Build an action that LOOKS like a recorded check on the
        # hidden ARIA checkbox, but with NO ``_hidden_input_type`` —
        # mimicking an older recording or an ARIA-only widget.
        loc = page.locator("#cb_agree")
        action = {
            "kind": "check",
            "field_id": "agree",
            "checked": True,
            "_hidden_input_name": "agree",
            # _hidden_input_type intentionally omitted
            "fingerprint": {"role": "checkbox", "type": "checkbox"},
        }

        ok = await _do_check(page, loc, action, logger=logger)

        is_checked = await page.evaluate(
            "() => document.getElementById('cb_agree').checked"
        )
        await browser.close()
        return ok and bool(is_checked)


@pytest.mark.asyncio
async def test_aria_checkbox_heal_without_hidden_input_type():
    """Heal must succeed when ``_hidden_input_type`` is missing."""
    ok = await _run()
    assert ok, (
        "ARIA-checkbox heal failed when _hidden_input_type was missing — "
        "the JS selector likely defaulted to type=\"radio\" and missed the input."
    )


if __name__ == "__main__":
    print("ok=", asyncio.run(_run()))
