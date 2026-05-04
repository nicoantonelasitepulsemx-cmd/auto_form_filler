"""Unit tests for ``stealth_profile``.

Profile generation is pure — same name + salt → same profile. We
also exercise the JS init script with a fast hand-rolled JS parser
(string checks) since spinning up V8 in CI for a smoke test is
overkill.
"""
from __future__ import annotations

import json
import re

import pytest

import stealth_profile as sp


# --------------------------------------------------------------------------- determinism

def test_same_name_yields_same_profile() -> None:
    a = sp.generate_profile("alice")
    b = sp.generate_profile("alice")
    assert a == b


def test_different_names_yield_different_profiles_in_general() -> None:
    """Account name space is large enough that random pairs differ.

    We try ten unrelated names and require at least one differs from
    'alice' — guards against the pool being so small that everyone
    collides on the same fingerprint."""
    a = sp.generate_profile("alice")
    differences = sum(
        sp.generate_profile(n) != a
        for n in ("bob", "charlie", "dave", "eve", "frank", "grace", "harry", "isla", "joe", "kim")
    )
    assert differences >= 5


def test_salt_changes_profile() -> None:
    a = sp.generate_profile("alice", salt="v4")
    b = sp.generate_profile("alice", salt="experimental")
    assert a != b


def test_user_data_dir_round_trips() -> None:
    p = sp.generate_profile("alice", user_data_dir="/tmp/profiles/alice")
    assert p.user_data_dir == "/tmp/profiles/alice"
    # Re-generating without user_data_dir doesn't carry it over.
    q = sp.generate_profile("alice")
    assert q.user_data_dir is None


# --------------------------------------------------------------------------- profile shape

def test_profile_fields_are_plausible() -> None:
    p = sp.generate_profile("alice")
    assert "Mozilla/5.0" in p.user_agent
    assert "Chrome/" in p.user_agent
    assert p.platform in ("Win32", "MacIntel", "Linux x86_64")
    assert p.viewport[0] >= 1366
    assert p.viewport[1] >= 768
    # Locale + timezone are paired — never an obvious mismatch.
    assert "-" in p.locale
    assert "/" in p.timezone


def test_profile_hardware_is_plausible() -> None:
    p = sp.generate_profile("alice")
    assert p.hardware_concurrency in (4, 8, 12, 16)
    assert p.device_memory in (8, 16, 32)


def test_profile_to_dict_is_json_serialisable() -> None:
    p = sp.generate_profile("alice")
    blob = json.dumps(p.to_dict())
    parsed = json.loads(blob)
    # Viewport must round-trip as a list (JSON has no tuples).
    assert isinstance(parsed["viewport"], list)
    assert parsed["user_agent"] == p.user_agent


# --------------------------------------------------------------------------- init script

def test_build_init_script_strips_webdriver() -> None:
    p = sp.generate_profile("alice")
    js = sp.build_init_script(p)
    # Critical mask: navigator.webdriver must be undefined-able.
    assert "webdriver" in js
    assert "() => undefined" in js


def test_build_init_script_embeds_profile_values() -> None:
    p = sp.generate_profile("alice")
    js = sp.build_init_script(p)
    # JSON-encoded payload appears verbatim — search for the platform
    # and webgl renderer so we know they made it into the script.
    assert json.dumps(p.platform) in js
    assert p.webgl_renderer in js
    # Languages array must contain locale + base.
    assert json.dumps([p.locale, p.locale.split("-")[0]]) in js


def test_build_init_script_patches_webgl_constants() -> None:
    js = sp.build_init_script(sp.generate_profile("alice"))
    # 0x9245 = 37445 (UNMASKED_VENDOR), 0x9246 = 37446 (UNMASKED_RENDERER).
    assert "37445" in js
    assert "37446" in js


def test_build_init_script_patches_permissions_query() -> None:
    js = sp.build_init_script(sp.generate_profile("alice"))
    assert "Permissions.prototype.query" in js
    assert "notifications" in js


def test_build_init_script_provides_window_chrome_stub() -> None:
    js = sp.build_init_script(sp.generate_profile("alice"))
    assert "window.chrome" in js


# --------------------------------------------------------------------------- apply_profile / new_context options

class _FakeContext:
    def __init__(self) -> None:
        self.scripts: list = []

    async def add_init_script(self, js: str) -> None:
        self.scripts.append(js)


@pytest.mark.asyncio
async def test_apply_profile_registers_init_script() -> None:
    ctx = _FakeContext()
    p = sp.generate_profile("alice")
    await sp.apply_profile(ctx, p)
    assert len(ctx.scripts) == 1
    assert "webdriver" in ctx.scripts[0]


def test_profile_to_context_options_shape() -> None:
    p = sp.generate_profile("alice")
    opts = sp.profile_to_context_options(p)
    assert opts["user_agent"] == p.user_agent
    assert opts["viewport"]["width"] == p.viewport[0]
    assert opts["viewport"]["height"] == p.viewport[1]
    assert opts["locale"] == p.locale
    assert opts["timezone_id"] == p.timezone
    assert opts["is_mobile"] is False


def test_profile_to_context_options_does_not_include_user_data_dir() -> None:
    """user_data_dir is a launch-time concept, not a context kwarg."""
    p = sp.generate_profile("alice", user_data_dir="/tmp/x")
    opts = sp.profile_to_context_options(p)
    assert "user_data_dir" not in opts
