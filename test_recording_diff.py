"""Unit tests for ``recording_diff``.

Pure data tests — no network, no Playwright. We construct minimal
recorder configs as plain dicts and assert the report shape /
similarity score.
"""
from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

import recording_diff as rd


# --------------------------------------------------------------------------- helpers

def _act(
    field_id: str,
    *,
    kind: str = "fill",
    value: str = "",
    role: str = "textbox",
    name: str = "",
    label: str = "",
    placeholder: str = "",
    frame_chain: tuple = (),
    type_: str = "",
    testid: str = "",
) -> dict:
    """Build a recorder-style action dict.

    Mirrors the real recorder layout: ``frame_chain`` lives at the
    action level, and ``id``/``name``/``placeholder`` live inside
    ``fingerprint.attributes`` (NOT directly on ``fingerprint``).
    """
    return {
        "field_id":    field_id,
        "kind":        kind,
        "value":       value,
        "frame_chain": list(frame_chain) or ["top"],
        "fingerprint": {
            "tag":             "input",
            "role":            role,
            "type":            type_,
            "accessible_name": name or label or field_id,
            "neighbour_text":  label,
            "attributes": {
                "id":          field_id,
                "name":        field_id,
                "placeholder": placeholder,
                "data-testid": testid,
            },
        },
    }


# --------------------------------------------------------------------------- identical recordings

def test_identical_recordings_score_full() -> None:
    cfg = {"actions": [_act("name", value="alice"), _act("email", value="x@y")]}
    report = rd.diff_recordings(cfg, cfg)
    assert report.overall_score == pytest.approx(1.0)
    assert report.counts.get("unchanged") == 2
    assert report.counts.get("drifted", 0) == 0
    assert report.counts.get("changed", 0) == 0
    assert report.counts.get("added", 0) == 0
    assert report.counts.get("removed", 0) == 0


# --------------------------------------------------------------------------- drift

def test_drifted_when_fingerprint_weakens_but_kind_value_same() -> None:
    """Same identity (role + accessible_name + frame_chain) but the
    nested ``attributes.placeholder`` text changed \u2014 should drift."""
    old = {"actions": [_act("name", value="alice", placeholder="Your name", label="Name")]}
    new = {"actions": [_act("name", value="alice", placeholder="(removed)", label="Name")]}
    report = rd.diff_recordings(old, new, score_threshold=0.95)
    assert report.counts.get("drifted") == 1
    assert report.counts.get("changed", 0) == 0
    diff = report.actions[0]
    assert diff.status == "drifted"
    assert diff.score is not None and diff.score < 1.0


def test_frame_chain_is_read_from_action_level_not_fingerprint() -> None:
    """Regression: identity must include the action-level frame_chain.

    Two actions in different iframes that have the same role +
    accessible name MUST NOT collapse into the same identity bucket
    \u2014 doing so would let cross-iframe drift slip past the diff
    silently.
    """
    old = {"actions": [
        _act("name", frame_chain=("top", "#main")),
        _act("name", frame_chain=("top", "#popup")),
    ]}
    new = {"actions": [
        _act("name", frame_chain=("top", "#main")),
        _act("name", frame_chain=("top", "#popup")),
    ]}
    report = rd.diff_recordings(old, new)
    # All four actions match cleanly because frame_chain is read from
    # the action level, not the (always-absent) fingerprint sub-key.
    assert report.counts.get("unchanged") == 2
    assert report.counts.get("added", 0) == 0
    assert report.counts.get("removed", 0) == 0


def test_frame_chain_change_lowers_similarity_score() -> None:
    """A field that moved between iframes should not score 1.0."""
    old = {"actions": [_act("name", frame_chain=("top",))]}
    new = {"actions": [_act("name", frame_chain=("top", "#popup"))]}
    report = rd.diff_recordings(old, new)
    # Identity is now different (frame_chain differs), so the action
    # is reported as added/removed rather than drifted \u2014 either way
    # the user gets a clear signal something moved.
    assert report.counts.get("added", 0) + report.counts.get("removed", 0) >= 1


def test_attributes_change_marks_drift() -> None:
    """Change to ``fingerprint.attributes.id`` must lower the score."""
    old = {"actions": [_act("name")]}
    new_action = _act("name")
    new_action["fingerprint"]["attributes"]["id"] = "different_id"
    report = rd.diff_recordings(old, {"actions": [new_action]}, score_threshold=0.95)
    # Either drift (score below threshold) or surfaced via field_changes.
    assert report.counts.get("drifted", 0) + report.counts.get("changed", 0) >= 1


# --------------------------------------------------------------------------- value/kind change

def test_value_change_marks_action_as_changed() -> None:
    old = {"actions": [_act("name", value="alice")]}
    new = {"actions": [_act("name", value="bob")]}
    report = rd.diff_recordings(old, new)
    assert report.counts.get("changed") == 1
    diff = report.actions[0]
    assert "value" in diff.field_changes
    assert diff.field_changes["value"] == ("alice", "bob")


def test_kind_change_marks_action_as_changed() -> None:
    old = {"actions": [_act("agree", kind="check")]}
    new = {"actions": [_act("agree", kind="click")]}
    report = rd.diff_recordings(old, new)
    assert report.counts.get("changed") == 1
    assert "kind" in report.actions[0].field_changes


# --------------------------------------------------------------------------- add/remove

def test_added_field_appears_in_report() -> None:
    old = {"actions": [_act("name")]}
    new = {"actions": [_act("name"), _act("phone")]}
    report = rd.diff_recordings(old, new)
    assert report.counts.get("added") == 1
    added = next(d for d in report.actions if d.status == "added")
    assert added.index_old is None
    assert added.index_new == 1
    assert "phone" in added.summary


def test_removed_field_appears_in_report() -> None:
    old = {"actions": [_act("name"), _act("legacy_field")]}
    new = {"actions": [_act("name")]}
    report = rd.diff_recordings(old, new)
    assert report.counts.get("removed") == 1
    removed = next(d for d in report.actions if d.status == "removed")
    assert removed.index_new is None
    assert "legacy_field" in removed.summary


def test_overall_score_penalised_by_unmatched_actions() -> None:
    # Old has 4 actions, new has 4 — but the second pair is replaced.
    old = {"actions": [_act(f"f{i}") for i in range(4)]}
    new = {"actions": [_act("f0"), _act("REPLACEMENT"), _act("f2"), _act("f3")]}
    report = rd.diff_recordings(old, new)
    assert report.counts.get("added") == 1
    assert report.counts.get("removed") == 1
    # Three pairs match perfectly, but the unmatched-pair penalty cuts
    # the overall score below 1.0.
    assert report.overall_score < 1.0
    assert report.overall_score > 0.5


# --------------------------------------------------------------------------- identity matching

def test_identity_match_is_order_independent() -> None:
    """Swapping two fields whose identities differ shouldn't cascade."""
    old = {"actions": [_act("a"), _act("b"), _act("c")]}
    new = {"actions": [_act("c"), _act("a"), _act("b")]}
    report = rd.diff_recordings(old, new)
    assert report.counts.get("unchanged") == 3
    assert report.counts.get("added", 0) == 0
    assert report.counts.get("removed", 0) == 0


# --------------------------------------------------------------------------- file IO

def test_load_recording_round_trip(tmp_path: Path) -> None:
    cfg = {"actions": [_act("name", value="alice")]}
    path = tmp_path / "rec.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    loaded = rd.load_recording(path)
    assert loaded == cfg


def test_load_recording_handles_bom_and_trailing_whitespace(tmp_path: Path) -> None:
    """``encoding='utf-8-sig'`` should strip the BOM if present."""
    cfg = {"actions": [_act("name")]}
    path = tmp_path / "rec_bom.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(cfg).encode("utf-8") + b"\n  \n")
    loaded = rd.load_recording(path)
    assert loaded == cfg


def test_diff_recordings_accepts_paths(tmp_path: Path) -> None:
    cfg = {"actions": [_act("name", value="alice")]}
    p1 = tmp_path / "a.json"
    p2 = tmp_path / "b.json"
    p1.write_text(json.dumps(cfg), encoding="utf-8")
    p2.write_text(json.dumps(cfg), encoding="utf-8")
    report = rd.diff_recordings(p1, p2)
    assert report.overall_score == pytest.approx(1.0)


# --------------------------------------------------------------------------- formatting

def test_format_text_contains_overall_summary() -> None:
    cfg = {"actions": [_act("a")]}
    report = rd.diff_recordings(cfg, cfg)
    text = report.format_text()
    assert "Overall similarity:" in text
    assert "unchanged=1" in text


def test_to_dict_is_json_serialisable() -> None:
    old = {"actions": [_act("a", value="x")]}
    new = {"actions": [_act("a", value="y")]}
    report = rd.diff_recordings(old, new)
    blob = json.dumps(report.to_dict())
    parsed = json.loads(blob)
    assert parsed["counts"]["changed"] == 1
    # ``field_changes`` round-trips through JSON cleanly (tuples → lists).
    assert "actions" in parsed
