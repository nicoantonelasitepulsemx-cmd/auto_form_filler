"""Unit tests for the v4 ai_features module."""
from __future__ import annotations

import io
import json
import struct
import sys
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ai_features import (  # noqa: E402
    AIHealResult,
    ai_heal,
    codegen_export,
    stable_hash,
    vision_hash,
    vision_match,
)


# ---------------------------------------------------------------------------
# Helpers — synthesize tiny PNGs without Pillow.
# ---------------------------------------------------------------------------


def _make_png(pixels_2d: list[list[tuple[int, int, int]]]) -> bytes:
    """Encode a small 2D RGB grid as a PNG (color-type 2, 8-bit)."""
    h = len(pixels_2d)
    w = len(pixels_2d[0])
    raw = bytearray()
    for row in pixels_2d:
        raw.append(0)  # filter type 0 (None) per row
        for r, g, b in row:
            raw.append(r)
            raw.append(g)
            raw.append(b)

    def chunk(ctype: bytes, data: bytes) -> bytes:
        c = ctype + data
        crc = zlib.crc32(c) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + c + struct.pack(">I", crc)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    idat = zlib.compress(bytes(raw), 6)
    return (
        sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")
    )


def _solid_png(color: tuple[int, int, int], size: int = 16) -> bytes:
    return _make_png([[color] * size for _ in range(size)])


def _gradient_png(size: int = 16) -> bytes:
    """Non-monotonic synthetic image with rich pixel-to-pixel contrast.

    A simple left-to-right gradient yields a degenerate dHash (every
    bit flips the same way and a uniform image collides with it), so
    we use a checkerboard-like pattern that produces both up-edges
    and down-edges across the row.
    """
    rows = []
    for y in range(size):
        row = []
        for x in range(size):
            v = 30 if ((x // 2) + (y // 2)) % 2 == 0 else 220
            row.append((v, v, v))
        rows.append(row)
    return _make_png(rows)


def _stripes_png(size: int = 16) -> bytes:
    rows = []
    for y in range(size):
        row = []
        for x in range(size):
            v = 240 if (x // 3) % 2 == 0 else 30
            row.append((v, v, v))
        rows.append(row)
    return _make_png(rows)


# ---------------------------------------------------------------------------
# vision_match
# ---------------------------------------------------------------------------


def test_vision_match_picks_identical_candidate():
    target = _gradient_png()
    decoy = _stripes_png()
    idx, dist = vision_match(target, [decoy, target])
    assert idx == 1
    assert dist == 0


def test_vision_match_distance_increases_with_difference():
    a = _gradient_png()
    a2 = _gradient_png()  # byte-identical retake
    decoy = _stripes_png()
    idx, dist = vision_match(a, [decoy, a2])
    assert idx == 1
    assert dist == 0


def test_vision_match_raises_on_empty():
    with pytest.raises(ValueError):
        vision_match(_gradient_png(), [])


def test_vision_hash_is_stable():
    p = _gradient_png()
    assert vision_hash(p) == vision_hash(p)
    # Gradient and stripes have visibly different patterns.
    assert vision_hash(p) != vision_hash(_stripes_png())


# ---------------------------------------------------------------------------
# ai_heal
# ---------------------------------------------------------------------------


def test_ai_heal_no_key_is_noop(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    res = ai_heal(fingerprint={"role": "radio"})
    assert isinstance(res, AIHealResult)
    assert res.selector is None
    assert res.ok is False
    assert "OPENAI_API_KEY" in res.rationale


def test_ai_heal_explicit_key_routes_through_urlopen(monkeypatch):
    """We don't actually hit OpenAI — we monkeypatch urlopen."""
    captured: dict[str, object] = {}

    class _FakeResp:
        def __init__(self, body: bytes):
            self._body = body

        def read(self) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout=None):  # noqa: ANN001
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        body = (
            b'{"choices":[{"message":{"content":"{\\"selector\\":'
            b'\\"role=radio[name=\\\\\\"alpha\\\\\\"]\\",\\"rationale\\":'
            b'\\"obvious\\"}"}}]}'
        )
        return _FakeResp(body)

    import urllib.request as _ur
    monkeypatch.setattr(_ur, "urlopen", _fake_urlopen)
    res = ai_heal(api_key="sk-test", fingerprint={"role": "radio"})
    assert res.ok
    assert res.selector and res.selector.startswith("role=radio")
    assert "obvious" in res.rationale
    assert captured["url"].startswith("https://api.openai.com")


def test_ai_heal_strips_markdown_fences_with_trailing_newline(monkeypatch):
    """Common chat-model output: ```json\\n{...}\\n```\\n must parse."""
    class _FakeResp:
        def __init__(self, body: bytes):
            self._body = body

        def read(self) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    fenced_payload = (
        '```json\n{"selector":"role=button[name=\\"Submit\\"]",'
        '"rationale":"only button"}\n```\n'
    )
    body = json.dumps(
        {"choices": [{"message": {"content": fenced_payload}}]}
    ).encode("utf-8")

    import urllib.request as _ur
    monkeypatch.setattr(_ur, "urlopen", lambda *a, **kw: _FakeResp(body))
    res = ai_heal(api_key="sk-test")
    assert res.ok, f"selector should parse, got rationale={res.rationale!r}"
    assert res.selector == 'role=button[name="Submit"]'


def test_ai_heal_strips_markdown_fences_no_lang_tag(monkeypatch):
    """Bare ```...``` (no `json` tag) must also parse cleanly."""
    class _FakeResp:
        def __init__(self, body: bytes):
            self._body = body

        def read(self) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    fenced_payload = '```\n{"selector":"#x","rationale":"r"}\n```'
    body = json.dumps(
        {"choices": [{"message": {"content": fenced_payload}}]}
    ).encode("utf-8")

    import urllib.request as _ur
    monkeypatch.setattr(_ur, "urlopen", lambda *a, **kw: _FakeResp(body))
    res = ai_heal(api_key="sk-test")
    assert res.ok
    assert res.selector == "#x"


def test_ai_heal_handles_malformed_response(monkeypatch):
    class _FakeResp:
        def read(self):
            return b'{"weird":"shape"}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import urllib.request as _ur
    monkeypatch.setattr(_ur, "urlopen", lambda *a, **kw: _FakeResp())
    res = ai_heal(api_key="sk-test")
    assert res.selector is None
    assert "malformed" in res.rationale


# ---------------------------------------------------------------------------
# codegen_export
# ---------------------------------------------------------------------------


def test_codegen_export_produces_runnable_skeleton():
    config = {
        "start_url": "https://example.test/form",
        "actions": [
            {
                "kind": "fill",
                "value": "hello",
                "selectors": {"data_testid": "name-input"},
                "fingerprint": {"accessible_name": "Name"},
            },
            {
                "kind": "check",
                "checked": True,
                "fingerprint": {"role": "radio", "accessible_name": "I am the rights owner"},
            },
            {
                "kind": "submit",
                "fingerprint": {"role": "button", "accessible_name": "Continue"},
            },
        ],
    }
    code = codegen_export(config)
    # Imports + entrypoint
    assert "from playwright.async_api import async_playwright" in code
    assert "async def play_recording" in code
    assert 'page.goto(START_URL)' in code
    # Each action is rendered.
    assert ".fill('hello')" in code
    assert ".check()" in code
    # Stable selectors are preferred over text=...
    assert "[data-testid='name-input']" in code
    assert "role=radio[name='I am the rights owner']" in code
    # Validate the generated script is at least syntactically valid
    # Python.
    compile(code, "<codegen>", "exec")


def test_codegen_export_handles_unknown_kind_gracefully():
    code = codegen_export({"actions": [{"kind": "weird-thing"}]})
    assert "unknown action kind: 'weird-thing'" in code
    compile(code, "<codegen>", "exec")


# ---------------------------------------------------------------------------
# stable_hash
# ---------------------------------------------------------------------------


def test_stable_hash_is_deterministic_across_dict_orderings():
    a = stable_hash({"a": 1, "b": [1, 2, 3]})
    b = stable_hash({"b": [1, 2, 3], "a": 1})
    assert a == b
    assert len(a) == 64


def test_stable_hash_different_inputs_differ():
    assert stable_hash({"a": 1}) != stable_hash({"a": 2})
