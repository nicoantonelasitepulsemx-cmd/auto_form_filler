"""kuku_lu_cli.py — provision and inspect kuku.lu accounts from the shell.

Examples
--------

Mint a brand-new kuku.lu identity and save it to a file the recorder can
load via ``--kuku-creds``::

    $ python kuku_lu_cli.py mint --out kuku_acct1.json
    {"csrf_token": "...", "sessionhash": "...", "current_address": "abc@neko2.net"}

Reuse an existing creds file but mint a fresh disposable address (e.g.
when the previous one has been spammed too much)::

    $ python kuku_lu_cli.py new-address --creds kuku_acct1.json --domain kpay.be

Drain the inbox (debugging)::

    $ python kuku_lu_cli.py list --creds kuku_acct1.json

Wait for a one-time code (same path the replay engine uses)::

    $ python kuku_lu_cli.py wait-code --creds kuku_acct1.json --from facebook \\
        --regex '(\\d{5,8})' --timeout 180

If you're behind Cloudflare on a datacenter IP, drive a real Chromium
session by passing ``--via-browser``: a non-headless Chromium opens
``m.kuku.lu``, you solve the challenge once, and the same context is
reused for every subsequent call.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Optional

from kuku_lu import Kuku, KukuCreds, KukuError


def _save_creds(creds: KukuCreds, path: str) -> None:
    Path(path).write_text(
        json.dumps(creds.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _load_creds(path: str) -> KukuCreds:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return KukuCreds.from_dict(data)


async def _make_client(args: argparse.Namespace) -> Kuku:
    creds: Optional[KukuCreds] = None
    if getattr(args, "creds", None):
        try:
            creds = _load_creds(args.creds)
        except FileNotFoundError:
            creds = None
    if not getattr(args, "via_browser", False):
        return await Kuku.from_requests(creds=creds)

    # via_browser: spin up a Playwright Chromium so we share a real
    # browser fingerprint with the user's existing session.
    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=False)
    page = await browser.new_page()
    await page.goto("https://m.kuku.lu/")
    print("[kuku] solve any Cloudflare challenge in the opened window, "
          "then press Enter here to continue...", file=sys.stderr)
    await asyncio.to_thread(input)
    return await Kuku.from_playwright(page, creds=creds)


async def _cmd_mint(args: argparse.Namespace) -> int:
    k = await _make_client(args)
    domain = args.domain
    addr = await k.create_address(domain=domain) if domain else await k.create_address()
    creds = k.credentials()
    creds.current_address = addr
    if args.out:
        _save_creds(creds, args.out)
        print(f"[kuku] wrote creds → {args.out}", file=sys.stderr)
    print(json.dumps(creds.to_dict(), indent=2, ensure_ascii=False))
    return 0


async def _cmd_new_address(args: argparse.Namespace) -> int:
    k = await _make_client(args)
    addr = await k.create_address(domain=args.domain)
    creds = k.credentials()
    creds.current_address = addr
    _save_creds(creds, args.creds)
    print(addr)
    return 0


async def _cmd_list(args: argparse.Namespace) -> int:
    k = await _make_client(args)
    creds = k.credentials()
    address = args.address or creds.current_address
    if not address:
        print("error: no address (pass --address or set current_address)", file=sys.stderr)
        return 2
    mails = await k.list_mails(address)
    if not mails:
        print(f"[kuku] inbox of {address} is empty", file=sys.stderr)
        return 0
    print(f"[kuku] {len(mails)} message(s) in {address}:", file=sys.stderr)
    for i, m in enumerate(mails):
        body = await k.read_mail(m)
        snippet = re.sub(r"\s+", " ", body)[:200]
        print(f"  #{i+1} num={m.num} key={m.key} :: {snippet}")
    return 0


async def _cmd_wait_code(args: argparse.Namespace) -> int:
    k = await _make_client(args)
    creds = k.credentials()
    address = args.address or creds.current_address
    if not address:
        print("error: no address provided/known", file=sys.stderr)
        return 2
    try:
        code = await k.wait_for_code(
            address,
            regex=args.regex,
            timeout=args.timeout,
            poll_interval=args.poll,
            from_filter=args.from_filter,
        )
    except KukuError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(code)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Provision + drive m.kuku.lu disposable mail.")
    p.add_argument("--via-browser", action="store_true",
                   help="Drive kuku.lu through a real Chromium (helps with Cloudflare).")
    sp = p.add_subparsers(dest="cmd", required=True)

    pm = sp.add_parser("mint", help="Mint a fresh creds + address pair.")
    pm.add_argument("--out", required=False, help="Write the creds JSON here.")
    pm.add_argument("--domain", default=None, help="Force a specific kuku.lu domain.")

    pn = sp.add_parser("new-address", help="Mint a new alias for an existing creds file.")
    pn.add_argument("--creds", required=True)
    pn.add_argument("--domain", default=None)

    pl = sp.add_parser("list", help="List all messages currently in the inbox.")
    pl.add_argument("--creds", required=True)
    pl.add_argument("--address", default=None)

    pw = sp.add_parser("wait-code", help="Poll the inbox for a code matching --regex.")
    pw.add_argument("--creds", required=True)
    pw.add_argument("--address", default=None)
    pw.add_argument("--regex", default=r"(?<!\d)(\d{5,8})(?!\d)")
    pw.add_argument("--timeout", type=float, default=180.0)
    pw.add_argument("--poll", type=float, default=4.0)
    pw.add_argument("--from", dest="from_filter", default=None,
                    help="Substring filter applied to mail body (case-insensitive).")

    args = p.parse_args()
    handler = {
        "mint":        _cmd_mint,
        "new-address": _cmd_new_address,
        "list":        _cmd_list,
        "wait-code":   _cmd_wait_code,
    }[args.cmd]
    return asyncio.run(handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
