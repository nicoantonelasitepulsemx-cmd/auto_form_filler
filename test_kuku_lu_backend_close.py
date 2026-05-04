"""Regression tests for `_RequestsBackend.aclose`.

Devin Review BUG #3182701817: the base `_Backend.aclose` is a no-op.
Without overriding it on `_RequestsBackend`, every explicit
``await k._backend.aclose()`` in the bulk-mint codepath silently
leaked the underlying ``requests.Session`` and its connection pool
until the GC eventually reclaimed it.

These tests assert that calling ``aclose`` on a `_RequestsBackend`
actually closes the session.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from kuku_lu import _RequestsBackend  # noqa: E402


@pytest.mark.asyncio
async def test_requests_backend_aclose_closes_session(monkeypatch):
    """``aclose`` must call ``self._session.close``."""
    backend = _RequestsBackend()

    closed = {"count": 0}
    real_close = backend._session.close

    def fake_close():
        closed["count"] += 1
        real_close()

    monkeypatch.setattr(backend._session, "close", fake_close)

    await backend.aclose()
    assert closed["count"] == 1, (
        "aclose() did not close the underlying requests.Session — "
        "fix #3182701817 regressed."
    )


@pytest.mark.asyncio
async def test_requests_backend_aclose_idempotent_and_silent_on_error(monkeypatch):
    """``aclose`` must swallow exceptions from ``Session.close`` so a
    failure during one row doesn't bubble up and abort the rest of a
    bulk-mint run."""
    backend = _RequestsBackend()

    def boom():
        raise RuntimeError("simulated session close failure")

    monkeypatch.setattr(backend._session, "close", boom)

    await backend.aclose()
    await backend.aclose()


@pytest.mark.asyncio
async def test_requests_backend_aclose_overrides_base_noop():
    """Sanity: the override exists on the subclass — not just inherited
    from the base class as a no-op."""
    assert (
        _RequestsBackend.aclose is not _RequestsBackend.__mro__[1].aclose
    ), "_RequestsBackend.aclose still points to the base no-op"


if __name__ == "__main__":
    asyncio.run(test_requests_backend_aclose_overrides_base_noop())
    print("ok")
