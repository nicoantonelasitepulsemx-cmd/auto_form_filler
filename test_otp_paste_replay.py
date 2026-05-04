"""Integration test: replay engine handles `kind="otp_paste"` end-to-end.

We:
  1. Spin up the same fake kuku.lu server used by ``test_kuku_lu.py`` and
     deliver a confirmation code into a virtual inbox.
  2. Open a real Playwright Chromium page on a tiny ``data:`` URL with a
     single ``<input id="otp">``.
  3. Build an ``otp_paste`` action and call ``run_action`` with a
     ``Kuku`` instance attached to ``ctx['_kuku']``.
  4. Assert the input ended up populated with the delivered code.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import urllib.parse

from aiohttp import web
from playwright.async_api import async_playwright

sys.path.insert(0, ".")

from kuku_lu import Kuku  # type: ignore
from replay_engine import run_action  # type: ignore
from test_kuku_lu import FakeKuku, _spawn_fake  # type: ignore


PAGE_HTML = """
<!doctype html>
<meta charset="utf-8">
<form>
  <label for="otp">Confirmation code</label>
  <input id="otp" name="confirmation_code" type="text" autocomplete="one-time-code">
</form>
"""


def _make_logger() -> logging.Logger:
    log = logging.getLogger("otp-test")
    log.setLevel(logging.INFO)
    if not log.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        log.addHandler(h)
    return log


async def main() -> None:
    fake, runner, base = await _spawn_fake()
    try:
        async with await Kuku.from_requests(base_url=base) as k:
            address = await k.create_address()
            # Drop a Facebook-style confirmation mail so wait_for_code sees it.
            fake.deliver(address, body_html=(
                "<html><body>Your <b>Facebook</b> confirmation code is "
                "<b>826341</b>. Don't share.</body></html>"
            ))

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.goto(
                    "data:text/html;charset=utf-8," +
                    urllib.parse.quote(PAGE_HTML)
                )
                # Pre-focus the input so the resolver has something to match
                # if we ever extend the test to record one.
                await page.click("#otp")

                action = {
                    "kind": "otp_paste",
                    "field_id": "confirmation_code",
                    "field_type": "text",
                    "selectors": [
                        {"strategy": "css", "selector": "#otp", "weight": 90},
                        {"strategy": "css", "selector": "input[name='confirmation_code']", "weight": 80},
                    ],
                    "fingerprint": {
                        "tag": "input",
                        "type": "text",
                        "name": "confirmation_code",
                        "id": "otp",
                        "accessible_name": "Confirmation code",
                    },
                    "frame_chain": ["top"],
                    "value": "000000",  # the recorded (now-stale) value
                    "input_method": "type",
                    "source": {
                        "kind": "kuku.lu",
                        "address": address,
                        "regex": r"(?<!\d)(\d{5,8})(?!\d)",
                        "from_filter": "facebook",
                        "timeout_ms": 8000,
                    },
                }

                ctx = {"_kuku": k}
                ok = await run_action(
                    page, action, ctx=ctx,
                    threshold=0.4, dry_run=False,
                    logger=_make_logger(),
                )
                assert ok, "run_action returned False"

                live = await page.eval_on_selector("#otp", "el => el.value")
                assert live == "826341", f"expected 826341, got {live!r}"
                print(f"[ok] otp_paste replay typed {live!r} into the OTP input")

                # Run again with no client — should fall back to the
                # recorded value instead of crashing.
                await page.eval_on_selector("#otp", "el => el.value = ''")
                ok2 = await run_action(
                    page, action, ctx={},
                    threshold=0.4, dry_run=False,
                    logger=_make_logger(),
                )
                assert ok2
                live2 = await page.eval_on_selector("#otp", "el => el.value")
                assert live2 == "000000", f"expected fallback 000000, got {live2!r}"
                print("[ok] fallback to recorded value when no Kuku in ctx")

                await browser.close()
    finally:
        await runner.cleanup()
    print("\n[ok] otp_paste replay end-to-end")


if __name__ == "__main__":
    asyncio.run(main())
