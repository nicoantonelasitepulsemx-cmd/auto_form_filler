"""End-to-end test for A15: recorder must IGNORE programmatic
``change`` / ``click`` events fired by frameworks (React/Vue/etc.) and
only ship events that originate from a real user gesture.

The synthetic page sets up a radio group similar in spirit to the
Facebook trademark form: clicking one radio causes the page's JS to
re-dispatch ``change`` events on the OTHER radios with
``isTrusted=false`` (because they come from ``dispatchEvent``).

Before the A15 fix, the recorder shipped a ``check`` action for every
ghost event, polluting the recording with the wrong radio_value.
After the fix, only the real user click is captured.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import recorder_v2  # noqa: E402

GHOST_PAGE = """
<!doctype html>
<html><body>
<form id="f">
  <fieldset>
    <legend>Who are you?</legend>
    <label><input type="radio" name="role" value="alice"> Alice</label>
    <label><input type="radio" name="role" value="bob">   Bob</label>
    <label><input type="radio" name="role" value="carol"> Carol</label>
  </fieldset>
</form>
<script>
  // Simulate a React-style controlled-input re-render: every time the
  // user really picks a radio, replay synthetic change events on the
  // OTHER two so the page's "controller" can read them. These events
  // have isTrusted === false.
  let busy = false;
  document.querySelectorAll('input[type=radio]').forEach((inp) => {
    inp.addEventListener("change", () => {
      if (busy) return;
      if (!inp.checked) return;
      busy = true;
      try {
        // Fire ghost change events on the OTHER radios, then on this one
        // again, all programmatically. None of these are user gestures.
        document.querySelectorAll('input[type=radio]').forEach((other) => {
          other.dispatchEvent(new Event("change", { bubbles: true }));
        });
      } finally { busy = false; }
    });
  });
</script>
</body></html>
"""


async def main() -> int:
    from playwright.async_api import async_playwright

    tmp = Path("/tmp/_ghost_form.html")
    tmp.write_text(GHOST_PAGE, encoding="utf-8")
    actions: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto(f"file://{tmp}")

        # Install just the OVERLAY_JS — we don't need the full record_to
        # harness, only the in-page event hooks.
        def _ship(_src: object, payload: str) -> None:
            import json as _json
            try:
                actions.append(_json.loads(payload))
            except Exception:
                pass

        await page.expose_binding("__afRecord", _ship)
        await page.evaluate(recorder_v2.OVERLAY_JS)

        # Real user click on the FIRST radio (Alice). The page's listener
        # will then dispatch ghost change events on Alice/Bob/Carol.
        await page.click("text=Alice")
        # Give the ghost event burst a chance to flush.
        await page.wait_for_timeout(150)

        await browser.close()

    check_actions = [a for a in actions if a.get("kind") == "check"]
    if len(check_actions) != 1:
        print(
            f"FAIL: expected exactly 1 check action, got {len(check_actions)}"
        )
        for i, a in enumerate(check_actions):
            print(f"  {i}: radio_value={a.get('radio_value')!r} "
                  f"isTrusted-filter would have caught={a.get('radio_value') != 'alice'}")
        return 1

    only = check_actions[0]
    if only.get("radio_value") != "alice":
        print(f"FAIL: wrong radio_value: {only.get('radio_value')!r}")
        return 1

    print("[ok] recorder filtered out 2 ghost change events (isTrusted=false)")
    print(f"     captured exactly 1 check: radio_value='alice'")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
