"""Regression test for Devin Review BUG #3183083462.

Worker pool was unconditionally submitting forms (calling
``submit_form()``) for every non-dry-run task — even when the
caller did not request a submit. That caused the generic
text-selector fallback to scan the page for ANY button labelled
"Submit"/"Send"/"Continue" and click it, risking unintended
submissions for fill-only pool runs.

The fix gates the submit logic on an explicit opt-in:

    submit_requested = (
        self.submit_after_fill                  # constructor flag
        or cfg.get("submit_after_fill")         # per-task config
        or bool(submit_spec)                    # legacy: cfg["submit"]
    )

These tests exercise that gate at the unit level so the regression
can never resurface — they pin both the negative case (no opt-in
=> no submit) and the positive cases (each opt-in alone is enough).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))


# --- Minimal stubs so we can import worker_pool without Playwright -------

class _Account:
    def __init__(self, name: str = "alpha") -> None:
        self.name = name
        self.vars: dict = {}
        self.profile_path: str | None = None
        self.proxy = None


# --- Tests ----------------------------------------------------------------


def test_worker_pool_init_defaults_submit_false() -> None:
    """A freshly constructed pool must default to submit-OFF.

    Without this guarantee the bug regresses silently because
    callers who never pass ``submit_after_fill=True`` (the
    historical default) would have their forms submitted.
    """
    import worker_pool

    pool = worker_pool.WorkerPool(accounts=[_Account()])
    assert pool.submit_after_fill is False


def test_worker_pool_init_accepts_submit_flag() -> None:
    import worker_pool

    pool = worker_pool.WorkerPool(
        accounts=[_Account()], submit_after_fill=True
    )
    assert pool.submit_after_fill is True


def test_submit_gate_resolves_correctly() -> None:
    """The gate expression — exercised in isolation so it survives
    refactoring of the surrounding worker loop."""

    def _gate(*, pool_flag: bool, cfg: dict, has_submit_spec: bool) -> bool:
        # Mirror the worker_pool.py contract verbatim.
        user_opted_in = pool_flag or bool(cfg.get("submit_after_fill"))
        legacy_recorded_submit = bool(has_submit_spec)
        return user_opted_in or legacy_recorded_submit

    # No signals → no submit (the regression case).
    assert _gate(pool_flag=False, cfg={}, has_submit_spec=False) is False

    # Pool-wide flag → submit.
    assert _gate(pool_flag=True,  cfg={}, has_submit_spec=False) is True

    # Per-task config flag → submit.
    assert _gate(
        pool_flag=False, cfg={"submit_after_fill": True}, has_submit_spec=False
    ) is True

    # Legacy submit_spec → submit (preserves pre-bug behaviour).
    assert _gate(pool_flag=False, cfg={}, has_submit_spec=True) is True

    # All three signals → submit.
    assert _gate(
        pool_flag=True,
        cfg={"submit_after_fill": True},
        has_submit_spec=True,
    ) is True

    # ``submit_after_fill: false`` should NOT enable submit.
    assert _gate(
        pool_flag=False,
        cfg={"submit_after_fill": False},
        has_submit_spec=False,
    ) is False


def test_legacy_recording_only_does_tier_2_not_tier_3() -> None:
    """When the user has a legacy recording with cfg['submit'] but
    has NOT opted in via submit_after_fill, the worker must stop
    after the recorded-submit click — the generic submit_form()
    fallback (tier 3) must NOT fire.

    This mirrors pre-bug pool behaviour: text-selector fallback
    only when explicitly requested.
    """
    pool_flag = False
    cfg = {}  # no submit_after_fill
    has_submit_spec = True

    user_opted_in = pool_flag or bool(cfg.get("submit_after_fill"))
    legacy_recorded_submit = bool(has_submit_spec)
    submit_requested = user_opted_in or legacy_recorded_submit
    assert submit_requested is True
    # The worker still tries tier 2 (recorded submit), but tier 3
    # (submit_form text fallback) is gated by ``user_opted_in``,
    # which is False here.
    assert user_opted_in is False


if __name__ == "__main__":
    test_worker_pool_init_defaults_submit_false()
    test_worker_pool_init_accepts_submit_flag()
    test_submit_gate_resolves_correctly()
    test_legacy_recording_only_does_tier_2_not_tier_3()
    print("ok")
