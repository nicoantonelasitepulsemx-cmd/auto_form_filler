"""run_with_email_otp.py — orchestrator for two-step forms.

Phase 1: fill + submit a form (with built-in CAPTCHA pause).
Phase 2: poll an IMAP inbox for a confirmation code email,
         extract the code, and fill it into a follow-up "enter code" page.

Designed for Facebook's Trademark / DMCA report flow, but generic.

Usage
-----
    export OTP_IMAP_HOST=imap.gmail.com
    export OTP_IMAP_USER=your.address@gmail.com
    export OTP_IMAP_PASS=your_app_password           # NOT your real Gmail password — use an App Password
    export OTP_FROM_FILTER=facebookmail.com          # optional — sender filter
    export OTP_SUBJECT_FILTER='confirmation code'    # optional — subject filter (case-insensitive)

    python run_with_email_otp.py \\
        --form-config example_facebook_trademark.json \\
        --otp-config  example_facebook_trademark_otp.json \\
        --debug \\
        --screenshot result.png

Notes
-----
* IMAP creds are read from env vars only — never put them in the JSON config.
* For Gmail you MUST use an App Password (Account → Security → App passwords).
* The script reuses ONE browser session for both phases so cookies / session
  state are preserved across submit → confirmation.
"""
from __future__ import annotations

import argparse
import asyncio
import email
import imaplib
import json
import os
import re
import sys
import time
from email.header import decode_header
from pathlib import Path
from typing import Optional

from playwright.async_api import (
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from auto_fill import (
    _maybe_handle_captcha,
    fill_one_field,
    submit_form,
)
from logger import get_logger
from proxy_utils import add_cli_args as _add_proxy_cli_args, mask_proxy, resolve_proxy


# --------------------------------------------------------------------------------------
#  IMAP — fetch confirmation code
# --------------------------------------------------------------------------------------

# Default regex: 5–8 contiguous digits, with optional separators (e.g. "123 456")
_CODE_RE = re.compile(r"(?<!\d)(\d{5,8})(?!\d)")


def _decode(s: bytes | str) -> str:
    if isinstance(s, bytes):
        try:
            return s.decode("utf-8", errors="replace")
        except Exception:
            return s.decode("latin-1", errors="replace")
    return s


def _extract_text(msg: email.message.Message) -> str:
    """Return a flat string of plain-text + HTML stripped, for regex scanning."""
    parts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype in ("text/plain", "text/html"):
                payload = part.get_payload(decode=True) or b""
                parts.append(_decode(payload))
    else:
        payload = msg.get_payload(decode=True) or b""
        parts.append(_decode(payload))
    raw = "\n".join(parts)
    # very crude HTML strip — fine for code extraction
    return re.sub(r"<[^>]+>", " ", raw)


def _decoded_subject(msg: email.message.Message) -> str:
    raw = msg.get("Subject", "")
    decoded = decode_header(raw)
    out: list[str] = []
    for chunk, enc in decoded:
        if isinstance(chunk, bytes):
            try:
                out.append(chunk.decode(enc or "utf-8", errors="replace"))
            except Exception:
                out.append(chunk.decode("latin-1", errors="replace"))
        else:
            out.append(chunk)
    return "".join(out)


def fetch_code_from_imap(
    *,
    host: str,
    user: str,
    password: str,
    since_unix: float,
    from_filter: Optional[str],
    subject_filter: Optional[str],
    code_re: re.Pattern[str],
    logger,
    poll_interval: float = 5.0,
    timeout: float = 180.0,
) -> Optional[str]:
    """Poll IMAP until a matching email arrives, then return the extracted code.

    `since_unix` is a Unix timestamp; only messages received at-or-after this
    point are considered (so we don't grab an old code from a previous run).
    """
    deadline = time.time() + timeout
    logger.info(
        f"[IMAP] connecting to {host} as {user} "
        f"(filters: from~{from_filter!r} subject~{subject_filter!r})"
    )
    while time.time() < deadline:
        try:
            with imaplib.IMAP4_SSL(host) as M:
                M.login(user, password)
                M.select("INBOX")

                # IMAP SINCE is date-only granularity; we filter precisely below.
                since_date = time.strftime("%d-%b-%Y", time.localtime(since_unix - 86400))
                criteria = ["UNSEEN", "SINCE", since_date]
                if from_filter:
                    criteria += ["FROM", f'"{from_filter}"']
                typ, data = M.search(None, *criteria)
                if typ != "OK" or not data or not data[0]:
                    logger.info("[IMAP] no matching messages yet, sleeping...")
                else:
                    ids = data[0].split()
                    # Walk newest → oldest
                    for msg_id in reversed(ids):
                        typ, msg_data = M.fetch(msg_id, "(RFC822)")
                        if typ != "OK":
                            continue
                        raw = msg_data[0][1]
                        msg = email.message_from_bytes(raw)

                        # Filter by subject if requested
                        subj = _decoded_subject(msg)
                        if subject_filter and subject_filter.lower() not in subj.lower():
                            continue

                        # Filter by date (precise)
                        date_hdr = msg.get("Date", "")
                        try:
                            msg_ts = email.utils.parsedate_to_datetime(date_hdr).timestamp()
                        except Exception:
                            msg_ts = since_unix  # fail-open
                        if msg_ts + 30 < since_unix:
                            # 30s grace window for clock skew
                            continue

                        body = _extract_text(msg)
                        m = code_re.search(body)
                        if m:
                            code = m.group(1)
                            logger.info(
                                f"[IMAP] matched message subject={subj!r} → code={code}"
                            )
                            return code
                        logger.info(
                            f"[IMAP] message matched filters but no code found: {subj!r}"
                        )
        except imaplib.IMAP4.error as exc:
            logger.error(f"[IMAP] login/search error: {exc}")
            return None
        except Exception as exc:
            logger.warning(f"[IMAP] transient error, retrying: {exc}")

        time.sleep(poll_interval)

    logger.error(f"[IMAP] timed out after {timeout}s waiting for code")
    return None


# --------------------------------------------------------------------------------------
#  Browser orchestration
# --------------------------------------------------------------------------------------


class _Args:
    """Minimal stand-in for argparse.Namespace — what auto_fill helpers expect."""
    def __init__(
        self,
        *,
        headless: bool,
        dry_run: bool,
        debug: bool,
        no_captcha_pause: bool,
        submit: bool,
        screenshot: Optional[str],
        proxy: Optional[str] = None,
        proxy_list: Optional[str] = None,
        proxy_rotate: Optional[str] = None,
        proxy_bypass: Optional[str] = None,
        no_proxy: bool = False,
    ) -> None:
        self.headless = headless
        self.dry_run = dry_run
        self.debug = debug
        self.no_captcha_pause = no_captcha_pause
        self.submit = submit
        self.screenshot = screenshot
        self.proxy = proxy
        self.proxy_list = proxy_list
        self.proxy_rotate = proxy_rotate
        self.proxy_bypass = proxy_bypass
        self.no_proxy = no_proxy


async def _run_phase(
    page: Page,
    config: dict,
    args: _Args,
    logger,
    *,
    do_submit: bool,
) -> int:
    target_url = config["target_url"]
    if target_url and target_url != "RUNTIME_OVERRIDE":
        logger.info(f"[OPEN] {target_url}")
        await page.goto(target_url, wait_until="domcontentloaded")

    wait_sel = config.get("wait_for_selector")
    if wait_sel:
        try:
            await page.wait_for_selector(wait_sel, timeout=15000)
        except PlaywrightTimeoutError:
            logger.warning(f"[WAIT] selector not seen within 15s: {wait_sel}")

    await _maybe_handle_captcha(page, args, config, logger)

    filled = skipped = 0
    for field in config.get("fields", []):
        ok = await fill_one_field(page, field, args.dry_run, args.debug, logger)
        if ok:
            filled += 1
        else:
            skipped += 1

    if do_submit and not args.dry_run:
        await _maybe_handle_captcha(page, args, config, logger)
        await submit_form(page, config, logger)
        await _maybe_handle_captcha(page, args, config, logger)

    logger.info(f"[PHASE DONE] filled={filled} skipped={skipped}")
    return 0 if skipped == 0 else 1


async def run_two_phase(
    form_cfg: dict,
    otp_cfg: dict,
    args: _Args,
    imap_cfg: dict,
    logger,
) -> int:
    proxy = resolve_proxy(
        cli_proxy=getattr(args, "proxy", None),
        cli_proxy_list=getattr(args, "proxy_list", None),
        cli_no_proxy=getattr(args, "no_proxy", False),
        cli_rotate=getattr(args, "proxy_rotate", None),
        cli_bypass=getattr(args, "proxy_bypass", None),
        config=form_cfg,
    )
    if proxy:
        logger.info(f"[PROXY] using {mask_proxy(proxy)}")
    else:
        logger.info("[PROXY] none")

    async with async_playwright() as p:
        launch_kwargs: dict = {"headless": args.headless}
        if proxy:
            launch_kwargs["proxy"] = proxy
        browser = await p.chromium.launch(**launch_kwargs)
        context = await browser.new_context()
        page = await context.new_page()

        # --- Phase 1: fill + submit the main form ---
        rc1 = await _run_phase(page, form_cfg, args, logger, do_submit=True)
        if args.dry_run:
            logger.info("[DRY] skipping IMAP + phase 2")
            await context.close()
            await browser.close()
            return rc1

        # Mark t0 AFTER the submit returns so we don't grab an old code.
        t0 = time.time()
        logger.info(f"[OTP] waiting for confirmation email since unix-ts {int(t0)}...")

        # Try to detect the OTP-input page in the same window.
        otp_input_appeared = False
        try:
            await page.wait_for_selector(
                "input[name*='code' i], input[aria-label*='code' i], input[placeholder*='code' i]",
                timeout=15000,
            )
            otp_input_appeared = True
            logger.info("[OTP] code input field appeared in current window")
        except PlaywrightTimeoutError:
            logger.info("[OTP] no code input visible yet — will fetch code anyway")

        # --- Fetch code from IMAP (blocking, runs in a thread to avoid blocking event loop) ---
        code = await asyncio.to_thread(
            fetch_code_from_imap,
            host=imap_cfg["host"],
            user=imap_cfg["user"],
            password=imap_cfg["password"],
            since_unix=t0,
            from_filter=imap_cfg.get("from_filter"),
            subject_filter=imap_cfg.get("subject_filter"),
            code_re=imap_cfg["code_re"],
            logger=logger,
            poll_interval=imap_cfg.get("poll_interval", 5.0),
            timeout=imap_cfg.get("timeout", 180.0),
        )
        if not code:
            logger.error("[OTP] couldn't retrieve code; aborting phase 2")
            if args.screenshot:
                try:
                    await page.screenshot(path=args.screenshot, full_page=True)
                except Exception:
                    pass
            await context.close()
            await browser.close()
            return 2

        # --- Phase 2: fill code field + submit confirmation ---
        # Override the placeholder value at runtime.
        for f in otp_cfg.get("fields", []):
            if f.get("field_id") == "confirmation_code":
                f["value"] = code

        # If a fresh URL was supplied for phase 2, navigate. Otherwise stay
        # on the current page (FB usually shows the code input in-place).
        otp_target = otp_cfg.get("target_url")
        if otp_target and otp_target != "RUNTIME_OVERRIDE":
            await page.goto(otp_target, wait_until="domcontentloaded")
        elif not otp_input_appeared:
            logger.warning(
                "[OTP] no code input was seen and no target_url override — "
                "trying to fill on whatever page we ended up on"
            )

        rc2 = await _run_phase(page, otp_cfg, args, logger, do_submit=True)

        if args.screenshot:
            try:
                await page.screenshot(path=args.screenshot, full_page=True)
                logger.info(f"[SHOT] saved {args.screenshot}")
            except Exception as exc:
                logger.error(f"[SHOT] failed: {exc}")

        if not args.headless:
            logger.info("[HOLD] keeping browser open for 5s...")
            await asyncio.sleep(5)

        await context.close()
        await browser.close()
        return max(rc1, rc2)


# --------------------------------------------------------------------------------------
#  CLI
# --------------------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Two-step form filler: fill+submit, then auto-fetch email code and confirm.")
    p.add_argument("--form-config", required=True, help="Phase-1 config (the main form).")
    p.add_argument("--otp-config",  required=True, help="Phase-2 config (the confirmation code page).")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="Resolve targets only; skip IMAP + phase 2.")
    p.add_argument("--debug", action="store_true")
    p.add_argument("--no-captcha-pause", action="store_true")
    p.add_argument("--screenshot", help="Save final full-page screenshot to PATH.")
    p.add_argument("--code-regex", default=None, help="Override the code-extraction regex.")
    p.add_argument("--imap-timeout", type=float, default=180.0, help="Seconds to wait for the email.")
    _add_proxy_cli_args(p)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    logger = get_logger(debug=args.debug)

    form_cfg = json.loads(Path(args.form_config).read_text(encoding="utf-8"))
    otp_cfg  = json.loads(Path(args.otp_config).read_text(encoding="utf-8"))

    imap_cfg: dict = {}
    if not args.dry_run:
        try:
            imap_cfg = {
                "host":            os.environ["OTP_IMAP_HOST"],
                "user":            os.environ["OTP_IMAP_USER"],
                "password":        os.environ["OTP_IMAP_PASS"],
                "from_filter":     os.environ.get("OTP_FROM_FILTER") or None,
                "subject_filter":  os.environ.get("OTP_SUBJECT_FILTER") or None,
                "code_re":         re.compile(args.code_regex) if args.code_regex else _CODE_RE,
                "poll_interval":   float(os.environ.get("OTP_POLL_INTERVAL", "5")),
                "timeout":         args.imap_timeout,
            }
        except KeyError as missing:
            print(
                f"[FATAL] missing required env var: {missing}. "
                "Set OTP_IMAP_HOST, OTP_IMAP_USER, OTP_IMAP_PASS (and optionally "
                "OTP_FROM_FILTER, OTP_SUBJECT_FILTER, OTP_POLL_INTERVAL).",
                file=sys.stderr,
            )
            sys.exit(2)

    headless = args.headless or form_cfg.get("headless", False)
    dry_run  = args.dry_run  or form_cfg.get("dry_run",  False)

    inner = _Args(
        headless=headless,
        dry_run=dry_run,
        debug=args.debug,
        no_captcha_pause=args.no_captcha_pause,
        submit=True,
        screenshot=args.screenshot,
        proxy=getattr(args, "proxy", None),
        proxy_list=getattr(args, "proxy_list", None),
        proxy_rotate=getattr(args, "proxy_rotate", None),
        proxy_bypass=getattr(args, "proxy_bypass", None),
        no_proxy=getattr(args, "no_proxy", False),
    )
    rc = asyncio.run(run_two_phase(form_cfg, otp_cfg, inner, imap_cfg, logger))
    sys.exit(rc)


if __name__ == "__main__":
    main()
