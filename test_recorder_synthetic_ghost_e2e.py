"""End-to-end regression test for the Facebook-trademark synthetic-ghost bug.

User report (verbatim, in Vietnamese):
    "tôi chọn ô thứ 1 continue with trademark report, rồi tiếp tục i am
     the right owner để tiếp tục fill, nhưng khi ngay sau khi chọn nó
     lại sang mục i am reporting on behalf of someone else."

Mechanism (after investigation):
    1. The form's controlled-component framework (React/Facebook
       internal) listens for trusted clicks on each radio's wrapping
       label. When the user picks ``r_own`` ("I am the right owner")
       it dispatches a synthetic ``click`` on the next sibling
       (``r_be`` — "reporting on behalf of someone else") ~30 ms
       later as part of its re-render bookkeeping.
    2. Old recorder shipped TWO actions (real + ghost). Replay played
       both in order, ending in the wrong sibling.
    3. Even after the recorder is fixed (only the real action ships),
       the live page's ghost script still fires during replay because
       Playwright's ``page.click()`` is a trusted click — the form
       happily re-runs the sibling-flip on every replay tick. So the
       replay engine ALSO needs to verify state after each click and
       fall back to a synthetic-click-free heal path when it detects
       drift.

This test reproduces both halves end-to-end:
    * recorder must emit exactly the user's two clicks (no ghost
      siblings)
    * replay must end up with the user's two choices selected,
      regardless of how many times the page tries to flip them
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from logger import get_logger  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402
from recorder_v2 import OVERLAY_JS, _Session, build_config  # noqa: E402
from replay_engine import run_actions  # noqa: E402


HTML = """\
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>FB Trademark repro</title></head>
<body>
<form id="tmform">
  <fieldset>
    <legend>Step 1</legend>
    <label><input type="radio" name="step1" value="trademark" id="r_tm">
      Continue with trademark report</label><br>
    <label><input type="radio" name="step1" value="copyright" id="r_cp">
      Continue with copyright report</label><br>
    <label><input type="radio" name="step1" value="other" id="r_ot">
      Continue with something else</label>
  </fieldset>
  <fieldset>
    <legend>Step 2</legend>
    <label><input type="radio" name="step2" value="owner" id="r_own">
      I am the right owner</label><br>
    <label><input type="radio" name="step2" value="behalf" id="r_be">
      I am reporting on behalf of someone else</label><br>
    <label><input type="radio" name="step2" value="other" id="r_ot2">
      Other</label>
  </fieldset>
</form>
<script>
// React-style ghost: trusted click on a label dispatches a synthetic
// click on the *next* radio sibling 30ms later. Reproduces the exact
// pattern observed on the Facebook trademark form.
document.querySelectorAll('label').forEach((lab) => {
  lab.addEventListener('click', (origEv) => {
    if (!origEv.isTrusted) return;
    const r = lab.querySelector('input[type=radio]');
    if (!r) return;
    const all = Array.from(document.querySelectorAll(
      'input[name="' + r.name + '"]'));
    const i = all.indexOf(r);
    const sib = all[(i + 1) % all.length];
    if (!sib) return;
    setTimeout(() => {
      sib.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
    }, 30);
  });
});
</script>
</body></html>
"""


async def _capture_user_clicks(html_path: Path) -> dict:
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
        await page.goto(html_path.as_uri())
        await page.wait_for_selector("form")

        await page.check("#r_tm")
        await page.wait_for_timeout(150)
        await page.check("#r_own")
        await page.wait_for_timeout(300)

        await ctx.close()
        await browser.close()

    return build_config(
        sess, target_url=html_path.as_uri(), wait_for_selector="form",
        auto_template=False,
    )


async def _replay_and_read_state(actions: list, html_path: Path) -> dict:
    logger = get_logger(debug=False)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto(html_path.as_uri())
        await page.wait_for_selector("form")
        await run_actions(
            page, actions, ctx=None, logger=logger,
            threshold=0.55,
            action_delay_ms=50, action_jitter_ms=20,
        )
        await page.wait_for_timeout(300)
        state = await page.evaluate(
            """() => {
                const out = {};
                document.querySelectorAll('input[type=radio]').forEach((r) => {
                    if (r.checked) out[r.name] = r.value;
                });
                return out;
            }"""
        )
        await browser.close()
    return state


@pytest.mark.asyncio
async def test_facebook_trademark_radio_ghost_recorded_clean(tmp_path: Path):
    """Recorder must NOT ship the synthetic-ghost sibling click."""
    html_file = tmp_path / "form.html"
    html_file.write_text(HTML, encoding="utf-8")

    cfg = await _capture_user_clicks(html_file)
    actions = cfg["actions"]

    # User picked 2 radios; recorder must ship EXACTLY 2 check actions
    # (no ghost siblings). Allow auto-injected wait/load actions in the
    # action stream as long as the check count is right.
    checks = [a for a in actions if a.get("kind") == "check"]
    assert len(checks) == 2, (
        f"recorder shipped {len(checks)} check actions; "
        f"expected exactly 2 (the user's real clicks). actions={actions!r}"
    )

    # The two check actions must be on the user's chosen radios.
    radio_values = {(c.get("field_id"), c.get("radio_value")) for c in checks}
    assert ("step1", "trademark") in radio_values, (
        f"recorder lost the trademark click: {radio_values!r}"
    )
    assert ("step2", "owner") in radio_values, (
        f"recorder lost the right-owner click: {radio_values!r}"
    )


@pytest.mark.asyncio
async def test_facebook_trademark_radio_ghost_replay_lands_correct(tmp_path: Path):
    """Replay must leave the user's choices selected — no sibling-flip."""
    html_file = tmp_path / "form.html"
    html_file.write_text(HTML, encoding="utf-8")

    cfg = await _capture_user_clicks(html_file)
    state = await _replay_and_read_state(cfg["actions"], html_file)

    expected = {"step1": "trademark", "step2": "owner"}
    assert state == expected, (
        f"replay landed on wrong sibling: {state!r}, expected {expected!r}"
    )


@pytest.mark.asyncio
async def test_facebook_trademark_radio_ghost_replay_idempotent(tmp_path: Path):
    """Two consecutive replays must both land on the user's choices."""
    html_file = tmp_path / "form.html"
    html_file.write_text(HTML, encoding="utf-8")

    cfg = await _capture_user_clicks(html_file)
    state1 = await _replay_and_read_state(cfg["actions"], html_file)
    state2 = await _replay_and_read_state(cfg["actions"], html_file)

    expected = {"step1": "trademark", "step2": "owner"}
    assert state1 == expected, f"replay-1 wrong: {state1!r}"
    assert state2 == expected, f"replay-2 wrong: {state2!r}"


if __name__ == "__main__":
    # Allow running outside pytest for debugging.
    async def _dbg():
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            html = tmp / "form.html"
            html.write_text(HTML, encoding="utf-8")
            cfg = await _capture_user_clicks(html)
            print("captured actions:")
            for a in cfg["actions"]:
                print(" ", a.get("kind"), a.get("field_id"), a.get("radio_value"))
            state = await _replay_and_read_state(cfg["actions"], html)
            print("final state:", state)

    asyncio.run(_dbg())
