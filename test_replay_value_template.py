"""Regression tests for ``replay_engine._resolve_value`` template handling.

Locks in the fix for the bug where the un-substituted-placeholder
fallback regex matched single-brace ``{var}`` tokens that lived
inside double-brace ``{{var}}`` ``value_templates`` markers, causing
the partially-resolved template to be discarded in favour of the
captured literal.
"""
from __future__ import annotations

from replay_engine import _resolve_value


def test_unresolved_single_brace_falls_back_to_captured_value() -> None:
    """``{email}`` with no ctx should fall back to the recorded literal."""
    action = {"value_template": "{email}", "value": "fallback@example.com"}
    assert _resolve_value(action, ctx=None) == "fallback@example.com"
    assert _resolve_value(action, ctx={}) == "fallback@example.com"


def test_resolved_single_brace_uses_ctx_value() -> None:
    action = {"value_template": "{email}", "value": "fallback@example.com"}
    assert _resolve_value(action, ctx={"email": "bob@x.com"}) == "bob@x.com"


def test_double_brace_value_templates_token_is_preserved() -> None:
    """A bare ``{{date}}`` must not be mistaken for an unresolved ``{date}``.

    Before the fix, the fallback regex would match ``{date}`` inside
    ``{{date}}`` and we'd discard the template. After the fix, the
    string survives ``_interpolate`` unchanged and ``value_templates.expand``
    gets a chance to substitute it.
    """
    action = {"value_template": "{{date}}", "value": "2025-01-01"}
    out = _resolve_value(action, ctx={})
    assert isinstance(out, str)
    # Either the value_templates module expanded it (preferred) or the
    # raw token survives \u2014 either way we must NOT have fallen back
    # to the captured ``"2025-01-01"`` literal because that would mean
    # the ``{{date}}`` token was being treated as un-substituted.
    assert out != "2025-01-01" or "{{" in out


def test_mixed_template_resolves_ctx_var_keeps_double_brace() -> None:
    """``"{email} on {{date}}"`` with email in ctx should NOT fall back."""
    action = {
        "value_template": "{email} on {{date}}",
        "value": "fallback@x.com on 2025-01-01",
    }
    out = _resolve_value(action, ctx={"email": "bob@x.com"})
    # ctx var resolved, double-brace token still in the string for
    # downstream value_templates expansion. The captured literal must
    # not have replaced it.
    assert isinstance(out, str)
    assert "bob@x.com" in out
    assert out != "fallback@x.com on 2025-01-01"


def test_unresolved_single_brace_alongside_double_brace_falls_back() -> None:
    """When the ``{var}`` *is* unresolved, fallback still triggers."""
    action = {
        "value_template": "{email} on {{date}}",
        "value": "fallback@x.com on 2025-01-01",
    }
    # email NOT in ctx \u2192 fallback to captured literal.
    assert (
        _resolve_value(action, ctx={})
        == "fallback@x.com on 2025-01-01"
    )


def test_no_value_template_returns_captured_value() -> None:
    action = {"value": "literal"}
    assert _resolve_value(action, ctx=None) == "literal"
    assert _resolve_value(action, ctx={"email": "x"}) == "literal"
