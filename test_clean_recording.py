"""Unit tests for the recording sanitizer (A15 ghost-event scrubber).

These cases reproduce real-world bugs reported in the wild:

1. **Facebook trademark form**: a single radio in a group fires
   repeatedly with a non-user value as React re-renders the group
   while the user types in other fields. Sanitizer keeps the user's
   genuine first pick.

2. **Multi-select checkbox group** (Facebook ``content_type[]``): user
   ticks two boxes in the same group; ghosts duplicate each. Sanitizer
   preserves both genuine ticks and drops only duplicates.

3. **Toggle then untoggle** a checkbox: tick + untick should both
   survive because they have different ``checked`` states.

4. **Custom role=radio widget** with no hidden input: dedup falls back
   to ``(field_id, radio_value, checked)``.
"""

from __future__ import annotations

import json
from pathlib import Path

from clean_recording import sanitize_actions, sanitize_config


def test_facebook_trademark_radio_ghosts() -> None:
    """User's tm1.json shape: one real click, six ghost re-renders."""
    actions = [
        {"kind": "check", "field_id": "continuereport",
         "_hidden_input_name": "continuereport",
         "_hidden_input_type": "radio",
         "radio_value": "trademark", "checked": True},
        {"kind": "check", "field_id": "relationship_rightsowner",
         "_hidden_input_name": "relationship_rightsowner",
         "_hidden_input_type": "radio",
         "radio_value": "I am the rights owner", "checked": True},
        # Ghost re-renders below — wrong value, fired between fills.
        {"kind": "check", "field_id": "relationship_rightsowner",
         "_hidden_input_name": "relationship_rightsowner",
         "_hidden_input_type": "radio",
         "radio_value": "I am reporting on behalf of someone else.",
         "checked": True},
        {"kind": "fill", "field_id": "email",
         "value": "alice@example.com"},
        {"kind": "check", "field_id": "relationship_rightsowner",
         "_hidden_input_name": "relationship_rightsowner",
         "_hidden_input_type": "radio",
         "radio_value": "I am reporting on behalf of someone else.",
         "checked": True},
        {"kind": "fill", "field_id": "your_name", "value": "Alice"},
        {"kind": "check", "field_id": "relationship_rightsowner",
         "_hidden_input_name": "relationship_rightsowner",
         "_hidden_input_type": "radio",
         "radio_value": "I am reporting on behalf of someone else.",
         "checked": True},
    ]
    cleaned, dropped = sanitize_actions(actions)
    assert dropped == 3, f"expected 3 ghost drops, got {dropped}"
    radio_picks = [
        a for a in cleaned
        if a["kind"] == "check" and a.get("_hidden_input_name") == "relationship_rightsowner"
    ]
    assert len(radio_picks) == 1, radio_picks
    assert radio_picks[0]["radio_value"] == "I am the rights owner"
    # Both fills must survive.
    fills = [a for a in cleaned if a["kind"] == "fill"]
    assert len(fills) == 2


def test_multi_select_checkbox_group_preserved() -> None:
    """Facebook content_type[] style: two ticks in same name group, both kept."""
    actions = [
        {"kind": "check", "field_id": "content_type",
         "_hidden_input_name": "content_type[]",
         "_hidden_input_type": "checkbox",
         "radio_value": "Photo", "checked": True},
        # Ghost duplicate.
        {"kind": "check", "field_id": "content_type",
         "_hidden_input_name": "content_type[]",
         "_hidden_input_type": "checkbox",
         "radio_value": "Photo", "checked": True},
        {"kind": "check", "field_id": "content_type",
         "_hidden_input_name": "content_type[]",
         "_hidden_input_type": "checkbox",
         "radio_value": "Ad", "checked": True},
        # Ghost again.
        {"kind": "check", "field_id": "content_type",
         "_hidden_input_name": "content_type[]",
         "_hidden_input_type": "checkbox",
         "radio_value": "Ad", "checked": True},
    ]
    cleaned, dropped = sanitize_actions(actions)
    assert dropped == 2
    values = sorted(a["radio_value"] for a in cleaned)
    assert values == ["Ad", "Photo"]


def test_checkbox_toggle_then_untoggle_survives() -> None:
    """User ticks then unticks the same checkbox; both events kept."""
    actions = [
        {"kind": "check", "field_id": "agree",
         "_hidden_input_name": "agree",
         "_hidden_input_type": "checkbox",
         "radio_value": "on", "checked": True},
        {"kind": "check", "field_id": "agree",
         "_hidden_input_name": "agree",
         "_hidden_input_type": "checkbox",
         "radio_value": "on", "checked": False},
    ]
    cleaned, dropped = sanitize_actions(actions)
    assert dropped == 0
    assert [a["checked"] for a in cleaned] == [True, False]


def test_role_widget_without_hidden_input() -> None:
    """role=radio custom widget: dedup by (field_id, value, checked)."""
    actions = [
        {"kind": "check", "field_id": "plan",
         "radio_value": "pro", "checked": True},
        # Ghost.
        {"kind": "check", "field_id": "plan",
         "radio_value": "pro", "checked": True},
        # Genuine new pick.
        {"kind": "check", "field_id": "plan",
         "radio_value": "enterprise", "checked": True},
    ]
    cleaned, dropped = sanitize_actions(actions)
    assert dropped == 1
    values = [a["radio_value"] for a in cleaned]
    assert values == ["pro", "enterprise"]


def test_non_check_actions_pass_through_unchanged() -> None:
    """``fill``, ``submit``, ``select``, ``set_files`` are never deduped."""
    duplicate_fill = {"kind": "fill", "field_id": "email", "value": "a@b.c"}
    actions = [duplicate_fill, dict(duplicate_fill), {"kind": "submit"}]
    cleaned, dropped = sanitize_actions(actions)
    assert dropped == 0
    assert len(cleaned) == 3


def test_top_level_config_round_trip(tmp_path: Path) -> None:
    """``sanitize_config`` preserves all non-action keys verbatim."""
    cfg = {
        "version": 2,
        "target_url": "https://example.com/form",
        "wait_for_selector": "input[name=email]",
        "actions": [
            {"kind": "check", "_hidden_input_name": "x",
             "_hidden_input_type": "radio",
             "radio_value": "a", "checked": True},
            {"kind": "check", "_hidden_input_name": "x",
             "_hidden_input_type": "radio",
             "radio_value": "b", "checked": True},  # ghost
        ],
        "submit": {"kind": "submit"},
        "captured_at": "2025-05-03T09:30:00",
    }
    out, stats = sanitize_config(cfg)
    assert stats["actions_dropped"] == 1
    assert stats["actions_in"] == 2
    assert stats["actions_out"] == 1
    # Non-action keys untouched.
    for k in ("version", "target_url", "wait_for_selector", "submit", "captured_at"):
        assert out[k] == cfg[k]


def test_real_tm1_json_if_present() -> None:
    """End-to-end: the user's actual tm1.json shrinks 45 → 25 actions."""
    p = Path(__file__).parent / "tm1.json"
    if not p.is_file():
        return  # not bundled — skip
    cfg = json.loads(p.read_text(encoding="utf-8"))
    out, stats = sanitize_config(cfg)
    assert stats["actions_in"] == 45
    assert stats["actions_dropped"] == 20
    assert stats["actions_out"] == 25
    # The single ``relationship_rightsowner`` keeper is the user's pick.
    pick = next(
        a for a in out["actions"]
        if a.get("_hidden_input_name") == "relationship_rightsowner"
    )
    assert pick["radio_value"] == "I am the rights owner"
