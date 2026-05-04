"""Sanitize a recording produced before the A15 ``isTrusted`` filter
landed.

Background
----------
React / SPA forms (Facebook trademark report form, GDPR consent flows,
Material-UI radio groups, etc.) re-render their radio + checkbox groups
on every state transition. Each re-render dispatches a synthetic
``change`` event whose ``Event.isTrusted`` is ``false`` because it did
not originate from a user gesture. The recorder used to ship those
events as ``check`` actions, polluting the recording with "ghost"
entries that picked the WRONG radio at replay time.

The recorder now drops ``isTrusted === false`` events at capture time
(see ``recorder_v2.OVERLAY_JS``). This module provides an offline
sanitizer for recordings captured BEFORE that fix landed so users
don't have to re-record long forms.

Strategy
~~~~~~~~
Walk the action list in order. For each ``check`` action build a key:

* radios (``_hidden_input_type == "radio"`` and ``_hidden_input_name``
  set): the key is the **group name**. Radios are mutually exclusive
  so the user's intended pick is the FIRST ``check`` for that group.
  Subsequent ghosts are dropped.

* checkboxes (``_hidden_input_type == "checkbox"`` and
  ``_hidden_input_name`` set): the key is ``(name, value, checked)``.
  This preserves multi-select groups (e.g. Facebook ``content_type[]``)
  and toggle-then-untoggle sequences (each unique combo gets one
  keeper) while still dropping duplicate ghosts.

* ``role=...`` custom widgets (no hidden input): key is
  ``(field_id, radio_value, checked)`` — same intuition.

All other action kinds (``fill``, ``submit``, ``select``,
``set_files``, ``combobox``…) are passed through unchanged.

Usage
-----
::

    python3 clean_recording.py tm1.json -o tm1_fixed.json
    python3 clean_recording.py tm1.json --in-place
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Tuple


def sanitize_actions(actions: Iterable[dict]) -> Tuple[list[dict], int]:
    """Return ``(cleaned_actions, dropped_count)``.

    ``actions`` may be any iterable; the input order is preserved.
    """
    cleaned: list[dict] = []
    seen_radio_groups: set[str] = set()
    seen_checkbox_keys: set[tuple] = set()
    seen_role_keys: set[tuple] = set()
    dropped = 0

    for a in actions:
        if not isinstance(a, dict) or a.get("kind") != "check":
            cleaned.append(a)
            continue

        rtype = a.get("_hidden_input_type")
        rname = a.get("_hidden_input_name")
        rv = a.get("radio_value")
        chk = a.get("checked")
        fid = a.get("field_id")

        if rtype == "radio" and rname:
            if rname in seen_radio_groups:
                dropped += 1
                continue
            seen_radio_groups.add(rname)
            cleaned.append(a)
            continue

        if rtype == "checkbox" and rname:
            key = (rname, rv, chk)
            if key in seen_checkbox_keys:
                dropped += 1
                continue
            seen_checkbox_keys.add(key)
            cleaned.append(a)
            continue

        # role=checkbox / role=radio custom widget — no hidden input
        # info. Use the resolved field_id as a fallback group key.
        key = (fid, rv, chk)
        if key in seen_role_keys:
            dropped += 1
            continue
        seen_role_keys.add(key)
        cleaned.append(a)

    return cleaned, dropped


def sanitize_config(cfg: dict) -> Tuple[dict, dict]:
    """Sanitize a top-level recording dict.

    Returns ``(new_cfg, stats)`` where ``stats`` summarises what was
    dropped from each list-of-actions field.
    """
    out = dict(cfg)
    stats: dict[str, Any] = {"actions_dropped": 0, "submits_dropped": 0}

    if isinstance(cfg.get("actions"), list):
        cleaned, dropped = sanitize_actions(cfg["actions"])
        out["actions"] = cleaned
        stats["actions_dropped"] = dropped
        stats["actions_in"] = len(cfg["actions"])
        stats["actions_out"] = len(cleaned)

    # ``submits`` (legacy multi-step submit list) and ``submit`` (single
    # submit dict) only contain submit-kind actions; check-style ghosts
    # never go there. We pass them through but report counts for sanity.
    if isinstance(cfg.get("submits"), list):
        stats["submits_in"] = len(cfg["submits"])

    return out, stats


def _cli(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=(
            "Strip ghost check actions from a recording. "
            "Useful for recordings captured before the recorder's "
            "isTrusted=false guard landed."
        )
    )
    p.add_argument("input", help="Path to the recorded JSON file.")
    p.add_argument(
        "-o", "--output",
        help="Output path. Default: <input>.cleaned.json",
        default=None,
    )
    p.add_argument(
        "--in-place",
        action="store_true",
        help="Overwrite the input file. Mutually exclusive with -o.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be dropped but do not write anything.",
    )
    args = p.parse_args(argv)

    if args.output and args.in_place:
        p.error("--output and --in-place are mutually exclusive")

    src = Path(args.input)
    if not src.is_file():
        print(f"error: input file not found: {src}", file=sys.stderr)
        return 2

    cfg = json.loads(src.read_text(encoding="utf-8"))
    cleaned, stats = sanitize_config(cfg)

    if args.in_place:
        dst = src
    elif args.output:
        dst = Path(args.output)
    else:
        dst = src.with_suffix(".cleaned" + src.suffix)

    print(
        f"[clean] {src.name}: actions {stats.get('actions_in', '?')}"
        f" → {stats.get('actions_out', '?')}"
        f"  (dropped {stats['actions_dropped']} ghost check{'s' if stats['actions_dropped'] != 1 else ''})"
    )

    if args.dry_run:
        print(f"[dry-run] would write to {dst}")
        return 0

    dst.write_text(json.dumps(cleaned, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[clean] wrote {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
