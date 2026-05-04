"""v4 RADIO RULE classifier — pin down the predicate so the
ARIA-checkbox uncheck regression cannot come back.

The replay engine drops ``check checked=false`` actions on radios
(those are sibling-deselect ghosts). Earlier the predicate defaulted
``_hidden_input_type`` to ``"radio"``, which silently classified
ARIA checkboxes (``div[role=checkbox]`` — recorder ships them with
no ``_hidden_input_type``) as radios and made every uncheck a no-op.

These tests verify ``_is_radio_check_action`` returns the right
answer for the four interesting shapes.
"""
from __future__ import annotations

from replay_engine import _is_radio_check_action


# --------------------------------------------------------------------------- radios

def test_native_hidden_radio_is_classified_as_radio() -> None:
    action = {
        "kind": "check",
        "checked": True,
        "_hidden_input_name": "relationship_rightsowner",
        "_hidden_input_type": "radio",
        "radio_value": "I am the rights owner",
        "fingerprint": {"role": "radio"},
    }
    assert _is_radio_check_action(action) is True


def test_aria_only_radio_is_classified_as_radio() -> None:
    """No `_hidden_input_type`, but fingerprint.role='radio' (Facebook style)."""
    action = {
        "kind": "check",
        "checked": True,
        "fingerprint": {"role": "radio", "accessible_name": "I am the rights owner"},
    }
    assert _is_radio_check_action(action) is True


def test_native_input_type_radio_is_classified_as_radio() -> None:
    """Recorder captured the native `<input type=radio>` without proxy promotion."""
    action = {
        "kind": "check",
        "checked": True,
        "fingerprint": {"type": "radio", "accessible_name": "Yes"},
    }
    assert _is_radio_check_action(action) is True


# --------------------------------------------------------------------------- checkboxes

def test_aria_checkbox_is_NOT_classified_as_radio() -> None:
    """The bug: ARIA checkbox with no `_hidden_input_type` was being
    misclassified because the predicate defaulted to ``"radio"``."""
    action = {
        "kind": "check",
        "checked": False,  # uncheck
        "fingerprint": {"role": "checkbox", "accessible_name": "I agree"},
    }
    assert _is_radio_check_action(action) is False


def test_native_hidden_checkbox_is_NOT_classified_as_radio() -> None:
    action = {
        "kind": "check",
        "checked": False,
        "_hidden_input_name": "agree",
        "_hidden_input_type": "checkbox",
        "fingerprint": {"role": "checkbox"},
    }
    assert _is_radio_check_action(action) is False


# --------------------------------------------------------------------------- edge cases

def test_no_fingerprint_and_no_hidden_input_returns_false() -> None:
    """A bare ``check`` action with no metadata is NOT a radio."""
    action = {"kind": "check", "checked": True}
    assert _is_radio_check_action(action) is False


def test_empty_fingerprint_dict_returns_false() -> None:
    action = {"kind": "check", "checked": False, "fingerprint": {}}
    assert _is_radio_check_action(action) is False


def test_role_uppercase_still_matches() -> None:
    """Fingerprint role is lower-cased before comparison."""
    action = {
        "kind": "check",
        "checked": True,
        "fingerprint": {"role": "RADIO"},
    }
    assert _is_radio_check_action(action) is True
