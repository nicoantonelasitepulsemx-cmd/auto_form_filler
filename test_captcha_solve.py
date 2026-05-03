"""Unit tests for ``captcha_solve``.

Network is fully monkey-patched — no real provider is contacted.
"""
from __future__ import annotations

import json

import pytest

import captcha_solve


# --------------------------------------------------------------------------- detect_provider

def test_detect_provider_none_when_no_env() -> None:
    assert captcha_solve.detect_provider(env={}) is None


def test_detect_provider_picks_first_available_by_default() -> None:
    env = {"ANTICAPTCHA_API_KEY": "abc"}
    assert captcha_solve.detect_provider(env=env) == ("anticaptcha", "abc")


def test_detect_provider_priority_2captcha_over_anticaptcha() -> None:
    """Built-in priority: 2captcha > anticaptcha > capsolver."""
    env = {
        "TWOCAPTCHA_API_KEY": "two",
        "ANTICAPTCHA_API_KEY": "anti",
        "CAPSOLVER_API_KEY": "cap",
    }
    assert captcha_solve.detect_provider(env=env) == ("2captcha", "two")


def test_detect_provider_preferred_order_overrides() -> None:
    env = {
        "TWOCAPTCHA_API_KEY": "two",
        "CAPSOLVER_API_KEY": "cap",
    }
    out = captcha_solve.detect_provider(env=env, preferred_order=("capsolver",))
    assert out == ("capsolver", "cap")


def test_detect_provider_unknown_preferred_name_falls_back() -> None:
    env = {"ANTICAPTCHA_API_KEY": "anti"}
    out = captcha_solve.detect_provider(env=env, preferred_order=("nonsense",))
    assert out == ("anticaptcha", "anti")


def test_detect_provider_strips_whitespace_only_keys() -> None:
    """Empty/whitespace keys must not register as configured."""
    env = {"TWOCAPTCHA_API_KEY": "   ", "ANTICAPTCHA_API_KEY": "real"}
    assert captcha_solve.detect_provider(env=env) == ("anticaptcha", "real")


# --------------------------------------------------------------------------- solve_recaptcha_v2

def _stub_2captcha(monkeypatch, *, fail_at: str | None = None) -> list[dict]:
    """Replace ``_http_post_json`` with an in-memory mini-server that
    walks the createTask → processing → ready handshake. Captures
    every call into the returned list for inspection."""
    calls: list[dict] = []

    state = {"task_id": None, "polls": 0}

    def fake(url: str, payload: dict, *, timeout: float = 30.0) -> dict:
        calls.append({"url": url, "payload": payload})
        if "createTask" in url:
            if fail_at == "create":
                return {"errorId": 1, "errorDescription": "stub error"}
            state["task_id"] = "TASK-1"
            return {"errorId": 0, "taskId": state["task_id"]}
        if "getTaskResult" in url:
            state["polls"] += 1
            if fail_at == "result":
                return {"errorId": 1, "errorDescription": "result fail"}
            if state["polls"] < 2:
                return {"status": "processing"}
            return {
                "status": "ready",
                "solution": {"gRecaptchaResponse": "TOKEN-XYZ"},
                "cost": 0.0029,
            }
        return {}

    monkeypatch.setattr(captcha_solve, "_http_post_json", fake)
    monkeypatch.setattr(captcha_solve.time, "sleep", lambda _s: None)
    return calls


def test_solve_recaptcha_v2_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_2captcha(monkeypatch)
    res = captcha_solve.solve_recaptcha_v2(
        site_key="6Lc-test",
        page_url="https://example.com/",
        env={"TWOCAPTCHA_API_KEY": "key"},
        poll_min=0.0, poll_max=0.0, timeout=10.0,
    )
    assert res.ok is True
    assert res.token == "TOKEN-XYZ"
    assert res.provider == "2captcha"
    assert res.cost == pytest.approx(0.0029)
    # Confirm the create call carries the right task type + site key.
    create_calls = [c for c in calls if "createTask" in c["url"]]
    assert len(create_calls) == 1
    payload = create_calls[0]["payload"]
    assert payload["task"]["type"] == "RecaptchaV2TaskProxyless"
    assert payload["task"]["websiteKey"] == "6Lc-test"
    assert payload["task"]["websiteURL"] == "https://example.com/"
    assert payload["clientKey"] == "key"


def test_solve_recaptcha_v2_no_provider_returns_error_result() -> None:
    res = captcha_solve.solve_recaptcha_v2(
        site_key="6Lc",
        page_url="https://example.com/",
        env={},
    )
    assert res.ok is False
    assert "no provider configured" in res.error
    assert res.token is None


def test_solve_recaptcha_v2_create_failure_surfaces_in_result(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_2captcha(monkeypatch, fail_at="create")
    res = captcha_solve.solve_recaptcha_v2(
        site_key="6Lc",
        page_url="https://example.com/",
        env={"TWOCAPTCHA_API_KEY": "key"},
        poll_min=0.0, poll_max=0.0, timeout=10.0,
    )
    assert res.ok is False
    assert "stub error" in res.error
    assert res.provider == "2captcha"


def test_solve_recaptcha_v2_invisible_flag_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_2captcha(monkeypatch)
    captcha_solve.solve_recaptcha_v2(
        site_key="6Lc-inv",
        page_url="https://example.com/x",
        is_invisible=True,
        env={"TWOCAPTCHA_API_KEY": "k"},
        poll_min=0.0, poll_max=0.0, timeout=10.0,
    )
    create = next(c for c in calls if "createTask" in c["url"])
    assert create["payload"]["task"]["isInvisible"] is True


# --------------------------------------------------------------------------- v3 / hcaptcha / turnstile

def test_solve_recaptcha_v3_uses_v3_method(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_2captcha(monkeypatch)
    captcha_solve.solve_recaptcha_v3(
        site_key="K", page_url="https://x", min_score=0.9,
        env={"TWOCAPTCHA_API_KEY": "k"},
        poll_min=0.0, poll_max=0.0, timeout=10.0,
    )
    create = next(c for c in calls if "createTask" in c["url"])
    assert create["payload"]["task"]["type"] == "RecaptchaV3TaskProxyless"
    assert create["payload"]["task"]["minScore"] == 0.9


def test_solve_hcaptcha_uses_hcaptcha_method(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_2captcha(monkeypatch)
    captcha_solve.solve_hcaptcha(
        site_key="K", page_url="https://x",
        env={"TWOCAPTCHA_API_KEY": "k"},
        poll_min=0.0, poll_max=0.0, timeout=10.0,
    )
    create = next(c for c in calls if "createTask" in c["url"])
    assert create["payload"]["task"]["type"] == "HCaptchaTaskProxyless"


def test_solve_turnstile_omits_action_when_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_2captcha(monkeypatch)
    captcha_solve.solve_turnstile(
        site_key="K", page_url="https://x", action="",
        env={"TWOCAPTCHA_API_KEY": "k"},
        poll_min=0.0, poll_max=0.0, timeout=10.0,
    )
    create = next(c for c in calls if "createTask" in c["url"])
    assert "action" not in create["payload"]["task"]


def test_solve_turnstile_includes_action_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_2captcha(monkeypatch)
    captcha_solve.solve_turnstile(
        site_key="K", page_url="https://x", action="login",
        env={"TWOCAPTCHA_API_KEY": "k"},
        poll_min=0.0, poll_max=0.0, timeout=10.0,
    )
    create = next(c for c in calls if "createTask" in c["url"])
    assert create["payload"]["task"]["action"] == "login"


# --------------------------------------------------------------------------- anticaptcha / capsolver dispatch

def test_anticaptcha_used_when_only_that_key_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    def fake(url: str, payload: dict, *, timeout: float = 30.0) -> dict:
        calls.append({"url": url})
        if "createTask" in url:
            return {"errorId": 0, "taskId": "T"}
        return {"status": "ready", "solution": {"gRecaptchaResponse": "TOK"}}

    monkeypatch.setattr(captcha_solve, "_http_post_json", fake)
    monkeypatch.setattr(captcha_solve.time, "sleep", lambda _: None)

    res = captcha_solve.solve_recaptcha_v2(
        site_key="K", page_url="https://x",
        env={"ANTICAPTCHA_API_KEY": "abc"},
        poll_min=0.0, poll_max=0.0, timeout=10.0,
    )
    assert res.ok is True
    assert res.provider == "anticaptcha"
    assert all("anti-captcha.com" in c["url"] for c in calls)


def test_capsolver_used_when_only_that_key_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    def fake(url: str, payload: dict, *, timeout: float = 30.0) -> dict:
        calls.append({"url": url})
        if "createTask" in url:
            return {"errorId": 0, "taskId": "T"}
        return {"status": "ready", "solution": {"token": "TUR-TOK"}}

    monkeypatch.setattr(captcha_solve, "_http_post_json", fake)
    monkeypatch.setattr(captcha_solve.time, "sleep", lambda _: None)

    res = captcha_solve.solve_turnstile(
        site_key="K", page_url="https://x",
        env={"CAPSOLVER_API_KEY": "cap"},
        poll_min=0.0, poll_max=0.0, timeout=10.0,
    )
    assert res.ok is True
    assert res.provider == "capsolver"
    assert res.token == "TUR-TOK"
    assert all("capsolver.com" in c["url"] for c in calls)
