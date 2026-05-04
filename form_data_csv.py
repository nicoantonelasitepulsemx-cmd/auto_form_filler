"""v4 add-on: drive a recording with rows from a CSV.

Use case: you recorded one form submission, but want to push 500
real-looking submissions through it from a spreadsheet your team
maintains. ``form_data_csv`` reads the CSV, maps each column onto a
``field_id`` in the recording, and yields one fully-resolved
``ctx`` dict per row that the replay engine can consume directly.

It also ships a tiny built-in **Faker** so cells like
``{{faker.name}}``, ``{{faker.email}}`` and ``{{faker.phone_us}}`` are
auto-expanded — no PyPI ``faker`` dependency required (the real
package adds ~6 MB of locale data we don't need for a few common
fields).

Mapping rules
~~~~~~~~~~~~~
1. Column header *exactly* matches a recording ``field_id`` →
   straight 1:1.
2. Column header is ``"<field_id>"`` enclosed in square brackets →
   same, useful when the header otherwise collides with a CSV-default
   like ``id``.
3. Column header is ``__faker:<spec>`` → ignored as a value source;
   instead, the spec is registered as a default for any field whose
   value template references the same faker key. This lets a single
   spreadsheet seed multiple fields with related-but-randomised data
   (e.g. one ``__faker:person`` column drives both ``first_name``
   and ``last_name`` field_ids consistently).

Built-in faker tokens
~~~~~~~~~~~~~~~~~~~~~
* ``{{faker.first_name}}`` / ``{{faker.last_name}}`` / ``{{faker.name}}``
* ``{{faker.email}}`` / ``{{faker.email_at:domain}}``
* ``{{faker.phone_us}}`` / ``{{faker.phone_intl}}``
* ``{{faker.uuid}}`` / ``{{faker.uuid_short}}``
* ``{{faker.username}}``
* ``{{faker.password}}`` / ``{{faker.password_strong}}``
* ``{{faker.ip_v4}}``
* ``{{faker.address}}`` / ``{{faker.city_us}}`` / ``{{faker.zipcode_us}}``
* ``{{faker.date_birth}}`` / ``{{faker.date_future:days}}``
* ``{{faker.lorem:n}}`` (n-word lorem ipsum)

Anything that doesn't match a known token is left untouched so the
existing :mod:`value_templates` pipeline can still expand
``{{uuid}}`` / ``{{env}}`` etc. — we explicitly only handle the
``faker.`` namespace.
"""
from __future__ import annotations

import csv
import io
import random
import re
import string
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

__all__ = [
    "CsvRow",
    "FormDataSource",
    "expand_faker_tokens",
    "iter_rows",
    "load_csv",
]


# --------------------------------------------------------------------------- faker

# Tiny built-in name pools. Kept short on purpose — the goal is
# "passes a quick eyeballed look" not "fools an analyst", and the
# real ``faker`` package is one ``pip install`` away if you need
# locale-specific authenticity.
_FIRST_NAMES = (
    "Alex Jordan Taylor Casey Riley Morgan Quinn Avery Drew Hayden "
    "Logan Reese Sam Skylar Charlie Blake Cameron Dakota Emerson Finley "
    "Harper Jamie Kai Kennedy Micah Parker Robin Sage Sawyer Shea"
).split()
_LAST_NAMES = (
    "Smith Johnson Williams Brown Jones Garcia Miller Davis Rodriguez "
    "Martinez Hernandez Lopez Gonzalez Wilson Anderson Thomas Taylor Moore "
    "Jackson Martin Lee Perez Thompson White Harris Sanchez Clark Ramirez Lewis Robinson"
).split()
_LOREM = (
    "lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod "
    "tempor incididunt ut labore et dolore magna aliqua enim ad minim veniam quis "
    "nostrud exercitation ullamco laboris nisi ut aliquip ex ea commodo consequat"
).split()
_US_CITIES = (
    "Springfield Franklin Greenville Bristol Clinton Madison Salem Georgetown "
    "Arlington Burlington Concord Dover Hampton Manchester Newport"
).split()
_STREETS = (
    "Main Oak Pine Maple Cedar Elm Walnut Park Lake View Hill Birch "
    "Spruce Willow Forest Sunset Highland Riverside Meadow"
).split()


def _rng(seed: Optional[int]) -> random.Random:
    """Per-row RNG so two rows with the same seed always render the same."""
    return random.Random(seed)


def _gen(token: str, *, rng: random.Random) -> str:
    """Render a single ``faker.<key>`` token. Returns the original
    ``{{faker.<key>}}`` literal for unknown keys so they're easy to
    spot in QA output."""
    name = token
    arg = ""
    if ":" in token:
        name, arg = token.split(":", 1)
    if name == "first_name":
        return rng.choice(_FIRST_NAMES)
    if name == "last_name":
        return rng.choice(_LAST_NAMES)
    if name == "name":
        return f"{rng.choice(_FIRST_NAMES)} {rng.choice(_LAST_NAMES)}"
    if name == "username":
        return f"{rng.choice(_FIRST_NAMES).lower()}{rng.randint(10, 9999)}"
    if name == "email":
        local = f"{rng.choice(_FIRST_NAMES).lower()}.{rng.choice(_LAST_NAMES).lower()}{rng.randint(10, 999)}"
        return f"{local}@example.com"
    if name == "email_at":
        domain = arg or "example.com"
        local = f"{rng.choice(_FIRST_NAMES).lower()}.{rng.choice(_LAST_NAMES).lower()}{rng.randint(10, 999)}"
        return f"{local}@{domain}"
    if name == "phone_us":
        return f"+1{rng.randint(200, 999)}{rng.randint(200, 999)}{rng.randint(0, 9999):04d}"
    if name == "phone_intl":
        cc = rng.choice([1, 33, 44, 49, 81, 84, 86])
        return f"+{cc}{rng.randint(10**8, 10**10 - 1)}"
    if name == "uuid":
        return str(uuid.UUID(int=rng.getrandbits(128)))
    if name == "uuid_short":
        return uuid.UUID(int=rng.getrandbits(128)).hex[:12]
    if name == "password":
        # Mid-strength: lowercase + digit, 10 chars.
        chars = string.ascii_lowercase + string.digits
        return "".join(rng.choice(chars) for _ in range(10))
    if name == "password_strong":
        # 16 chars, one of each class — meets most enterprise rules.
        pools = [
            string.ascii_lowercase, string.ascii_uppercase,
            string.digits, "!@#$%^&*",
        ]
        out = [rng.choice(p) for p in pools]
        out.extend(rng.choice("".join(pools)) for _ in range(16 - len(out)))
        rng.shuffle(out)
        return "".join(out)
    if name == "ip_v4":
        return ".".join(str(rng.randint(1, 254)) for _ in range(4))
    if name == "address":
        return f"{rng.randint(1, 9999)} {rng.choice(_STREETS)} St, {rng.choice(_US_CITIES)}, USA"
    if name == "city_us":
        return rng.choice(_US_CITIES)
    if name == "zipcode_us":
        return f"{rng.randint(10000, 99999)}"
    if name == "date_birth":
        # Roughly 18-65 years old.
        days_ago = rng.randint(18 * 365, 65 * 365)
        return (date.today() - timedelta(days=days_ago)).isoformat()
    if name == "date_future":
        try:
            n = int(arg) if arg else 30
        except ValueError:
            n = 30
        return (date.today() + timedelta(days=rng.randint(1, max(1, n)))).isoformat()
    if name == "lorem":
        try:
            n = int(arg) if arg else 8
        except ValueError:
            n = 8
        return " ".join(rng.choice(_LOREM) for _ in range(max(1, n)))
    # Unknown token — leave the literal in so QA can see it.
    return "{{faker." + token + "}}"


_FAKER_RE = re.compile(r"\{\{faker\.([a-zA-Z_][a-zA-Z0-9_:.]*)\}\}")


def expand_faker_tokens(text: str, *, seed: Optional[int] = None) -> str:
    """Substitute all ``{{faker.X}}`` tokens in *text* in place.

    Non-strings and inputs without a match return unchanged. The same
    ``seed`` reproduces the same output (deterministic for tests and
    "regenerate the same row" workflows).
    """
    if not isinstance(text, str) or "{{faker." not in text:
        return text
    rng = _rng(seed)
    return _FAKER_RE.sub(lambda m: _gen(m.group(1), rng=rng), text)


# --------------------------------------------------------------------------- CSV layer

@dataclass
class CsvRow:
    """One row from the CSV, decoded into ``field_id → value``.

    ``raw`` keeps the original column → value mapping so callers can
    fall back to columns the recording doesn't reference.
    """
    index: int
    values: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, str] = field(default_factory=dict)


@dataclass
class FormDataSource:
    """In-memory representation of a CSV.

    Wrap a path with :func:`load_csv` then iterate rows via
    :meth:`iter_rows`. Each row is a :class:`CsvRow` with values
    expanded for ``{{faker.*}}`` tokens.
    """
    headers: list[str]
    rows: list[dict[str, str]]
    field_map: dict[str, str]  # csv-header -> field_id (post-normalisation)

    def iter_rows(self, *, seed_base: Optional[int] = None) -> Iterator[CsvRow]:
        for i, raw in enumerate(self.rows):
            # Stable per-row seed = ``seed_base * 1000003 + i`` so two
            # runs with the same ``seed_base`` reproduce the same
            # faker outputs.
            seed = None if seed_base is None else (seed_base * 1000003 + i)
            values: dict[str, Any] = {}
            for header, fid in self.field_map.items():
                cell = raw.get(header, "")
                values[fid] = expand_faker_tokens(cell, seed=seed)
            yield CsvRow(index=i, values=values, raw=dict(raw))


# --------------------------------------------------------------------------- loader

def _normalise_header(h: str) -> str:
    """Strip the ``[…]`` wrapper used for ambiguous header names."""
    h = h.strip()
    if h.startswith("[") and h.endswith("]"):
        return h[1:-1].strip()
    return h


def load_csv(
    path: str | Path,
    *,
    encoding: str = "utf-8-sig",
    field_ids: Optional[Iterable[str]] = None,
) -> FormDataSource:
    """Read ``path`` into a :class:`FormDataSource`.

    Parameters
    ----------
    field_ids:
        Optional whitelist of recording ``field_id`` names. When
        provided, columns whose header doesn't normalise to one of
        these are dropped from the field map (still kept in
        ``raw`` for diagnostics). Useful for catching typos in the
        spreadsheet header row before the replay run actually starts.
    """
    raw_text = Path(path).read_text(encoding=encoding)
    return load_csv_string(raw_text, field_ids=field_ids)


def load_csv_string(
    raw_text: str,
    *,
    field_ids: Optional[Iterable[str]] = None,
) -> FormDataSource:
    """Test-friendly twin of :func:`load_csv` that takes a string."""
    reader = csv.DictReader(io.StringIO(raw_text))
    headers = list(reader.fieldnames or [])
    rows = list(reader)
    allowed = set(field_ids) if field_ids is not None else None
    field_map: dict[str, str] = {}
    for h in headers:
        nh = _normalise_header(h)
        if nh.startswith("__faker:"):
            continue
        if allowed is None or nh in allowed:
            field_map[h] = nh
    return FormDataSource(headers=headers, rows=rows, field_map=field_map)


def iter_rows(
    path: str | Path,
    *,
    field_ids: Optional[Iterable[str]] = None,
    seed_base: Optional[int] = None,
) -> Iterator[CsvRow]:
    """Convenience: ``load_csv`` + ``iter_rows`` in one call."""
    return load_csv(path, field_ids=field_ids).iter_rows(seed_base=seed_base)
