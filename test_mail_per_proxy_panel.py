"""Headless tests for mail_per_proxy_panel.

We don't actually open a Tk window — those tests exercise the
serialisation, helpers and load/save flow that don't need a display.
The dialog itself is smoke-tested with a hidden root in the final
``test_dialog_smoke`` test (skipped if no display is available).

Run::

    python test_mail_per_proxy_panel.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from accounts import Account, load_accounts  # noqa: E402
from kuku_lu import KukuCreds  # noqa: E402
from mail_per_proxy_panel import (  # noqa: E402
    DEFAULT_OTP_POLL,
    DEFAULT_OTP_REGEX,
    DEFAULT_OTP_TIMEOUT,
    KNOWN_KUKU_DOMAINS,
    OTP_PREF_FILE,
    _AccountRow,
    _format_kuku,
    _format_proxy,
    _playwright_to_requests_proxy,
    _proxy_to_playwright_dict,
    load_otp_defaults,
    save_otp_defaults,
)


def _ok(msg: str) -> None:
    print(f"[ok] {msg}")


def test_format_helpers() -> None:
    assert _format_proxy(None) == "—"
    assert _format_proxy("") == "—"
    masked = _format_proxy("http://user:secret@1.2.3.4:8080")
    assert "secret" not in masked, f"expected mask: {masked!r}"
    assert "1.2.3.4" in masked or ":8080" in masked

    assert _format_kuku(None) == "—"
    creds = KukuCreds(csrf_token="c", sessionhash="s", current_address="abc@kpay.be")
    assert _format_kuku(creds) == "abc@kpay.be"
    creds2 = KukuCreds(csrf_token="c", sessionhash="s", current_address=None)
    assert _format_kuku(creds2) == "(no address)"
    _ok("format helpers")


def test_proxy_translation() -> None:
    pw = _proxy_to_playwright_dict("1.2.3.4:8080:u:p")
    assert pw is not None and pw["server"].endswith("1.2.3.4:8080")
    assert pw["username"] == "u"
    assert pw["password"] == "p"

    rq = _playwright_to_requests_proxy(pw)
    assert rq is not None
    assert "http" in rq and "https" in rq
    assert "u:p@" in rq["http"]
    assert "1.2.3.4:8080" in rq["http"]

    assert _playwright_to_requests_proxy(None) is None
    assert _playwright_to_requests_proxy({}) is None
    _ok("proxy translation playwright<->requests")


def test_account_row_roundtrip() -> None:
    raw = {
        "name": "acct1",
        "user_data_dir": "./profiles/acct1",
        "proxy": "http://u:p@host:8080",
        "vars": {"email": "a@b.c"},
        "kuku": {
            "csrf_token": "tok",
            "sessionhash": "hash",
            "current_address": "abc@kpay.be",
        },
    }
    acct = Account.from_dict(raw)
    row = _AccountRow.from_account(acct, raw=raw)
    assert row.name == "acct1"
    assert row.proxy == "http://u:p@host:8080"
    assert row.kuku is not None and row.kuku.current_address == "abc@kpay.be"

    # Mutating a few fields, then writing back, must preserve untouched
    # keys (vars, user_data_dir, etc.).
    row.kuku = KukuCreds(csrf_token="tok2", sessionhash="hash2", current_address="xyz@kpay.be")
    out = row.to_dict()
    assert out["name"] == "acct1"
    assert out["vars"] == {"email": "a@b.c"}
    assert out["kuku"]["current_address"] == "xyz@kpay.be"
    assert out["proxy"] == "http://u:p@host:8080"
    _ok("AccountRow round-trip preserves untouched keys")


def test_account_row_handles_dict_proxy() -> None:
    """Accounts loaded from JSON may have a Playwright-shaped proxy dict.

    Round-tripping through the row should stringify it for editing.
    """
    raw = {
        "name": "acct1",
        "proxy": {"server": "http://1.2.3.4:8080", "username": "u", "password": "p"},
    }
    acct = Account.from_dict(raw)
    row = _AccountRow.from_account(acct, raw=raw)
    assert row.proxy is not None
    assert "1.2.3.4" in row.proxy
    assert "u:p@" in row.proxy or row.proxy.startswith("http://1.2.3.4")
    _ok("AccountRow handles dict proxy")


def test_save_load_accounts_json() -> None:
    """The dialog's save path produces a file that ``accounts.load_accounts``
    can read back without losing kuku creds or vars."""
    rows = [
        _AccountRow(
            name="acct1",
            proxy="http://u:p@host:8080",
            kuku=KukuCreds(
                csrf_token="tok",
                sessionhash="hash",
                current_address="abc@kpay.be",
            ),
            raw={"vars": {"email": "x@y.z"}},
        ),
        _AccountRow(name="acct2", proxy="2.2.2.2:1080"),
    ]
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "accounts.json"
        path.write_text(
            json.dumps([r.to_dict() for r in rows], indent=2),
            encoding="utf-8",
        )
        accts = load_accounts(path)
        assert len(accts) == 2
        a1, a2 = accts
        assert a1.name == "acct1"
        assert a1.kuku is not None and a1.kuku.current_address == "abc@kpay.be"
        assert a1.vars == {"email": "x@y.z"}
        assert a2.name == "acct2"
        assert a2.kuku is None
    _ok("save + load round-trip via accounts.load_accounts")


def test_otp_defaults_persistence() -> None:
    """Saving + reading OTP defaults should round-trip values."""
    backup = OTP_PREF_FILE.read_text(encoding="utf-8") if OTP_PREF_FILE.exists() else None
    try:
        # Default load when missing.
        if OTP_PREF_FILE.exists():
            OTP_PREF_FILE.unlink()
        d = load_otp_defaults()
        assert d["regex"] == DEFAULT_OTP_REGEX
        assert d["timeout"] == DEFAULT_OTP_TIMEOUT
        assert d["poll"] == DEFAULT_OTP_POLL

        save_otp_defaults({
            "regex": r"(\d{6})",
            "from_filter": "facebook",
            "timeout": 240.0,
            "poll": 2.5,
            "domain": "exdonuts.com",
        })
        d2 = load_otp_defaults()
        assert d2["regex"] == r"(\d{6})"
        assert d2["from_filter"] == "facebook"
        assert d2["timeout"] == 240.0
        assert d2["poll"] == 2.5
        assert d2["domain"] == "exdonuts.com"

        # Corrupt file → defaults fallback (don't crash).
        OTP_PREF_FILE.write_text("{not valid json", encoding="utf-8")
        d3 = load_otp_defaults()
        assert d3["regex"] == DEFAULT_OTP_REGEX
    finally:
        if backup is not None:
            OTP_PREF_FILE.write_text(backup, encoding="utf-8")
        elif OTP_PREF_FILE.exists():
            OTP_PREF_FILE.unlink()
    _ok("OTP defaults persistence + corruption tolerance")


def test_dialog_smoke() -> None:
    """Open the dialog with a hidden root; verify it builds without errors.

    Skipped if no $DISPLAY is available (e.g. CI without xvfb).
    """
    if not os.environ.get("DISPLAY"):
        print("[skip] dialog smoke — no $DISPLAY")
        return
    import tkinter as tk

    from mail_per_proxy_panel import MailPerProxyDialog

    try:
        root = tk.Tk()
    except tk.TclError as e:
        print(f"[skip] dialog smoke — Tcl/Tk init failed: {e}")
        return
    root.withdraw()
    try:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "accounts.json"
            path.write_text(
                json.dumps([
                    {
                        "name": "acct1",
                        "proxy": "1.2.3.4:8080:u:p",
                        "kuku": {
                            "csrf_token": "tok",
                            "sessionhash": "hash",
                            "current_address": "abc@kpay.be",
                        },
                    },
                ], indent=2),
                encoding="utf-8",
            )
            d = MailPerProxyDialog(root, accounts_path=path)
            d.update_idletasks()
            # Verify the table has 1 row.
            children = d.tree.get_children()
            assert len(children) == 1, f"expected 1 row, got {len(children)}"
            # Verify selecting it populates the editor fields.
            d.tree.selection_set(children[0])
            d._on_selection_changed()
            assert d.sel_name_var.get() == "acct1"
            assert d.sel_proxy_var.get() == "1.2.3.4:8080:u:p"
            assert "abc@kpay.be" in d.sel_mailbox_var.get()
            # Domain combobox: present, populated, empty by default.
            assert hasattr(d, "domain_var")
            assert d.domain_var.get() == ""
            # Pick a domain and confirm it sticks.
            d.domain_var.set("exdonuts.com")
            assert d.domain_var.get() == "exdonuts.com"
            # Local part field exists and starts empty.
            assert hasattr(d, "local_part_var")
            assert d.local_part_var.get() == ""
            # "Use Name" copies the row's name and sanitises it.
            d._cmd_local_from_name()
            assert d.local_part_var.get() == "acct1"
            d.destroy()
    finally:
        root.destroy()
    _ok("dialog smoke — table + selection wiring")


def test_known_domains_present() -> None:
    """Domain combobox values must include the kuku.lu shipping list."""
    expected = {
        "",  # auto / random
        "exdonuts.com",
        "boxfi.uk",
        "haren.uk",
        "bangban.uk",
        "catgroup.uk",
        "goatmail.uk",
        "sendnow.win",
        "ccmail.uk",
        "tensi.org",
    }
    assert expected.issubset(set(KNOWN_KUKU_DOMAINS)), \
        f"missing kuku.lu domains: {expected - set(KNOWN_KUKU_DOMAINS)}"
    _ok("KNOWN_KUKU_DOMAINS covers shipping kuku.lu domains")


def test_default_regex_matches_meta_otp() -> None:
    """Verify the default OTP regex extracts the code from a Meta email.

    The plaintext payload below mirrors what `Kuku.read_mail()` returns
    after stripping the HTML in the screenshot the user provided
    (Meta verification email containing ``<span class="mb_text">664251</span>``).
    """
    import re
    body = (
        "Hi,\n\nWe noticed you are trying to submit a report to Meta from "
        "your email address.\nPlease enter the confirmation code below to "
        "verify your email address.\n\n664251\n\nThis confirmation code will "
        "be valid for 1 hour.\n\nThank you,\nMeta Support"
    )
    m = re.search(DEFAULT_OTP_REGEX, body)
    assert m is not None and m.group(1) == "664251", \
        f"default regex did not extract 664251 from Meta body: match={m!r}"
    _ok("default OTP regex extracts 664251 from Meta verification email")


def main() -> int:
    test_format_helpers()
    test_proxy_translation()
    test_account_row_roundtrip()
    test_account_row_handles_dict_proxy()
    test_save_load_accounts_json()
    test_otp_defaults_persistence()
    test_known_domains_present()
    test_default_regex_matches_meta_otp()
    test_dialog_smoke()
    print("\n[ok] mail_per_proxy_panel.py: all tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
