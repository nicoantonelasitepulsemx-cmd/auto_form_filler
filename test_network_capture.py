"""Unit tests for ``network_capture``.

The blocker predicate is pure — those tests don't need Playwright.
For the ``attach_blocker`` glue we use a tiny mock context with
``route``/``unroute`` async methods so we don't pull in Playwright.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

import network_capture as nc


# --------------------------------------------------------------------------- compile_blocker / should_block

def test_should_block_matches_user_pattern() -> None:
    b = nc.compile_blocker(block=[r"foo\.com"])
    assert nc.should_block(b, "https://foo.com/x")
    assert not nc.should_block(b, "https://bar.com/x")


def test_should_block_uses_bundles() -> None:
    b = nc.compile_blocker(bundles=[nc.BLOCK_ADS])
    assert nc.should_block(b, "https://ads.doubleclick.net/track")
    assert not nc.should_block(b, "https://api.example.com/data")


def test_should_block_combines_user_patterns_and_bundles() -> None:
    b = nc.compile_blocker(block=[r"facebook\.com/tr"], bundles=[nc.BLOCK_ANALYTICS])
    assert nc.should_block(b, "https://www.google-analytics.com/collect")
    assert nc.should_block(b, "https://www.facebook.com/tr/123")
    assert not nc.should_block(b, "https://www.example.com/")


def test_should_block_case_insensitive_default() -> None:
    b = nc.compile_blocker(block=[r"DoubleClick\.NeT"])
    assert nc.should_block(b, "https://doubleclick.net/x")


def test_should_block_case_sensitive_when_requested() -> None:
    b = nc.compile_blocker(block=[r"foo"], case_insensitive=False)
    assert nc.should_block(b, "https://foo.com")
    assert not nc.should_block(b, "https://FOO.com")


def test_should_block_updates_stats_per_call() -> None:
    b = nc.compile_blocker(bundles=[nc.BLOCK_ADS])
    nc.should_block(b, "https://ads.doubleclick.net/x")
    nc.should_block(b, "https://api.example.com/y")
    nc.should_block(b, "https://ads.doubleclick.net/z")
    assert b.stats.blocked == 2
    assert b.stats.allowed == 1
    assert b.stats.blocked_hosts.get("ads.doubleclick.net") == 2


def test_block_heavy_includes_ads_analytics_fonts() -> None:
    b = nc.compile_blocker(bundles=[nc.BLOCK_HEAVY])
    assert nc.should_block(b, "https://ads.doubleclick.net/")
    assert nc.should_block(b, "https://www.google-analytics.com/")
    assert nc.should_block(b, "https://fonts.googleapis.com/css")


def test_block_media_catches_image_extensions() -> None:
    b = nc.compile_blocker(bundles=[nc.BLOCK_MEDIA])
    assert nc.should_block(b, "https://cdn.example.com/img.png")
    assert nc.should_block(b, "https://cdn.example.com/img.jpg?v=1")
    assert nc.should_block(b, "https://cdn.example.com/font.woff2")
    assert not nc.should_block(b, "https://api.example.com/data.json")


# --------------------------------------------------------------------------- Playwright glue

class _FakeRequest:
    def __init__(self, url: str) -> None:
        self.url = url


class _FakeRoute:
    def __init__(self) -> None:
        self.aborted = False
        self.continued = False

    async def abort(self) -> None:
        self.aborted = True

    async def continue_(self) -> None:
        self.continued = True


class _FakeContext:
    def __init__(self) -> None:
        self.handlers: list = []

    async def route(self, pattern: str, handler) -> None:
        self.handlers.append((pattern, handler))

    async def unroute(self, pattern: str, handler) -> None:
        self.handlers.remove((pattern, handler))


@pytest.mark.asyncio
async def test_attach_blocker_aborts_blocked_requests() -> None:
    ctx = _FakeContext()
    blocker = nc.compile_blocker(block=[r"ads\.com"])
    detacher = await nc.attach_blocker(ctx, blocker)

    pattern, handler = ctx.handlers[0]
    assert pattern == "**/*"

    blocked_route = _FakeRoute()
    await handler(blocked_route, _FakeRequest("https://ads.com/x"))
    assert blocked_route.aborted is True
    assert blocked_route.continued is False

    allowed_route = _FakeRoute()
    await handler(allowed_route, _FakeRequest("https://api.example.com/x"))
    assert allowed_route.continued is True
    assert allowed_route.aborted is False

    await detacher()
    assert ctx.handlers == []


@pytest.mark.asyncio
async def test_attach_blocker_safe_when_request_url_throws() -> None:
    """Playwright tearing the context down mid-flight must not crash us."""
    ctx = _FakeContext()
    blocker = nc.compile_blocker(block=[r"never_match"])
    detacher = await nc.attach_blocker(ctx, blocker)
    _, handler = ctx.handlers[0]

    class BadRequest:
        @property
        def url(self):  # type: ignore[no-redef]
            raise RuntimeError("context closed")

    route = _FakeRoute()
    await handler(route, BadRequest())
    assert route.continued is True

    await detacher()


@pytest.mark.asyncio
async def test_attach_har_replay_calls_route_from_har(tmp_path) -> None:
    """Replay mode delegates to ``context.route_from_har``."""

    class Ctx:
        def __init__(self) -> None:
            self.calls: list = []

        async def route_from_har(self, path: str, *, update: bool, not_found: str) -> None:
            self.calls.append({"path": path, "update": update, "not_found": not_found})

    ctx = Ctx()
    har = tmp_path / "session.har"
    handle = await nc.attach_har(ctx, har, mode="replay", update=False)
    assert handle.mode == "replay"
    assert handle.path == har
    assert ctx.calls == [{"path": str(har), "update": False, "not_found": "fallback"}]


@pytest.mark.asyncio
async def test_attach_har_unknown_mode_raises(tmp_path) -> None:
    class Ctx:
        async def route_from_har(self, *_a, **_kw) -> None:
            pass

    with pytest.raises(ValueError):
        await nc.attach_har(Ctx(), tmp_path / "x.har", mode="bogus")
