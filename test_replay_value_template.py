"""Regression tests for ``replay_engine._resolve_value`` template handling.

Locks in the fix for the bug where the un-substituted-placeholder
fallback regex matched single-brace ``{var}`` tokens that lived
inside double-brace ``{{var}}`` ``value_templates`` markers, causing
the partially-resolved template to be discarded in favour of the
captured literal.
"""
from __future__ import annotations

from replay_engine import _interpolate, _resolve_value


# -----------------------------------------------------------------------------
# _interpolate (lower-level): regression for ctx-key vs {{var}} collision.
# -----------------------------------------------------------------------------


def test_interpolate_preserves_double_brace_when_ctx_collides() -> None:
    """ctx['date']='2025-01-01' must NOT corrupt ``{{date}}``.

    Before the fix, ``_interpolate`` matched the inner ``{date}`` of the
    ``{{date}}`` value-templates token and substituted it, leaving
    ``{2025-01-01}`` — which value_templates.expand cannot recognize.
    The double-brace token must survive unchanged for downstream
    expansion.
    """
    out = _interpolate("{{date}}", {"date": "2025-01-01"})
    assert out == "{{date}}", (
        f"double-brace token corrupted by ctx-key collision: {out!r}"
    )


def test_interpolate_resolves_single_brace_keeps_double_brace() -> None:
    """Mixed string: ``{email}`` resolves, ``{{date}}`` survives."""
    out = _interpolate(
        "{email} on {{date}}",
        {"email": "bob@x.com", "date": "2025-01-01"},
    )
    assert out == "bob@x.com on {{date}}", (
        f"mixed-template interpolation wrong: {out!r}"
    )


def test_interpolate_unknown_var_stays_verbatim() -> None:
    out = _interpolate("{email}", {})
    assert out == "{email}"


def test_interpolate_no_ctx_collision_leaves_double_brace() -> None:
    out = _interpolate("{{uuid4}}", {})
    assert out == "{{uuid4}}"


# -----------------------------------------------------------------------------
# _resolve_value (higher-level): end-to-end coverage of the same behaviour.
# -----------------------------------------------------------------------------


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


# -----------------------------------------------------------------------------
# Devin Review #3182604136: ctx values containing literal {word} must NOT be
# mistaken for unresolved placeholders.
# -----------------------------------------------------------------------------


def test_resolve_value_ctx_value_with_literal_braces_is_not_discarded() -> None:
    """A free-text ctx value like ``"Use {standard} format"`` must reach
    the form intact.

    Before the fix, _resolve_value scanned the *result* of _interpolate
    for leftover ``{word}`` patterns and triggered the fallback to the
    captured literal — silently discarding the successful ctx
    substitution. The fix moved the unresolved-placeholder check to
    inspect the **template** against the **ctx**, not the result.
    """
    action = {
        "value_template": "{description}",
        "value": "fallback-literal",
    }
    out = _resolve_value(
        action, ctx={"description": "Use {standard} format"},
    )
    assert out == "Use {standard} format", (
        f"ctx value with literal braces wrongly discarded: {out!r}"
    )


def test_resolve_value_ctx_value_with_braces_alongside_known_var() -> None:
    """Mixed: ``{a}`` resolves to a value containing ``{b}``;
    ``{b}`` not in ctx is fine because it came from the resolved value."""
    action = {
        "value_template": "{a}",
        "value": "fallback",
    }
    out = _resolve_value(action, ctx={"a": "literal {b} text"})
    assert out == "literal {b} text"


def test_resolve_value_unknown_placeholder_still_falls_back() -> None:
    """Sanity: when the template has a placeholder the ctx can't fill,
    fallback to the captured value still triggers."""
    action = {"value_template": "{email}", "value": "fallback@x.com"}
    assert _resolve_value(action, ctx={}) == "fallback@x.com"
    assert _resolve_value(action, ctx={"name": "x"}) == "fallback@x.com"


def test_resolve_value_partial_template_falls_back_when_missing_var() -> None:
    """``"{email} on {{date}}"`` with email absent → fall back."""
    action = {
        "value_template": "{email} on {{date}}",
        "value": "fallback@x.com on 2025-01-01",
    }
    assert (
        _resolve_value(action, ctx={"other": "x"})
        == "fallback@x.com on 2025-01-01"
    )
