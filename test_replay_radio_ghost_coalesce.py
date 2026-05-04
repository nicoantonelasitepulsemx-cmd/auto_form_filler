"""Regression tests for `_normalize_radio_ghost_actions`.

Pre-v4 recordings made by the OLD recorder contain a real
sibling-flip ghost: when the user clicked a radio, React fired a
synthetic click on a sibling to deselect it, Chromium ran the
activation behavior on that sibling and dispatched a TRUSTED
``change`` event — which the old recorder happily captured as a
second ``check checked=true`` action.

On replay, the form lands on the wrong sibling (the user's reported
"I am the rights owner" → "I am reporting on behalf of someone else"
symptom in tm4.json) because there's no way for the new recorder
fix to retroactively scrub existing recordings.

These tests cover the replay-time coalescer that scans a recording
just before execution and drops the second action when two
``check checked=true`` actions land on the same radio group within
350ms of each other.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from replay_engine import (  # noqa: E402
    _normalize_radio_ghost_actions,
    _radio_group_key,
)


def _radio(field_id: str, name: str, value: str, ts: float) -> dict:
    """Build a minimally valid radio check action."""
    return {
        "kind": "check",
        "field_id": field_id,
        "ts": ts,
        "checked": True,
        "_hidden_input_name": name,
        "_hidden_input_type": "radio",
        "radio_value": value,
        "frame_chain": [],
        "fingerprint": {
            "tag": "input",
            "type": "radio",
            "role": None,
            "attributes": {"name": name, "value": value},
        },
    }


def test_radio_group_key_returns_none_for_non_check_action() -> None:
    assert _radio_group_key({"kind": "fill", "ts": 0}) is None


def test_radio_group_key_returns_none_for_unchecked() -> None:
    a = _radio("r1", "rel", "self", 100)
    a["checked"] = False
    assert _radio_group_key(a) is None


def test_radio_group_key_returns_none_for_checkbox() -> None:
    a = _radio("c1", "agree", "1", 100)
    a["_hidden_input_type"] = "checkbox"
    a["fingerprint"]["type"] = "checkbox"
    assert _radio_group_key(a) is None


def test_radio_group_key_groups_same_name() -> None:
    a = _radio("r1", "rel", "self", 100)
    b = _radio("r2", "rel", "agent", 110)
    assert _radio_group_key(a) == _radio_group_key(b)


def test_radio_group_key_separates_different_names() -> None:
    a = _radio("r1", "rel", "self", 100)
    b = _radio("r2", "topic", "ip", 110)
    assert _radio_group_key(a) != _radio_group_key(b)


def test_facebook_trademark_ghost_is_dropped() -> None:
    """Exact tm4.json reproduction: user picked ``self`` but the
    recording also contains a 60ms-later ghost on ``agent``.
    Replay must drop the ghost."""
    actions = [
        _radio("self", "relationship", "self", 100.0),
        # React fires synthetic click on agent ~60ms later, recorder
        # captures the trusted change event as a 2nd action.
        _radio("agent", "relationship", "agent", 160.0),
    ]
    out = _normalize_radio_ghost_actions(actions)
    assert len(out) == 1
    assert out[0]["radio_value"] == "self", (
        "Sibling-flip ghost was not dropped — replay would land on "
        f"{out[-1]['radio_value']!r} instead of the user's actual choice"
    )


def test_three_radio_burst_keeps_only_first() -> None:
    """Some pages chain-flip across multiple siblings."""
    actions = [
        _radio("self", "rel", "self", 100.0),
        _radio("organization", "rel", "organization", 130.0),
        _radio("agent", "rel", "agent", 160.0),
    ]
    out = _normalize_radio_ghost_actions(actions)
    assert len(out) == 1
    assert out[0]["radio_value"] == "self"


def test_legitimate_user_change_of_mind_not_coalesced() -> None:
    """If the user genuinely changes their mind 10s later, both
    clicks must survive."""
    actions = [
        _radio("self", "rel", "self", 100.0),
        _radio("agent", "rel", "agent", 10_100.0),  # 10s later
    ]
    out = _normalize_radio_ghost_actions(actions)
    assert len(out) == 2


def test_coalesce_preserves_non_radio_actions() -> None:
    actions = [
        _radio("self", "rel", "self", 100.0),
        {
            "kind": "fill",
            "field_id": "name",
            "ts": 200.0,
            "value": "Alice",
            "frame_chain": [],
            "fingerprint": {"tag": "input", "type": "text"},
        },
        _radio("agent", "rel", "agent", 250.0),  # ghost — within 250ms of self click? no, 150ms after fill...
    ]
    out = _normalize_radio_ghost_actions(actions)
    # The agent radio is ~150ms after self ghost-window-wise (250 - 100 = 150ms < 350ms)
    # so it should be dropped, but the fill in between must survive.
    assert len(out) == 2
    assert out[0]["kind"] == "check"
    assert out[1]["kind"] == "fill"


def test_two_separate_groups_dont_interfere() -> None:
    """Two different radio groups must each keep their first pick."""
    actions = [
        _radio("self", "rel", "self", 100.0),
        _radio("agent", "rel", "agent", 160.0),  # ghost
        _radio("ip", "topic", "ip", 200.0),
        _radio("counterfeit", "topic", "counterfeit", 260.0),  # ghost
    ]
    out = _normalize_radio_ghost_actions(actions)
    assert len(out) == 2
    assert out[0]["radio_value"] == "self"
    assert out[1]["radio_value"] == "ip"


def test_aria_radio_ghost_dropped_via_fingerprint_role() -> None:
    """ARIA radios use ``role=radio`` instead of ``type=radio`` —
    the fingerprint-based path must still classify them."""
    actions = [
        {
            "kind": "check",
            "field_id": "self",
            "ts": 100.0,
            "checked": True,
            "frame_chain": [],
            "fingerprint": {
                "tag": "div",
                "role": "radio",
                "attributes": {"name": "rel", "value": "self"},
            },
        },
        {
            "kind": "check",
            "field_id": "agent",
            "ts": 160.0,
            "checked": True,
            "frame_chain": [],
            "fingerprint": {
                "tag": "div",
                "role": "radio",
                "attributes": {"name": "rel", "value": "agent"},
            },
        },
    ]
    out = _normalize_radio_ghost_actions(actions)
    assert len(out) == 1
    assert (out[0]["fingerprint"]["attributes"]["value"]) == "self"


def test_empty_input_returns_empty_list() -> None:
    assert _normalize_radio_ghost_actions([]) == []


def test_actions_without_ts_are_passed_through() -> None:
    """If the recording is missing timestamps (very old format),
    we can't measure ghost windows — pass through unchanged rather
    than incorrectly dropping legitimate actions."""
    a = _radio("self", "rel", "self", 0)
    del a["ts"]
    b = _radio("agent", "rel", "agent", 0)
    del b["ts"]
    out = _normalize_radio_ghost_actions([a, b])
    assert len(out) == 2


def test_input_list_is_not_mutated() -> None:
    actions = [
        _radio("self", "rel", "self", 100.0),
        _radio("agent", "rel", "agent", 160.0),
    ]
    before = list(actions)
    _ = _normalize_radio_ghost_actions(actions)
    assert actions == before, "input list was mutated"


if __name__ == "__main__":
    test_radio_group_key_returns_none_for_non_check_action()
    test_radio_group_key_returns_none_for_unchecked()
    test_radio_group_key_returns_none_for_checkbox()
    test_radio_group_key_groups_same_name()
    test_radio_group_key_separates_different_names()
    test_facebook_trademark_ghost_is_dropped()
    test_three_radio_burst_keeps_only_first()
    test_legitimate_user_change_of_mind_not_coalesced()
    test_coalesce_preserves_non_radio_actions()
    test_two_separate_groups_dont_interfere()
    test_aria_radio_ghost_dropped_via_fingerprint_role()
    test_empty_input_returns_empty_list()
    test_actions_without_ts_are_passed_through()
    test_input_list_is_not_mutated()
    print("ok all")
