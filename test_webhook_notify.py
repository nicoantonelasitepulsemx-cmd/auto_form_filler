"""Unit tests for ``webhook_notify``.

We monkey-patch ``_post`` / ``_post_form`` so no actual HTTP fires;
the tests assert on the URL and payload shape we hand each provider.
"""
from __future__ import annotations

from typing import Any

import pytest

import webhook_notify as wn


# --------------------------------------------------------------------------- helpers

class _Recorder:
    """Captures every HTTP call so the tests can inspect them."""
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def post_json(self, url: str, payload: dict, **kw: Any) -> wn.SendResult:
        self.calls.append({"kind": "json", "url": url, "payload": payload, "kw": kw})
        return wn.SendResult(ok=True, status=204)

    def post_form(self, url: str, fields: dict, **kw: Any) -> wn.SendResult:
        self.calls.append({"kind": "form", "url": url, "fields": fields, "kw": kw})
        return wn.SendResult(ok=True, status=200)


@pytest.fixture
def rec(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    r = _Recorder()
    monkeypatch.setattr(wn, "_post", r.post_json)
    monkeypatch.setattr(wn, "_post_form", r.post_form)
    return r


# --------------------------------------------------------------------------- discord

def test_discord_send_uses_content_field(rec: _Recorder) -> None:
    n = wn.Notifier.discord("https://discord.com/api/webhooks/1/abc")
    res = n.send("hello world")
    assert res.ok
    assert len(rec.calls) == 1
    call = rec.calls[0]
    assert call["kind"] == "json"
    assert call["url"] == "https://discord.com/api/webhooks/1/abc"
    assert call["payload"] == {"content": "hello world"}


def test_discord_send_truncates_at_2000_chars(rec: _Recorder) -> None:
    """Discord rejects > 2000 chars on ``content`` — we cap at 1990 + ellipsis."""
    n = wn.Notifier.discord("https://x")
    n.send("x" * 5000)
    payload = rec.calls[0]["payload"]
    assert len(payload["content"]) <= 1990
    assert payload["content"].endswith("…")


def test_discord_send_summary_emits_embed(rec: _Recorder) -> None:
    n = wn.Notifier.discord("https://x")
    n.send_summary(
        title="Nightly batch",
        ok_count=198, fail_count=2, warn_count=0,
        elapsed_s=45.6,
        link="https://logs/run-1",
        details="2 accounts failed CAPTCHA",
    )
    payload = rec.calls[0]["payload"]
    assert "embeds" in payload
    embed = payload["embeds"][0]
    assert embed["title"] == "Nightly batch"
    # Status="warning" because ok>0 and fail>0.
    assert embed["color"] == wn._DISCORD_COLOURS["warning"]
    field_names = [f["name"] for f in embed["fields"]]
    assert any("ok" in n for n in field_names)
    assert any("fail" in n for n in field_names)
    assert embed["url"] == "https://logs/run-1"


def test_discord_summary_red_when_all_failed(rec: _Recorder) -> None:
    n = wn.Notifier.discord("https://x")
    n.send_summary(title="Bad night", ok_count=0, fail_count=10, warn_count=0)
    embed = rec.calls[0]["payload"]["embeds"][0]
    assert embed["color"] == wn._DISCORD_COLOURS["error"]


def test_discord_summary_green_when_all_ok(rec: _Recorder) -> None:
    n = wn.Notifier.discord("https://x")
    n.send_summary(title="Smooth", ok_count=50, fail_count=0)
    embed = rec.calls[0]["payload"]["embeds"][0]
    assert embed["color"] == wn._DISCORD_COLOURS["ok"]


def test_discord_mention_prepended_to_text(rec: _Recorder) -> None:
    n = wn.Notifier.discord("https://x", mention="<@&12345>")
    n.send("alert")
    assert rec.calls[0]["payload"]["content"].startswith("<@&12345>")


# --------------------------------------------------------------------------- telegram

def test_telegram_send_uses_form_post(rec: _Recorder) -> None:
    n = wn.Notifier.telegram("BOT-TOKEN", "987654321")
    n.send("hi")
    assert len(rec.calls) == 1
    call = rec.calls[0]
    assert call["kind"] == "form"
    assert call["url"] == "https://api.telegram.org/botBOT-TOKEN/sendMessage"
    assert call["fields"]["chat_id"] == "987654321"
    assert call["fields"]["text"] == "hi"
    assert call["fields"]["parse_mode"] == "HTML"


def test_telegram_summary_renders_plain_text(rec: _Recorder) -> None:
    n = wn.Notifier.telegram("TOK", "1")
    n.send_summary(title="Run", ok_count=2, fail_count=1, warn_count=0, elapsed_s=3.0,
                   details="d1\nd2")
    body = rec.calls[0]["fields"]["text"]
    assert "**Run**" in body
    assert "✓ 2" in body
    assert "✗ 1" in body
    assert "d1" in body


def test_telegram_send_truncates_long_text(rec: _Recorder) -> None:
    n = wn.Notifier.telegram("TOK", "1")
    n.send("z" * 10000)
    text = rec.calls[0]["fields"]["text"]
    assert len(text) <= 4090
    assert text.endswith("…")


# --------------------------------------------------------------------------- slack

def test_slack_send_uses_text_field(rec: _Recorder) -> None:
    n = wn.Notifier.slack("https://hooks.slack.com/services/T/B/X")
    n.send("ping")
    assert len(rec.calls) == 1
    call = rec.calls[0]
    assert call["payload"] == {"text": "ping"}


# --------------------------------------------------------------------------- broadcast

def test_broadcast_calls_all_notifiers(rec: _Recorder) -> None:
    a = wn.Notifier.discord("https://discord/A")
    b = wn.Notifier.slack("https://slack/B")
    results = wn.broadcast([a, b], "shared payload")
    assert len(results) == 2
    assert all(r.ok for r in results)
    assert any("discord/A" in c["url"] for c in rec.calls)
    assert any("slack/B" in c["url"] for c in rec.calls)


def test_broadcast_isolates_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """A throw inside one provider must not stop the others."""
    a = wn.Notifier.discord("https://A")
    b = wn.Notifier.slack("https://B")

    calls: list[str] = []

    def flaky(url: str, payload: dict, **_: Any) -> wn.SendResult:
        calls.append(url)
        if url == "https://A":
            raise RuntimeError("oh no")
        return wn.SendResult(ok=True, status=200)

    monkeypatch.setattr(wn, "_post", flaky)
    results = wn.broadcast([a, b], "x")
    assert len(results) == 2
    assert results[0].ok is False
    assert "RuntimeError" in results[0].error
    assert results[1].ok is True
    # B was still attempted despite A throwing.
    assert "https://B" in calls
