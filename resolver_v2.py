"""resolver_v2.py — frame-aware, fingerprint-verifying locator resolver.

Difference vs `target_resolver.py`:

  * Walks the **frame chain** so iframe-based forms work.
  * Tries every selector strategy in *weighted* priority order.
  * For every candidate locator it computes the live fingerprint and
    compares it to the recorded one — if the score is below threshold
    the candidate is rejected and we move to the next strategy.
  * When two strategies tie, prefer the one with higher base weight.
  * Always picks the highest-scoring locator across ALL strategies, not
    the first one that "kind of" matches.

Selector strategies (ordered by default weight, highest first):

    weight  strategy
    100     data_testid           input[data-testid='email-input']
     95     stable_id             input[id='email']
     90     name                  input[name='email']
     85     role_name             role=textbox + accessible name
     80     aria_label            input[aria-label='Email address']
     75     aria_placeholder      input[aria-placeholder='Email']
     70     placeholder           input[placeholder='Email']
     65     label_text            <label> resolution chain
     55     css                   raw CSS path (snapshot from DOM)
     45     xpath                 raw XPath
     35     nearby_text           heuristic
     20     text                  page.get_by_text
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Optional

from playwright.async_api import Frame, FrameLocator, Locator, Page

from element_fingerprint import (
    FINGERPRINT_JS,
    fingerprint_match,
    fingerprint_score,
)


_STRATEGY_WEIGHT: dict[str, int] = {
    "data_testid": 100,
    "id":           95,
    "stable_id":    95,
    "name_value":   92,   # [name="..."][value="..."] for radio/checkbox siblings
    "name":         90,
    "role_name":    85,
    "role":         80,
    "aria_label":   80,
    "aria_placeholder": 75,
    "placeholder":  70,
    "label_text":   65,
    "aria_labelledby": 60,
    "css":          55,
    "type":         50,
    "value":        50,
    "xpath":        45,
    "nth":          40,
    "nearby_text":  35,
    "text":         20,
}


@dataclass
class ResolveResult:
    locator: Locator
    frame: Optional[Frame]
    strategy: str
    selector: str
    score: float


# --------------------------------------------------------------------------------------
# Frame-chain walk
# --------------------------------------------------------------------------------------


def _walk_frame_chain(page: Page, frame_chain: Optional[list[str]]) -> Optional[Frame]:
    """Return the frame referenced by the recorded chain.

    A10 fix: the previous implementation was strict — every entry in
    the chain had to match exactly OR the call returned ``None`` (and
    the caller fell back to ``page.main_frame``). Real iframes change
    URLs (auth flow, OAuth popup tabs, captcha refresh) so the
    intermediate hops often don't match even when the deepest frame is
    still findable.

    New behaviour:
      1. Try the strict walk first \u2014 best signal when nothing has
         changed.
      2. If that fails, fall back to looking for any frame on the page
         whose URL/name matches the LAST chain entry (the most specific
         signal). This recovers when the deepest frame is intact but its
         parents have rotated. The match must be a descendant of
         ``page.main_frame``, which it always is.
      3. Otherwise return ``None`` so the caller can decide (typically
         falls back to ``page.main_frame``).
    """
    if not frame_chain or frame_chain == ["top"]:
        return page.main_frame

    # 1. Strict walk: drill down child_frames one level at a time.
    def _match(entry: str, frames: list[Frame]) -> Optional[Frame]:
        for f in frames:
            url = f.url or ""
            name = f.name or ""
            if entry and (entry in url or entry == name):
                return f
        return None

    current: Frame = page.main_frame
    strict_ok = True
    for entry in frame_chain[1:]:  # skip "top"
        match = _match(entry, current.child_frames)
        if match is None:
            strict_ok = False
            break
        current = match
    if strict_ok:
        return current

    # 2. Loose fallback: find any frame on the page whose URL or name
    #    matches the LAST recorded chain entry. This is the most
    #    specific entry in the chain so collisions are unlikely.
    last_entry = frame_chain[-1]
    if last_entry and last_entry != "top":
        match = _match(last_entry, page.frames)
        if match is not None:
            return match

    # 3. Penultimate fallback: walk from the end, picking the first
    #    chain entry that resolves to a unique descendant. This is
    #    deliberately permissive so partial matches still help.
    for entry in reversed(frame_chain[1:-1]):
        if not entry:
            continue
        match = _match(entry, page.frames)
        if match is not None:
            return match

    return None


# --------------------------------------------------------------------------------------
# Strategy → Locator
# --------------------------------------------------------------------------------------


async def _exists(loc: Locator) -> bool:
    try:
        return (await loc.count()) > 0
    except Exception:
        return False


async def _strategy_locators(
    frame: Frame, strategy: str, selector: str
) -> list[Locator]:
    """Return ALL locators matching a given strategy (not just `.first`)."""
    s = (strategy or "").lower().strip()

    def all_of(loc: Locator) -> Locator:
        # Locator with multiple matches; we'll iterate by index later.
        return loc

    if s in ("css", "id", "name", "name_value", "type", "value", "nth", "data_testid",
             "placeholder", "aria_label", "aria_placeholder"):
        return [frame.locator(selector)]

    if s == "stable_id":
        return [frame.locator(f"#{selector}")]

    if s == "xpath":
        sel = selector if selector.startswith(("xpath=", "//")) else f"xpath={selector}"
        return [frame.locator(sel)]

    if s == "role":
        if ":" in selector:
            role, name = selector.split(":", 1)
            return [frame.get_by_role(role.strip(), name=name.strip())]
        return [frame.get_by_role(selector.strip())]

    if s == "role_name":
        # selector is "role|name" (we don't reuse `:` because names contain colons)
        if "|" in selector:
            role, name = selector.split("|", 1)
            return [frame.get_by_role(role.strip(), name=name.strip())]
        return [frame.get_by_role(selector.strip())]

    if s == "label_text":
        return [
            frame.get_by_label(selector, exact=False),
            frame.locator(
                "xpath=//label[contains(normalize-space(.), \"" +
                selector.replace('"', '\\"') + "\")]"
                "/following::*[self::input or self::textarea or self::select][1]"
            ),
        ]

    if s == "aria_labelledby":
        return [
            frame.locator(f'[aria-labelledby="{selector}"]'),
            frame.locator(
                "xpath=//*[normalize-space(.)=\"" + selector.replace('"', '\\"') +
                "\" and @id]/@id"
            ),
        ]

    if s == "nearby_text":
        quoted = selector.replace('"', '\\"')
        return [
            frame.locator(
                f'xpath=//*[contains(normalize-space(.), "{quoted}")]'
                "/following-sibling::*[self::input or self::textarea or self::select][1]"
            ),
            frame.locator(
                f'xpath=//*[contains(normalize-space(.), "{quoted}")]'
                "/parent::*//*[self::input or self::textarea or self::select][1]"
            ),
        ]

    if s == "text":
        return [frame.get_by_text(selector, exact=False)]

    # unknown strategy → treat as raw CSS
    return [frame.locator(selector)]


async def _live_fingerprint(loc: Locator) -> Optional[dict]:
    try:
        return await loc.evaluate(FINGERPRINT_JS)
    except Exception:
        return None


# --------------------------------------------------------------------------------------
# Public resolve
# --------------------------------------------------------------------------------------


async def resolve(
    page: Page,
    *,
    selectors: list[dict[str, Any]],
    fingerprint: Optional[dict] = None,
    frame_chain: Optional[list[str]] = None,
    threshold: float = 0.55,
    logger=None,
    timeout_per_strategy_s: float = 0.6,
    max_candidates_per_strategy: int = 5,
) -> Optional[ResolveResult]:
    """Resolve the recorded element on the live page.

    `selectors` is a list of ``{"strategy": str, "selector": str, "weight": int?}``.
    `fingerprint` is the recorded element fingerprint; if provided, candidate
    locators whose fingerprint scores below `threshold` are discarded.

    Returns the best-scoring `ResolveResult`, or None if nothing reached threshold.
    """
    frame = _walk_frame_chain(page, frame_chain) or page.main_frame

    # Sort strategies by weight (record-time weight wins, fall back to default).
    def w(t: dict) -> int:
        if "weight" in t:
            return int(t["weight"])
        return _STRATEGY_WEIGHT.get((t.get("strategy") or "").lower(), 30)

    ordered = sorted(selectors, key=w, reverse=True)

    best: Optional[ResolveResult] = None
    for t in ordered:
        strategy = (t.get("strategy") or "").lower()
        selector = t.get("selector") or ""
        if not selector:
            continue

        try:
            candidate_groups = await _strategy_locators(frame, strategy, selector)
        except Exception as exc:
            if logger:
                logger.debug(f"  [RESOLVE_ERR] {strategy} :: {exc!r}")
            continue

        for group in candidate_groups:
            try:
                count = await asyncio.wait_for(
                    group.count(), timeout=timeout_per_strategy_s
                )
            except (asyncio.TimeoutError, Exception):
                count = 0
            if count == 0:
                if logger:
                    logger.debug(f"  [MISS] {strategy:>16} :: {selector!r}")
                continue

            for i in range(min(count, max_candidates_per_strategy)):
                cand = group.nth(i)
                live_fp = None
                if fingerprint:
                    try:
                        live_fp = await asyncio.wait_for(
                            _live_fingerprint(cand),
                            timeout=timeout_per_strategy_s,
                        )
                    except (asyncio.TimeoutError, Exception):
                        live_fp = None
                    score = fingerprint_score(fingerprint, live_fp or {})
                else:
                    score = w(t) / 100.0

                if logger:
                    logger.debug(
                        f"  [CAND] {strategy:>16}#{i} :: {selector!r}  score={score:.2f}"
                    )

                if fingerprint and score < threshold:
                    continue

                # Combine fingerprint score with strategy weight so a
                # data_testid hit at score 0.6 outranks a nearby_text hit
                # at score 0.7 only if both clear threshold.
                combined = score + (w(t) / 1000.0)
                if best is None or combined > best.score:
                    best = ResolveResult(
                        locator=cand,
                        frame=frame,
                        strategy=strategy,
                        selector=selector,
                        score=combined,
                    )

        # Early stop: if we have an extremely confident match, no need to
        # keep trying weaker strategies.
        if best is not None and best.score >= 0.95:
            break

    if logger:
        if best:
            logger.debug(
                f"  [PICK] {best.strategy} :: {best.selector!r} (score={best.score:.2f})"
            )
        else:
            logger.debug("  [PICK] none — no candidate cleared threshold")
    return best


__all__ = ["resolve", "ResolveResult"]
