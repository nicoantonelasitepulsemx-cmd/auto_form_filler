"""Multi-strategy form-field resolver.

Given a list of target specs (strategy + selector), tries each in order and
returns the first matching Playwright Locator.

Supported strategies (case-insensitive in `strategy` key):

  Priority 1 - id, name, css        → raw CSS selector
  Priority 2 - aria_label,
               aria_placeholder,
               aria_labelledby      → ARIA / accessibility attributes
  Priority 3 - label_text           → find <label> by text → associated input
  Priority 4 - placeholder          → raw CSS placeholder selector
  Priority 5 - nearby_text          → element containing text → nearest input
  Priority 6 - type, nth            → input type / nth-of-type
  Priority 7 - text, role,
               data_testid, value   → various structural / heuristic selectors

Any unknown strategy is treated as a raw CSS selector.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from playwright.async_api import Locator, Page

# Strategies whose `selector` is a raw CSS expression we can pass straight to page.locator.
_RAW_CSS_STRATEGIES = {
    "id",
    "name",
    "css",
    "placeholder",
    "aria_label",
    "aria_placeholder",
    "type",
    "value",
    "nth",
    "data_testid",
}


def _css_escape_id(value: str) -> str:
    """Minimal escaping for embedding an arbitrary id in a CSS selector."""
    return re.sub(r"([^\w-])", r"\\\1", value)


def _quote_text(text: str) -> str:
    """Escape a literal text value for use inside Playwright `:has-text("...")`."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


async def _exists(loc: Locator) -> bool:
    try:
        return (await loc.count()) > 0
    except Exception:
        return False


async def resolve_label_text(page: Page, text: str) -> Optional[Locator]:
    """Find an input/textarea/select associated with a <label> matching `text`."""
    quoted = _quote_text(text)

    # 1. `<label for="x">text</label>` → `#x`
    label = page.locator(f'label:has-text("{quoted}")').first
    if await _exists(label):
        try:
            for_id = await label.get_attribute("for")
        except Exception:
            for_id = None
        if for_id:
            input_loc = page.locator(f"#{_css_escape_id(for_id)}").first
            if await _exists(input_loc):
                return input_loc

        # 2. <label>text<input/></label>
        wrapped = label.locator("input, textarea, select").first
        if await _exists(wrapped):
            return wrapped

        # 3. nearest input/textarea/select following the label in document order
        following = label.locator(
            "xpath=following::*[self::input or self::textarea or self::select][1]"
        ).first
        if await _exists(following):
            return following

    # 4. fieldset/legend
    legend = page.locator(f'legend:has-text("{quoted}")').first
    if await _exists(legend):
        inner = legend.locator(
            "xpath=ancestor::fieldset[1]//*[self::input or self::textarea or self::select][1]"
        ).first
        if await _exists(inner):
            return inner

    return None


async def resolve_aria_labelledby(page: Page, label_id_or_text: str) -> Optional[Locator]:
    """Resolve via aria-labelledby — accepts either the id of the label element or its text."""
    direct = page.locator(f'[aria-labelledby="{label_id_or_text}"]').first
    if await _exists(direct):
        return direct

    # Treat the value as text inside an element whose id is then referenced.
    quoted = _quote_text(label_id_or_text)
    candidate = page.locator(
        f'xpath=//*[normalize-space(.)="{quoted}" and @id]'
    ).first
    if await _exists(candidate):
        try:
            cid = await candidate.get_attribute("id")
        except Exception:
            cid = None
        if cid:
            ref = page.locator(f'[aria-labelledby~="{cid}"]').first
            if await _exists(ref):
                return ref
    return None


async def resolve_nearby_text(page: Page, text: str) -> Optional[Locator]:
    """Heuristic: find element containing `text`, then locate nearest input after it."""
    quoted = _quote_text(text)
    candidates = [
        # Direct sibling
        f'xpath=//*[contains(normalize-space(.), "{quoted}")]'
        "/following-sibling::*[self::input or self::textarea or self::select][1]",
        # Within a shared parent
        f'xpath=//*[contains(normalize-space(.), "{quoted}")]'
        "/parent::*//*[self::input or self::textarea or self::select][1]",
        # Anywhere following in document order
        f'xpath=//*[contains(normalize-space(.), "{quoted}")]'
        "/following::*[self::input or self::textarea or self::select][1]",
    ]
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            if await _exists(loc):
                return loc
        except Exception:
            continue
    return None


async def resolve_role(page: Page, selector: str) -> Optional[Locator]:
    """Strategy `role` — selector is `role[:name]`, e.g. `button:Submit`."""
    if ":" in selector:
        role, name = selector.split(":", 1)
        loc = page.get_by_role(role.strip(), name=name.strip()).first
    else:
        loc = page.get_by_role(selector.strip()).first
    if await _exists(loc):
        return loc
    return None


async def resolve_text(page: Page, text: str) -> Optional[Locator]:
    loc = page.get_by_text(text, exact=False).first
    if await _exists(loc):
        return loc
    return None


async def try_strategy(
    page: Page, strategy: str, selector: str
) -> Optional[Locator]:
    """Try a single (strategy, selector) and return a Locator on success."""
    s = (strategy or "").lower().strip()
    try:
        if s in _RAW_CSS_STRATEGIES:
            loc = page.locator(selector).first
            return loc if await _exists(loc) else None
        if s == "label_text":
            return await resolve_label_text(page, selector)
        if s == "aria_labelledby":
            return await resolve_aria_labelledby(page, selector)
        if s == "nearby_text":
            return await resolve_nearby_text(page, selector)
        if s == "role":
            return await resolve_role(page, selector)
        if s == "text":
            return await resolve_text(page, selector)
        # Unknown strategy: treat selector as raw CSS.
        loc = page.locator(selector).first
        return loc if await _exists(loc) else None
    except Exception:
        return None


async def resolve_target(
    page: Page,
    targets: list[dict[str, Any]],
    logger=None,
) -> tuple[Optional[Locator], Optional[dict[str, Any]]]:
    """Walk through the target list in order; return (locator, target_used)."""
    for t in targets:
        strategy = t.get("strategy", "css")
        selector = t.get("selector", "")
        loc = await try_strategy(page, strategy, selector)
        if loc is not None:
            if logger is not None:
                logger.debug(f"  [HIT ] {strategy:>16} :: {selector!r}")
            return loc, t
        if logger is not None:
            logger.debug(f"  [MISS] {strategy:>16} :: {selector!r}")
    return None, None
