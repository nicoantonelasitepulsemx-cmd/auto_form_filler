"""CAPTCHA detection + pause-for-human handler.

`detect_captcha(page)` scans the page (and visible iframes) for known CAPTCHA
fingerprints and returns the kind found, or None.

`pause_for_human(page, kind, logger, ...)` injects a fixed top banner with a
"✓ Continue" button and blocks until the user clicks it (or the page goes
away, or a timeout fires).
"""
from __future__ import annotations

import asyncio
from typing import Optional

from playwright.async_api import Page

# Ordered list — the first kind that matches wins. Each entry is
# (kind, list_of_css_selectors). A kind matches if ANY selector matches.
_DETECTORS: list[tuple[str, list[str]]] = [
    (
        "recaptcha-v2",
        [
            "iframe[src*='recaptcha']",
            "div.g-recaptcha",
            "#g-recaptcha-response",
        ],
    ),
    (
        "hcaptcha",
        [
            "iframe[src*='hcaptcha.com']",
            "div.h-captcha",
            "#h-captcha-response",
        ],
    ),
    (
        "cloudflare-turnstile",
        [
            "iframe[src*='challenges.cloudflare.com']",
            "div.cf-turnstile",
            "[name='cf-turnstile-response']",
        ],
    ),
    (
        "funcaptcha",
        [
            "iframe[src*='funcaptcha.com']",
            "iframe[src*='arkoselabs.com']",
            "div.funcaptcha",
        ],
    ),
    (
        "image-captcha",
        [
            "img[src*='captcha' i]",
            "input[name*='captcha' i]",
            "input[id*='captcha' i]",
        ],
    ),
]


async def _selector_present(page: Page, sel: str) -> bool:
    """Match if the element is in the DOM. We DON'T require visibility because
    many CAPTCHA widgets boot in a hidden state (or render inside iframes
    Playwright considers non-visible) — a false positive ("paused but no
    CAPTCHA") is much cheaper than a false negative ("auto-submitted into a
    CAPTCHA wall")."""
    try:
        return (await page.locator(sel).first.count()) > 0
    except Exception:
        return False


async def detect_captcha(page: Page) -> Optional[str]:
    """Return the kind of CAPTCHA detected, or None.

    Detection order matches `_DETECTORS`. The first match wins.
    """
    for kind, selectors in _DETECTORS:
        for sel in selectors:
            if await _selector_present(page, sel):
                return kind

    # Heuristic text-challenge — only as a last resort.
    try:
        for phrase in ("I'm not a robot", "verify you are human", "Vui lòng xác minh"):
            loc = page.get_by_text(phrase, exact=False).first
            if await loc.count() > 0:
                try:
                    if await loc.is_visible():
                        return "text-challenge"
                except Exception:
                    return "text-challenge"
    except Exception:
        pass

    return None


_PAUSE_BANNER_JS = r"""
(function(kind) {
    // Remove any existing banner first
    const old = document.getElementById('af-captcha-banner');
    if (old) old.remove();

    const banner = document.createElement('div');
    banner.id = 'af-captcha-banner';
    banner.style.cssText = [
        'position:fixed','top:0','left:0','right:0','z-index:2147483647',
        'background:#ff3c00','color:#fff','font:600 14px system-ui',
        'padding:12px 16px','box-shadow:0 4px 12px rgba(0,0,0,0.3)',
        'display:flex','align-items:center','gap:12px'
    ].join(';');
    banner.innerHTML =
        '<span style="flex:1">⚠ CAPTCHA detected (' + kind + '). ' +
        'Solve it in this window, then click Continue.</span>' +
        '<button id="af-captcha-continue" style="padding:8px 14px;background:#fff;color:#ff3c00;' +
        'border:none;border-radius:4px;font-weight:700;cursor:pointer">✓ Continue</button>';
    document.body.appendChild(banner);
    document.getElementById('af-captcha-continue').addEventListener('click', function () {
        try { window.__afCaptchaContinue && window.__afCaptchaContinue(); } catch (e) {}
        banner.remove();
    });
})("__KIND__");
"""

# Track exposed-function state per browser context — `expose_function` errors
# if called twice with the same name on the same context.
_CONTEXTS_BOUND: set[int] = set()


async def pause_for_human(
    page: Page,
    kind: str,
    logger,
    timeout: float = 300.0,
) -> bool:
    """Block until the user clicks the Continue button, or timeout fires.

    Returns True if the user continued, False on timeout.
    """
    fut: asyncio.Future = asyncio.get_event_loop().create_future()

    async def _on_continue() -> None:
        if not fut.done():
            fut.set_result(True)

    ctx = page.context
    if id(ctx) not in _CONTEXTS_BOUND:
        try:
            await ctx.expose_function("__afCaptchaContinue", _on_continue)
            _CONTEXTS_BOUND.add(id(ctx))
        except Exception as exc:
            logger.warning(f"[CAPTCHA] couldn't expose continue function: {exc}")
    else:
        # Already exposed — replace the handler via a simple JS hook.
        try:
            await ctx.expose_function(f"__afCaptchaContinue_{id(fut)}", _on_continue)
            await page.evaluate(
                f"window.__afCaptchaContinue = window.__afCaptchaContinue_{id(fut)};"
            )
        except Exception:
            pass

    try:
        await page.evaluate(_PAUSE_BANNER_JS.replace("__KIND__", kind))
    except Exception as exc:
        logger.warning(f"[CAPTCHA] couldn't inject banner: {exc}")

    logger.warning(
        f"[CAPTCHA] detected {kind!r} — waiting up to {int(timeout)}s for you to solve and click Continue"
    )
    try:
        await asyncio.wait_for(fut, timeout=timeout)
        logger.info("[CAPTCHA] resumed by user")
        return True
    except asyncio.TimeoutError:
        logger.error(f"[CAPTCHA] timed out after {int(timeout)}s — aborting")
        return False
