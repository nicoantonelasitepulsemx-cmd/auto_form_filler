"""Regression test: recorder + replay must tick the *correct* checkbox when
multiple checkboxes share the same `name` attribute (e.g. Facebook trademark
form's ``content_type[]`` group with options Photo / Ad / Page / Other).

Before the upgrade the recorder shipped four actions all targeting the FIRST
checkbox in the group (because `_findAssociatedInput` walked up the DOM tree
and grabbed the first matching input via `querySelector`). The replay engine
then ticked only one checkbox — the same one — four times instead of all four
distinct options.
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


HTML = """
<!doctype html><html><body>
<form id="report">
  <fieldset>
    <legend>Content You Want to Report</legend>
    <div class="content-types">
      <label class="row"><input type="checkbox" name="content_type[]" value="photo_video_post"
             style="position:absolute;left:-9999px"> <span>Photo, video or post</span></label>
      <label class="row"><input type="checkbox" name="content_type[]" value="ad"
             style="position:absolute;left:-9999px"> <span>Ad</span></label>
      <label class="row"><input type="checkbox" name="content_type[]" value="page_group_profile"
             style="position:absolute;left:-9999px"> <span>Page, group or profile</span></label>
      <label class="row"><input type="checkbox" name="content_type[]" value="other"
             style="position:absolute;left:-9999px"> <span>Other</span></label>
    </div>
  </fieldset>
</form>
</body></html>
"""


def _data_url() -> str:
    """Return a data: URL for HTML.  Using `page.goto(data_url)` instead of
    `set_content` is REQUIRED so that ``context.add_init_script`` actually
    runs (init scripts only fire on navigation)."""
    import urllib.parse
    return "data:text/html;charset=utf-8," + urllib.parse.quote(HTML)


async def _capture_via_proxy_clicks() -> dict:
    """Drive the page by clicking the *visible spans* (so the recorder hits
    its proxy-click branch — the same branch that mis-fired on the FB form)."""
    sess = _Session()

    async def on_record(payload: str) -> None:
        sess.add(json.loads(payload))

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        await ctx.expose_function("__afRecord", on_record)
        await ctx.expose_function("__afFinish", lambda: None)
        await ctx.expose_function("__afCancel", lambda: None)
        await ctx.expose_function("__afUndo",   lambda: None)
        await ctx.add_init_script(OVERLAY_JS)
        page = await ctx.new_page()
        await page.goto(_data_url())
        # Click each visible span — these are *not* labels, just children of
        # the label, so we go through the closest("label") path.
        for label in [
            "Photo, video or post",
            "Ad",
            "Page, group or profile",
            "Other",
        ]:
            await page.get_by_text(label, exact=True).click()
        await asyncio.sleep(0.3)
        await ctx.close()
        await browser.close()

    return build_config(
        sess,
        target_url="about:blank",
        wait_for_selector=None,
        auto_template=False,
    )


async def _replay_and_assert(config: dict) -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto(_data_url())
        logger = get_logger(debug=False)
        filled, skipped = await run_actions(
            page, config["actions"], ctx={}, dry_run=False, logger=logger,
        )
        state = await page.evaluate("""() => Array.from(
            document.querySelectorAll('input[name="content_type[]"]'))
            .filter(i => i.checked).map(i => i.value)""")
        await ctx.close()
        await browser.close()

    print(f"[replay] filled={filled} skipped={skipped} checked={state}")
    expected = {"photo_video_post", "ad", "page_group_profile", "other"}
    got = set(state)
    assert expected.issubset(got), (
        f"expected all four checkboxes ticked but got {got!r} — "
        f"recorder lost option identity (this is the FB trademark bug)"
    )


async def main() -> None:
    config = await _capture_via_proxy_clicks()

    # Sanity-check the captured config: every action should target a
    # *distinct* value, otherwise we know the recorder still mis-identifies
    # the input.
    values = [a.get("radio_value") for a in config["actions"] if a.get("kind") == "check"]
    print(f"[capture] radio_values: {values}")
    assert len(set(values)) == 4, (
        f"recorder shipped duplicate / missing radio_value for proxy clicks: {values}"
    )

    await _replay_and_assert(config)
    print("[ok] recorder + replay tick all four content_type checkboxes")


if __name__ == "__main__":
    asyncio.run(main())
