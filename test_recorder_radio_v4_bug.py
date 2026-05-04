"""Regression test reproducing the v3 radio-mistargeting bug the user reported.

User report (Vietnamese):
    "tôi chọn ô thứ 1 continue with trademark report, rồi tiếp tục
     i am the right owner để tiếp tục fill, nhưng khi ngay sau khi
     chọn nó lại sang mục i am reporting on behalf of someone else"

Translation: "I picked option 1 'continue with trademark report', then
'I am the right owner' to keep filling, but right after selecting it
flipped to 'I am reporting on behalf of someone else'."

Three independent root causes contribute to this symptom:

  R1. The recorder's role-based ``click`` handler reads ``aria-checked``
      *before* React updates state, so it ships ``check checked=false``.
      On replay ``_do_check`` calls ``loc.uncheck()`` first, which on a
      role=radio div either no-ops or worse triggers a deselect cycle
      that leaves the FORM back at its initial / previously-rendered
      state — which on Facebook's trademark form is "I am reporting
      on behalf of someone else".

  R2. The MutationObserver on aria-checked then ships a SECOND action
      ``check checked=true`` for the same option, but by the time replay
      reaches that action the form has already settled on the wrong
      sibling (per R1) AND the resolver, faced with two ARIA radios in
      the same fieldset whose fingerprints differ only by accessible
      name, sometimes picks the wrong sibling (the one currently
      ``aria-checked=true``, i.e. the wrong one) because Playwright's
      ``get_by_role("radio", name=...)`` accessibility-tree match is
      whitespace-tolerant but the fingerprint scoring isn't strict
      enough to *reject* the mismatched sibling.

  R3. The replay engine's normal-path handler, when ``check`` fails
      with ``loc.uncheck()`` on a role=radio (uncheck is undefined for
      ARIA radios), falls through to a generic JS `click()` walk that
      may land on a sibling.

This test pins all three down. It builds a minimal React-style ARIA
radio group (two options, only accessible name + value differ),
records a real user click on option A, then replays the recording on
a fresh page and asserts that option A — and only option A — ends up
checked.

The fix lives in ``recorder_v2.py`` (R1: ship ``checked=true`` for
role=radio clicks; ghost-suppress sibling false flips), ``replay_engine.py``
(R2: never uncheck a radio; verify radio_value after click; retry on
mismatch), and ``resolver_v2.py`` (R3: weight accessible_name match
strongly so wrong-sibling candidates are rejected).
"""
from __future__ import annotations

import asyncio
import json
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from logger import get_logger  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402
from recorder_v2 import OVERLAY_JS, _Session, build_config  # noqa: E402
from replay_engine import run_actions  # noqa: E402


# Two role=radio divs sharing the same fieldset, distinguished only by
# accessible name (via aria-labelledby) and a stable data-value attribute.
# This is structurally what Facebook's trademark form uses for its
# "I am the right owner" / "I am reporting on behalf..." radio group.
HTML = """
<!doctype html><html><body>
<form id="report">
  <fieldset role="radiogroup" aria-labelledby="who-legend">
    <span id="who-legend">Who are you?</span>

    <div role="radio" aria-checked="false"
         aria-labelledby="lbl-self" data-value="self"
         tabindex="0" class="opt opt-self">
      <span class="bullet"></span>
      <span id="lbl-self">I am the rights owner</span>
    </div>

    <div role="radio" aria-checked="false"
         aria-labelledby="lbl-agent" data-value="agent"
         tabindex="0" class="opt opt-agent">
      <span class="bullet"></span>
      <span id="lbl-agent">I am reporting on behalf of someone else</span>
    </div>
  </fieldset>
</form>
<script>
  // React-ish behaviour: clicking a radio updates aria-checked on the
  // group, AND fires a synthetic change burst on every radio (so the
  // controller can re-read state). The burst events are isTrusted=false.
  const radios = Array.from(document.querySelectorAll('div[role="radio"]'));
  for (const r of radios) {
    r.addEventListener('click', () => {
      for (const rr of radios) rr.setAttribute('aria-checked',
        rr === r ? 'true' : 'false');
      // Fire a ghost change burst on every radio (synthetic, not user).
      for (const rr of radios) {
        rr.dispatchEvent(new Event('change', {bubbles: true}));
      }
    });
  }
</script>
</body></html>
"""


def _data_url() -> str:
    return "data:text/html;charset=utf-8," + urllib.parse.quote(HTML)


async def _capture_click_on_self() -> dict:
    """Record a user click on the 'I am the rights owner' radio."""
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
        # Click the visible label text — same as a user clicking on
        # "I am the rights owner". Triggers React-style state update
        # and the ghost change burst.
        await page.get_by_text("I am the rights owner", exact=True).click()
        await asyncio.sleep(0.4)  # let ghost events flush
        await ctx.close()
        await browser.close()

    return build_config(
        sess,
        target_url="about:blank",
        wait_for_selector=None,
        auto_template=False,
    )


async def _replay_and_check(config: dict) -> dict:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto(_data_url())
        logger = get_logger(debug=False)
        filled, skipped = await run_actions(
            page, config["actions"], ctx={}, dry_run=False, logger=logger,
        )
        state = await page.evaluate(
            """() => {
                const out = {};
                for (const r of document.querySelectorAll('div[role="radio"]')) {
                    out[r.dataset.value] = r.getAttribute('aria-checked') === 'true';
                }
                return out;
            }"""
        )
        await ctx.close()
        await browser.close()
    return {"filled": filled, "skipped": skipped, "state": state}


async def _run_full_check() -> dict:
    """Capture a radio click, then replay, returning a single dict so
    the pytest test can run the slow Playwright pipeline once.
    """
    config = await _capture_click_on_self()
    res = await _replay_and_check(config)
    return {"config": config, "replay": res}


def test_v4_radio_bug_recorder_does_not_ship_false_flips():
    """v4 R1: clicking a role=radio must not produce check-false ghosts."""
    out = asyncio.run(_run_full_check())
    config = out["config"]
    check_actions = [a for a in config["actions"] if a.get("kind") == "check"]

    # At least one action must select 'self'.
    self_actions_true = [
        a for a in check_actions
        if (a.get("fingerprint") or {}).get("accessible_name") == "I am the rights owner"
        and a.get("checked") is True
    ]
    assert self_actions_true, (
        "no check-true action shipped for 'I am the rights owner'"
        f" — got: {check_actions}"
    )

    # The recorder must NEVER ship check checked=false for an ARIA
    # radio. For role=radio / type=radio elements the only legitimate
    # user action is *selecting* (checked=true).
    bad = [
        a for a in check_actions
        if a.get("checked") is False
        and (
            (a.get("fingerprint") or {}).get("role") == "radio"
            or a.get("_hidden_input_type") == "radio"
        )
    ]
    assert not bad, (
        f"recorder shipped {len(bad)} check-false action(s) on radio elements: {bad}"
    )

    # No sibling-flip ghost on the agent radio.
    agent_actions = [
        a for a in check_actions
        if (a.get("fingerprint") or {}).get("accessible_name")
            == "I am reporting on behalf of someone else"
    ]
    assert not agent_actions, (
        f"recorder shipped {len(agent_actions)} sibling ghost action(s) on "
        f"the untouched agent radio: {agent_actions}"
    )

    # Replay end state.
    state = out["replay"]["state"]
    assert state.get("self") is True, f"replay did not select 'self' radio: {state}"
    assert state.get("agent") is not True, (
        f"replay accidentally selected 'agent' radio (the bug!): {state}"
    )


def main() -> int:
    """Manual / CLI entrypoint kept for ad-hoc debugging."""
    out = asyncio.run(_run_full_check())
    config = out["config"]
    check_actions = [a for a in config["actions"] if a.get("kind") == "check"]
    print(f"[capture] check actions: {len(check_actions)}")
    for i, a in enumerate(check_actions):
        fp = a.get("fingerprint") or {}
        print(
            f"  {i}: checked={a.get('checked')!r} "
            f"radio_value={a.get('radio_value')!r} "
            f"accessible_name={fp.get('accessible_name')!r}"
        )
    print(f"[replay] {out['replay']}")
    state = out["replay"]["state"]
    if state.get("self") is True and state.get("agent") is not True:
        print("[ok] v4 radio bug fix verified — only 'self' is checked")
        return 0
    print("[FAIL] replay state mismatch")
    return 1


if __name__ == "__main__":
    sys.exit(main())
