"""auto_template.py — heuristic variable extraction for recorded configs.

After a user records a form-filling session, most of the captured ``fill``
values are personal data the user does NOT want hard-coded into the
config (email address, phone number, dates, etc.). For the config to be
reusable across accounts, those values need to become *template
variables* like ``{email}``, ``{phone}``, … and the per-account values
need to live in a separate place.

This module scans a list of recorded actions and:

  * **detects** values that match strong patterns (email, international
    phone, ISO date, full name) — only these are replaced. We err on
    the side of caution: a value that's only weakly matched stays
    literal rather than risk turning ``"owner"`` into ``{role}``.
  * **rewrites** the matching action by adding a ``value_template`` key
    while keeping the original ``value`` as a fallback. The replay
    engine prefers ``value_template`` when a context dict is supplied,
    so existing replays without ctx continue to use the literal value
    and nothing breaks.
  * **returns** a side dict ``{var_name: original_value}`` so the user
    has an obvious starting point when wiring up accounts.

The heuristics are intentionally conservative; users can pass
``auto_template=False`` to skip this entirely.
"""
from __future__ import annotations

import re
from typing import Any


_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$"
)
# International or local phone: optional +, country code, digits with
# spaces/dashes/dots, total 7-15 digits.
_PHONE_RE = re.compile(
    r"^\+?\d[\d\s\-\.]{6,18}\d$"
)
# YYYY-MM-DD or YYYY/MM/DD
_DATE_RE = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}$")
# Full name heuristic. We require *3+ capitalised words* (or an explicit
# field_id hint, handled separately) — two-word strings like "Acme Co" or
# "United States" are too ambiguous to templatise. 3+ capitalised words is
# overwhelmingly a person's name in EN/VI/ES locales.
_NAME_RE = re.compile(
    r"^[A-Z\u00C0-\u1EF9][a-z\u00C0-\u1EF9'\-]{1,}"
    r"(?:\s+[A-Z\u00C0-\u1EF9][a-z\u00C0-\u1EF9'\-]{1,}){2,4}$"
)
# When the field_id literally says "name", we accept 2-word names too.
_NAME_FIELD_RE = re.compile(r"name|fullname|first_?name|last_?name", re.IGNORECASE)
_NAME_RE_LOOSE = re.compile(
    r"^[A-Z\u00C0-\u1EF9][a-z\u00C0-\u1EF9'\-]{1,}"
    r"(?:\s+[A-Z\u00C0-\u1EF9][a-z\u00C0-\u1EF9'\-]{1,}){1,4}$"
)
_URL_RE = re.compile(r"^https?://[^\s]+$")


def _classify(value: str, field_id: str = "") -> str | None:
    """Return the canonical variable name for ``value`` or None.

    ``field_id`` is the recorded action's field_id; when it looks like a
    name field (``name``, ``full_name``, ``first_name``, …) we accept a
    looser 2-word capitalised value, otherwise we require 3+ words to
    avoid mis-classifying things like "Acme Co".
    """
    if not isinstance(value, str):
        return None
    v = value.strip()
    if not v or len(v) < 3:
        return None
    if _EMAIL_RE.match(v):
        return "email"
    if _URL_RE.match(v):
        return "url"
    # Date BEFORE phone: 1990-05-12 also matches the loose phone regex.
    if _DATE_RE.match(v):
        return "date"
    # Phones: also require at least 7 digits to avoid catching e.g. "12345"
    if _PHONE_RE.match(v) and sum(c.isdigit() for c in v) >= 7:
        return "phone"
    if _NAME_RE.match(v) and " " in v:
        return "full_name"
    if field_id and _NAME_FIELD_RE.search(field_id) and _NAME_RE_LOOSE.match(v):
        return "full_name"
    return None


def _unique_var(base: str, used: dict[str, str]) -> str:
    """Pick a non-colliding variable name for ``base``.

    If ``base`` already maps to the same value, reuse it (so two ``email``
    fills with the same address share ``{email}``). If it maps to a
    different value, append a numeric suffix.
    """
    if base not in used:
        return base
    # Already reserved — find the lowest free suffix.
    i = 2
    while f"{base}_{i}" in used:
        i += 1
    return f"{base}_{i}"


def extract_variables(
    actions: list[dict],
    *,
    seed: dict[str, str] | None = None,
) -> tuple[list[dict], dict[str, str]]:
    """Return (rewritten_actions, variables).

    ``variables`` is a flat dict of ``{var_name: original_value}`` ready
    to drop into the config or split into per-account JSON. Rewritten
    actions get a new ``value_template`` key set to ``"{var_name}"`` so
    the replay engine substitutes from a ctx mapping at run time.

    ``seed`` is an optional pre-existing mapping (e.g. when re-running
    auto-template on a partially templatised config) — the new mapping
    starts from it so existing var names are preserved.
    """
    variables: dict[str, str] = dict(seed or {})
    # Reverse lookup: value -> var_name, so duplicate values share a var.
    by_value: dict[str, str] = {v: k for k, v in variables.items()}
    out: list[dict] = []

    for action in actions:
        kind = (action.get("kind") or "").lower()
        if kind not in ("fill", "input", "contenteditable"):
            out.append(action)
            continue
        value = action.get("value")
        fid = action.get("field_id") or ""
        cat = _classify(value, fid) if isinstance(value, str) else None
        if cat is None:
            out.append(action)
            continue
        # Reuse existing variable for the same value, otherwise mint a new one.
        var = by_value.get(value)
        if var is None:
            var = _unique_var(cat, variables)
            variables[var] = value
            by_value[value] = var
        new_action = dict(action)
        new_action["value_template"] = f"{{{var}}}"
        # Preserve `value` as a literal fallback so a replay without ctx
        # still works.
        out.append(new_action)
    return out, variables


__all__ = ["extract_variables"]
