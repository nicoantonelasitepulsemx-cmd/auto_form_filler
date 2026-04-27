"""End-to-end test for the v2 stack.

Steps:
  1. Drive `test_form.html` programmatically *with the v2 overlay JS injected*
     so we capture a real-looking action stream.
  2. Build a v2 config from the captured actions.
  3. Replay the config against a *fresh* page and assert the fields actually
     receive the recorded values (read DOM state via JS).
  4. Run the same config through `WorkerPool` with two synthetic accounts
     using `value_template` to confirm per-account vars + concurrency work.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from accounts import Account  # noqa: E402
from logger import get_logger  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402
from recorder_v2 import OVERLAY_JS, _Session, build_config  # noqa: E402
from replay_engine import run_actions  # noqa: E402
from worker_pool import Task, WorkerPool  # noqa: E402

HERE = Path(__file__).parent
URL  = (HERE / "test_form.html").resolve().as_uri()


async def _capture_actions() -> dict:
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
        await page.goto(URL)

        # Drive the form like a user would.
        await page.fill("#email", "phu@example.com");           await page.locator("#email").blur()
        await page.fill("#full_name", "Nguyen Phu");            await page.locator("#full_name").blur()
        await page.fill("#message", "hello v2");                await page.locator("#message").blur()
        await page.select_option("#country", "vn")
        await page.check("#agree")
        await page.check('input[name=plan][value=pro]')
        # Click submit (captured as submit action by our recorder)
        # We don't actually wait for navigation since the form has no action= attribute.
        await page.click("#submit")
        await asyncio.sleep(0.3)
        await ctx.close()
        await browser.close()

    config = build_config(sess, target_url=URL)
    print(f"[capture] {len(config['actions'])} action(s); submit={'yes' if config.get('submit') else 'no'}")
    return config


async def _replay_and_verify(config: dict) -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto(URL)

        logger = get_logger(debug=False)
        filled, skipped = await run_actions(
            page, config["actions"], ctx={}, dry_run=False, logger=logger
        )
        print(f"[replay] filled={filled}  skipped={skipped}")

        # Read DOM state to verify every recorded value was applied.
        state = await page.evaluate("""() => ({
            email:      document.querySelector('#email').value,
            full_name:  document.querySelector('#full_name').value,
            message:    document.querySelector('#message').value,
            country:    document.querySelector('#country').value,
            agree:      document.querySelector('#agree').checked,
            plan:       (document.querySelector('input[name=plan]:checked') || {}).value,
        })""")
        print(f"[replay] DOM state: {state}")

        await ctx.close()
        await browser.close()

        assert state["email"]     == "phu@example.com",  state
        assert state["full_name"] == "Nguyen Phu",       state
        assert state["message"]   == "hello v2",         state
        assert state["country"]   == "vn",               state
        assert state["agree"]     is True,               state
        assert state["plan"]      == "pro",              state
        assert filled >= 6, f"expected >=6 filled actions, got {filled}"
        assert skipped == 0,  f"expected 0 skipped, got {skipped}"
        print("[replay] all assertions OK")


async def _replay_with_workerpool(config: dict) -> None:
    """Same config, but driven by WorkerPool with 2 'accounts' that each fill
    a different email/name via `value_template`."""

    # Inject value_template into the relevant text fields.
    cfg = json.loads(json.dumps(config))
    for a in cfg["actions"]:
        if a.get("field_id") == "email":
            a["value_template"] = "{email}"
        if a.get("field_id") == "full_name":
            a["value_template"] = "{full_name}"

    accts = [
        Account(name="alpha", vars={"email": "alpha@example.com", "full_name": "Alpha"}),
        Account(name="beta",  vars={"email": "beta@example.com",  "full_name": "Beta"}),
    ]
    # Make each account headless and use no proxy.
    for a in accts:
        a.headless = True

    events: list[tuple[str, dict]] = []
    pool = WorkerPool(
        accts,
        max_concurrency=2,
        report_cb=lambda evt, payload: events.append((evt, payload)),
        debug=False,
    )
    tasks = [
        Task(config=cfg, vars={}, label="alpha"),
        Task(config=cfg, vars={}, label="beta"),
    ]
    results = await pool.run_tasks(tasks)
    print(f"[pool] {sum(r.ok for r in results)}/{len(results)} task(s) OK")
    for r in results:
        print(f"       {r.account:>5} ok={r.ok} filled={r.filled} skipped={r.skipped} attempts={r.attempts}")
    assert all(r.ok for r in results), [r.error for r in results if not r.ok]
    assert sum(1 for e, _ in events if e == "task_done") == 2
    print("[pool] all assertions OK")


async def main() -> None:
    config = await _capture_actions()
    out = HERE / "samples" / "v2_recorded.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[capture] config -> {out}")

    await _replay_and_verify(config)
    await _replay_with_workerpool(config)


if __name__ == "__main__":
    asyncio.run(main())
