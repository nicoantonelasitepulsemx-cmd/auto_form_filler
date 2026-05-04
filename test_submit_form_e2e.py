"""Playwright e2e regression: ``submit_form`` must actually click
a real Submit button on a real (in-memory) HTML page.

Reproduces the layout of Facebook trademark form's last step (the
screenshot the user shared):

    [ I have a court order ]    o
    Declaration Statement
    Electronic signature
    [           ]
                                 [ Submit ]

When the user clicks "submit after fill" in the GUI, the
fallback fires after the action stream and must successfully
click that Submit button to actually send the form.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import auto_fill  # noqa: E402

try:
    from playwright.async_api import async_playwright
except Exception:  # pragma: no cover — skip when Playwright not installed
    pytestmark = pytest.mark.skip("playwright not available")


_HTML = """
<!doctype html>
<html><body>
  <form id="f" onsubmit="document.title='SUBMITTED'; return false;">
    <h3>Declaration Statement</h3>
    <label>Electronic signature
      <input name="sig" required />
    </label>
    <button type="submit">Submit</button>
  </form>
</body></html>
"""


class _Logger:
    def __init__(self) -> None:
        self.lines: list[str] = []
    def info(self, m, *a, **k):    self.lines.append(f"I {m}")
    def debug(self, m, *a, **k):   self.lines.append(f"D {m}")
    def warning(self, m, *a, **k): self.lines.append(f"W {m}")
    def error(self, m, *a, **k):   self.lines.append(f"E {m}")


@pytest.mark.asyncio
async def test_submit_form_clicks_real_button() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(_HTML)
        # Pre-fill so the submit handler doesn't bounce on "required".
        await page.fill('input[name="sig"]', "Antonela")
        logger = _Logger()
        ok = await auto_fill.submit_form(page, {}, logger)
        assert ok is True, f"submit_form returned False; logs={logger.lines}"
        # The form's onsubmit sets the document title to "SUBMITTED".
        title = await page.title()
        assert title == "SUBMITTED", (
            f"submit handler never fired; title={title!r} logs={logger.lines}"
        )
        await browser.close()


@pytest.mark.asyncio
async def test_submit_form_skips_disabled_button_e2e() -> None:
    """If the recorded form has the Submit button disabled (e.g. user
    forgot to fill a required field), the fallback must NOT click it
    and instead return False so the caller can surface the issue."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(
            """<!doctype html><html><body>
            <form><button type="submit" disabled>Submit</button></form>
            </body></html>"""
        )
        logger = _Logger()
        ok = await auto_fill.submit_form(page, {}, logger)
        assert ok is False, (
            f"clicked a disabled button; logs={logger.lines}"
        )
        await browser.close()


@pytest.mark.asyncio
async def test_submit_form_handles_multiple_buttons() -> None:
    """When several buttons match, the first VISIBLE one wins. The
    user's tm4.json has several stages with their own Submit buttons
    and we want the first reachable one to fire."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(
            """<!doctype html><html><body>
            <button type="submit" style="display:none">Hidden</button>
            <form id="f" onsubmit="document.title='HIT'; return false;">
              <button type="submit">Submit</button>
            </form>
            </body></html>"""
        )
        logger = _Logger()
        ok = await auto_fill.submit_form(page, {}, logger)
        assert ok is True, f"logs={logger.lines}"
        assert (await page.title()) == "HIT"
        await browser.close()
