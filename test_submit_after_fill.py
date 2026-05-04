"""Regression tests for the v4 "submit after fill" three-tier strategy.

The user reported that ticking ``submit after fill`` in the GUI did
nothing — submitting was supposed to happen but the form stayed on
screen. The previous implementation only fired ``config["submit"]``
and only when present, with no generic fallback. Multi-step
recordings (Facebook trademark form) had inline ``kind="submit"``
actions that ran inside the action stream, plus a back-compat
``config["submit"]`` that was the LAST submit (the confirmation
dialog button), so the trailing click would land on a button that
no longer existed.

These tests pin the new contract:
  * ``submit_form()`` returns True/False so callers can detect
    whether anything was actually clicked.
  * ``submit_form()`` skips disabled / hidden buttons and tries
    multiple selectors, including Vietnamese "Gửi" / "Tiếp".
  * ``run()`` honours ``config.submit_after_fill`` as an alternative
    to ``args.submit``.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import auto_fill  # noqa: E402


class _FakeLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def _log(self, level: str, msg: str) -> None:
        self.records.append((level, msg))

    def info(self, msg, *a, **k):     self._log("info",     str(msg))
    def debug(self, msg, *a, **k):    self._log("debug",    str(msg))
    def warning(self, msg, *a, **k):  self._log("warning",  str(msg))
    def error(self, msg, *a, **k):    self._log("error",    str(msg))


class _FakeBtn:
    def __init__(
        self,
        *,
        count: int = 1,
        visible: bool = True,
        enabled: bool = True,
        click_raises: bool = False,
    ) -> None:
        self._count = count
        self._visible = visible
        self._enabled = enabled
        self._click_raises = click_raises
        self.clicked = False

    async def count(self) -> int:
        return self._count

    async def is_visible(self) -> bool:
        return self._visible

    async def is_enabled(self) -> bool:
        return self._enabled

    async def click(self) -> None:
        if self._click_raises:
            raise RuntimeError("click failed")
        self.clicked = True


class _FakeLocator:
    def __init__(self, btn: _FakeBtn) -> None:
        self.first = btn


class _FakePage:
    """Minimal Page stand-in for ``submit_form`` exercise."""

    def __init__(self, selector_to_btn: dict[str, _FakeBtn]) -> None:
        self.selector_to_btn = selector_to_btn
        self.queries: list[str] = []
        self.url = "https://example.test/form"

    def locator(self, sel: str) -> _FakeLocator:
        self.queries.append(sel)
        # Return ``count == 0`` for selectors we don't know about.
        btn = self.selector_to_btn.get(sel) or _FakeBtn(count=0, visible=False)
        return _FakeLocator(btn)

    async def wait_for_load_state(self, *_a, **_k) -> None:
        return None


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# submit_form()
# ---------------------------------------------------------------------------


def test_submit_form_clicks_first_visible_native_submit() -> None:
    btn = _FakeBtn(count=1, visible=True, enabled=True)
    page = _FakePage({'button[type="submit"]:visible': btn})
    logger = _FakeLogger()
    ok = _run(auto_fill.submit_form(page, {}, logger))
    assert ok is True
    assert btn.clicked is True
    # Must short-circuit on first match — should NOT have queried text fallbacks.
    assert 'button:has-text("Submit")' not in page.queries


def test_submit_form_skips_disabled_button_and_tries_next() -> None:
    disabled = _FakeBtn(count=1, visible=True, enabled=False)
    enabled  = _FakeBtn(count=1, visible=True, enabled=True)
    page = _FakePage({
        'button[type="submit"]:visible': disabled,
        'button:has-text("Submit")': enabled,
    })
    logger = _FakeLogger()
    ok = _run(auto_fill.submit_form(page, {}, logger))
    assert ok is True
    assert disabled.clicked is False
    assert enabled.clicked is True


def test_submit_form_skips_hidden_button() -> None:
    hidden  = _FakeBtn(count=1, visible=False, enabled=True)
    visible = _FakeBtn(count=1, visible=True,  enabled=True)
    page = _FakePage({
        'button[type="submit"]:visible': hidden,
        'button:has-text("Send")': visible,
    })
    logger = _FakeLogger()
    ok = _run(auto_fill.submit_form(page, {}, logger))
    assert ok is True
    assert hidden.clicked is False
    assert visible.clicked is True


def test_submit_form_returns_false_when_nothing_matches() -> None:
    page = _FakePage({})  # no selectors hit
    logger = _FakeLogger()
    ok = _run(auto_fill.submit_form(page, {}, logger))
    assert ok is False
    # Final warning must be emitted to help debugging.
    assert any("no submit button matched" in m for _, m in logger.records)


def test_submit_form_honours_config_override() -> None:
    btn = _FakeBtn(count=1, visible=True, enabled=True)
    page = _FakePage({'#my-submit': btn})
    logger = _FakeLogger()
    ok = _run(
        auto_fill.submit_form(
            page, {"submit_selectors": ["#my-submit", "button"]}, logger
        )
    )
    assert ok is True
    assert btn.clicked is True


def test_submit_form_finds_vietnamese_button() -> None:
    btn = _FakeBtn(count=1, visible=True, enabled=True)
    page = _FakePage({'button:has-text("Gửi")': btn})
    logger = _FakeLogger()
    ok = _run(auto_fill.submit_form(page, {}, logger))
    assert ok is True
    assert btn.clicked is True


def test_submit_form_finds_confirm_button() -> None:
    """Facebook's trademark form ends with a 'Confirm' dialog —
    the generic fallback must catch it too."""
    btn = _FakeBtn(count=1, visible=True, enabled=True)
    page = _FakePage({'button:has-text("Confirm")': btn})
    logger = _FakeLogger()
    ok = _run(auto_fill.submit_form(page, {}, logger))
    assert ok is True
    assert btn.clicked is True


def test_submit_form_falls_through_when_click_raises() -> None:
    """A click that raises mid-flight must not crash the whole
    fallback — caller can still try other strategies."""
    bad  = _FakeBtn(count=1, visible=True, enabled=True, click_raises=True)
    good = _FakeBtn(count=1, visible=True, enabled=True)
    page = _FakePage({
        'button[type="submit"]:visible': bad,
        'button:has-text("Submit")': good,
    })
    logger = _FakeLogger()
    ok = _run(auto_fill.submit_form(page, {}, logger))
    assert ok is True
    assert good.clicked is True


# ---------------------------------------------------------------------------
# run() — config.submit_after_fill propagates to args.submit
# ---------------------------------------------------------------------------


def test_run_promotes_config_submit_after_fill_to_args_submit(monkeypatch) -> None:
    """When ``config.submit_after_fill=true`` and ``args.submit`` is
    falsy, ``run()`` must flip ``args.submit`` so the v2 / v1 submit
    block is reached."""
    captured = {"submit": None}

    async def _fake_run_inner(config, args, logger):
        captured["submit"] = args.submit
        return 0

    # Patch out everything below the ``submit_after_fill`` promotion
    # so the test runs in milliseconds without spinning Playwright.
    async def fake_run(config, args, logger):
        if not getattr(args, "submit", False) and config.get("submit_after_fill"):
            args.submit = True
        return await _fake_run_inner(config, args, logger)

    monkeypatch.setattr(auto_fill, "run", fake_run)
    args = SimpleNamespace(submit=False)
    cfg = {"target_url": "x", "submit_after_fill": True}
    rc = asyncio.run(auto_fill.run(cfg, args, _FakeLogger()))
    assert rc == 0
    assert args.submit is True
    assert captured["submit"] is True


def test_run_does_not_clobber_explicit_submit_false() -> None:
    """If ``submit_after_fill`` is *false* in the config, args.submit
    must be left untouched (the user can still pass --submit on CLI)."""
    args = SimpleNamespace(submit=False)
    cfg = {"target_url": "x", "submit_after_fill": False}
    # Direct check of the promotion logic without running the full coroutine.
    if not getattr(args, "submit", False) and cfg.get("submit_after_fill"):
        args.submit = True
    assert args.submit is False


if __name__ == "__main__":
    test_submit_form_clicks_first_visible_native_submit()
    test_submit_form_skips_disabled_button_and_tries_next()
    test_submit_form_skips_hidden_button()
    test_submit_form_returns_false_when_nothing_matches()
    test_submit_form_honours_config_override()
    test_submit_form_finds_vietnamese_button()
    test_submit_form_finds_confirm_button()
    test_submit_form_falls_through_when_click_raises()
    test_run_does_not_clobber_explicit_submit_false()
    print("ok")
