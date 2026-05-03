"""Tests for kuku_lu.py — drives the HTTP backend against a local fake.

We don't hit the live kuku.lu in CI: the service is behind Cloudflare and
datacenter IPs hit a 403 challenge. Instead we spin up an aiohttp server
that re-implements the four endpoints we depend on with the exact wire
format of the real site, and assert the client behaves correctly:

  POST  /                              → set both cookies, return 200
  GET   /index.php?action=addMailAddrByAuto    → "OK:abc@neko2.net"
  GET   /index.php?action=addMailAddrByManual  → "OK:user@<newdomain>"
  GET   /recv._ajax.php?q=<addr>      → inbox HTML with openMailData calls
  POST  /smphone.app.recv.view.php    → mail body HTML
"""
from __future__ import annotations

import asyncio
import sys
from typing import Any

from aiohttp import web

sys.path.insert(0, ".")

from kuku_lu import (
    Kuku,
    KukuCreds,
    KukuError,
    _parse_inbox,
    _parse_offer_alternatives,
    _strip_status_prefix,
)


# --------------------------------------------------------------------------------------
#  fake server
# --------------------------------------------------------------------------------------

class FakeKuku:
    """In-memory replica of the bits of kuku.lu we depend on."""

    def __init__(self) -> None:
        self.addresses: list[str] = []
        # Mailbox: {address: list[(num, key, body_html)]}
        self.mailbox: dict[str, list[tuple[str, str, str]]] = {}
        self._next_num = 1000
        self._csrf = "csrf-test-1"
        self._sessionhash = "sess-test-1"

    def routes(self) -> list[web.RouteDef]:
        return [
            web.post("/", self.handle_root),
            web.get("/index.php", self.handle_index),
            web.get("/recv._ajax.php", self.handle_inbox),
            web.post("/smphone.app.recv.view.php", self.handle_view),
        ]

    async def handle_root(self, req: web.Request) -> web.Response:
        # Drop the cookies kuku.lu would normally set on the first POST.
        resp = web.Response(text="ok")
        resp.set_cookie("cookie_csrf_token", self._csrf, path="/")
        resp.set_cookie("cookie_sessionhash", self._sessionhash, path="/")
        return resp

    async def handle_index(self, req: web.Request) -> web.Response:
        action = req.query.get("action")
        if action == "addMailAddrByAuto":
            addr = f"auto{len(self.addresses)+1}@neko2.net"
        elif action == "addMailAddrByManual":
            domain = req.query.get("newdomain") or "kpay.be"
            addr = f"user{len(self.addresses)+1}@{domain}"
        else:
            return web.Response(status=400, text="bad action")
        self.addresses.append(addr)
        self.mailbox.setdefault(addr, [])
        return web.Response(text=f"OK:{addr}")

    async def handle_inbox(self, req: web.Request) -> web.Response:
        addr = req.query.get("q") or ""
        rows = self.mailbox.get(addr, [])
        # The real site serializes each mail as <script>...openMailData('num',
        # 'key')...</script>. We just need the function call to be present.
        scripts = "\n".join(
            f"<script>openMailData('{num}', '{key}')</script>"
            for num, key, _ in rows
        )
        return web.Response(text=f"<html><body>{scripts}</body></html>")

    async def handle_view(self, req: web.Request) -> web.Response:
        data = await req.post()
        num = data.get("num")
        key = data.get("key")
        for _, mails in self.mailbox.items():
            for n, k, body in mails:
                if n == num and k == key:
                    return web.Response(text=body)
        return web.Response(status=404, text="not found")

    # --- test helpers -------------------------------------------------

    def deliver(self, address: str, *, body_html: str) -> None:
        self._next_num += 1
        num = str(self._next_num)
        key = f"key-{num}"
        self.mailbox.setdefault(address, []).insert(0, (num, key, body_html))


# --------------------------------------------------------------------------------------
#  unit tests for the parsers
# --------------------------------------------------------------------------------------

def test_strip_status_prefix() -> None:
    assert _strip_status_prefix("OK:abc@x.com") == ("OK", "abc@x.com")
    assert _strip_status_prefix("NG:already")    == ("NG", "already")
    # OFFER prefix is recognised so that the GUI can present alternatives.
    s, p = _strip_status_prefix("OFFER:foo@x.com,bar,x.com")
    assert s == "OFFER"
    assert p == "foo@x.com,bar,x.com"
    # legacy `text[3:]` slicing equivalent: graceful when there's no prefix
    s, p = _strip_status_prefix("just text")
    assert (s, p) == ("", "just text")


def test_parse_offer_alternatives() -> None:
    # Real reply observed from kuku.lu when 'oishi.counterfeit'@exdonuts.com
    # is taken — kuku.lu suggests 'oishi.counterfeit598'@exdonuts.com.
    payload = "oishi.counterfeit@exdonuts.com,oishi.counterfeit598,exdonuts.com,"
    alts = _parse_offer_alternatives(payload, "exdonuts.com")
    assert alts == [
        "oishi.counterfeit@exdonuts.com",
        "oishi.counterfeit598@exdonuts.com",
    ]
    # Pure local-part list pairs each name with the requested domain.
    payload2 = "alpha,beta,gamma"
    assert _parse_offer_alternatives(payload2, "neko2.net") == [
        "alpha@neko2.net",
        "beta@neko2.net",
        "gamma@neko2.net",
    ]
    # Empty / malformed input doesn't blow up.
    assert _parse_offer_alternatives("", "x.com") == []


def test_parse_inbox() -> None:
    html = (
        "<script>openMailData('1001','keyA')</script>"
        "<script>openMailData('1002','keyB')</script>"
    )
    rows = _parse_inbox(html)
    assert [(r.num, r.key) for r in rows] == [("1001", "keyA"), ("1002", "keyB")]


# --------------------------------------------------------------------------------------
#  e2e against the fake server
# --------------------------------------------------------------------------------------

async def _spawn_fake() -> tuple[FakeKuku, web.AppRunner, str]:
    fake = FakeKuku()
    app = web.Application()
    app.add_routes(fake.routes())
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore
    return fake, runner, f"http://127.0.0.1:{port}"


async def test_create_and_fetch_code() -> None:
    fake, runner, base = await _spawn_fake()
    try:
        async with await Kuku.from_requests(base_url=base) as k:
            creds = k.credentials()
            assert creds.csrf_token == "csrf-test-1"
            assert creds.sessionhash == "sess-test-1"

            addr = await k.create_address()
            assert addr.endswith("@neko2.net")
            assert k.credentials().current_address == addr

            # No mail yet -> wait_for_code times out promptly.
            try:
                await k.wait_for_code(timeout=0.4, poll_interval=0.1)
                raise AssertionError("expected timeout")
            except KukuError as e:
                assert "timed out" in str(e), e

            # Drop a confirmation mail and assert we extract the code.
            fake.deliver(addr, body_html=(
                "<html><body>Your Facebook confirmation code is "
                "<b>147239</b>. Don't share it.</body></html>"
            ))
            code = await k.wait_for_code(timeout=2.0, poll_interval=0.1)
            assert code == "147239", code
    finally:
        await runner.cleanup()


async def test_resume_with_creds() -> None:
    fake, runner, base = await _spawn_fake()
    try:
        # First session: mint a creds pair.
        async with await Kuku.from_requests(base_url=base) as k1:
            addr = await k1.create_address(domain="kpay.be")
            saved = k1.credentials()
            saved.current_address = addr

        # Second session: come back with the saved creds.
        # The mock's per-session cookie state doesn't actually matter for
        # this test; what we check is that the client doesn't blow up
        # initialising from existing creds and can hit list_mails.
        async with await Kuku.from_requests(creds=saved, base_url=base) as k2:
            assert k2.credentials().csrf_token == saved.csrf_token
            mails = await k2.list_mails(addr)
            assert mails == []
    finally:
        await runner.cleanup()


async def test_creds_roundtrip_dict() -> None:
    c = KukuCreds(csrf_token="A", sessionhash="B", current_address="x@y.com")
    d = c.to_dict()
    c2 = KukuCreds.from_dict(d)
    assert c == c2


async def test_handles_ng_response() -> None:
    """A failed mint surfaces as KukuError, not a slice IndexError."""

    class BadServer(FakeKuku):
        async def handle_index(self, req: web.Request) -> web.Response:
            return web.Response(text="NG:domain not allowed")

    bad = BadServer()
    app = web.Application()
    app.add_routes(bad.routes())
    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0); await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore
    base = f"http://127.0.0.1:{port}"
    try:
        async with await Kuku.from_requests(base_url=base) as k:
            try:
                await k.create_address()
                raise AssertionError("expected KukuError on NG")
            except KukuError as e:
                assert "NG" in str(e) or "domain not allowed" in str(e)
    finally:
        await runner.cleanup()


async def test_wait_for_code_since_filters_stale() -> None:
    """v4: ``since`` must hide mails that already exist when called.

    Reproduces the bug Devin Review flagged — without the
    pre-seeding, ``wait_for_code(since=time.time())`` would return
    an OTP from a *previous* registration on the same alias.

    Two assertions:
      1. With a stale mail already in the inbox and no fresh mail
         arriving during the poll window, ``wait_for_code(since=...)``
         must time out (NOT return the stale code).
      2. With a stale mail already in the inbox and a fresh mail
         arriving mid-poll, ``wait_for_code(since=...)`` returns the
         fresh code, never the stale one.
    """
    import time as _time

    fake, runner, base = await _spawn_fake()
    try:
        async with await Kuku.from_requests(base_url=base) as k:
            addr = await k.create_address()
            # Stage a stale OTP that pre-dates the cutoff.
            fake.deliver(addr, body_html="Old code: <b>111111</b>.")
            cutoff = _time.time()

            # (1) No fresh mail → must time out, not return stale.
            try:
                code = await k.wait_for_code(
                    timeout=0.4, poll_interval=0.1, since=cutoff
                )
            except KukuError as e:
                assert "timed out" in str(e), e
            else:
                raise AssertionError(
                    f"expected timeout, got stale code {code!r}"
                )

            # (2) Fresh mail arrives mid-poll. Schedule a delayed
            # delivery and call wait_for_code with a NEW cutoff that
            # is captured BEFORE the fresh delivery, but AFTER the
            # stale one is already sitting in the box.
            new_cutoff = _time.time()

            async def _drop_fresh_after_a_beat() -> None:
                await asyncio.sleep(0.2)
                fake.deliver(addr, body_html="Fresh code: <b>222222</b>.")

            deliver_task = asyncio.create_task(_drop_fresh_after_a_beat())
            try:
                code = await k.wait_for_code(
                    timeout=2.0, poll_interval=0.1, since=new_cutoff
                )
            finally:
                await deliver_task
            assert code == "222222", code
    finally:
        await runner.cleanup()


async def main() -> None:
    test_strip_status_prefix()
    test_parse_inbox()
    test_parse_offer_alternatives()
    print("[unit] parsers OK")

    await test_creds_roundtrip_dict()
    print("[unit] creds roundtrip OK")

    await test_create_and_fetch_code()
    print("[fake] create + wait_for_code OK")

    await test_resume_with_creds()
    print("[fake] resume creds OK")

    await test_handles_ng_response()
    print("[fake] NG response handled OK")

    await test_wait_for_code_since_filters_stale()
    print("[fake] wait_for_code since-filter OK")

    print("\n[ok] kuku_lu.py: all tests passed")


if __name__ == "__main__":
    asyncio.run(main())
