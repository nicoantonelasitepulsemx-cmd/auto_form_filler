"""v4 add-on: automatic CAPTCHA solving via third-party providers.

The legacy ``captcha.py`` only *detects* a CAPTCHA and pauses for a
human. This module is the opt-in escalation that runs **before** the
human-pause: if an API key is configured for one of the supported
solvers, we submit the challenge, poll for the answer, and inject the
token back into the page so the form can keep going unattended.

Supported challenge types
~~~~~~~~~~~~~~~~~~~~~~~~~
* reCAPTCHA v2 (checkbox + invisible)
* reCAPTCHA v3 (returns a token; site decides whether to honour it)
* hCaptcha
* Cloudflare Turnstile

Supported providers
~~~~~~~~~~~~~~~~~~~
* `2captcha <https://2captcha.com>`_ — env ``TWOCAPTCHA_API_KEY``
* `Anti-Captcha <https://anti-captcha.com>`_ — env
  ``ANTICAPTCHA_API_KEY``
* `CapSolver <https://capsolver.com>`_ — env ``CAPSOLVER_API_KEY``

The selection is automatic: whichever key is present wins. If multiple
keys are set the order above is honoured (cheapest first by default,
configurable via ``preferred_order``).

Design notes
~~~~~~~~~~~~
* All HTTP runs through :mod:`urllib.request` so we don't add a new pip
  dependency. Each provider speaks JSON; we round-trip via
  :func:`json.loads`/:func:`json.dumps`.
* Polling uses a simple bounded loop with exponential backoff capped
  at ``poll_max``. The default ``timeout=180s`` matches the upstream
  recommended worker SLA.
* Pure functions where possible — the only side effects are HTTP
  calls. ``solve_*`` returns a :class:`SolveResult` so the caller can
  branch on ``ok`` rather than catching exceptions.
* Page-level helpers (``inject_recaptcha_token`` etc.) only run when a
  Playwright ``Page`` is supplied; the unit tests stub them out.

Example
~~~~~~~
.. code-block:: python

    from captcha_solve import solve_recaptcha_v2, inject_recaptcha_token

    result = solve_recaptcha_v2(
        site_key="6Lc...",
        page_url="https://example.com/form",
    )
    if result.ok:
        await inject_recaptcha_token(page, result.token)
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

__all__ = [
    "SolveResult",
    "ProviderError",
    "detect_provider",
    "solve_recaptcha_v2",
    "solve_recaptcha_v3",
    "solve_hcaptcha",
    "solve_turnstile",
    "inject_recaptcha_token",
    "inject_hcaptcha_token",
    "inject_turnstile_token",
]


# --------------------------------------------------------------------------- types

class ProviderError(Exception):
    """Raised when a CAPTCHA provider returns a hard error.

    Soft errors (rate-limit, no slot available) are *not* raised — the
    poller waits and retries until ``timeout`` so the caller doesn't
    have to special-case them.
    """


@dataclass
class SolveResult:
    """Outcome of a single solve attempt.

    Attributes
    ----------
    token:
        The solver's answer (gReCaptchaResponse / hCaptcha token /
        Turnstile token). ``None`` on failure.
    provider:
        Which provider produced the result, for logging.
    cost:
        Cost in USD if the provider returned one. ``None`` otherwise.
    elapsed:
        Wall-clock seconds the solve took.
    error:
        Human-readable error string when ``ok`` is False.
    raw:
        Last raw response payload, useful for debugging.
    """
    token: Optional[str] = None
    provider: str = ""
    cost: Optional[float] = None
    elapsed: float = 0.0
    error: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.token)


# --------------------------------------------------------------------------- provider selection

# (env-var, label) — order = priority
_PROVIDER_ENVS: tuple[tuple[str, str], ...] = (
    ("TWOCAPTCHA_API_KEY", "2captcha"),
    ("ANTICAPTCHA_API_KEY", "anticaptcha"),
    ("CAPSOLVER_API_KEY", "capsolver"),
)


def detect_provider(
    *,
    preferred_order: Iterable[str] = (),
    env: Optional[dict[str, str]] = None,
) -> Optional[tuple[str, str]]:
    """Return ``(provider_name, api_key)`` for the first available key.

    Parameters
    ----------
    preferred_order:
        Optional iterable of provider names (``"2captcha"``,
        ``"anticaptcha"``, ``"capsolver"``) to override the built-in
        priority. Unknown names are ignored.
    env:
        Mapping to read keys from, defaults to :data:`os.environ`.
        Mostly here so the unit tests can pass their own dict.
    """
    src = env if env is not None else os.environ

    # Build the lookup honouring the user's preferred order first,
    # then any remaining defaults so we never silently drop a key.
    order: list[tuple[str, str]] = []
    seen: set[str] = set()
    for label in preferred_order:
        for var, name in _PROVIDER_ENVS:
            if name == label and name not in seen:
                order.append((var, name))
                seen.add(name)
    for var, name in _PROVIDER_ENVS:
        if name not in seen:
            order.append((var, name))

    for var, name in order:
        key = src.get(var, "").strip()
        if key:
            return name, key
    return None


# --------------------------------------------------------------------------- HTTP helper

def _http_post_json(url: str, payload: dict[str, Any], *, timeout: float = 30.0) -> dict[str, Any]:
    """Tiny POST-JSON helper that always returns a dict.

    Network/parse failures become :class:`ProviderError` so callers can
    branch on the same exception regardless of which provider failed.
    """
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
    except urllib.error.URLError as exc:
        raise ProviderError(f"HTTP error contacting {url}: {exc}") from exc
    try:
        return json.loads(data.decode("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        raise ProviderError(f"non-JSON response from {url}: {data!r}") from exc


def _http_get_json(url: str, *, timeout: float = 30.0) -> dict[str, Any]:
    """GET-JSON twin of :func:`_http_post_json` for legacy 2captcha endpoints."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = resp.read()
    except urllib.error.URLError as exc:
        raise ProviderError(f"HTTP error contacting {url}: {exc}") from exc
    text = data.decode("utf-8")
    # 2captcha sometimes returns ``OK|<token>`` plain text — handle that.
    if text.startswith("OK|"):
        return {"status": 1, "request": text.split("|", 1)[1]}
    if text.startswith("CAPCHA_NOT_READY"):
        return {"status": 0, "request": "CAPCHA_NOT_READY"}
    if text.startswith("ERROR_"):
        return {"status": 0, "request": text}
    try:
        return json.loads(text or "{}")
    except json.JSONDecodeError:
        # Last-ditch: wrap the raw text so callers can still log it.
        return {"raw": text}


# --------------------------------------------------------------------------- 2captcha

def _2captcha_create(
    api_key: str,
    *,
    method: str,
    extra: dict[str, Any],
    timeout: float = 30.0,
) -> str:
    """Submit a job and return the captcha id. Raises on hard errors."""
    payload = {
        "clientKey": api_key,
        "task": {"type": method, **extra},
    }
    res = _http_post_json("https://api.2captcha.com/createTask", payload, timeout=timeout)
    if res.get("errorId"):
        raise ProviderError(f"2captcha createTask: {res.get('errorDescription') or res}")
    return str(res["taskId"])


def _2captcha_poll(
    api_key: str,
    task_id: str,
    *,
    timeout: float,
    poll_min: float,
    poll_max: float,
) -> dict[str, Any]:
    """Poll until ``ready``/timeout. Returns the final result dict."""
    deadline = time.time() + timeout
    delay = poll_min
    last: dict[str, Any] = {}
    while time.time() < deadline:
        time.sleep(delay)
        delay = min(delay * 1.5, poll_max)
        res = _http_post_json(
            "https://api.2captcha.com/getTaskResult",
            {"clientKey": api_key, "taskId": task_id},
        )
        last = res
        if res.get("errorId"):
            raise ProviderError(f"2captcha getTaskResult: {res.get('errorDescription') or res}")
        if res.get("status") == "ready":
            return res
    raise ProviderError(f"2captcha timed out after {timeout:.0f}s; last={last}")


def _2captcha_solve(
    api_key: str,
    *,
    method: str,
    extra: dict[str, Any],
    timeout: float,
    poll_min: float,
    poll_max: float,
) -> SolveResult:
    started = time.time()
    try:
        task_id = _2captcha_create(api_key, method=method, extra=extra)
        res = _2captcha_poll(api_key, task_id, timeout=timeout, poll_min=poll_min, poll_max=poll_max)
    except ProviderError as exc:
        return SolveResult(provider="2captcha", error=str(exc), elapsed=time.time() - started)
    sol = res.get("solution") or {}
    token = (
        sol.get("gRecaptchaResponse")
        or sol.get("token")
        or sol.get("text")
    )
    cost = res.get("cost")
    return SolveResult(
        token=str(token) if token else None,
        provider="2captcha",
        cost=float(cost) if cost is not None else None,
        elapsed=time.time() - started,
        raw=res,
        error="" if token else "no token in solution",
    )


# --------------------------------------------------------------------------- Anti-Captcha

def _anticaptcha_solve(
    api_key: str,
    *,
    method: str,
    extra: dict[str, Any],
    timeout: float,
    poll_min: float,
    poll_max: float,
) -> SolveResult:
    """Anti-Captcha speaks the same JSON dialect 2captcha cloned, so the
    code path is structurally identical — only the host differs."""
    started = time.time()
    try:
        create = _http_post_json(
            "https://api.anti-captcha.com/createTask",
            {"clientKey": api_key, "task": {"type": method, **extra}},
        )
        if create.get("errorId"):
            raise ProviderError(f"anticaptcha createTask: {create.get('errorDescription') or create}")
        task_id = str(create["taskId"])
        deadline = time.time() + timeout
        delay = poll_min
        last: dict[str, Any] = {}
        while time.time() < deadline:
            time.sleep(delay)
            delay = min(delay * 1.5, poll_max)
            res = _http_post_json(
                "https://api.anti-captcha.com/getTaskResult",
                {"clientKey": api_key, "taskId": task_id},
            )
            last = res
            if res.get("errorId"):
                raise ProviderError(f"anticaptcha getTaskResult: {res.get('errorDescription') or res}")
            if res.get("status") == "ready":
                sol = res.get("solution") or {}
                token = sol.get("gRecaptchaResponse") or sol.get("token") or sol.get("text")
                return SolveResult(
                    token=str(token) if token else None,
                    provider="anticaptcha",
                    cost=float(res["cost"]) if "cost" in res else None,
                    elapsed=time.time() - started,
                    raw=res,
                    error="" if token else "no token in solution",
                )
        raise ProviderError(f"anticaptcha timed out after {timeout:.0f}s; last={last}")
    except ProviderError as exc:
        return SolveResult(provider="anticaptcha", error=str(exc), elapsed=time.time() - started)


# --------------------------------------------------------------------------- CapSolver

def _capsolver_solve(
    api_key: str,
    *,
    method: str,
    extra: dict[str, Any],
    timeout: float,
    poll_min: float,
    poll_max: float,
) -> SolveResult:
    """CapSolver is API-compatible with the createTask/getTaskResult
    convention. We keep our own copy rather than aliasing because the
    error semantics drift over time (e.g. CapSolver returns ``status``
    as ``processing|ready`` exactly like Anti-Captcha but with extra
    ``ipAddress``/``proxy`` fields we want to preserve in ``raw``)."""
    started = time.time()
    try:
        create = _http_post_json(
            "https://api.capsolver.com/createTask",
            {"clientKey": api_key, "task": {"type": method, **extra}},
        )
        if create.get("errorId"):
            raise ProviderError(f"capsolver createTask: {create.get('errorDescription') or create}")
        task_id = str(create["taskId"])
        deadline = time.time() + timeout
        delay = poll_min
        last: dict[str, Any] = {}
        while time.time() < deadline:
            time.sleep(delay)
            delay = min(delay * 1.5, poll_max)
            res = _http_post_json(
                "https://api.capsolver.com/getTaskResult",
                {"clientKey": api_key, "taskId": task_id},
            )
            last = res
            if res.get("errorId"):
                raise ProviderError(f"capsolver getTaskResult: {res.get('errorDescription') or res}")
            if res.get("status") == "ready":
                sol = res.get("solution") or {}
                token = sol.get("gRecaptchaResponse") or sol.get("token") or sol.get("text")
                return SolveResult(
                    token=str(token) if token else None,
                    provider="capsolver",
                    cost=float(res["cost"]) if "cost" in res else None,
                    elapsed=time.time() - started,
                    raw=res,
                    error="" if token else "no token in solution",
                )
        raise ProviderError(f"capsolver timed out after {timeout:.0f}s; last={last}")
    except ProviderError as exc:
        return SolveResult(provider="capsolver", error=str(exc), elapsed=time.time() - started)


# --------------------------------------------------------------------------- public API

def _dispatch(
    *,
    method: str,
    extra: dict[str, Any],
    preferred_order: Iterable[str],
    timeout: float,
    poll_min: float,
    poll_max: float,
    env: Optional[dict[str, str]] = None,
) -> SolveResult:
    detected = detect_provider(preferred_order=preferred_order, env=env)
    if detected is None:
        return SolveResult(error="no provider configured (set TWOCAPTCHA_API_KEY / ANTICAPTCHA_API_KEY / CAPSOLVER_API_KEY)")
    provider, api_key = detected
    if provider == "2captcha":
        return _2captcha_solve(
            api_key, method=method, extra=extra,
            timeout=timeout, poll_min=poll_min, poll_max=poll_max,
        )
    if provider == "anticaptcha":
        return _anticaptcha_solve(
            api_key, method=method, extra=extra,
            timeout=timeout, poll_min=poll_min, poll_max=poll_max,
        )
    if provider == "capsolver":
        return _capsolver_solve(
            api_key, method=method, extra=extra,
            timeout=timeout, poll_min=poll_min, poll_max=poll_max,
        )
    return SolveResult(error=f"unknown provider {provider!r}")  # pragma: no cover (defensive)


def solve_recaptcha_v2(
    *,
    site_key: str,
    page_url: str,
    is_invisible: bool = False,
    preferred_order: Iterable[str] = (),
    timeout: float = 180.0,
    poll_min: float = 4.0,
    poll_max: float = 20.0,
    env: Optional[dict[str, str]] = None,
) -> SolveResult:
    """Solve reCAPTCHA v2 (checkbox or invisible).

    ``site_key`` and ``page_url`` are the only mandatory inputs — both
    can be scraped from the page (``[data-sitekey]`` / iframe URL).
    """
    return _dispatch(
        method="RecaptchaV2TaskProxyless",
        extra={
            "websiteURL": page_url,
            "websiteKey": site_key,
            "isInvisible": bool(is_invisible),
        },
        preferred_order=preferred_order,
        timeout=timeout, poll_min=poll_min, poll_max=poll_max,
        env=env,
    )


def solve_recaptcha_v3(
    *,
    site_key: str,
    page_url: str,
    page_action: str = "verify",
    min_score: float = 0.7,
    preferred_order: Iterable[str] = (),
    timeout: float = 180.0,
    poll_min: float = 4.0,
    poll_max: float = 20.0,
    env: Optional[dict[str, str]] = None,
) -> SolveResult:
    """Solve reCAPTCHA v3 (frictionless score)."""
    return _dispatch(
        method="RecaptchaV3TaskProxyless",
        extra={
            "websiteURL": page_url,
            "websiteKey": site_key,
            "pageAction": page_action,
            "minScore": float(min_score),
        },
        preferred_order=preferred_order,
        timeout=timeout, poll_min=poll_min, poll_max=poll_max,
        env=env,
    )


def solve_hcaptcha(
    *,
    site_key: str,
    page_url: str,
    preferred_order: Iterable[str] = (),
    timeout: float = 180.0,
    poll_min: float = 4.0,
    poll_max: float = 20.0,
    env: Optional[dict[str, str]] = None,
) -> SolveResult:
    """Solve hCaptcha (used by Cloudflare for some site profiles)."""
    return _dispatch(
        method="HCaptchaTaskProxyless",
        extra={"websiteURL": page_url, "websiteKey": site_key},
        preferred_order=preferred_order,
        timeout=timeout, poll_min=poll_min, poll_max=poll_max,
        env=env,
    )


def solve_turnstile(
    *,
    site_key: str,
    page_url: str,
    action: str = "",
    preferred_order: Iterable[str] = (),
    timeout: float = 180.0,
    poll_min: float = 4.0,
    poll_max: float = 20.0,
    env: Optional[dict[str, str]] = None,
) -> SolveResult:
    """Solve Cloudflare Turnstile.

    Note: Turnstile tokens are *single-use and IP-bound* — submit the
    form quickly after solving (typically <60s). If you're behind a
    proxy, all three providers offer a proxy-attached endpoint; this
    helper sticks to the proxyless variant for simplicity.
    """
    return _dispatch(
        method="AntiTurnstileTaskProxyless",
        extra={
            "websiteURL": page_url,
            "websiteKey": site_key,
            **({"action": action} if action else {}),
        },
        preferred_order=preferred_order,
        timeout=timeout, poll_min=poll_min, poll_max=poll_max,
        env=env,
    )


# --------------------------------------------------------------------------- page injection helpers

# These three helpers run on a Playwright ``Page``. Kept tiny because
# the form-filler already has its own self-healing fill primitives —
# we only need to set the hidden response field that the site reads.

async def inject_recaptcha_token(page: Any, token: str) -> bool:
    """Fill reCAPTCHA's ``g-recaptcha-response`` textarea with ``token``.

    Returns True on success. Looks at every frame because reCAPTCHA
    typically lives inside a sandboxed iframe on the same origin as
    the form (``rc-anchor``/``rc-bumper`` patterns).
    """
    js = """
    (token) => {
      // The hidden response textarea the site reads when the form submits.
      const fields = document.querySelectorAll(
        'textarea[name="g-recaptcha-response"], textarea[id^="g-recaptcha-response"]'
      );
      let ok = false;
      for (const f of fields) {
        f.value = token;
        f.dispatchEvent(new Event('input', { bubbles: true }));
        f.dispatchEvent(new Event('change', { bubbles: true }));
        ok = true;
      }
      // Some sites also expose a global callback (data-callback="onSolved");
      // we deliberately don't invoke it because triggering submission
      // belongs to the replay engine, not this helper.
      return ok;
    }
    """
    return bool(await page.evaluate(js, token))


async def inject_hcaptcha_token(page: Any, token: str) -> bool:
    js = """
    (token) => {
      const fields = document.querySelectorAll(
        'textarea[name="h-captcha-response"], textarea[name="g-recaptcha-response"]'
      );
      let ok = false;
      for (const f of fields) {
        f.value = token;
        f.dispatchEvent(new Event('input', { bubbles: true }));
        f.dispatchEvent(new Event('change', { bubbles: true }));
        ok = true;
      }
      return ok;
    }
    """
    return bool(await page.evaluate(js, token))


async def inject_turnstile_token(page: Any, token: str) -> bool:
    js = """
    (token) => {
      const fields = document.querySelectorAll(
        'input[name="cf-turnstile-response"]'
      );
      let ok = false;
      for (const f of fields) {
        f.value = token;
        f.dispatchEvent(new Event('input', { bubbles: true }));
        f.dispatchEvent(new Event('change', { bubbles: true }));
        ok = true;
      }
      return ok;
    }
    """
    return bool(await page.evaluate(js, token))
