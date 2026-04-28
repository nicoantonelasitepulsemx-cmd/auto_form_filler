"""kuku_lu.py — automate the m.kuku.lu disposable-email service.

m.kuku.lu (also branded "InstAddr") is a free Japanese throw-away mail
service that lets you mint as many `<random>@<domain>` aliases as you
want without registering. Each *session* is identified by two cookies:

    cookie_csrf_token   — short opaque token, also echoed in URLs
    cookie_sessionhash  — long opaque session id

If you persist those two strings you have a stable "account" — you can
come back days later, see the same inbox, and create more aliases under
the same identity. We expose the pair via :py:meth:`Kuku.credentials` so
the GUI can save them next to a proxy in ``accounts.json``.

The endpoints we hit are reverse-engineered from the public client
(``taka-4602/m.kuku.lu-Generator``) and from inspecting the live site:

    POST  /                           → bootstraps the session cookies
    GET   /index.php?action=addMailAddrByAuto&...    → mint a random alias
    GET   /index.php?action=addMailAddrByManual&...  → mint a chosen alias
    GET   /recv._ajax.php?q=<addr>    → list mails for an alias (HTML)
    POST  /smphone.app.recv.view.php  → fetch one mail's body

Two backends are supported:

* **Playwright** (default, recommended). Uses an existing
  ``BrowserContext`` so the requests come from the same browser
  fingerprint + proxy + cookies as the form recording itself. This is
  the only path that reliably bypasses the Cloudflare "challenge"
  kuku.lu started shipping in 2024.

* **HTTP** (``requests``). Lightweight, useful for headless replay
  hosts on residential IPs where Cloudflare doesn't challenge. Falls
  through cleanly when blocked so the caller can switch to Playwright.

Public surface
--------------

>>> from kuku_lu import Kuku
>>> async with Kuku.from_playwright(context) as k:    # inside a Playwright BrowserContext
...     addr = await k.create_address()
...     code = await k.wait_for_code(addr, regex=r"(\\d{5,8})", timeout=180)

Testing
-------

The test ``test_kuku_lu.py`` spins up a local ``aiohttp`` server that
emulates the four endpoints above and drives the HTTP backend through
it. There is no live-network test in CI because kuku.lu sits behind
Cloudflare and would fail from datacenter IPs.
"""
from __future__ import annotations

import asyncio
import dataclasses
import re
import time
from typing import Any, Awaitable, Callable, Optional, Pattern
from urllib.parse import quote


_BASE_URL = "https://m.kuku.lu"
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)


@dataclasses.dataclass
class KukuCreds:
    """Persistable kuku.lu identity.

    Saved in ``accounts.json`` next to a proxy so each worker uses its
    own inbox. ``csrf_token`` and ``sessionhash`` are the two cookies
    that uniquely identify a kuku.lu account.

    ``current_address`` is the address most recently minted for this
    account; the recorder/replay engine uses it as the default unless
    the JSON config points at a specific alias.
    """
    csrf_token: str
    sessionhash: str
    current_address: Optional[str] = None
    base_url: str = _BASE_URL  # for tests / mirrors

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "KukuCreds":
        return cls(
            csrf_token=str(d["csrf_token"]),
            sessionhash=str(d["sessionhash"]),
            current_address=d.get("current_address"),
            base_url=d.get("base_url") or _BASE_URL,
        )


class KukuError(RuntimeError):
    """Raised when the upstream service refuses or cannot be parsed."""


# --------------------------------------------------------------------------------------
#  HTML parsing helpers
# --------------------------------------------------------------------------------------

# kuku.lu's "create address" endpoint replies with a 3-character status
# prefix followed by the address itself, e.g. "OK:abc123@neko2.net" or
# "NG:already_used".
_STATUS_PREFIX_RE = re.compile(r"^(OK|NG|EX|FN)[:\-]?", re.I)


def _strip_status_prefix(s: str) -> tuple[str, str]:
    """Return (status, payload) from a kuku.lu status reply.

    The legacy code in ``taka-4602/m.kuku.lu-Generator`` blindly slices
    the first 3 chars (``text[3:]``) which is fragile when the server
    answers with ``NG`` or with a longer marker. We do the same job with
    a regex and explicitly surface the status.
    """
    m = _STATUS_PREFIX_RE.match(s.strip())
    if not m:
        return ("", s.strip())
    return (m.group(1).upper(), s.strip()[m.end():])


# Each row in the inbox HTML embeds an ``openMailData('<num>', '<key>')``
# call which we use to fetch the body. The format is stable across kuku.lu
# locale switches.
_OPEN_MAIL_RE = re.compile(
    r"openMailData\(\s*'(?P<num>[^']+)'\s*,\s*'(?P<key>[^']+)'",
)


@dataclasses.dataclass
class MailEntry:
    """One row in the inbox listing.

    ``num`` and ``key`` are opaque tokens kuku.lu uses to identify a
    specific message; pass them to :py:meth:`Kuku.read_mail` to fetch
    the body.
    """
    num: str
    key: str
    timestamp: Optional[float] = None  # parsed if present, None otherwise


def _parse_inbox(html: str) -> list[MailEntry]:
    """Extract all (num, key) pairs from an inbox HTML page.

    Order is preserved (newest first, like kuku.lu's UI).
    """
    out: list[MailEntry] = []
    for m in _OPEN_MAIL_RE.finditer(html):
        out.append(MailEntry(num=m.group("num"), key=m.group("key")))
    return out


# --------------------------------------------------------------------------------------
#  Backends
# --------------------------------------------------------------------------------------

class _Backend:
    """Abstract HTTP backend — async by design so both Playwright and
    requests can fit behind the same shape. Returns the body as text."""

    async def get(self, url: str) -> str: raise NotImplementedError
    async def post(self, url: str, data: Optional[dict] = None) -> str: raise NotImplementedError
    async def cookie(self, name: str) -> Optional[str]: raise NotImplementedError
    async def set_cookie(self, name: str, value: str) -> None: raise NotImplementedError
    async def aclose(self) -> None: pass


class _RequestsBackend(_Backend):
    """Plain HTTP via ``requests`` — runs the blocking call in a thread.

    Suitable for headless servers on residential IPs. Fails closed when
    Cloudflare challenges (``403`` returned, no cookies set).
    """

    def __init__(
        self,
        *,
        proxy: Optional[dict] = None,
        user_agent: str = _DEFAULT_UA,
        base_url: str = _BASE_URL,
    ) -> None:
        import requests
        self._requests = requests
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": user_agent,
            "Accept-Language": "ja,en;q=0.7",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        self._proxy = proxy
        self._base_url = base_url

    async def get(self, url: str) -> str:
        return await asyncio.to_thread(self._do, "GET", url, None)

    async def post(self, url: str, data: Optional[dict] = None) -> str:
        return await asyncio.to_thread(self._do, "POST", url, data)

    def _do(self, method: str, url: str, data: Optional[dict]) -> str:
        kwargs: dict[str, Any] = {"timeout": 20}
        if self._proxy:
            kwargs["proxies"] = self._proxy
        if data:
            kwargs["data"] = data
        r = self._session.request(method, url, **kwargs)
        if r.status_code == 403:
            raise KukuError(
                "kuku.lu returned 403 (Cloudflare challenge). "
                "Use the Playwright backend or run from a residential IP."
            )
        r.raise_for_status()
        return r.text

    async def cookie(self, name: str) -> Optional[str]:
        v = self._session.cookies.get(name)
        return str(v) if v else None

    async def set_cookie(self, name: str, value: str) -> None:
        self._session.cookies.set(name, value)


class _PlaywrightBackend(_Backend):
    """Drive kuku.lu through an existing Playwright ``BrowserContext``.

    All requests run inside ``page.evaluate(() => fetch(...))`` so they
    inherit the context's cookies, UA, proxy, and whatever Cloudflare
    challenge cookies were already solved by the user.
    """

    def __init__(self, page: Any, *, base_url: str = _BASE_URL) -> None:
        self._page = page
        self._base_url = base_url

    async def get(self, url: str) -> str:
        return await self._page.evaluate(
            "u => fetch(u, {credentials:'include'}).then(r=>r.text())",
            url,
        )

    async def post(self, url: str, data: Optional[dict] = None) -> str:
        body = "&".join(
            f"{quote(str(k))}={quote(str(v))}" for k, v in (data or {}).items()
        )
        return await self._page.evaluate(
            """async (args) => {
                const r = await fetch(args.u, {
                    method: 'POST',
                    credentials: 'include',
                    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                    body: args.b,
                });
                return await r.text();
            }""",
            {"u": url, "b": body},
        )

    async def cookie(self, name: str) -> Optional[str]:
        cookies = await self._page.context.cookies(self._base_url)
        for c in cookies:
            if c.get("name") == name:
                return str(c.get("value") or "") or None
        return None

    async def set_cookie(self, name: str, value: str) -> None:
        # kuku.lu cookies sit on m.kuku.lu — we let the host's URL drive
        # the domain so this also works against a local mock server.
        from urllib.parse import urlsplit
        parts = urlsplit(self._base_url)
        domain = parts.hostname or "m.kuku.lu"
        await self._page.context.add_cookies([{
            "name": name,
            "value": value,
            "domain": domain,
            "path": "/",
        }])


# --------------------------------------------------------------------------------------
#  Public client
# --------------------------------------------------------------------------------------

class Kuku:
    """High-level kuku.lu client.

    Construct with one of the factories rather than directly:

    * :py:meth:`Kuku.from_playwright` — preferred, reuses a Playwright
      ``BrowserContext``. Requires you to navigate to ``m.kuku.lu``
      first so the context picks up Cloudflare's clearance cookie.
    * :py:meth:`Kuku.from_requests` — quick HTTP-only client. Pass
      saved :py:class:`KukuCreds` to resume a previous account.
    """

    def __init__(
        self,
        backend: _Backend,
        *,
        creds: Optional[KukuCreds] = None,
        base_url: str = _BASE_URL,
    ) -> None:
        self._backend = backend
        self._base_url = base_url.rstrip("/")
        self._creds = creds  # may be None until init_session()

    # ---- factories --------------------------------------------------

    @classmethod
    async def from_requests(
        cls,
        *,
        creds: Optional[KukuCreds] = None,
        proxy: Optional[dict] = None,
        base_url: str = _BASE_URL,
    ) -> "Kuku":
        backend = _RequestsBackend(proxy=proxy, base_url=base_url)
        k = cls(backend, creds=creds, base_url=base_url)
        await k.init_session()
        return k

    @classmethod
    async def from_playwright(
        cls,
        page: Any,
        *,
        creds: Optional[KukuCreds] = None,
        base_url: str = _BASE_URL,
    ) -> "Kuku":
        backend = _PlaywrightBackend(page, base_url=base_url)
        k = cls(backend, creds=creds, base_url=base_url)
        await k.init_session()
        return k

    # ---- async ctx --------------------------------------------------

    async def __aenter__(self) -> "Kuku":
        return self

    async def __aexit__(self, *exc) -> None:
        await self._backend.aclose()

    # ---- session lifecycle -----------------------------------------

    async def init_session(self) -> None:
        """Bootstrap or restore the kuku.lu session.

        If we already have credentials, push them into the backend's
        cookie jar and ping the home page so the server marks us
        active. Otherwise, do a clean POST and capture the cookies the
        server hands back.
        """
        if self._creds is not None:
            await self._backend.set_cookie("cookie_csrf_token", self._creds.csrf_token)
            await self._backend.set_cookie("cookie_sessionhash", self._creds.sessionhash)
            await self._backend.post(self._base_url, data={})
            return
        # Fresh session: POST / and capture both cookies.
        await self._backend.post(self._base_url, data={})
        csrf = await self._backend.cookie("cookie_csrf_token")
        sess = await self._backend.cookie("cookie_sessionhash")
        if not csrf or not sess:
            raise KukuError(
                "kuku.lu did not set session cookies — "
                "the IP may be Cloudflare-challenged."
            )
        self._creds = KukuCreds(
            csrf_token=csrf, sessionhash=sess, base_url=self._base_url,
        )

    def credentials(self) -> KukuCreds:
        """Return the current saved-able credentials.

        Raises if :py:meth:`init_session` has not run yet.
        """
        if self._creds is None:
            raise KukuError("kuku.lu session not yet initialised")
        return self._creds

    # ---- alias management ------------------------------------------

    async def create_address(
        self,
        *,
        domain: Optional[str] = None,
    ) -> str:
        """Mint a new disposable address.

        ``domain=None`` lets kuku.lu pick a random domain (the cheapest
        option). Pass e.g. ``"kpay.be"`` to force a specific domain.
        Returns the address as a string, e.g. ``"abc123@kpay.be"``.
        """
        if domain:
            url = (
                f"{self._base_url}/index.php"
                f"?action=addMailAddrByManual&nopost=1&by_system=1"
                f"&t={int(time.time())}"
                f"&csrf_token_check={self._csrf()}"
                f"&newdomain={quote(domain)}"
            )
        else:
            url = (
                f"{self._base_url}/index.php"
                f"?action=addMailAddrByAuto&nopost=1&by_system=1"
            )
        body = await self._backend.get(url)
        status, payload = _strip_status_prefix(body)
        if status != "OK" or "@" not in payload:
            raise KukuError(
                f"kuku.lu refused address creation: status={status!r} body={body[:120]!r}"
            )
        if self._creds is not None:
            self._creds.current_address = payload
        return payload

    async def list_mails(self, address: Optional[str] = None) -> list[MailEntry]:
        """Return all messages currently in ``address``'s inbox."""
        addr = address or (self._creds.current_address if self._creds else None)
        if not addr:
            raise KukuError("no address provided and no current_address on the session")
        url = (
            f"{self._base_url}/recv._ajax.php"
            f"?&q={quote(addr)}&&nopost=1&csrf_token_check={self._csrf()}"
        )
        html = await self._backend.get(url)
        return _parse_inbox(html)

    async def read_mail(self, entry: MailEntry) -> str:
        """Return the *plain text* of one message.

        We strip HTML tags conservatively so a regex search is
        straightforward — the upstream HTML view embeds a 1×1 tracking
        pixel and a confirmation-code anchor we don't need.
        """
        url = f"{self._base_url}/smphone.app.recv.view.php"
        html = await self._backend.post(url, data={
            "num": entry.num,
            "key": entry.key,
            "noscroll": "1",
        })
        # crude HTML strip — same approach as run_with_email_otp.py
        return re.sub(r"<[^>]+>", " ", html)

    # ---- code extraction ------------------------------------------

    async def wait_for_code(
        self,
        address: Optional[str] = None,
        *,
        regex: str | Pattern[str] = r"(?<!\d)(\d{5,8})(?!\d)",
        timeout: float = 180.0,
        poll_interval: float = 4.0,
        from_filter: Optional[str] = None,
        since: Optional[float] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> str:
        """Poll the inbox until a matching code arrives, then return it.

        ``regex`` must define one capture group — the digits we'll
        return. Defaults to 5–8 contiguous digits, the same shape FB /
        Google / most OTP senders use.

        ``since`` is a Unix timestamp; messages observed for the first
        time after this point are considered. ``None`` means "from
        now". Set this to e.g. ``time.time() - 10`` if you submitted
        the form just before calling.
        """
        pat = re.compile(regex) if isinstance(regex, str) else regex
        deadline = time.time() + timeout
        seen: set[str] = set()
        while time.time() < deadline:
            try:
                mails = await self.list_mails(address)
            except KukuError:
                await sleep(poll_interval)
                continue
            for entry in mails:
                if entry.num in seen:
                    continue
                seen.add(entry.num)
                try:
                    body = await self.read_mail(entry)
                except KukuError:
                    continue
                if from_filter and from_filter.lower() not in body.lower():
                    continue
                m = pat.search(body)
                if m:
                    return m.group(1) if m.groups() else m.group(0)
            await sleep(poll_interval)
        raise KukuError(
            f"timed out after {timeout:.0f}s waiting for a code matching {pat.pattern!r}"
        )

    # ---- internals --------------------------------------------------

    def _csrf(self) -> str:
        if self._creds is None:
            raise KukuError("kuku.lu session not yet initialised")
        return self._creds.csrf_token


__all__ = [
    "Kuku",
    "KukuCreds",
    "KukuError",
    "MailEntry",
]
