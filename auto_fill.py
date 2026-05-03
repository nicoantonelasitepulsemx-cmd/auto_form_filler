"""auto_fill.py — async-Playwright form-filling engine driven by a JSON config.

Usage:
    python auto_fill.py --config config.json --dry-run
    python auto_fill.py --config config.json --headless
    python auto_fill.py --config config.json --submit
    python auto_fill.py --config config.json --debug
    python auto_fill.py --profile facebook_dmca --debug
    python auto_fill.py --config config.json --proxy http://user:pass@host:8080
    python auto_fill.py --config config.json --proxy-list proxies.txt --proxy-rotate random
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

from playwright.async_api import (
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from accounts import accounts_from_proxies, load_accounts
from captcha import detect_captcha, pause_for_human
from logger import get_logger
from proxy_utils import (
    add_cli_args as _add_proxy_cli_args,
    load_proxy_dicts,
    mask_proxy,
    resolve_proxy,
)
from replay_engine import run_actions as _run_v2_actions, run_action as _run_v2_action
from target_resolver import resolve_target
from worker_pool import Task, WorkerPool


# --------------------------------------------------------------------------------------
#  Field-type detection + filling
# --------------------------------------------------------------------------------------


async def detect_field_type(loc: Locator, declared: Optional[str]) -> str:
    """Return one of: text, textarea, select, radio, checkbox, file."""
    if declared:
        return declared.lower()
    try:
        tag = (await loc.evaluate("el => el.tagName.toLowerCase()")) or ""
    except Exception:
        tag = ""
    if tag == "textarea":
        return "textarea"
    if tag == "select":
        return "select"
    if tag == "input":
        try:
            t = (await loc.get_attribute("type")) or "text"
        except Exception:
            t = "text"
        t = t.lower()
        if t in ("checkbox",):
            return "checkbox"
        if t in ("radio",):
            return "radio"
        if t == "file":
            return "file"
        # text, email, url, tel, search, password, number, ...
        return "text"
    return "text"


async def highlight(loc: Locator) -> None:
    """Briefly highlight a matched element in the page (debug mode only)."""
    try:
        await loc.evaluate(
            """(el) => {
                const prevOutline = el.style.outline;
                const prevShadow = el.style.boxShadow;
                el.style.outline = '3px solid #ff3c00';
                el.style.boxShadow = '0 0 8px 2px rgba(255,60,0,0.7)';
                try { el.scrollIntoView({block: 'center'}); } catch (e) {}
                setTimeout(() => {
                    el.style.outline = prevOutline;
                    el.style.boxShadow = prevShadow;
                }, 1200);
            }"""
        )
    except Exception:
        pass


def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "y", "on", "checked")
    return bool(value)


async def fill_field(
    page: Page,
    loc: Locator,
    value: Any,
    field_type: str,
    dry_run: bool,
    logger,
) -> bool:
    if dry_run:
        logger.info(f"  [DRY ] would fill ({field_type}) -> {value!r}")
        return True

    try:
        ftype = field_type.lower()
        if ftype in ("text", "email", "url", "tel", "search", "password", "number"):
            await loc.fill(str(value))
        elif ftype == "textarea":
            # Playwright `fill` already handles \n correctly inside textareas.
            await loc.fill(str(value))
        elif ftype in ("select", "dropdown"):
            try:
                await loc.select_option(label=str(value))
            except Exception:
                try:
                    await loc.select_option(value=str(value))
                except Exception:
                    await loc.select_option(str(value))
        elif ftype == "radio":
            await loc.check()
        elif ftype == "checkbox":
            if _is_truthy(value):
                await loc.check()
            else:
                await loc.uncheck()
        elif ftype == "file":
            paths = value if isinstance(value, list) else [value]
            await loc.set_input_files(paths)
        else:
            await loc.fill(str(value))
        return True
    except Exception as exc:
        logger.error(f"  [FILL_ERR] {field_type}: {exc}")
        return False


# --------------------------------------------------------------------------------------
#  Field-level orchestration
# --------------------------------------------------------------------------------------


async def fill_multi_textarea(
    page: Page,
    targets: list[dict[str, Any]],
    values: list,
    dry_run: bool,
    debug: bool,
    logger,
) -> bool:
    """Fill a list of values into N matching <textarea>s (one value per textarea)."""
    locator = None
    used = None
    for t in targets:
        strategy = (t.get("strategy") or "css").lower()
        selector = t.get("selector", "")
        # Only "set" strategies make sense here — those that can match multiple nodes.
        if strategy in ("placeholder", "css", "name", "type", "id"):
            try:
                cand = page.locator(selector)
                if await cand.count() >= 1:
                    locator = cand
                    used = t
                    break
            except Exception:
                continue
    if locator is None:
        cand = page.locator("textarea")
        if await cand.count() >= 1:
            locator = cand
            used = {"strategy": "fallback", "selector": "textarea"}

    if locator is None:
        logger.warning("  [MULTI] no textarea matched")
        return False

    n = await locator.count()
    logger.info(f"  [MULTI] matched {n} textarea(s) via {used['strategy']}")

    success = True
    for i, val in enumerate(values):
        if i >= n:
            logger.warning(f"  [MULTI] only {n} fields available, skipping value #{i + 1}")
            break
        item = locator.nth(i)
        if debug:
            await highlight(item)
        ok = await fill_field(page, item, val, "textarea", dry_run, logger)
        if not ok:
            success = False
    return success


async def fill_one_field(
    page: Page,
    field: dict,
    dry_run: bool,
    debug: bool,
    logger,
) -> bool:
    fid = field.get("field_id", "<unnamed>")
    targets = field.get("targets", [])
    value = field.get("value")
    ftype_decl = field.get("field_type")

    # Expand {{date}}, {{uuid4}}, {{random_email}}, {{env:HOME}}, ... so each
    # run gets a fresh value when the user templates fields.
    try:
        from value_templates import expand as _expand_tpl
        value = _expand_tpl(value)
    except Exception:
        pass

    logger.info(f"[FIELD] {fid}")

    if ftype_decl == "multi_textarea" and isinstance(value, list):
        return await fill_multi_textarea(page, targets, value, dry_run, debug, logger)

    last_err: Optional[str] = None
    for attempt in range(1, 4):
        loc, used = await resolve_target(page, targets, logger=logger)
        if loc is None:
            last_err = "no strategy matched"
            await asyncio.sleep(0.4)
            continue
        try:
            await loc.wait_for(state="attached", timeout=3000)
        except Exception:
            pass
        if debug:
            await highlight(loc)
        ftype = await detect_field_type(loc, ftype_decl)
        ok = await fill_field(page, loc, value, ftype, dry_run, logger)
        if ok:
            logger.info(
                f"  [SUCCESS] Filled '{fid}' via strategy: {used['strategy']} "
                f"(selector={used['selector']!r}, type={ftype})"
            )
            return True
        last_err = "fill failed"
        await asyncio.sleep(0.4)
    logger.warning(f"  [SKIP] '{fid}' — {last_err}")
    return False


# --------------------------------------------------------------------------------------
#  Submit
# --------------------------------------------------------------------------------------


async def submit_form(page: Page, config: dict, logger) -> None:
    selectors = config.get("submit_selectors") or [
        'button[type="submit"]',
        'input[type="submit"]',
        'button:has-text("Submit")',
        'button:has-text("Send")',
        'button:has-text("Continue")',
    ]
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if await btn.count() > 0:
                logger.info(f"[SUBMIT] clicking {sel}")
                await btn.click()
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except PlaywrightTimeoutError:
                    pass
                return
        except Exception:
            continue
    logger.warning("[SUBMIT] no submit button matched any selector")


# --------------------------------------------------------------------------------------
#  Run
# --------------------------------------------------------------------------------------


async def _maybe_handle_captcha(page, args, config, logger) -> None:
    """Detect and (optionally) pause for any CAPTCHA on the current page."""
    pause_enabled = (
        getattr(args, "no_captcha_pause", False) is False
        and config.get("pause_on_captcha", True)
    )
    kind = await detect_captcha(page)
    if not kind:
        return
    if not pause_enabled:
        logger.warning(f"[CAPTCHA] detected {kind!r} but pause is disabled — continuing")
        return
    if args.headless or config.get("headless", False):
        logger.error(
            f"[CAPTCHA] detected {kind!r} in headless mode — cannot pause for human. "
            f"Re-run without --headless or with pause_on_captcha=false."
        )
        return
    timeout = float(config.get("captcha_timeout", 300))
    await pause_for_human(page, kind, logger, timeout=timeout)


def _is_v2_config(config: dict) -> bool:
    return int(config.get("version", 1)) >= 2 or "actions" in config


def _resolve_chrome_profile(profile_path: str):
    """Split a Chrome profile path into (user_data_dir, profile_directory).

    E.g. ``…/User Data/Profile 36`` → (``…/User Data``, ``Profile 36``).
    """
    from typing import Optional
    p = Path(profile_path)
    if (p / "Local State").exists():
        return str(p), None
    if (p / "Preferences").exists():
        parent = p.parent
        if (parent / "Local State").exists():
            return str(parent), p.name
    return str(p), None


async def run(config: dict, args: argparse.Namespace, logger) -> int:
    target_url = config["target_url"]
    wait_sel = config.get("wait_for_selector")
    headless = args.headless if args.headless else config.get("headless", False)
    dry_run = args.dry_run or config.get("dry_run", False)
    debug = args.debug
    is_v2 = _is_v2_config(config)

    chrome_profile = getattr(args, "chrome_profile", None) or config.get("chrome_profile")

    proxy = resolve_proxy(
        cli_proxy=getattr(args, "proxy", None),
        cli_proxy_list=getattr(args, "proxy_list", None),
        cli_no_proxy=getattr(args, "no_proxy", False),
        cli_rotate=getattr(args, "proxy_rotate", None),
        cli_bypass=getattr(args, "proxy_bypass", None),
        config=config,
    )
    if proxy:
        logger.info(f"[PROXY] using {mask_proxy(proxy)}")
    else:
        logger.info("[PROXY] none")

    if chrome_profile:
        logger.info(f"[PROFILE] using Chrome profile: {chrome_profile}")
    logger.info(f"[CONFIG] format={'v2' if is_v2 else 'v1 (legacy)'}")

    async with async_playwright() as p:
        browser_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-default-browser-check",
        ]

        ctx_kwargs: dict = {
            "user_agent": config.get(
                "user_agent",
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            ),
            "viewport": config.get("viewport") or {"width": 1366, "height": 820},
            "locale": config.get("locale", "en-US"),
        }
        if config.get("timezone_id"):
            ctx_kwargs["timezone_id"] = config["timezone_id"]

        browser = None
        if chrome_profile:
            # Use an existing Chrome profile (persistent context).
            # This preserves cookies, extensions, proxy settings, etc.
            user_data_dir, profile_dir = _resolve_chrome_profile(chrome_profile)
            logger.info(f"[PROFILE] user_data_dir: {user_data_dir}")
            if profile_dir:
                logger.info(f"[PROFILE] profile_directory: {profile_dir}")
                browser_args.append(f"--profile-directory={profile_dir}")
            launch_kwargs: dict = {
                "headless": headless,
                "args": browser_args,
                **ctx_kwargs,
            }
            if proxy:
                launch_kwargs["proxy"] = proxy
            context = await p.chromium.launch_persistent_context(
                user_data_dir, **launch_kwargs,
            )
        else:
            launch_kwargs = {"headless": headless, "args": browser_args}
            if proxy:
                launch_kwargs["proxy"] = proxy
            browser = await p.chromium.launch(**launch_kwargs)
            context = await browser.new_context(**ctx_kwargs)

        # Strip the navigator.webdriver flag.
        await context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        page = await context.new_page()

        logger.info(f"[OPEN] {target_url}")
        await page.goto(target_url, wait_until="domcontentloaded")
        if wait_sel:
            try:
                await page.wait_for_selector(wait_sel, timeout=15000)
            except PlaywrightTimeoutError:
                logger.warning(f"[WAIT] selector not seen within 15s: {wait_sel}")

        # Pre-fill CAPTCHA check (e.g. forms behind a "verify you're human" gate)
        await _maybe_handle_captcha(page, args, config, logger)

        if is_v2:
            # ---- v2 path: action-driven replay ----
            actions = config.get("actions", [])
            ctx_vars = dict(config.get("vars") or {})
            filled, skipped = await _run_v2_actions(
                page, actions,
                ctx=ctx_vars,
                threshold=float(config.get("resolver_threshold", 0.55)),
                dry_run=dry_run,
                logger=logger,
                action_delay_ms=int(config.get("action_delay_ms", 120)),
                action_jitter_ms=int(config.get("action_jitter_ms", 80)),
            )
            if args.submit and not dry_run and config.get("submit"):
                # Post-fill CAPTCHA check
                await _maybe_handle_captcha(page, args, config, logger)
                submit_action = {**config["submit"], "kind": "click", "field_id": "submit"}
                await _run_v2_action(
                    page, submit_action,
                    threshold=float(config.get("resolver_threshold", 0.55)),
                    dry_run=False, logger=logger,
                )
                await _maybe_handle_captcha(page, args, config, logger)
        else:
            # ---- v1 legacy path ----
            filled = skipped = 0
            for field in config.get("fields", []):
                ok = await fill_one_field(page, field, dry_run, debug, logger)
                if ok:
                    filled += 1
                else:
                    skipped += 1

            if args.submit and not dry_run:
                await _maybe_handle_captcha(page, args, config, logger)
                await submit_form(page, config, logger)
                await _maybe_handle_captcha(page, args, config, logger)

        if args.screenshot:
            try:
                await page.screenshot(path=args.screenshot, full_page=True)
                logger.info(f"[SHOT] saved {args.screenshot}")
            except Exception as exc:
                logger.error(f"[SHOT] failed: {exc}")

        if not headless:
            logger.info("[HOLD] keeping browser open for 5s...")
            await asyncio.sleep(5)

        logger.info(f"[DONE] filled={filled} skipped={skipped}")
        await context.close()
        if browser:
            await browser.close()
        return 0 if skipped == 0 else 1


# --------------------------------------------------------------------------------------
#  Multi-account run (worker_pool driver)
# --------------------------------------------------------------------------------------


def _print_report_event(evt: str, payload: dict) -> None:
    parts = [f"[POOL] {evt}"]
    for k in ("account", "label", "target_url", "ok", "filled", "skipped",
              "attempts", "duration_s", "proxy", "error"):
        if k in payload and payload[k] is not None:
            parts.append(f"{k}={payload[k]!r}")
    print("  ".join(parts))


async def run_multi_proxy(
    *,
    proxy_pool_path: str,
    base_config: Optional[dict],
    args: argparse.Namespace,
    logger,
) -> int:
    """Run the same config concurrently across N proxies (one BrowserContext each).

    Triggered by ``auto_fill.py --proxy-pool proxies.txt``.
    """
    if not base_config:
        logger.error("[PROXY-POOL] need --config (or --profile) to know what to run")
        return 2
    proxies = load_proxy_dicts(proxy_pool_path, on_error="warn")
    if not proxies:
        logger.error(f"[PROXY-POOL] no valid proxies in {proxy_pool_path}")
        return 2
    logger.info(f"[PROXY-POOL] loaded {len(proxies)} proxy/ies from {proxy_pool_path}")

    udd_template: Optional[str] = None
    if getattr(args, "proxy_pool_persistent", False):
        from pathlib import Path as _P
        base = _P.home() / ".auto_form_filler_profiles"
        base.mkdir(parents=True, exist_ok=True)
        udd_template = str(base / "{name}")

    accts = accounts_from_proxies(
        proxies,
        headless=bool(getattr(args, "headless", False)),
        user_data_dir_template=udd_template,
    )
    # Strip single-proxy fields so each worker uses its own. Each
    # worker needs its OWN copy of the config because some pipeline
    # steps mutate nested values (e.g. ``actions`` lists for retries,
    # ``submit`` overrides for self-healing). Sharing a single shallow
    # copy across all workers in a proxy pool would let one worker's
    # mutation silently leak into every other concurrent worker. The
    # GUI counterpart at ``auto_fill_gui.py:2178`` already deep-copies
    # for the same reason — align the CLI path with it.
    base_cfg = copy.deepcopy(base_config)
    for k in ("proxy", "proxy_list", "proxy_rotate"):
        base_cfg.pop(k, None)

    tasks = [
        Task(config=copy.deepcopy(base_cfg), vars=dict(a.vars), label=a.name)
        for a in accts
    ]
    pool = WorkerPool(
        accts,
        max_concurrency=int(getattr(args, "workers", 0) or len(accts)),
        report_cb=_print_report_event,
        dry_run=args.dry_run,
        threshold=0.55,
        debug=args.debug,
    )
    results = await pool.run_tasks(tasks)
    ok = sum(1 for r in results if r.ok)
    logger.info(f"[PROXY-POOL] done — {ok}/{len(results)} task(s) succeeded")
    return 0 if ok == len(results) else 1


async def run_multi_account(
    *,
    accounts_path: str,
    tasks_path: Optional[str],
    base_config: Optional[dict],
    args: argparse.Namespace,
    logger,
) -> int:
    accts = load_accounts(accounts_path)
    logger.info(f"[POOL] loaded {len(accts)} account(s)")

    # Build the task list. Two ways:
    #   (a) --tasks tasks.json  → list of {config?: ..., vars?: ...}
    #   (b) one task per account (each account runs the same base config once)
    tasks: list[Task] = []
    if tasks_path:
        raw = json.loads(Path(tasks_path).read_text(encoding="utf-8"))
        items = raw.get("tasks", raw) if isinstance(raw, dict) else raw
        for item in items:
            cfg = item.get("config") or base_config
            if not cfg:
                logger.error("[POOL] task without config and no base config — skipping")
                continue
            tasks.append(Task(
                config=cfg,
                vars=dict(item.get("vars") or {}),
                label=item.get("label") or cfg.get("target_url", "<task>"),
                max_retries=int(item.get("max_retries", 1)),
                timeout_s=float(item.get("timeout_s", 180.0)),
            ))
    else:
        if not base_config:
            logger.error("[POOL] need --config or --tasks to know what to run")
            return 2
        for acct in accts:
            tasks.append(Task(config=base_config, vars=dict(acct.vars), label=acct.name))

    pool = WorkerPool(
        accts,
        max_concurrency=int(getattr(args, "workers", 0) or len(accts)),
        report_cb=_print_report_event,
        dry_run=args.dry_run,
        threshold=0.55,
        debug=args.debug,
    )
    results = await pool.run_tasks(tasks)
    ok = sum(1 for r in results if r.ok)
    logger.info(f"[POOL] done — {ok}/{len(results)} task(s) succeeded")
    return 0 if ok == len(results) else 1


# --------------------------------------------------------------------------------------
#  CLI
# --------------------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Multi-strategy auto form-filler.")
    p.add_argument("--config", default="config.json", help="Path to config JSON.")
    p.add_argument(
        "--profile",
        help="Profile name; loads ./profiles/<name>.json (overrides --config).",
    )
    p.add_argument("--headless", action="store_true", help="Run without a visible browser.")
    p.add_argument("--dry-run", action="store_true", help="Resolve targets but don't fill.")
    p.add_argument("--submit", action="store_true", help="Click the submit button after filling.")
    p.add_argument("--debug", action="store_true", help="Verbose logs + highlight matched fields.")
    p.add_argument("--screenshot", help="Save full-page screenshot to PATH after run.")
    p.add_argument(
        "--chrome-profile",
        help="Path to a Chrome user-data-dir (e.g. ~/.config/google-chrome/Default). "
             "Uses launch_persistent_context so the browser reuses cookies, extensions, "
             "proxy settings, and login sessions from the profile.",
    )
    p.add_argument(
        "--no-captcha-pause",
        action="store_true",
        help="Disable the pause-for-human banner when a CAPTCHA is detected.",
    )
    grp = p.add_argument_group("multi-account")
    grp.add_argument(
        "--accounts",
        help="Path to accounts.json. When set, runs the config concurrently "
             "across N accounts (one persistent BrowserContext per account).",
    )
    grp.add_argument(
        "--tasks",
        help="Path to tasks.json (a list of task objects). Each task may carry its "
             "own config and vars. Required when accounts.json doesn't carry the "
             "per-account vars itself.",
    )
    grp.add_argument(
        "--workers",
        type=int,
        default=0,
        help="Max concurrent workers (default: number of accounts/proxies).",
    )
    grp.add_argument(
        "--proxy-pool",
        help=(
            "Path to a proxies file. Each non-empty, non-comment line is one "
            "proxy in any of: host:port:user:pass | host:port | "
            "[scheme://][user:pass@]host:port. The same config is run "
            "concurrently — one BrowserContext per proxy."
        ),
    )
    grp.add_argument(
        "--proxy-pool-persistent",
        action="store_true",
        help="Use separate persistent profiles for each proxy worker "
             "(default: ephemeral context per worker).",
    )
    _add_proxy_cli_args(p)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    logger = get_logger(debug=args.debug)

    # Resolve base config (optional when --tasks is provided)
    base_config: Optional[dict] = None
    cfg_path = args.config
    if args.profile:
        cfg_path = os.path.join("profiles", f"{args.profile}.json")
    if cfg_path and os.path.exists(cfg_path):
        base_config = json.loads(Path(cfg_path).read_text(encoding="utf-8"))

    if args.accounts:
        # Multi-account mode
        rc = asyncio.run(run_multi_account(
            accounts_path=args.accounts,
            tasks_path=args.tasks,
            base_config=base_config,
            args=args,
            logger=logger,
        ))
        sys.exit(rc)

    if getattr(args, "proxy_pool", None):
        # Multi-proxy parallel mode
        rc = asyncio.run(run_multi_proxy(
            proxy_pool_path=args.proxy_pool,
            base_config=base_config,
            args=args,
            logger=logger,
        ))
        sys.exit(rc)

    # Single-account legacy mode
    if not base_config:
        print(f"config not found: {cfg_path}", file=sys.stderr)
        sys.exit(2)
    rc = asyncio.run(run(base_config, args, logger))
    sys.exit(rc)


if __name__ == "__main__":
    main()
