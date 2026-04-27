"""proxy_utils.py — parse, validate, and rotate proxies for Playwright.

Used by auto_fill.py, run_with_email_otp.py, recorder.py and the GUI to give
every browser launch the same proxy support without duplicating logic.

Accepted input shapes
---------------------

1. URL string (the most common form, also what the CLI accepts):

       http://host:port
       http://user:pass@host:port
       https://user:pass@host:port
       socks5://host:port
       socks5://user:pass@host:port           # see note below
       host:port                              # scheme defaults to http

2. Dict (what the JSON config and the GUI emit):

       {
           "server":   "http://host:port",
           "username": "user",                # optional
           "password": "pass",                # optional
           "bypass":   "*.local, 127.0.0.1"   # optional, comma-separated
       }

3. List of URL strings (proxy rotation):

       ["http://a:1", "http://b:2", "socks5://c:3"]

   Loaded from disk via `load_proxy_list(path)`. Lines starting with `#` and
   blank lines are ignored.

The output is always either:

    None                                     # no proxy → don't pass anything
    {"server": ..., "username"?: ..., "password"?: ..., "bypass"?: ...}

which is exactly what `playwright.chromium.launch(proxy=...)` expects.

SOCKS authentication note
-------------------------
Playwright's bundled Chromium does not support username/password auth on
SOCKS proxies (only HTTP/HTTPS). If a SOCKS URL with credentials is given,
`build_playwright_proxy` returns the proxy with the creds attached and lets
Playwright surface its own warning rather than silently dropping them.
"""
from __future__ import annotations

import itertools
import os
import random
import threading
from pathlib import Path
from typing import Any, Iterable, Optional, Union
from urllib.parse import unquote, urlparse


ProxyInput = Union[str, dict, None]


# --------------------------------------------------------------------------------------
#  Parsing
# --------------------------------------------------------------------------------------


def _normalize_url(raw: str) -> str:
    """Add an http:// scheme if the user just typed `host:port`."""
    s = raw.strip()
    if not s:
        return ""
    if "://" not in s:
        s = "http://" + s
    return s


def parse_proxy_string(raw: str) -> Optional[dict]:
    """Parse `[scheme://][user:pass@]host:port[/]` into a Playwright dict.

    Returns None if `raw` is empty/whitespace.
    """
    s = _normalize_url(raw)
    if not s:
        return None

    parsed = urlparse(s)
    if not parsed.hostname:
        raise ValueError(f"proxy URL is missing a host: {raw!r}")
    if not parsed.port:
        raise ValueError(f"proxy URL is missing a port: {raw!r}")

    scheme = (parsed.scheme or "http").lower()
    if scheme not in ("http", "https", "socks4", "socks5"):
        raise ValueError(f"unsupported proxy scheme: {scheme!r}")

    server = f"{scheme}://{parsed.hostname}:{parsed.port}"
    out: dict[str, Any] = {"server": server}
    if parsed.username:
        out["username"] = unquote(parsed.username)
    if parsed.password:
        out["password"] = unquote(parsed.password)
    return out


def normalize_proxy(value: ProxyInput) -> Optional[dict]:
    """Coerce any of the accepted inputs into the Playwright proxy dict."""
    if value is None:
        return None
    if isinstance(value, str):
        return parse_proxy_string(value)
    if isinstance(value, dict):
        # Already a dict — merge-style normalisation. We let `server` be either
        # a URL with creds (then we split them out) OR a plain `host:port` URL
        # plus separate `username` / `password` fields.
        server_raw = value.get("server") or value.get("url") or ""
        if not server_raw:
            return None
        base = parse_proxy_string(server_raw) or {}
        for k in ("username", "password", "bypass"):
            v = value.get(k)
            if v not in (None, ""):
                base[k] = str(v)
        return base or None
    raise TypeError(f"unsupported proxy value type: {type(value).__name__}")


def build_playwright_proxy(
    server: Optional[str] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    bypass: Optional[str] = None,
) -> Optional[dict]:
    """Build a Playwright proxy dict from explicit fields (used by the GUI)."""
    if not server or not server.strip():
        return None
    base = parse_proxy_string(server) or {}
    if username:
        base["username"] = username
    if password:
        base["password"] = password
    if bypass:
        base["bypass"] = bypass
    return base


# --------------------------------------------------------------------------------------
#  Lists / rotation
# --------------------------------------------------------------------------------------


def load_proxy_list(path: Union[str, Path]) -> list[str]:
    """Read a newline-separated proxy list from disk.

    Blank lines and `#` comments are skipped. Each remaining line is returned
    verbatim — call `parse_proxy_string` on it before use.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"proxy list file not found: {path}")
    lines: list[str] = []
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    return lines


class ProxyRotator:
    """Thread-safe iterator over a list of proxy URLs.

    Modes:
        - "round_robin" (default): walk the list in order, wrap around.
        - "random":                pick uniformly at random each call.
        - "none":                  always return the first proxy.
    """

    def __init__(self, proxies: Iterable[str], mode: str = "round_robin") -> None:
        self._proxies = [p for p in proxies if p and p.strip()]
        self._mode = (mode or "round_robin").lower()
        if self._mode not in ("round_robin", "random", "none"):
            raise ValueError(f"unknown rotate mode: {mode!r}")
        self._lock = threading.Lock()
        self._cycle = itertools.cycle(self._proxies) if self._proxies else None

    def __bool__(self) -> bool:
        return bool(self._proxies)

    def __len__(self) -> int:
        return len(self._proxies)

    def next(self) -> Optional[dict]:
        if not self._proxies:
            return None
        with self._lock:
            if self._mode == "random":
                raw = random.choice(self._proxies)
            elif self._mode == "none":
                raw = self._proxies[0]
            else:  # round_robin
                assert self._cycle is not None
                raw = next(self._cycle)
        return parse_proxy_string(raw)


# --------------------------------------------------------------------------------------
#  High-level "give me a proxy from CLI args + config" entry-point
# --------------------------------------------------------------------------------------


def resolve_proxy(
    *,
    cli_proxy: Optional[str] = None,
    cli_proxy_list: Optional[str] = None,
    cli_no_proxy: bool = False,
    cli_rotate: Optional[str] = None,
    cli_bypass: Optional[str] = None,
    config: Optional[dict] = None,
    env: bool = True,
) -> Optional[dict]:
    """Pick the right proxy for this run, in priority order:

        1. --no-proxy flag                                      → None (override)
        2. --proxy-list FILE (+ optional --proxy-rotate)        → first picked
        3. --proxy URL                                          → that URL
        4. config["proxy"] (string or dict)                     → that value
        5. config["proxy_list"] (+ config["proxy_rotate"])      → first picked
        6. env vars HTTPS_PROXY / HTTP_PROXY / ALL_PROXY        → that URL

    Returns the Playwright proxy dict, or None if no proxy is configured.
    """
    if cli_no_proxy:
        return None

    if cli_proxy_list:
        proxies = load_proxy_list(cli_proxy_list)
        if proxies:
            rot = ProxyRotator(proxies, mode=cli_rotate or "round_robin")
            picked = rot.next()
            if picked and cli_bypass:
                picked["bypass"] = cli_bypass
            return picked

    if cli_proxy:
        picked = parse_proxy_string(cli_proxy)
        if picked and cli_bypass:
            picked["bypass"] = cli_bypass
        return picked

    if config:
        if config.get("proxy"):
            picked = normalize_proxy(config["proxy"])
            if picked and cli_bypass:
                picked["bypass"] = cli_bypass
            return picked
        plist = config.get("proxy_list")
        if plist:
            if isinstance(plist, str):
                proxies = load_proxy_list(plist)
            else:
                proxies = list(plist)
            if proxies:
                rot = ProxyRotator(
                    proxies, mode=str(config.get("proxy_rotate") or "round_robin")
                )
                picked = rot.next()
                if picked and cli_bypass:
                    picked["bypass"] = cli_bypass
                return picked

    if env:
        for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
            v = os.environ.get(var)
            if v:
                return parse_proxy_string(v)

    return None


# --------------------------------------------------------------------------------------
#  Display helpers
# --------------------------------------------------------------------------------------


def mask_proxy(proxy: Optional[dict]) -> str:
    """Return a one-line, password-redacted representation for logs."""
    if not proxy:
        return "(none)"
    server = proxy.get("server", "?")
    user = proxy.get("username")
    if user:
        return f"{server} (auth: {user}:***)"
    return server


def add_cli_args(parser) -> None:
    """Register the standard --proxy / --proxy-list / --no-proxy flags.

    Used by every CLI script so we keep the surface identical everywhere.
    """
    g = parser.add_argument_group("proxy")
    g.add_argument(
        "--proxy",
        default=None,
        help=(
            "Proxy URL, e.g. http://user:pass@host:8080 or socks5://host:1080. "
            "Overrides any proxy in the config."
        ),
    )
    g.add_argument(
        "--proxy-list",
        default=None,
        help="Path to a newline-separated proxy list (one URL per line).",
    )
    g.add_argument(
        "--proxy-rotate",
        choices=["round_robin", "random", "none"],
        default=None,
        help="When using --proxy-list, how to pick a proxy (default round_robin).",
    )
    g.add_argument(
        "--proxy-bypass",
        default=None,
        help='Comma-separated hosts to bypass, e.g. "*.local, 127.0.0.1".',
    )
    g.add_argument(
        "--no-proxy",
        action="store_true",
        help="Disable proxies entirely, even if the config or env defines one.",
    )


__all__ = [
    "ProxyRotator",
    "add_cli_args",
    "build_playwright_proxy",
    "load_proxy_list",
    "mask_proxy",
    "normalize_proxy",
    "parse_proxy_string",
    "resolve_proxy",
]
