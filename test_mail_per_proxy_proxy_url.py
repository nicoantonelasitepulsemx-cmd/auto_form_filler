"""Regression tests for `_playwright_to_requests_proxy` URL-encoding.

Devin Review BUG #3182700858: passwords containing ``@`` or ``:`` (both
common in proxy-seller credentials) produced malformed URLs because the
function spliced raw user/password into the URL string. ``urllib3``'s
``urlparse`` then split on the **last** ``@`` and **first** ``:`` in
the netloc, scrambling host and auth fields and silently breaking proxy
auth for every kuku.lu HTTP call.

These tests assert URL-encoding is applied to user and password.
"""
from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).parent))

from mail_per_proxy_panel import _playwright_to_requests_proxy  # noqa: E402


def test_none_proxy_returns_none() -> None:
    assert _playwright_to_requests_proxy(None) is None
    assert _playwright_to_requests_proxy({}) is None


def test_no_auth_returns_server_unchanged() -> None:
    out = _playwright_to_requests_proxy({"server": "http://1.2.3.4:8080"})
    assert out == {
        "http": "http://1.2.3.4:8080",
        "https": "http://1.2.3.4:8080",
    }


def test_simple_auth_round_trips() -> None:
    out = _playwright_to_requests_proxy({
        "server": "http://1.2.3.4:8080",
        "username": "admin",
        "password": "secret",
    })
    assert out is not None
    assert out["http"] == "http://admin:secret@1.2.3.4:8080"


def test_password_with_at_sign_is_encoded() -> None:
    """Password ``p@ss`` must encode to ``p%40ss``.

    Without encoding, the URL ``http://admin:p@ss@1.2.3.4:8080`` would
    parse with host ``ss@1.2.3.4`` (urllib3 splits on the LAST ``@``).
    With encoding the URL is unambiguous and ``urlsplit`` recovers
    host, user, and password correctly.
    """
    out = _playwright_to_requests_proxy({
        "server": "http://1.2.3.4:8080",
        "username": "admin",
        "password": "p@ss",
    })
    assert out is not None
    url = out["http"]
    assert "%40" in url, f"@ sign was not URL-encoded: {url!r}"
    parts = urlsplit(url)
    assert parts.hostname == "1.2.3.4"
    assert parts.port == 8080
    assert parts.username == "admin"
    # urlsplit returns the raw, percent-encoded password; unquote to
    # recover the original.
    assert unquote(parts.password or "") == "p@ss"


def test_password_with_colon_is_encoded() -> None:
    """Password ``a:b`` must encode the colon so it isn't taken as
    the user/password separator."""
    out = _playwright_to_requests_proxy({
        "server": "http://1.2.3.4:8080",
        "username": "admin",
        "password": "a:b",
    })
    assert out is not None
    url = out["http"]
    assert "%3A" in url.upper(), f"colon was not URL-encoded: {url!r}"
    parts = urlsplit(url)
    assert parts.hostname == "1.2.3.4"
    assert parts.port == 8080
    assert parts.username == "admin"
    assert unquote(parts.password or "") == "a:b"


def test_username_with_special_chars_is_encoded() -> None:
    """Some proxy sellers issue email-style usernames containing ``@``."""
    out = _playwright_to_requests_proxy({
        "server": "http://1.2.3.4:8080",
        "username": "alice@corp",
        "password": "secret",
    })
    assert out is not None
    url = out["http"]
    parts = urlsplit(url)
    assert parts.hostname == "1.2.3.4"
    assert unquote(parts.username or "") == "alice@corp"
    assert parts.password == "secret"


def test_socks_scheme_preserved() -> None:
    out = _playwright_to_requests_proxy({
        "server": "socks5://10.0.0.1:1080",
        "username": "user",
        "password": "pass",
    })
    assert out is not None
    assert out["http"].startswith("socks5://")


if __name__ == "__main__":
    test_none_proxy_returns_none()
    test_no_auth_returns_server_unchanged()
    test_simple_auth_round_trips()
    test_password_with_at_sign_is_encoded()
    test_password_with_colon_is_encoded()
    test_username_with_special_chars_is_encoded()
    test_socks_scheme_preserved()
    print("ok all")
