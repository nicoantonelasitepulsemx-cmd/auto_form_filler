"""Unit tests for ``form_data_csv``.

Determinism + token coverage — the built-in faker is small enough
that we can pin every public token in a regression test.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import form_data_csv as fdc


# --------------------------------------------------------------------------- expand_faker_tokens

def test_expand_faker_known_tokens_expand() -> None:
    out = fdc.expand_faker_tokens("Hi {{faker.first_name}}!", seed=42)
    assert "{{faker." not in out
    assert out.startswith("Hi ")
    assert out.endswith("!")


def test_expand_faker_seed_is_deterministic() -> None:
    a = fdc.expand_faker_tokens("{{faker.email}}", seed=7)
    b = fdc.expand_faker_tokens("{{faker.email}}", seed=7)
    assert a == b


def test_expand_faker_different_seeds_differ() -> None:
    """Two different seeds *should* normally give different outputs.

    Picking from the small built-in name pool means there's a tiny
    chance of collision — we use seeds that are known-distinct
    against the current pool.
    """
    a = fdc.expand_faker_tokens("{{faker.name}}", seed=1)
    b = fdc.expand_faker_tokens("{{faker.name}}", seed=2)
    assert a != b


def test_expand_faker_unknown_token_left_intact() -> None:
    out = fdc.expand_faker_tokens("{{faker.does_not_exist}}", seed=0)
    assert out == "{{faker.does_not_exist}}"


def test_expand_faker_email_at_uses_custom_domain() -> None:
    out = fdc.expand_faker_tokens("{{faker.email_at:mydomain.test}}", seed=0)
    assert out.endswith("@mydomain.test")
    assert "@" in out


def test_expand_faker_phone_us_format() -> None:
    out = fdc.expand_faker_tokens("{{faker.phone_us}}", seed=0)
    assert re.match(r"^\+1\d{10}$", out)


def test_expand_faker_uuid_is_valid_format() -> None:
    out = fdc.expand_faker_tokens("{{faker.uuid}}", seed=0)
    assert re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", out)


def test_expand_faker_uuid_short_is_12_hex() -> None:
    out = fdc.expand_faker_tokens("{{faker.uuid_short}}", seed=0)
    assert re.match(r"^[0-9a-f]{12}$", out)


def test_expand_faker_password_strong_has_each_class() -> None:
    out = fdc.expand_faker_tokens("{{faker.password_strong}}", seed=0)
    assert any(c.islower() for c in out)
    assert any(c.isupper() for c in out)
    assert any(c.isdigit() for c in out)
    assert any(c in "!@#$%^&*" for c in out)
    assert len(out) == 16


def test_expand_faker_lorem_word_count() -> None:
    out = fdc.expand_faker_tokens("{{faker.lorem:5}}", seed=0)
    assert len(out.split()) == 5


def test_expand_faker_date_birth_iso() -> None:
    out = fdc.expand_faker_tokens("{{faker.date_birth}}", seed=0)
    assert re.match(r"^\d{4}-\d{2}-\d{2}$", out)


def test_expand_faker_ip_v4_format() -> None:
    out = fdc.expand_faker_tokens("{{faker.ip_v4}}", seed=0)
    parts = out.split(".")
    assert len(parts) == 4
    for p in parts:
        n = int(p)
        assert 1 <= n <= 254


def test_expand_faker_zipcode_us_5_digits() -> None:
    out = fdc.expand_faker_tokens("{{faker.zipcode_us}}", seed=0)
    assert re.match(r"^\d{5}$", out)


def test_expand_faker_handles_non_string() -> None:
    """Ints, None, lists must round-trip unchanged."""
    assert fdc.expand_faker_tokens(123) == 123  # type: ignore[arg-type]
    assert fdc.expand_faker_tokens(None) is None  # type: ignore[arg-type]


def test_expand_faker_no_token_returns_input_intact() -> None:
    assert fdc.expand_faker_tokens("plain string", seed=0) == "plain string"


# --------------------------------------------------------------------------- CSV layer

def _csv(*rows: str) -> str:
    return "\n".join(rows) + "\n"


def test_load_csv_string_simple_round_trip() -> None:
    src = fdc.load_csv_string(_csv("name,email", "alice,alice@x.com", "bob,bob@x.com"))
    assert src.headers == ["name", "email"]
    assert len(src.rows) == 2
    assert src.field_map == {"name": "name", "email": "email"}


def test_load_csv_string_normalises_bracket_headers() -> None:
    """``[id]`` header form should map to field_id ``id``."""
    src = fdc.load_csv_string(_csv("[id],name", "1,a", "2,b"))
    assert src.field_map == {"[id]": "id", "name": "name"}


def test_load_csv_string_drops_unwhitelisted_columns() -> None:
    src = fdc.load_csv_string(
        _csv("name,extra,email", "a,x,a@y", "b,y,b@y"),
        field_ids=["name", "email"],
    )
    assert src.field_map == {"name": "name", "email": "email"}


def test_load_csv_string_skips_faker_namespace_headers() -> None:
    """``__faker:person`` is a directive, not a value source."""
    src = fdc.load_csv_string(_csv("name,__faker:person", "a,seed", "b,seed"))
    assert "__faker:person" not in src.field_map


def test_iter_rows_expands_faker_tokens() -> None:
    src = fdc.load_csv_string(_csv("name,email", "{{faker.first_name}},{{faker.email}}", "alice,alice@x"))
    rows = list(src.iter_rows(seed_base=11))
    assert len(rows) == 2
    # Row 0 had templates → expanded.
    assert "{{faker." not in rows[0].values["name"]
    assert "@" in rows[0].values["email"]
    # Row 1 was literal → preserved.
    assert rows[1].values["name"] == "alice"
    assert rows[1].values["email"] == "alice@x"


def test_iter_rows_seed_base_is_deterministic() -> None:
    src = fdc.load_csv_string(_csv("name", "{{faker.name}}", "{{faker.name}}"))
    a = [r.values["name"] for r in src.iter_rows(seed_base=99)]
    b = [r.values["name"] for r in src.iter_rows(seed_base=99)]
    assert a == b


def test_iter_rows_keeps_raw_payload_for_diagnostics() -> None:
    src = fdc.load_csv_string(_csv("name,email,extra", "a,a@x,note", "b,b@x,note2"))
    rows = list(src.iter_rows())
    assert rows[0].raw["extra"] == "note"
    assert rows[1].raw["extra"] == "note2"


def test_load_csv_handles_utf8_bom(tmp_path: Path) -> None:
    p = tmp_path / "rows.csv"
    p.write_bytes(b"\xef\xbb\xbfname,email\nalice,alice@x.com\n")
    src = fdc.load_csv(p)
    assert src.rows[0]["name"] == "alice"


def test_iter_rows_top_level_helper(tmp_path: Path) -> None:
    p = tmp_path / "rows.csv"
    p.write_text("name\nalice\nbob\n", encoding="utf-8")
    rows = list(fdc.iter_rows(p))
    assert [r.values["name"] for r in rows] == ["alice", "bob"]
