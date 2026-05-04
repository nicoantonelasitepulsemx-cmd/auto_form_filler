"""test_proxy_pool.py — unit tests for the multi-proxy parser + pool builder.

Run with::

    python -m pytest test_proxy_pool.py -v
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from accounts import accounts_from_proxies
from proxy_utils import load_proxy_dicts, parse_proxy_string


# --------------------------------------------------------------------------------------
#  parse_proxy_string
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("raw, expected", [
    # flat host:port:user:pass (the new format)
    ("1.2.3.4:8080:alice:apass",
     {"server": "http://1.2.3.4:8080", "username": "alice", "password": "apass"}),
    # password contains `:`
    ("proxy.example.com:3128:bob:s3cr:et",
     {"server": "http://proxy.example.com:3128", "username": "bob", "password": "s3cr:et"}),
    # v4: password contains `@` — must NOT be misread as user:pass@host
    ("1.2.3.4:8080:admin:p@ss",
     {"server": "http://1.2.3.4:8080", "username": "admin", "password": "p@ss"}),
    # v4: password contains `/` — must NOT be misread as URL path
    ("1.2.3.4:8080:admin:p/ss",
     {"server": "http://1.2.3.4:8080", "username": "admin", "password": "p/ss"}),
    # v4: password contains both `@` and `:` — full mix.
    ("1.2.3.4:8080:admin:p@s:s",
     {"server": "http://1.2.3.4:8080", "username": "admin", "password": "p@s:s"}),
    # plain host:port
    ("1.2.3.4:8080", {"server": "http://1.2.3.4:8080"}),
    # creds-before-host
    ("user:pass@1.2.3.4:8080",
     {"server": "http://1.2.3.4:8080", "username": "user", "password": "pass"}),
    # explicit URL forms — unchanged behaviour
    ("http://user:pass@1.2.3.4:8080",
     {"server": "http://1.2.3.4:8080", "username": "user", "password": "pass"}),
    ("socks5://1.2.3.4:1080", {"server": "socks5://1.2.3.4:1080"}),
])
def test_parse_proxy_string_accepts(raw, expected):
    assert parse_proxy_string(raw) == expected


def test_parse_proxy_string_empty_returns_none():
    assert parse_proxy_string("") is None
    assert parse_proxy_string("   ") is None


def test_parse_proxy_string_rejects_garbage():
    with pytest.raises(ValueError):
        parse_proxy_string("not-a-proxy-line")


# --------------------------------------------------------------------------------------
#  load_proxy_dicts
# --------------------------------------------------------------------------------------


def test_load_proxy_dicts_skips_comments_and_blanks(tmp_path: Path):
    f = tmp_path / "proxies.txt"
    f.write_text(
        "# header comment\n"
        "\n"
        "1.2.3.4:8080:alice:apass\n"
        "5.6.7.8:9090:bob:bpass\n"
        "   \n"
        "# another comment\n"
        "http://10.0.0.1:3128\n",
        encoding="utf-8",
    )
    out = load_proxy_dicts(f)
    assert len(out) == 3
    assert out[0]["username"] == "alice"
    assert out[1]["password"] == "bpass"
    assert out[2]["server"] == "http://10.0.0.1:3128"
    assert "username" not in out[2]


def test_load_proxy_dicts_warns_on_bad_line(tmp_path: Path, capsys):
    f = tmp_path / "proxies.txt"
    f.write_text("1.2.3.4:8080:alice:apass\nbroken-line\n5.6.7.8:9090\n",
                 encoding="utf-8")
    out = load_proxy_dicts(f, on_error="warn")
    assert len(out) == 2
    captured = capsys.readouterr()
    assert "broken-line" in captured.err


def test_load_proxy_dicts_silent_skips(tmp_path: Path, capsys):
    f = tmp_path / "proxies.txt"
    f.write_text("1.2.3.4:8080\nbroken\n", encoding="utf-8")
    out = load_proxy_dicts(f, on_error="silent")
    assert len(out) == 1
    assert capsys.readouterr().err == ""


def test_load_proxy_dicts_raise_propagates(tmp_path: Path):
    f = tmp_path / "proxies.txt"
    f.write_text("broken\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_proxy_dicts(f, on_error="raise")


def test_load_proxy_dicts_default_bypass(tmp_path: Path):
    f = tmp_path / "proxies.txt"
    f.write_text("1.2.3.4:8080\n", encoding="utf-8")
    out = load_proxy_dicts(f, default_bypass="*.local")
    assert out[0]["bypass"] == "*.local"


# --------------------------------------------------------------------------------------
#  accounts_from_proxies
# --------------------------------------------------------------------------------------


def test_accounts_from_proxies_one_account_per_proxy():
    proxies = [
        {"server": "http://1.2.3.4:8080", "username": "alice", "password": "apass"},
        {"server": "http://5.6.7.8:9090"},
    ]
    accts = accounts_from_proxies(proxies, base_vars={"email": "x@y.com"})
    assert [a.name for a in accts] == ["proxy_1", "proxy_2"]
    assert accts[0].proxy == proxies[0]
    assert accts[1].proxy == proxies[1]
    assert accts[0].vars == {"email": "x@y.com"}
    # Sanity: vars is copied, not shared.
    accts[0].vars["mutate"] = True
    assert "mutate" not in accts[1].vars


def test_accounts_from_proxies_persistent_profile_template(tmp_path: Path):
    proxies = ["1.2.3.4:8080", "5.6.7.8:9090"]
    template = str(tmp_path / "profile_{i}_{name}")
    accts = accounts_from_proxies(proxies, user_data_dir_template=template)
    assert accts[0].user_data_dir == str(tmp_path / "profile_1_proxy_1")
    assert accts[1].user_data_dir == str(tmp_path / "profile_2_proxy_2")


def test_accounts_from_proxies_headless_pass_through():
    accts = accounts_from_proxies(["1.2.3.4:8080"], headless=True)
    assert accts[0].headless is True


def test_accounts_from_proxies_empty_input_returns_empty_list():
    assert accounts_from_proxies([]) == []


# --------------------------------------------------------------------------------------
#  End-to-end glue: file → dicts → accounts
# --------------------------------------------------------------------------------------


def test_file_to_accounts_round_trip(tmp_path: Path):
    f = tmp_path / "proxies.txt"
    f.write_text(
        "1.2.3.4:8080:alice:apass\n"
        "5.6.7.8:9090:bob:bpass\n"
        "10.0.0.1:3128\n",
        encoding="utf-8",
    )
    proxies = load_proxy_dicts(f)
    accts = accounts_from_proxies(proxies)
    assert len(accts) == 3
    assert accts[0].proxy["username"] == "alice"
    assert accts[2].proxy["server"] == "http://10.0.0.1:3128"
    assert "username" not in accts[2].proxy
