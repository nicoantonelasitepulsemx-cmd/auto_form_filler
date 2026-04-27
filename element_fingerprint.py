"""element_fingerprint.py — capture & verify a stable DOM-element fingerprint.

A fingerprint is the data we capture from an element at *record time* so the
resolver can confirm at *replay time* that the locator it resolved actually
points at the same element. If the fingerprint mismatches, we reject the
locator and move on to the next selector strategy — preventing the
silent-wrong-target class of bugs.

A fingerprint is intentionally redundant: any one field can drift between
record and replay, but the *combination* of role + accessible name + tag
+ a few key attributes is virtually never wrong.

JSON shape::

    {
        "tag":            "input",
        "type":           "email",
        "role":           "textbox",
        "accessible_name":"Email address",
        "attributes":     {"name": "email", "data-testid": "email-input"},
        "text_content":   "",
        "frame_chain":    ["top", "https://example.com/embed.html"],
        "neighbour_text": "Email address Password Forgot password?",
        "viewport_hint":  {"x_pct": 0.32, "y_pct": 0.41, "w_pct": 0.40, "h_pct": 0.04}
    }

`viewport_hint` is the bounding-box of the element expressed as fractions
of the viewport. It is a **last-resort tiebreaker** — if two candidates
match every textual fingerprint field, we prefer the one whose centre
is closer to the recorded centre.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from playwright.async_api import Frame, Locator


# ---------------- record-time JS that runs in the browser ----------------

# Returns a fingerprint dict for the element passed in. Frame-chain is
# attached on the Python side because JS doesn't know its parent frame URL.
FINGERPRINT_JS = r"""
(el) => {
  if (!el) return null;
  const tag = el.tagName.toLowerCase();

  const accessibleName = (() => {
    if (el.getAttribute("aria-label")) return el.getAttribute("aria-label").trim();
    const labelledby = el.getAttribute("aria-labelledby");
    if (labelledby) {
      const ref = document.getElementById(labelledby);
      if (ref) return (ref.innerText || ref.textContent || "").trim();
    }
    if (el.id) {
      const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lab) return (lab.innerText || lab.textContent || "").trim();
    }
    let p = el.parentElement;
    while (p && p !== document.body) {
      if (p.tagName === "LABEL") return (p.innerText || p.textContent || "").trim();
      p = p.parentElement;
    }
    if (el.getAttribute("placeholder")) return el.getAttribute("placeholder").trim();
    if (el.getAttribute("title")) return el.getAttribute("title").trim();
    return "";
  })();

  // Just the few attrs that meaningfully help disambiguate; we deliberately
  // avoid huge style/className blobs that change between record and replay.
  const interesting = ["id","name","type","role","data-testid","aria-label",
                       "placeholder","title","autocomplete"];
  const attributes = {};
  for (const k of interesting) {
    const v = el.getAttribute(k);
    if (v != null) attributes[k] = v;
  }

  // Neighbour text: a flat snippet of the surrounding 200 chars of text content.
  const neighbour = (() => {
    const parent = el.closest("form, fieldset, section, div") || el.parentElement;
    if (!parent) return "";
    const txt = (parent.innerText || parent.textContent || "")
      .replace(/\s+/g, " ").trim();
    return txt.slice(0, 200);
  })();

  // Bounding box as % of the visual viewport.
  const r = el.getBoundingClientRect();
  const vw = window.innerWidth || 1, vh = window.innerHeight || 1;
  const viewport_hint = (r && r.width >= 0 && r.height >= 0) ? {
    x_pct: +(r.left / vw).toFixed(3),
    y_pct: +(r.top  / vh).toFixed(3),
    w_pct: +(r.width  / vw).toFixed(3),
    h_pct: +(r.height / vh).toFixed(3),
  } : null;

  return {
    tag,
    type: (el.getAttribute("type") || "").toLowerCase() || null,
    role: el.getAttribute("role") || null,
    accessible_name: accessibleName,
    attributes,
    text_content: ((el.innerText || el.textContent || "").trim().slice(0, 120)) || "",
    neighbour_text: neighbour,
    viewport_hint,
  };
};
"""


async def fingerprint_element(loc: Locator, frame_chain: Optional[list[str]] = None) -> Optional[dict]:
    """Capture a fingerprint from a Playwright Locator. Frame chain comes from caller."""
    try:
        fp = await loc.evaluate(FINGERPRINT_JS)
    except Exception:
        return None
    if not fp:
        return None
    if frame_chain is not None:
        fp["frame_chain"] = list(frame_chain)
    return fp


async def fingerprint_handle(handle, frame_chain: Optional[list[str]] = None) -> Optional[dict]:
    """Same as fingerprint_element but for an ElementHandle (used by the
    recorder, which gets handles from a JS-side queue)."""
    try:
        fp = await handle.evaluate(FINGERPRINT_JS)
    except Exception:
        return None
    if not fp:
        return None
    if frame_chain is not None:
        fp["frame_chain"] = list(frame_chain)
    return fp


# ---------------- compare two fingerprints, score 0..1 ----------------


def _norm(s: Any) -> str:
    return (s or "").strip().lower() if isinstance(s, str) else ""


def fingerprint_score(recorded: dict, current: dict) -> float:
    """Score how confident we are that `current` is the same element as `recorded`.

    Returns a number in [0, 1]. The replay-time threshold is configurable but
    defaults to 0.55 — we want to err on the side of "ask the next strategy"
    rather than fill the wrong field.
    """
    if not recorded or not current:
        return 0.0

    score = 0.0
    weight_sum = 0.0

    def weigh(weight: float, hit: bool) -> None:
        nonlocal score, weight_sum
        weight_sum += weight
        if hit:
            score += weight

    # Tag + type form a strict precondition. A wrong tag is almost always the
    # wrong element (input vs textarea vs button), so we still SCORE them
    # rather than reject outright — to handle React's `<input type='text'>` →
    # `<input type='email'>` after validation kicks in.
    weigh(2.0, _norm(recorded.get("tag")) == _norm(current.get("tag")))
    weigh(1.0, _norm(recorded.get("type")) == _norm(current.get("type")))

    # Role: very stable for ARIA-conformant apps.
    weigh(1.5, _norm(recorded.get("role")) == _norm(current.get("role")))

    # Accessible name: the strongest single signal.
    weigh(3.0, _norm(recorded.get("accessible_name")) == _norm(current.get("accessible_name")))

    # Attributes: count exact matches on the interesting subset.
    rec_attrs = recorded.get("attributes") or {}
    cur_attrs = current.get("attributes") or {}
    common_keys = set(rec_attrs.keys()) | set(cur_attrs.keys())
    if common_keys:
        matches = sum(1 for k in common_keys if _norm(rec_attrs.get(k)) == _norm(cur_attrs.get(k)))
        weigh(2.0, matches / len(common_keys) >= 0.7)

    # Neighbour text: substring overlap. We only need a chunk to match because
    # menus/buttons around a field rarely change in their entirety.
    rec_n = _norm(recorded.get("neighbour_text"))
    cur_n = _norm(current.get("neighbour_text"))
    if rec_n and cur_n:
        # Fast & cheap: check whether half the recorded neighbour text is
        # present in the current neighbour text.
        chunk = rec_n[: max(40, len(rec_n) // 2)]
        weigh(1.5, chunk in cur_n)

    # Viewport hint: distance between recorded centre and current centre.
    rec_v = recorded.get("viewport_hint")
    cur_v = current.get("viewport_hint")
    if rec_v and cur_v:
        dx = (rec_v["x_pct"] + rec_v["w_pct"] / 2) - (cur_v["x_pct"] + cur_v["w_pct"] / 2)
        dy = (rec_v["y_pct"] + rec_v["h_pct"] / 2) - (cur_v["y_pct"] + cur_v["h_pct"] / 2)
        # Within 10% of the viewport on both axes is "same place".
        weigh(0.5, (dx * dx + dy * dy) ** 0.5 < 0.10)

    return score / weight_sum if weight_sum else 0.0


def fingerprint_match(recorded: dict, current: dict, threshold: float = 0.55) -> bool:
    """Convenience boolean wrapper around `fingerprint_score`."""
    return fingerprint_score(recorded, current) >= threshold


def to_json(fp: Optional[dict]) -> str:
    """Pretty-print a fingerprint for logs."""
    return json.dumps(fp or {}, ensure_ascii=False, sort_keys=True)


__all__ = [
    "FINGERPRINT_JS",
    "fingerprint_element",
    "fingerprint_handle",
    "fingerprint_match",
    "fingerprint_score",
    "to_json",
]
