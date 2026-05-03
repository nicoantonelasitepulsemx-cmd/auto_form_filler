"""Tests for the new Bulk register feature on mail_per_proxy_panel.

We exercise the parser exhaustively (it's pure, no I/O) and smoke-test
the dialog wiring with a hidden Tk root so the UI fields actually
exist.

Run::

    python test_mail_per_proxy_bulk.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from mail_per_proxy_panel import _BulkSpec, _parse_bulk_lines  # noqa: E402


def _ok(msg: str) -> None:
    print(f"[ok] {msg}")


def test_parser_skips_blank_and_comment() -> None:
    text = """
# this is a comment
   # also a comment

alice
"""
    out = _parse_bulk_lines(text)
    assert len(out) == 1
    assert out[0].name == "alice"
    _ok("parser skips blank + # lines")


def test_parser_three_shapes() -> None:
    text = (
        "alice\n"
        "bob@kpay.be\n"
        "carol | carol123@haren.uk | 1.2.3.4:8080:u:p\n"
    )
    out = _parse_bulk_lines(text, default_domain="exdonuts.com")
    assert len(out) == 3
    a, b, c = out
    # Bare local: name=local, domain=default
    assert a.name == "alice"
    assert a.local_part == "alice"
    assert a.domain == "exdonuts.com"
    assert a.proxy is None
    # local@domain: explicit alias, no proxy
    assert b.name == "bob"
    assert b.local_part == "bob"
    assert b.domain == "kpay.be"
    assert b.proxy is None
    # Three-field form: explicit name + alias + proxy
    assert c.name == "carol"
    assert c.local_part == "carol123"
    assert c.domain == "haren.uk"
    assert c.proxy == "1.2.3.4:8080:u:p"
    _ok("parser handles all three input shapes")


def test_parser_dedups_names() -> None:
    """Two lines with the same effective name must auto-rename."""
    text = "alice\nalice@kpay.be\nalice@haren.uk\n"
    out = _parse_bulk_lines(text)
    names = [s.name for s in out]
    assert len(set(names)) == len(names), f"names collided: {names!r}"
    assert names[0] == "alice"
    # Subsequent ones got `_2`, `_3` suffix.
    assert names[1].startswith("alice")
    assert names[2].startswith("alice")
    _ok("parser deduplicates clashing names")


def test_parser_three_field_with_empties() -> None:
    """Empty alias falls back to default domain; ``auto`` means no domain hint."""
    out = _parse_bulk_lines(
        "acct1 | | host:port:u:p\n"
        "acct2 | auto | host2:port:u:p\n",
        default_domain="kpay.be",
    )
    assert len(out) == 2
    a, b = out
    # Empty alias + default_domain set → use the default for the row.
    assert a.name == "acct1"
    assert a.local_part is None
    assert a.domain == "kpay.be"
    assert a.proxy == "host:port:u:p"
    # ``auto`` keyword means "kuku.lu picks" — no domain hint.
    assert b.local_part is None
    assert b.domain is None
    assert b.proxy == "host2:port:u:p"
    _ok("parser tolerates empty / auto alias field in 3-field form")


def test_parser_sanitises_local_part() -> None:
    """Spaces / weird punctuation collapse to dots — kuku.lu only
    accepts ``[a-z0-9._-]`` for local parts."""
    out = _parse_bulk_lines("Alice O'Connor\n")
    assert len(out) == 1
    assert out[0].local_part == "alice.o.connor"
    _ok("parser sanitises Unicode-ish names into kuku-safe local parts")


def test_parser_handles_auto_keyword() -> None:
    out = _parse_bulk_lines(
        "acct1 | auto | host:port:u:p\n"
        "acct2 | random | -\n",
        default_domain="kpay.be",
    )
    assert len(out) == 2
    a, b = out
    # ``auto`` for alias means "let kuku.lu pick".
    assert a.local_part is None
    assert a.proxy == "host:port:u:p"
    # ``-`` for proxy means "no proxy".
    assert b.proxy is None
    _ok("parser handles ``auto`` / ``-`` keywords")


def test_dialog_has_bulk_widgets() -> None:
    """Smoke-test: the dialog exposes bulk-register widgets."""
    if not os.environ.get("DISPLAY"):
        print("[skip] dialog bulk smoke — no $DISPLAY")
        return
    import tkinter as tk

    from mail_per_proxy_panel import MailPerProxyDialog

    try:
        root = tk.Tk()
    except tk.TclError as e:
        print(f"[skip] dialog bulk smoke — Tcl/Tk init failed: {e}")
        return
    root.withdraw()
    try:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "accounts.json"
            path.write_text("[]", encoding="utf-8")
            d = MailPerProxyDialog(root, accounts_path=path)
            d.update_idletasks()
            assert hasattr(d, "bulk_text"), "missing bulk textarea"
            assert hasattr(d, "bulk_concurrency_var"), "missing concurrency spinbox"
            assert hasattr(d, "bulk_status_var"), "missing status label var"
            # Insert text and ensure the parser path is reachable.
            d.bulk_text.insert("1.0", "alice\nbob@kpay.be\n")
            specs = _parse_bulk_lines(d.bulk_text.get("1.0", "end"))
            assert [s.name for s in specs] == ["alice", "bob"]
            d.destroy()
    finally:
        root.destroy()
    _ok("dialog exposes bulk_text + concurrency + status widgets")


if __name__ == "__main__":
    test_parser_skips_blank_and_comment()
    test_parser_three_shapes()
    test_parser_dedups_names()
    test_parser_three_field_with_empties()
    test_parser_sanitises_local_part()
    test_parser_handles_auto_keyword()
    test_dialog_has_bulk_widgets()
    print("\n[ok] all bulk-register tests passed")
