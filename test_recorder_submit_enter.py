"""Regression test: the recorder must capture form submit even when the user
presses Enter inside a text input (no click on the submit button).

Previously the click-only path meant Enter submissions were lost, and replay
had no `submit` action to reproduce them — the user's "nhấn Enter để Submit"
case was silently dropped.

Also exercises the new pre_submit_flush so the last typed value in a
React-style controlled input lands in the config even when `change` never
fires before navigation.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from playwright.async_api import async_playwright  # noqa: E402
from recorder_v2 import OVERLAY_JS, _Session, build_config  # noqa: E402


HTML = """
<!doctype html><html><body>
<form id="demo" onsubmit="return false;">
  <label for="email">Email</label>
  <input id="email" name="email" type="email" />
  <label for="msg">Message</label>
  <textarea id="msg" name="msg"></textarea>
  <button type="submit" id="go">Submit</button>
</form>
</body></html>
"""


def _data_url() -> str:
    import urllib.parse
    return "data:text/html;charset=utf-8," + urllib.parse.quote(HTML)


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
        await page.goto(_data_url())

        # 1. Type into email and press Enter — no click on submit button.
        email = page.locator("#email")
        await email.click()
        await page.keyboard.type("alice@example.com", delay=10)
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.3)

        await ctx.close()
        await browser.close()

    return build_config(
        sess, target_url="about:blank", wait_for_selector=None, auto_template=False,
    )


async def main() -> None:
    cfg = await _capture()
    kinds = [a.get("kind") for a in cfg["actions"]]
    print(f"[capture] kinds: {kinds}")
    assert "fill" in kinds, f"expected a fill action for email; got {kinds!r}"

    # The submit must appear — either explicitly in actions, or as cfg['submit'].
    submit_in_actions = any(a.get("kind") == "submit" for a in cfg["actions"])
    submit_top = cfg.get("submit") is not None
    assert submit_in_actions or submit_top, (
        "recorder lost the form-submit event (Enter keypress) \u2014 "
        f"actions={kinds!r} submit={cfg.get('submit')!r}"
    )

    # Value for email must be the full typed string (pre_submit_flush).
    emails = [a.get("value") for a in cfg["actions"]
              if a.get("kind") == "fill" and a.get("field_id") == "email"]
    assert emails, f"no email fill captured; actions={cfg['actions']}"
    assert emails[-1] == "alice@example.com", (
        f"pre_submit_flush lost the last keystrokes: got {emails!r}"
    )
    print("[ok] recorder captures form-submit-via-Enter + full email value")


if __name__ == "__main__":
    asyncio.run(main())
