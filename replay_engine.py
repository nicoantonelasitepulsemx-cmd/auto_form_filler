"""replay_engine.py — replay a recorded action sequence faithfully.

Each action in the v2 config schema records *what method* the user used
(`type`, `paste`, `select`, `click`, `check`, `set_files`, `combobox`,
`contenteditable`, `wait`) so replay can use the same method instead of
forcing every input through `Locator.fill()`.

Action shapes (also see ANALYSIS.md):

    {
        "kind":          "fill",                 # what to do
        "input_method":  "type" | "fill" | "paste" | "press_sequentially",
        "selectors":     [...],                  # for resolver_v2
        "fingerprint":   {...},                  # for resolver_v2
        "frame_chain":   ["top", "https://..."],
        "value":         "user@example.com",
        "field_id":      "email",
        "field_type":    "email",
        "value_template":"{email}"               # optional
    }

    {"kind": "click", ...,    "click_position": {"x_pct":0.5,"y_pct":0.5}}
    {"kind": "check",  ..., "checked": true}
    {"kind": "select", ..., "value": "vn", "value_label": "Vietnam"}
    {"kind": "set_files", ..., "files": ["/abs/path/photo.jpg"]}
    {"kind": "combobox", ..., "value": "Vietnam"}        # custom dropdowns
    {"kind": "contenteditable", ..., "value": "<rich text>"}
    {"kind": "wait", "duration_ms": 500}
    {"kind": "press_keys", "keys": ["Tab", "Tab", "Enter"]}
"""
from __future__ import annotations

import asyncio
import random
import re
from typing import Any, Mapping, Optional

from playwright.async_api import Locator, Page

from element_fingerprint import fingerprint_score
from resolver_v2 import ResolveResult, resolve


# Default tuning — overridden by config keys (see run_actions).
_DEFAULT_ACTION_DELAY_MS = 120     # baseline pause between actions
_DEFAULT_ACTION_JITTER_MS = 80     # random extra on top (0..jitter)
_DEFAULT_TYPE_DELAY_MS = 25        # per-keystroke delay when typing
_DEFAULT_STABILIZE_MS = 150        # wait for layout to settle before acting


async def _wait_for_actionable(loc: Locator, *, timeout_ms: int = 4000) -> bool:
    """Wait until the locator is visible+enabled and its bounding box is stable.

    Returns True if the element became actionable, False on timeout. We do not
    raise — callers fall back to best-effort interaction.
    """
    try:
        await loc.wait_for(state="visible", timeout=timeout_ms)
    except Exception:
        return False
    try:
        # `enabled` check via JS — Locator has no native helper.
        is_enabled = await loc.evaluate(
            "el => !el.disabled && el.getAttribute('aria-disabled') !== 'true'"
        )
        if not is_enabled:
            return False
    except Exception:
        pass
    # Layout stability: read bbox twice, ensure minimal drift.
    try:
        b1 = await loc.bounding_box()
        await asyncio.sleep(0.08)
        b2 = await loc.bounding_box()
        if b1 and b2:
            drift = abs(b1["x"] - b2["x"]) + abs(b1["y"] - b2["y"])
            if drift > 4:
                # one more wait if the page is animating
                await asyncio.sleep(0.18)
    except Exception:
        pass
    return True


async def _read_live_value(loc: Locator) -> Optional[str]:
    """Read the *effective* current value of an input/textarea/contenteditable."""
    try:
        return await loc.evaluate(
            """el => {
                if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') return el.value;
                if (el.isContentEditable) return el.innerText || el.textContent || '';
                return null;
            }"""
        )
    except Exception:
        return None


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _interpolate(template: str, ctx: Mapping[str, Any]) -> str:
    """Replace ``{var}`` placeholders in *template* from *ctx*. Unknown vars stay verbatim.

    The negative-lookbehind/lookahead skip ``{var}`` matches that are
    wrapped in another brace pair — i.e. ``{{var}}`` tokens that belong
    to ``value_templates`` (``{{date}}``, ``{{uuid4}}``,
    ``{{random_email}}``, ``{{env:NAME}}``…). Without the lookarounds,
    a ctx key whose name collides with a value-templates token name
    (auto_template emits ``date`` for date-like values, and ``{{date}}``
    is also a documented value-templates token) would have its inner
    ``{date}`` substituted, mangling the ``{{date}}`` token into
    ``{2025-01-01}`` and silently losing the date expansion downstream.
    """
    if not template:
        return template
    def sub(m: re.Match[str]) -> str:
        key = m.group(1)
        if key in ctx:
            return str(ctx[key])
        return m.group(0)
    return re.sub(r"(?<!\{)\{([a-zA-Z_][a-zA-Z0-9_]*)\}(?!\})", sub, template)


async def _fetch_otp_code(
    page: Page,
    action: dict,
    ctx: Optional[Mapping[str, Any]],
    *,
    logger,
) -> Optional[str]:
    """Resolve the value for an ``otp_paste`` action.

    Strategy (first wins):

    1. ``ctx['_kuku']`` is a pre-built :py:class:`kuku_lu.Kuku` instance.
       Use it directly — pure function, no network setup happens here.
    2. ``ctx['_kuku_creds']`` is a :py:class:`kuku_lu.KukuCreds`. Build
       a Playwright-backed client against the current ``page`` and reuse
       it for the duration of this action.
    3. Fall back to the literal ``action['value']`` recorded at capture
       time. This is mostly useful for dry-runs and unit tests; in real
       runs the recorded code has long since expired.

    Returns the code as a string, or ``None`` when no path produces one
    so the caller can skip cleanly.
    """
    source = action.get("source") or {}
    if (source.get("kind") or "kuku.lu") != "kuku.lu":
        logger.warning(f"  [otp] unknown source kind {source.get('kind')!r}; using recorded value")
        return action.get("value") or None

    address = source.get("address")
    regex = source.get("regex") or r"(?<!\d)(\d{5,8})(?!\d)"
    from_filter = source.get("from_filter")
    timeout_ms = int(source.get("timeout_ms") or 180000)

    if ctx is not None:
        existing = ctx.get("_kuku")
        if existing is not None:
            try:
                return await existing.wait_for_code(
                    address,
                    regex=regex,
                    timeout=timeout_ms / 1000.0,
                    from_filter=from_filter,
                )
            except Exception as exc:
                logger.warning(f"  [otp] Kuku.wait_for_code failed: {exc!r}")

        creds = ctx.get("_kuku_creds")
        if creds is not None:
            try:
                from kuku_lu import Kuku
                async with await Kuku.from_playwright(page, creds=creds) as k:
                    return await k.wait_for_code(
                        address,
                        regex=regex,
                        timeout=timeout_ms / 1000.0,
                        from_filter=from_filter,
                    )
            except Exception as exc:
                logger.warning(f"  [otp] Kuku.from_playwright failed: {exc!r}")

    # Last-resort: replay the literally-recorded code so dry-runs / unit
    # tests still produce a value. The actual code will be stale in a
    # real OTP scenario but this keeps the pipeline functional.
    rec = action.get("value")
    if rec:
        logger.warning(
            "  [otp] no Kuku client in ctx — using recorded code as fallback. "
            "Pass ctx['_kuku_creds']=KukuCreds(...) to fetch a fresh code."
        )
        return str(rec)
    return None


_PLACEHOLDER_RE = re.compile(r"(?<!\{)\{([a-zA-Z_][a-zA-Z0-9_]*)\}(?!\})")


def _template_has_unresolved_placeholders(
    template: str, ctx: Mapping[str, Any]
) -> bool:
    """True iff *template* contains any ``{var}`` whose key is not in *ctx*.

    Used by :func:`_resolve_value` to decide whether the
    ``value_template`` interpolation can produce a usable value, OR
    whether the recorder's captured literal ``value`` should be used
    as a fallback. Critically, this inspects the **template**'s
    placeholders against the **ctx**, not the interpolated **result** —
    the result may legitimately contain literal ``{word}`` patterns
    coming from a ctx value (e.g. a free-text field where the user
    typed ``"Use {standard} format"``), and treating those as
    unresolved would discard the successful interpolation.

    The negative lookbehind/lookahead skip ``{{var}}`` tokens that
    belong to :mod:`value_templates` and are not subject to
    ctx-substitution at all.
    """
    if not isinstance(template, str):
        return False
    for m in _PLACEHOLDER_RE.finditer(template):
        if m.group(1) not in ctx:
            return True
    return False


def _resolve_value(action: dict, ctx: Optional[Mapping[str, Any]]) -> Any:
    """Pick ``value_template`` (with interpolation) over ``value`` if present.

    After picking the raw value, expand ``{{date}}`` / ``{{uuid4}}`` /
    ``{{random_email}}`` / ``{{env:NAME}}`` / etc. via :mod:`value_templates`
    so each replay run gets a fresh value when the user templates fields.

    Behavior:
      * ``value_template`` missing or empty → use captured ``value``.
      * ``value_template`` present and **every** ``{var}`` placeholder
        in it is satisfied by *ctx* → interpolate and use the result.
      * ``value_template`` present but **at least one** ``{var}``
        placeholder is missing from *ctx* → fall back to the captured
        ``value`` so the form is filled with something usable rather
        than a half-resolved template.

    The "missing placeholder" check is run on the **template**, not the
    interpolation **result** — the result may legitimately contain
    literal ``{word}`` patterns from a ctx value (e.g. a free-text
    field where the user typed ``"Use {standard} format"``). Inspecting
    the result would false-positive there and silently discard the
    successful interpolation.
    """
    raw: Any
    if "value_template" in action and action["value_template"]:
        template = action["value_template"]
        ctx_view: Mapping[str, Any] = ctx or {}
        if isinstance(template, str) and _template_has_unresolved_placeholders(
            template, ctx_view
        ):
            raw = action.get("value", template)
        else:
            raw = _interpolate(template, ctx_view)
    else:
        raw = action.get("value")
    try:
        from value_templates import expand as _expand_tpl
        return _expand_tpl(raw)
    except Exception:
        return raw


async def _safe_resolve(
    page: Page, action: dict, *, threshold: float, logger
) -> Optional[ResolveResult]:
    return await resolve(
        page,
        selectors=action.get("selectors") or action.get("targets") or [],
        fingerprint=action.get("fingerprint"),
        frame_chain=action.get("frame_chain"),
        threshold=threshold,
        logger=logger,
    )


# --------------------------------------------------------------------------------------
# Action handlers
# --------------------------------------------------------------------------------------


async def _react_set_value(loc: Locator, sval: str) -> None:
    """Set value via the native HTMLInputElement/HTMLTextAreaElement setter.

    React (and Relay/FB) intercept `el.value = x`; the only reliable way to
    update its internal state is to call the prototype setter directly and then
    dispatch a native `input` event. This is the canonical "react fill".
    """
    await loc.evaluate(
        """(el, v) => {
            const proto = el.tagName === 'TEXTAREA'
                ? window.HTMLTextAreaElement.prototype
                : window.HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
            setter.call(el, v);
            el.dispatchEvent(new Event('input',  {bubbles:true}));
            el.dispatchEvent(new Event('change', {bubbles:true}));
            el.dispatchEvent(new Event('blur',   {bubbles:true}));
        }""",
        sval,
    )


async def _do_fill(
    page: Page, loc: Locator, action: dict, value: Any, *, logger,
    type_delay_ms: int = _DEFAULT_TYPE_DELAY_MS,
) -> bool:
    """Replay a fill on a real <input> / <textarea> with self-healing.

    `input_method` tells us *what the user did at record time*:
      - "fill"                : Locator.fill
      - "type" / "press_sequentially" : Locator.press_sequentially (real keystrokes)
      - "paste"               : direct value-setter + input event

    After whichever method we picked, we **read back the live value**. If it
    doesn't match what we wanted (common on React/Relay-controlled inputs like
    Facebook), we automatically retry with stronger methods until the value
    sticks: fill → type → react_setter.
    """
    method = (action.get("input_method") or "fill").lower()
    sval = "" if value is None else str(value)

    # Make sure the element is reachable before we touch it.
    try:
        await loc.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    await _wait_for_actionable(loc, timeout_ms=4000)

    async def _try_method(m: str) -> None:
        if m in ("type", "press_sequentially"):
            try:
                await loc.click(timeout=4000)
            except Exception:
                pass
            # Clear by selecting all + delete, robust across browsers.
            try:
                await loc.press("Control+A")
                await loc.press("Delete")
            except Exception:
                pass
            await loc.press_sequentially(sval, delay=type_delay_ms)
        elif m == "paste":
            await _react_set_value(loc, sval)
        else:  # "fill"
            await loc.fill(sval, timeout=8000)

    # Order of attempts: the recorded method, then escalate.
    order: list[str] = []
    if method in ("type", "press_sequentially"):
        order = ["type", "fill", "paste"]
    elif method == "paste":
        order = ["paste", "type", "fill"]
    else:
        order = ["fill", "type", "paste"]

    last_exc: Optional[Exception] = None
    for idx, m in enumerate(order):
        try:
            await _try_method(m)
        except Exception as exc:
            last_exc = exc
            logger.debug(f"  [FILL_TRY] method={m} raised {exc!r}")
            continue

        # Verify the value stuck. Empty string is a legitimate target.
        live = await _read_live_value(loc)
        if live is None:
            # Unable to read back — assume best-effort succeeded.
            return True
        if live == sval:
            if idx > 0:
                logger.info(f"  [FILL_OK] healed via {m!r}")
            return True
        logger.debug(
            f"  [FILL_MISMATCH] method={m} wanted={sval!r} got={live!r}"
        )

    # Fire blur as a last courtesy so the host can validate.
    try:
        await loc.evaluate("el => el.dispatchEvent(new Event('blur', {bubbles:true}))")
    except Exception:
        pass

    logger.error(
        f"  [FILL_ERR] {action.get('field_id')!r}: value did not stick"
        + (f" (last_exc={last_exc!r})" if last_exc else "")
    )
    return False


async def _do_click(page: Page, loc: Locator, action: dict, *, logger) -> bool:
    """Click an element. If `click_position` is set, click at that fractional offset."""
    pos = action.get("click_position")
    try:
        await loc.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    await _wait_for_actionable(loc, timeout_ms=4000)
    try:
        if pos and "x_pct" in pos and "y_pct" in pos:
            box = await loc.bounding_box()
            if box and box["width"] > 0 and box["height"] > 0:
                px = box["width"]  * float(pos["x_pct"])
                py = box["height"] * float(pos["y_pct"])
                await loc.click(position={"x": px, "y": py}, timeout=10000)
                return True
        await loc.click(timeout=10000)
        return True
    except Exception as exc:
        logger.error(f"  [CLICK_ERR] {action.get('field_id')!r}: {exc}")
        return False


async def _verify_radio_outcome(
    page: Page, loc: Locator, action: dict, logger,
    settle_ms: int = 250,
) -> bool:
    """Confirm the right radio is selected after a check action.

    Returns True iff:
      * the resolved element ends up aria-checked / .checked = true, AND
      * its accessible name (or recorded radio_value, if present)
        matches what the recording expected.

    A False return tells the caller to keep falling through the
    attempt ladder. This is the central guard against the Facebook
    trademark-form bug where the resolver's role_name lookup picked
    a sibling whose accessible name differed only by a few words.

    ``settle_ms`` waits for late-arriving sibling-flip mutations from
    React/SPA forms — the page may dispatch a controlled re-render or
    a synthetic click on the previously-selected sibling shortly after
    the user click, leaving the wrong option selected if we verify too
    eagerly. 250 ms covers the typical React batched-render tick
    (~16 ms × N) without slowing replay down meaningfully.
    """
    fp = action.get("fingerprint") or {}
    rec_name = (fp.get("accessible_name") or "").strip()
    rec_value = (action.get("radio_value") or "").strip()
    if not rec_name and not rec_value:
        return True
    if settle_ms > 0:
        try:
            await asyncio.sleep(settle_ms / 1000.0)
        except Exception:
            pass
    try:
        live = await loc.evaluate(
            """el => {
                const accName = (() => {
                    const al = el.getAttribute('aria-label');
                    if (al) return al.trim();
                    const lbid = el.getAttribute('aria-labelledby');
                    if (lbid) {
                        const ref = document.getElementById(lbid);
                        if (ref) return (ref.innerText || ref.textContent || '').trim();
                    }
                    return ((el.innerText || el.textContent || '').trim());
                })();
                const checked = el.getAttribute('aria-checked') === 'true' || !!el.checked;
                return {
                    name: accName,
                    value: el.value || el.getAttribute('value') || '',
                    checked: checked,
                };
            }"""
        )
    except Exception:
        return True  # can't verify — give up on the strict check
    if not live:
        return True
    if not live.get("checked"):
        logger.debug(
            f"  [CHECK_VERIFY] live element not checked yet (name={live.get('name')!r})"
        )
        return False

    def _norm(s: str) -> str:
        return " ".join((s or "").lower().split())

    if rec_value and _norm(live.get("value", "")) and _norm(rec_value) == _norm(live.get("value", "")):
        return True
    if rec_name and _norm(live.get("name", "")) and _norm(rec_name) == _norm(live.get("name", "")):
        return True
    if rec_name and _norm(live.get("name", "")):
        # Substring match in either direction handles "I am the rights
        # owner." vs "I am the rights owner" or extra decoration.
        if _norm(rec_name) in _norm(live.get("name", "")) or _norm(live.get("name", "")) in _norm(rec_name):
            return True
    logger.warning(
        f"  [CHECK_VERIFY] checked the wrong sibling: "
        f"wanted name={rec_name!r}/value={rec_value!r}, "
        f"got name={live.get('name')!r}/value={live.get('value')!r}"
    )
    return False


def _is_radio_check_action(action: dict) -> bool:
    """True when *action* is a check on a radio (native or ARIA).

    Pure helper extracted so the v4 RADIO RULE can be unit-tested
    without driving Playwright. Returns False for ARIA checkboxes and
    plain hidden-input checkboxes — those must NOT be classified as
    radios because the rule unconditionally drops checked=false on
    radios, which would silently break checkbox unchecks.

    Detection signals (any one is enough):
      * ``_hidden_input_type == "radio"`` — recorder paired the click
        with a hidden ``<input type=radio>``
      * ``fingerprint.role == "radio"`` — ARIA-conformant radio (e.g.
        Facebook's ``div[role=radio]``)
      * ``fingerprint.type == "radio"`` — recorder captured the native
        ``<input>`` directly without proxy promotion
    """
    hidden_input_type = action.get("_hidden_input_type")
    fp = action.get("fingerprint") or {}
    fp_role = (fp.get("role") or "").lower()
    fp_type = (fp.get("type") or "").lower()
    return (
        hidden_input_type == "radio"
        or fp_role == "radio"
        or fp_type == "radio"
    )


async def _do_check(page: Page, loc: Locator, action: dict, *, logger) -> bool:
    """Set a checkbox / radio (real <input> or `[role=checkbox|radio]`).

    Strategy order depends on whether the action was recorded as a proxy click
    (Facebook-style hidden radio behind a visible label/span):

    **Normal (visible inputs):**
      1. check()/uncheck()
      2. plain click
      3. force-click
      4. parent <label> click
      5. JS direct set
      6. nearby hidden input walk

    **Proxy click (_click_proxy=true):**
      The real input is hidden and a visual wrapper (label, span, div) is the
      clickable area.  Force-clicking the hidden input sets ``input.checked``
      but does NOT trigger React/Relay state updates — the form never reveals
      the next sections.  So we MUST click a **visible** proxy:
        P1. Click the parent <label> of the resolved input
        P2. Find the input by name+value → click its parent label via JS
        P3. Find a nearby visible label/span with matching text → click it
        P4. Structured check()/uncheck() (Playwright may handle some cases)
        P5. JS direct set + React-compatible synthetic event as last resort

    v4 RADIO RULE
        For radios we **never** uncheck. Old recordings that contain
        ``check checked=false`` on a role=radio / type=radio element are
        always sibling-deselect ghosts; we skip them silently rather than
        risk landing the form on the wrong sibling. Use
        ``deselect_radio_action`` semantics by recording an explicit
        check on the desired sibling.
    """
    desired = bool(action.get("checked", True))
    is_click_proxy = bool(action.get("_click_proxy"))
    hidden_input_name = action.get("_hidden_input_name")
    # IMPORTANT: do NOT default ``_hidden_input_type`` to ``"radio"``.
    # The recorder only sets this field when it actually saw a hidden
    # ``<input type=radio|checkbox>`` paired with a styled proxy
    # element. ARIA-only widgets (``div[role=checkbox]``,
    # ``div[role=radio]``, …) ship without it, and a stale ``"radio"``
    # default would misclassify ARIA-checkbox uncheck actions as
    # radio sibling-deselect ghosts and silently drop them.
    hidden_input_type = action.get("_hidden_input_type")
    radio_value = action.get("radio_value")
    neighbour_text = (action.get("fingerprint") or {}).get("neighbour_text", "")
    is_radio_action = _is_radio_check_action(action)

    # v4 RADIO RULE: drop check-false actions on radios.
    if is_radio_action and not desired:
        logger.info(
            f"  [CHECK_SKIP] {action.get('field_id')!r}: ignoring "
            "checked=false on a radio (sibling-deselect ghost)"
        )
        return True

    try:
        await loc.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    await _wait_for_actionable(loc, timeout_ms=4000)

    # ---------- helper to verify the checked state after an attempt ----------
    async def _is_checked() -> bool:
        try:
            return await loc.evaluate(
                "el => el.getAttribute('aria-checked') === 'true' || !!el.checked"
            )
        except Exception:
            pass
        if hidden_input_name:
            try:
                return await page.evaluate(
                    # When ``inputType`` is empty, drop the ``[type="..."]``
                    # filter so we still find the right ARIA-only widget.
                    # Defaulting to "radio" here would silently miss
                    # ARIA-checkbox recordings whose action dict carries
                    # ``_hidden_input_name`` but no ``_hidden_input_type``.
                    """([name, val, inputType]) => {
                        const sel = inputType
                            ? 'input[type="' + inputType + '"][name="' + name + '"]'
                            : 'input[name="' + name + '"]';
                        const inputs = document.querySelectorAll(sel);
                        for (const inp of inputs) {
                            if (val && inp.value === val) return !!inp.checked;
                            if (!val) return !!inp.checked;
                        }
                        return false;
                    }""",
                    [hidden_input_name, radio_value or "", hidden_input_type or ""],
                )
            except Exception:
                pass
        return False

    # Detect hidden inputs at runtime — even if _click_proxy was not set in the
    # recording (e.g. the action came from the change handler, not the click handler).
    is_hidden_input = False
    if not is_click_proxy:
        try:
            is_hidden_input = await loc.evaluate(
                """el => {
                    if (!el || el.tagName !== 'INPUT') return false;
                    const t = (el.type || '').toLowerCase();
                    if (t !== 'radio' && t !== 'checkbox') return false;
                    const st = window.getComputedStyle(el);
                    if (st.display === 'none' || st.visibility === 'hidden' ||
                        st.opacity === '0') return true;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 2 || r.height <= 2) return true;
                    if (st.position === 'absolute' || st.position === 'fixed') {
                        if (st.clip && st.clip !== 'auto') return true;
                        if (st.clipPath && st.clipPath !== 'none') return true;
                    }
                    return false;
                }"""
            )
        except Exception:
            pass
        if is_hidden_input:
            # Infer proxy metadata from the element itself
            try:
                meta = await loc.evaluate(
                    """el => ({
                        name: el.name || null,
                        type: (el.type || 'radio').toLowerCase(),
                        value: el.value || null,
                    })"""
                )
                if meta:
                    if not hidden_input_name:
                        hidden_input_name = meta.get("name")
                    if not hidden_input_type:
                        hidden_input_type = meta.get("type", "radio")
                    if not radio_value:
                        radio_value = meta.get("value")
            except Exception:
                pass

    use_proxy_path = (is_click_proxy or is_hidden_input) and hidden_input_name

    # ==================== PROXY-CLICK PATH (Facebook-style hidden radios) ====================
    if use_proxy_path:
        # -- P1: click the parent <label> wrapping the resolved element --
        try:
            label_loc = loc.locator("xpath=./ancestor::label")
            if await label_loc.count() > 0:
                await label_loc.first.click(timeout=5000)
                await asyncio.sleep(0.3)
                if await _is_checked() == desired:
                    logger.info("  [CHECK_OK] proxy → parent label click")
                    return True
        except Exception:
            pass

        # -- P2: find the hidden input by name+value, then click its parent label --
        try:
            clicked = await page.evaluate(
                # Conditional selector: drop ``[type="..."]`` when inputType
                # is empty so ARIA-only widgets without ``_hidden_input_type``
                # still resolve.
                """([name, val, inputType]) => {
                    const sel = inputType
                        ? 'input[type="' + inputType + '"][name="' + name + '"]'
                        : 'input[name="' + name + '"]';
                    const inputs = document.querySelectorAll(sel);
                    for (const inp of inputs) {
                        if (val && inp.value !== val) continue;
                        // Walk up to find a clickable label or container
                        const label = inp.closest('label');
                        if (label) { label.click(); return 'label'; }
                        let parent = inp.parentElement;
                        for (let d = 0; parent && d < 4; d++) {
                            if (parent.tagName === 'LABEL' ||
                                parent.getAttribute('role') === 'radio' ||
                                parent.getAttribute('role') === 'checkbox') {
                                parent.click();
                                return 'parent';
                            }
                            parent = parent.parentElement;
                        }
                        // Try clicking the input's label via for= attribute
                        if (inp.id) {
                            const lbl = document.querySelector('label[for="' + inp.id + '"]');
                            if (lbl) { lbl.click(); return 'for-label'; }
                        }
                    }
                    return null;
                }""",
                [hidden_input_name, radio_value or "", hidden_input_type or ""],
            )
            if clicked:
                await asyncio.sleep(0.3)
                if await _is_checked() == desired:
                    logger.info(f"  [CHECK_OK] proxy → JS {clicked} click by name")
                    return True
        except Exception:
            pass

        # -- P3: find a visible label/span near the input whose text matches --
        match_text = radio_value or neighbour_text
        if match_text:
            try:
                clicked = await page.evaluate(
                    """([name, matchText, inputType]) => {
                        const sel = inputType
                            ? 'input[type="' + inputType + '"][name="' + name + '"]'
                            : 'input[name="' + name + '"]';
                        const inputs = document.querySelectorAll(sel);
                        for (const inp of inputs) {
                            // Find the container holding this radio group
                            let container = inp.parentElement;
                            for (let d = 0; container && d < 6; d++) {
                                // Look for a sibling label or span with matching text
                                const labels = container.querySelectorAll('label, span, div');
                                for (const el of labels) {
                                    const txt = (el.textContent || '').trim();
                                    if (txt && matchText.includes(txt) || txt.includes(matchText)) {
                                        const st = window.getComputedStyle(el);
                                        if (st.display !== 'none' && st.visibility !== 'hidden') {
                                            el.click();
                                            return 'text-match';
                                        }
                                    }
                                }
                                container = container.parentElement;
                            }
                        }
                        return null;
                    }""",
                    [hidden_input_name, match_text, hidden_input_type or ""],
                )
                if clicked:
                    await asyncio.sleep(0.3)
                    if await _is_checked() == desired:
                        logger.info("  [CHECK_OK] proxy → text-matching label click")
                        return True
            except Exception:
                pass

        # -- P4: Playwright get_by_label with the neighbour text --
        if match_text:
            try:
                lbl_loc = page.get_by_text(match_text, exact=False).first
                if await lbl_loc.count() > 0:
                    await lbl_loc.click(timeout=5000)
                    await asyncio.sleep(0.3)
                    if await _is_checked() == desired:
                        logger.info("  [CHECK_OK] proxy → Playwright text click")
                        return True
            except Exception:
                pass

        # -- P5: structured check (Playwright may handle even hidden inputs) --
        try:
            if desired:
                await loc.check(timeout=5000)
            else:
                await loc.uncheck(timeout=5000)
            return True
        except Exception:
            pass

        # -- P6: last resort — JS direct set + React-compatible events --
        try:
            ok = await page.evaluate(
                """([name, val, inputType, desired]) => {
                    const sel = inputType
                        ? 'input[type="' + inputType + '"][name="' + name + '"]'
                        : 'input[name="' + name + '"]';
                    const inputs = document.querySelectorAll(sel);
                    for (const inp of inputs) {
                        if (val && inp.value !== val) continue;
                        // Use native setter to bypass React's synthetic events
                        const proto = Object.getOwnPropertyDescriptor(
                            HTMLInputElement.prototype, 'checked');
                        if (proto && proto.set) {
                            proto.set.call(inp, desired);
                        } else {
                            inp.checked = desired;
                        }
                        inp.dispatchEvent(new MouseEvent('click', {bubbles: true}));
                        inp.dispatchEvent(new Event('input',  {bubbles: true}));
                        inp.dispatchEvent(new Event('change', {bubbles: true}));
                        // Also try clicking the parent label for React
                        const label = inp.closest('label');
                        if (label) label.click();
                        return true;
                    }
                    return false;
                }""",
                [hidden_input_name, radio_value or "", hidden_input_type or "", desired],
            )
            if ok:
                logger.info("  [CHECK_OK] proxy → JS direct set (last resort)")
                return True
        except Exception as exc:
            logger.error(f"  [CHECK_ERR] proxy {action.get('field_id')!r}: {exc}")
        return False

    # ==================== NORMAL PATH (visible inputs / ARIA widgets) ====================

    # v4 PRE-CHECK target verification for radios: before we touch the
    # element, confirm the resolved candidate's accessible name / value
    # matches what we recorded. If the resolver picked the wrong sibling
    # (a real risk on FB-style radio groups where every option shares
    # tag, role, neighbour text, viewport position) we re-resolve with
    # the recorded label/value as a stricter Playwright text selector
    # before issuing any click.
    if is_radio_action and desired:
        recorded_name = ((action.get("fingerprint") or {}).get("accessible_name") or "").strip()
        try:
            live = await loc.evaluate(
                """el => ({
                    aria: (el.getAttribute('aria-label') || '').trim(),
                    labelledby: el.getAttribute('aria-labelledby') || '',
                    text: ((el.innerText || el.textContent || '').trim()),
                    value: el.value || el.getAttribute('value') || '',
                })"""
            )
        except Exception:
            live = None
        live_name = ""
        if live:
            live_name = (live.get("aria") or "").strip()
            if not live_name and live.get("labelledby"):
                try:
                    live_name = (
                        await page.evaluate(
                            "id => { const e = document.getElementById(id);"
                            " return e ? (e.innerText || e.textContent || '').trim() : ''; }",
                            live["labelledby"],
                        )
                    ) or ""
                except Exception:
                    pass
            if not live_name:
                live_name = (live.get("text") or "").strip()

        def _norm_name(s: str) -> str:
            return " ".join((s or "").lower().split())

        if recorded_name and _norm_name(recorded_name) != _norm_name(live_name):
            logger.warning(
                f"  [CHECK_VERIFY] resolver picked {live_name!r} but "
                f"recording wanted {recorded_name!r} — re-resolving"
            )
            # Re-resolve via Playwright's role-name lookup with the
            # recorded accessible name as an exact-ish match. We allow
            # the `name` to be a substring (exact=False) because some
            # apps add invisible decoration to the label.
            try:
                candidate = page.get_by_role("radio", name=recorded_name, exact=False).first
                if (await candidate.count()) > 0:
                    loc = candidate
                    logger.info("  [CHECK_VERIFY] re-resolved via get_by_role(radio, name=...)")
            except Exception:
                pass

    # ---- Attempt 1: structured check/uncheck (works for real visible inputs) ----
    try:
        if desired:
            await loc.check(timeout=5000)
        else:
            await loc.uncheck(timeout=5000)
        # v4 POST-CHECK: for radios, verify the action actually
        # succeeded AND no sibling is wrongly checked.
        if is_radio_action and not await _verify_radio_outcome(
            page, loc, action, logger,
        ):
            logger.warning("  [CHECK_VERIFY] structured check passed but state mismatch; falling through")
        else:
            return True
    except Exception:
        pass

    # ---- Attempt 2: plain click on the resolved element ----
    try:
        await loc.click(timeout=5000)
        await asyncio.sleep(0.15)
        if await _is_checked() == desired:
            if is_radio_action and not await _verify_radio_outcome(
                page, loc, action, logger,
            ):
                logger.warning("  [CHECK_VERIFY] click succeeded but wrong sibling — falling through")
            else:
                logger.info("  [CHECK_OK] healed via click")
                return True
    except Exception:
        pass

    # ---- Attempt 3: force-click (bypasses visibility/actionability checks) ----
    try:
        await loc.click(force=True, timeout=5000)
        await asyncio.sleep(0.15)
        if await _is_checked() == desired:
            if is_radio_action and not await _verify_radio_outcome(
                page, loc, action, logger,
            ):
                logger.warning("  [CHECK_VERIFY] force-click succeeded but wrong sibling — falling through")
            else:
                logger.info("  [CHECK_OK] healed via force-click")
                return True
    except Exception:
        pass

    # ---- Attempt 4: click the parent <label> (common proxy wrapper) ----
    try:
        label_loc = loc.locator("xpath=./ancestor::label")
        if await label_loc.count() > 0:
            await label_loc.first.click(timeout=5000)
            await asyncio.sleep(0.15)
            if await _is_checked() == desired:
                logger.info("  [CHECK_OK] healed via parent label click")
                return True
    except Exception:
        pass

    # ---- Attempt 5: JS direct manipulation of the hidden input ----
    # Critical: we deliberately do NOT dispatch a synthetic ``click`` event
    # here. React/SPA forms (Facebook trademark, etc.) hook the label's
    # click handler to dispatch a sibling-flip on the *next* radio in the
    # group. Firing ``click`` from our heal would re-trigger that ghost
    # and undo the heal. ``change`` + ``input`` alone are enough to sync
    # the controlled-component state of every framework we care about.
    #
    # We also retry the heal up to 3 times with a 250 ms settle between
    # attempts, in case the ghost listener is keyed off ``change`` (rare,
    # but seen in some headless-ui radio-group implementations) and tries
    # to flip our pick back. Each iteration re-reads ``el.checked`` and
    # only re-sets when it has drifted.
    if hidden_input_name:
        try:
            for _attempt in range(3):
                ok = await page.evaluate(
                    # Same broadened selector as ``_is_checked``: drop the
                    # ``[type="..."]`` filter when the recorder didn't
                    # capture a hidden_input_type, so ARIA-only checkbox
                    # widgets are still healed.
                    """([name, val, inputType, desired]) => {
                        const sel = inputType
                            ? 'input[type="' + inputType + '"][name="' + name + '"]'
                            : 'input[name="' + name + '"]';
                        const inputs = document.querySelectorAll(sel);
                        for (const inp of inputs) {
                            if (val && inp.value !== val) continue;
                            inp.checked = desired;
                            inp.dispatchEvent(new Event('change', {bubbles: true}));
                            inp.dispatchEvent(new Event('input',  {bubbles: true}));
                            return true;
                        }
                        return false;
                    }""",
                    [hidden_input_name, radio_value or "", hidden_input_type or "", desired],
                )
                if not ok:
                    break
                await asyncio.sleep(0.25)
                state_ok = await page.evaluate(
                    """([name, val, inputType, desired]) => {
                        const sel = inputType
                            ? 'input[type="' + inputType + '"][name="' + name + '"]'
                            : 'input[name="' + name + '"]';
                        const inputs = document.querySelectorAll(sel);
                        for (const inp of inputs) {
                            if (val && inp.value !== val) continue;
                            return inp.checked === desired;
                        }
                        return false;
                    }""",
                    [hidden_input_name, radio_value or "", hidden_input_type or "", desired],
                )
                if state_ok:
                    logger.info("  [CHECK_OK] healed via JS direct set on hidden input")
                    return True
        except Exception:
            pass

    # ---- Attempt 6: find ANY nearby hidden radio/checkbox via JS and click its label ----
    try:
        ok = await page.evaluate(
            """(elOuter) => {
                // Walk up to find a label or container with a hidden input
                let node = elOuter;
                for (let i = 0; i < 5 && node; i++) {
                    const inp = node.querySelector('input[type="radio"], input[type="checkbox"]');
                    if (inp) {
                        inp.checked = true;
                        inp.dispatchEvent(new Event('change', {bubbles: true}));
                        inp.dispatchEvent(new Event('input',  {bubbles: true}));
                        return true;
                    }
                    node = node.parentElement;
                }
                return false;
            }""",
            await loc.element_handle(),
        )
        if ok:
            logger.info("  [CHECK_OK] healed via nearby hidden input JS set")
            return True
    except Exception as exc:
        logger.error(f"  [CHECK_ERR] {action.get('field_id')!r}: {exc}")
    return False


async def _do_select(page: Page, loc: Locator, action: dict, *, logger) -> bool:
    val = action.get("value")
    label = action.get("value_label")
    try:
        await loc.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    await _wait_for_actionable(loc, timeout_ms=4000)
    try:
        if label:
            await loc.select_option(label=str(label))
            return True
        if val is not None:
            try:
                await loc.select_option(value=str(val))
                return True
            except Exception:
                await loc.select_option(label=str(val))
                return True
    except Exception as exc:
        logger.error(f"  [SELECT_ERR] {action.get('field_id')!r}: {exc}")
        return False
    return False


async def _do_set_files(page: Page, loc: Locator, action: dict, *, logger) -> bool:
    files = action.get("files")
    if not files:
        logger.warning(f"  [FILE] no files given for {action.get('field_id')!r}")
        return False
    try:
        await loc.set_input_files(files)
        return True
    except Exception as exc:
        logger.error(f"  [FILE_ERR] {action.get('field_id')!r}: {exc}")
        return False


async def _do_combobox(
    page: Page, frame_loc_result: ResolveResult, action: dict, value: str, *, logger
) -> bool:
    """Custom dropdown: click the combobox, then click the option whose text == value.

    Improvements vs naive sleep-200ms:
      * scroll-into-view + actionable-wait on the trigger
      * actively wait for a listbox/menu/option to appear (up to 3s)
      * try get_by_role(option, exact=False), then text=, then partial-match.
    """
    trigger = frame_loc_result.locator
    try:
        await trigger.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    await _wait_for_actionable(trigger, timeout_ms=4000)
    try:
        await trigger.click(timeout=8000)
    except Exception as exc:
        logger.error(f"  [COMBO_OPEN_ERR] {action.get('field_id')!r}: {exc}")
        return False

    frame = frame_loc_result.frame or page.main_frame
    # Wait for SOME listbox/menu to render — up to 3s.
    appeared = False
    for _ in range(30):
        try:
            cnt = await frame.locator(
                "[role=listbox], [role=menu], [aria-haspopup='listbox']:not([aria-expanded='false'])"
            ).count()
            if cnt > 0:
                appeared = True
                break
        except Exception:
            break
        await asyncio.sleep(0.1)
    if not appeared:
        logger.debug(f"  [COMBO] no listbox detected — proceeding anyway")

    # Try several pick strategies in order.
    pick_attempts = [
        lambda: frame.get_by_role("option", name=str(value), exact=True),
        lambda: frame.get_by_role("option", name=str(value), exact=False),
        lambda: frame.locator(f'[role=listbox] >> text="{value}"').first,
        lambda: frame.locator(f'[role=option] >> text="{value}"').first,
        lambda: frame.get_by_text(str(value), exact=True).first,
    ]
    for make in pick_attempts:
        try:
            opt = make()
            if (await opt.count()) > 0:
                await opt.first.click(timeout=6000)
                return True
        except Exception:
            continue

    logger.error(f"  [COMBO_PICK_ERR] could not pick option {value!r}")
    return False


async def _do_contenteditable(page: Page, loc: Locator, value: str, *, logger) -> bool:
    try:
        await loc.click()
        await loc.evaluate(
            """(el, v) => {
                el.focus();
                document.execCommand && document.execCommand('selectAll', false, null);
                document.execCommand && document.execCommand('delete',    false, null);
                document.execCommand && document.execCommand('insertText', false, v);
                el.dispatchEvent(new Event('input',  {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
            }""",
            value,
        )
        return True
    except Exception as exc:
        logger.error(f"  [CE_ERR] {exc}")
        return False


# --------------------------------------------------------------------------------------
# Top-level: run one action
# --------------------------------------------------------------------------------------


async def run_action(
    page: Page,
    action: dict,
    *,
    ctx: Optional[Mapping[str, Any]] = None,
    threshold: float = 0.55,
    dry_run: bool = False,
    logger,
    max_attempts: int = 3,
) -> bool:
    kind = (action.get("kind") or "fill").lower()
    fid  = action.get("field_id", "<unnamed>")

    if kind == "wait":
        # Smart-wait kinds (set by the recorder when a navigation / network
        # idle / DOM burst is observed right after an action). Fall back to
        # the legacy duration_ms sleep when wait_kind is missing.
        wait_kind = (action.get("wait_kind") or "").strip().lower()
        timeout_ms = int(action.get("timeout_ms", 15000))
        if wait_kind in ("navigation", "domcontentloaded", "load", "networkidle"):
            tgt = "domcontentloaded" if wait_kind in ("navigation", "domcontentloaded") else wait_kind
            logger.info(f"[WAIT] for_load_state={tgt!r} (timeout {timeout_ms} ms)")
            if not dry_run:
                try:
                    await page.wait_for_load_state(tgt, timeout=timeout_ms)
                except Exception as exc:
                    logger.warning(f"  [WAIT] {tgt} timed out: {exc!r}")
            return True
        if wait_kind == "selector":
            sel = action.get("selector") or ""
            logger.info(f"[WAIT] selector={sel!r} (timeout {timeout_ms} ms)")
            if not dry_run and sel:
                try:
                    await page.wait_for_selector(sel, timeout=timeout_ms)
                except Exception as exc:
                    logger.warning(f"  [WAIT] selector {sel!r} timed out: {exc!r}")
            return True
        ms = int(action.get("duration_ms", 250))
        logger.info(f"[WAIT] {ms} ms")
        if not dry_run:
            await asyncio.sleep(ms / 1000.0)
        return True

    if kind == "press_keys":
        keys = action.get("keys") or []
        logger.info(f"[KEYS] {keys}")
        if not dry_run:
            for k in keys:
                await page.keyboard.press(k)
        return True

    logger.info(f"[ACTION] kind={kind} id={fid}")

    # Resolve the target — possibly retry, in case the page is mid-render.
    resolved: Optional[ResolveResult] = None
    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            resolved = await _safe_resolve(page, action, threshold=threshold, logger=logger)
            if resolved is not None:
                break
        except Exception as exc:
            last_err = exc
        await asyncio.sleep(0.3 * attempt)

    if not resolved:
        # v4 escalation: optional LLM-backed self-heal. The hook is a
        # pure no-op when ``OPENAI_API_KEY`` is not set, so importing
        # / wiring it has zero impact on default behaviour.
        try:
            from ai_features import ai_heal  # local import keeps module optional
        except Exception:
            ai_heal = None  # type: ignore[assignment]
        if ai_heal is not None:
            try:
                snippet = ""
                try:
                    snippet = await page.content()
                    if len(snippet) > 4000:
                        snippet = snippet[:4000]
                except Exception:
                    pass
                tried = ", ".join(
                    s.get("selector", "?")
                    for s in (action.get("selectors") or [])
                    if isinstance(s, dict)
                )[:300]
                heal = ai_heal(
                    fingerprint=action.get("fingerprint"),
                    last_selector=tried,
                    snippet=snippet,
                    failure_reason="resolver returned no candidate",
                )
                if heal.ok:
                    try:
                        ai_loc = page.locator(heal.selector)
                        if (await ai_loc.count()) > 0:
                            logger.info(
                                f"  [AI_HEAL] {heal.selector!r} ({heal.rationale})"
                            )
                            resolved = ResolveResult(
                                locator=ai_loc.first,
                                frame=page.main_frame,
                                strategy="ai_heal",
                                selector=heal.selector,
                                score=0.51,
                            )
                    except Exception as exc:
                        logger.debug(f"  [AI_HEAL] selector unusable: {exc}")
                else:
                    logger.debug(f"  [AI_HEAL] skipped: {heal.rationale}")
            except Exception as exc:
                logger.debug(f"  [AI_HEAL] error: {exc}")

    if not resolved:
        logger.warning(
            f"  [SKIP] {fid!r} — no candidate matched fingerprint "
            f"(last_err={last_err!r})"
        )
        return False

    logger.info(
        f"  [HIT ] {resolved.strategy} :: {resolved.selector!r}  score={resolved.score:.2f}"
    )

    if dry_run:
        value = _resolve_value(action, ctx)
        logger.info(f"  [DRY ] would {kind} {fid!r} = {value!r}")
        return True

    # Dispatch.
    if kind in ("fill", "input"):
        return await _do_fill(page, resolved.locator, action, _resolve_value(action, ctx), logger=logger)
    if kind == "click":
        return await _do_click(page, resolved.locator, action, logger=logger)
    if kind in ("check", "checkbox", "radio"):
        return await _do_check(page, resolved.locator, action, logger=logger)
    if kind in ("select", "dropdown"):
        return await _do_select(page, resolved.locator, action, logger=logger)
    if kind in ("file", "set_files"):
        return await _do_set_files(page, resolved.locator, action, logger=logger)
    if kind == "combobox":
        return await _do_combobox(page, resolved, action, str(_resolve_value(action, ctx)), logger=logger)
    if kind == "contenteditable":
        return await _do_contenteditable(page, resolved.locator, str(_resolve_value(action, ctx)), logger=logger)
    if kind == "otp_paste":
        code = await _fetch_otp_code(page, action, ctx, logger=logger)
        if code is None:
            logger.warning(f"  [SKIP] otp_paste {fid!r} — no code available")
            return False
        # Reuse _do_fill so the existing fill→type→paste self-heal applies.
        return await _do_fill(page, resolved.locator, action, code, logger=logger)
    if kind == "submit":
        # v4 multi-step forms keep all submit actions inline rather than
        # collapsing them into a single trailing ``cfg["submit"]``. Those
        # inline submits resolve to the actual button locator, so a click
        # is always the right thing to do here. The fall-through to
        # ``_do_fill`` was a no-op (submit buttons have no value) and
        # left multi-step forms stuck on the first page.
        return await _do_click(page, resolved.locator, action, logger=logger)

    # Default: best-effort fill.
    return await _do_fill(page, resolved.locator, action, _resolve_value(action, ctx), logger=logger)


# --------------------------------------------------------------------------------------
# Top-level: run a whole config
# --------------------------------------------------------------------------------------


async def run_actions(
    page: Page,
    actions: list[dict],
    *,
    ctx: Optional[Mapping[str, Any]] = None,
    threshold: float = 0.55,
    dry_run: bool = False,
    stop_on_fail: bool = False,
    logger,
    action_delay_ms: int = _DEFAULT_ACTION_DELAY_MS,
    action_jitter_ms: int = _DEFAULT_ACTION_JITTER_MS,
) -> tuple[int, int]:
    """Run a list of actions sequentially. Returns (filled, skipped).

    Between actions we sleep for ``action_delay_ms + rand(0..action_jitter_ms)``
    to mimic human pacing — important for FB-style anti-bot heuristics.

    After actions that *can* trigger navigation (any ``submit`` / ``click``
    that lands on a button-like element) we also call
    ``page.wait_for_load_state('domcontentloaded')`` with a short timeout.
    This is a defensive auto-wait that helps replay survive lazy-loading
    pages without the recording having to capture explicit waits. It
    short-circuits silently if the page never navigated — nothing breaks
    when the click did not cause a load.
    """
    filled = 0
    skipped = 0
    for i, action in enumerate(actions):
        ok = await run_action(
            page, action, ctx=ctx, threshold=threshold, dry_run=dry_run, logger=logger
        )
        if ok:
            filled += 1
        else:
            skipped += 1
            if stop_on_fail:
                break
        # Auto-settle after navigation-prone actions.
        if not dry_run and ok and _action_may_navigate(action):
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                # Page didn't navigate — expected for non-link clicks.
                pass
        # Pause before the next action (skip after the last one).
        if i < len(actions) - 1 and not dry_run:
            base = max(0, int(action_delay_ms))
            jitter = max(0, int(action_jitter_ms))
            extra = random.randint(0, jitter) if jitter > 0 else 0
            await asyncio.sleep((base + extra) / 1000.0)
    return filled, skipped


def _action_may_navigate(action: dict) -> bool:
    """Heuristic: does this action plausibly trigger navigation/page load?"""
    kind = (action.get("kind") or "").lower()
    if kind == "submit":
        return True
    if kind != "click":
        return False
    fp = action.get("fingerprint") or {}
    tag = (fp.get("tag") or "").lower()
    role = (fp.get("role") or "").lower()
    if tag == "a" or tag == "button":
        return True
    if role in ("link", "button"):
        return True
    # Anything that looks like a submit by text — heuristic from recorder.
    name = (fp.get("accessible_name") or "").lower()
    return any(k in name for k in ("submit", "send", "continue", "next", "gửi", "tiếp"))


__all__ = ["run_action", "run_actions"]
