"""v4 add-on: structural diff between two recorder JSON files.

Catches "form drift" early — when Facebook / a vendor form rearranges
fields or renames a `data-testid`, an old recording silently mistargets
elements at replay time. Running ``recording_diff`` between yesterday's
known-good recording and today's snapshot tells you *exactly* which
actions look risky before you submit hundreds of accounts through them.

What it compares
~~~~~~~~~~~~~~~~
* Length and order of the ``actions`` array.
* For each pair of actions matched by stable identity (``frame`` chain
  + accessible name + role + index-within-form), the score of their
  fingerprint similarity. We borrow the same weights
  ``element_fingerprint`` uses at replay time so the threshold is
  semantically meaningful: a score under ~0.6 means the resolver
  would now be ambiguous on this field.
* Field deltas: missing in old/new, type change, value-template change.

What it deliberately does NOT compare
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
* Screenshot pixel-level diffs (use ``vision_match`` for that).
* Timing / jitter values.
* Anything that's expected to drift between sessions (timestamps,
  ``recorded_at`` metadata, generated session ids).

Output
~~~~~~
:func:`diff_recordings` returns a :class:`DiffReport` dataclass; call
:meth:`DiffReport.to_dict` for JSON serialisation or
:meth:`DiffReport.format_text` for a readable, colour-friendly
summary suitable for a CI log.
"""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "DiffReport",
    "ActionDiff",
    "diff_recordings",
    "load_recording",
]


# --------------------------------------------------------------------------- types


@dataclass
class ActionDiff:
    """Pairwise diff for one action.

    ``status`` values:
      * ``"unchanged"`` — fingerprint score > ``score_threshold`` and
        every interesting field is identical.
      * ``"drifted"``   — same identity but some fingerprint signal
        weakened (e.g. accessible_name changed, classes rotated).
      * ``"changed"``   — kind, value, or selector differs.
      * ``"added"``     — only present in ``new``.
      * ``"removed"``   — only present in ``old``.
    """
    index_old: Optional[int]
    index_new: Optional[int]
    status: str
    summary: str
    score: Optional[float] = None
    field_changes: dict[str, tuple[Any, Any]] = field(default_factory=dict)


@dataclass
class DiffReport:
    actions: list[ActionDiff] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    overall_score: float = 1.0  # 1.0 = identical, 0.0 = no overlap

    def to_dict(self) -> dict[str, Any]:
        return {
            "counts": dict(self.counts),
            "overall_score": round(self.overall_score, 4),
            "actions": [dataclasses.asdict(a) for a in self.actions],
        }

    def format_text(self) -> str:
        """Pretty, lineraly-readable summary for terminals/CI logs."""
        lines: list[str] = []
        c = self.counts
        lines.append(
            f"Overall similarity: {self.overall_score:.2%} — "
            f"unchanged={c.get('unchanged', 0)} "
            f"drifted={c.get('drifted', 0)} "
            f"changed={c.get('changed', 0)} "
            f"added={c.get('added', 0)} "
            f"removed={c.get('removed', 0)}"
        )
        for d in self.actions:
            tag = {
                "unchanged": "  =",
                "drifted":   "  ~",
                "changed":   "  *",
                "added":     "  +",
                "removed":   "  -",
            }.get(d.status, "  ?")
            score = f" score={d.score:.2f}" if d.score is not None else ""
            lines.append(f"{tag} [{d.index_old}/{d.index_new}] {d.status:<9}{score}  {d.summary}")
            for f_name, (old_v, new_v) in d.field_changes.items():
                lines.append(f"        {f_name}: {old_v!r} -> {new_v!r}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- loaders


def load_recording(path: str | Path) -> dict[str, Any]:
    """Read a recorder JSON file. Tolerates BOMs and trailing whitespace."""
    text = Path(path).read_text(encoding="utf-8-sig").strip()
    if not text:
        return {}
    return json.loads(text)


# --------------------------------------------------------------------------- identity / scoring


# Mirrors the field weights used by the resolver (kept private so we
# can change either side without breaking the other). The numbers
# only have to be *relatively* correct for the diff signal — absolute
# values are normalised in :func:`_score`.
_WEIGHTS: dict[str, float] = {
    "accessible_name": 5.0,
    "role":             3.0,
    "type":             2.0,
    "frame_chain":      2.5,
    "label_text":       2.0,
    "placeholder":      1.5,
    "id":               1.5,
    "name":             1.0,
    "neighbour_text":   0.8,
    "tag_name":         0.4,
}


def _identity_key(action: dict[str, Any]) -> tuple[Any, ...]:
    """Stable identity for matching old vs new actions.

    We don't use ``field_id`` because the recorder regenerates it on
    every run (counter-based). Instead we hash on the (frame chain,
    role, accessible name, label) — these are what the resolver also
    keys on at replay time.
    """
    fp = action.get("fingerprint") or {}
    return (
        tuple(fp.get("frame_chain") or ()),
        (fp.get("role") or "").lower(),
        (fp.get("type") or "").lower(),
        (fp.get("accessible_name") or "").strip().lower(),
        (fp.get("label_text") or "").strip().lower(),
    )


def _score(old_fp: dict[str, Any], new_fp: dict[str, Any]) -> float:
    """0..1 weighted Jaccard over fingerprint fields."""
    total = 0.0
    matched = 0.0
    for key, weight in _WEIGHTS.items():
        ov = old_fp.get(key)
        nv = new_fp.get(key)
        if ov is None and nv is None:
            continue
        total += weight
        if isinstance(ov, list) or isinstance(nv, list):
            ov_set = {str(x) for x in (ov or [])}
            nv_set = {str(x) for x in (nv or [])}
            if not ov_set and not nv_set:
                matched += weight
            else:
                inter = len(ov_set & nv_set)
                union = len(ov_set | nv_set) or 1
                matched += weight * inter / union
        else:
            ov_s = "" if ov is None else str(ov).strip().lower()
            nv_s = "" if nv is None else str(nv).strip().lower()
            if ov_s == nv_s:
                matched += weight
    return matched / total if total else 1.0


# --------------------------------------------------------------------------- the diff


def _field_changes(
    a: dict[str, Any], b: dict[str, Any], keys: tuple[str, ...]
) -> dict[str, tuple[Any, Any]]:
    out: dict[str, tuple[Any, Any]] = {}
    for k in keys:
        av = a.get(k)
        bv = b.get(k)
        if av != bv:
            out[k] = (av, bv)
    return out


def diff_recordings(
    old: dict[str, Any] | str | Path,
    new: dict[str, Any] | str | Path,
    *,
    score_threshold: float = 0.85,
) -> DiffReport:
    """Compare two recorder configs, return a :class:`DiffReport`.

    Either argument may be a path or an already-parsed dict. Identity
    matching is order-independent — we line up actions by their
    structural identity (frame + role + accessible name + label)
    rather than by index, so swapping two unrelated fields doesn't
    cascade into "everything changed".
    """
    if not isinstance(old, dict):
        old = load_recording(old)
    if not isinstance(new, dict):
        new = load_recording(new)

    old_actions: list[dict[str, Any]] = list(old.get("actions") or [])
    new_actions: list[dict[str, Any]] = list(new.get("actions") or [])

    # Build identity → list-of-indices for both sides. We use lists
    # (not sets) so that duplicate fields — e.g. two text inputs with
    # the same label inside different sections — pair up by order.
    old_idx: dict[tuple[Any, ...], list[int]] = {}
    new_idx: dict[tuple[Any, ...], list[int]] = {}
    for i, act in enumerate(old_actions):
        old_idx.setdefault(_identity_key(act), []).append(i)
    for i, act in enumerate(new_actions):
        new_idx.setdefault(_identity_key(act), []).append(i)

    matched_old: set[int] = set()
    matched_new: set[int] = set()
    diffs: list[ActionDiff] = []

    # Pass 1: paired actions (same identity).
    for key, old_list in old_idx.items():
        new_list = new_idx.get(key, [])
        for oi, ni in zip(old_list, new_list):
            matched_old.add(oi)
            matched_new.add(ni)
            o_act = old_actions[oi]
            n_act = new_actions[ni]
            score = _score(o_act.get("fingerprint") or {}, n_act.get("fingerprint") or {})

            field_changes = _field_changes(
                o_act, n_act, keys=("kind", "value", "checked", "selector"),
            )
            # Compare a few interesting fingerprint sub-fields too —
            # those are what the resolver actually weighs.
            field_changes.update({
                f"fingerprint.{k}": (
                    (o_act.get("fingerprint") or {}).get(k),
                    (n_act.get("fingerprint") or {}).get(k),
                )
                for k in ("placeholder", "id", "name", "label_text")
                if (o_act.get("fingerprint") or {}).get(k) != (n_act.get("fingerprint") or {}).get(k)
            })

            if field_changes and any(
                k in field_changes for k in ("kind", "value", "checked", "selector")
            ):
                status = "changed"
            elif score < score_threshold:
                status = "drifted"
            else:
                status = "unchanged"

            summary = f"{o_act.get('kind', '?')} on {(o_act.get('fingerprint') or {}).get('accessible_name') or o_act.get('field_id') or '?'!r}"
            diffs.append(ActionDiff(
                index_old=oi, index_new=ni, status=status,
                summary=summary, score=score, field_changes=field_changes,
            ))

    # Pass 2: unmatched old → removed.
    for i, act in enumerate(old_actions):
        if i in matched_old:
            continue
        fp = act.get("fingerprint") or {}
        diffs.append(ActionDiff(
            index_old=i, index_new=None, status="removed",
            summary=f"{act.get('kind', '?')} on {fp.get('accessible_name') or act.get('field_id') or '?'!r}",
        ))

    # Pass 3: unmatched new → added.
    for i, act in enumerate(new_actions):
        if i in matched_new:
            continue
        fp = act.get("fingerprint") or {}
        diffs.append(ActionDiff(
            index_old=None, index_new=i, status="added",
            summary=f"{act.get('kind', '?')} on {fp.get('accessible_name') or act.get('field_id') or '?'!r}",
        ))

    # Sort by index_new (then index_old) so the report reads in the
    # order the user will encounter the actions during replay.
    diffs.sort(key=lambda d: (d.index_new if d.index_new is not None else 1e9,
                              d.index_old if d.index_old is not None else 1e9))

    counts: dict[str, int] = {}
    for d in diffs:
        counts[d.status] = counts.get(d.status, 0) + 1

    paired = [d for d in diffs if d.score is not None]
    overall = (
        sum(d.score for d in paired) / len(paired)
        if paired
        else (1.0 if not diffs else 0.0)
    )
    # Penalise additions/removals — they can be just as disruptive as
    # a drifted score even when the matched pairs all look healthy.
    unmatched = counts.get("added", 0) + counts.get("removed", 0)
    total_seen = max(len(old_actions), len(new_actions), 1)
    overall *= max(0.0, 1.0 - 0.5 * unmatched / total_seen)

    return DiffReport(actions=diffs, counts=counts, overall_score=overall)
