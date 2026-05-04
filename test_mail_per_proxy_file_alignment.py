"""Regression tests for `_parse_proxy_file_aligned`.

Devin Review BUG #3182774684: ``_cmd_from_proxy_file`` previously called
``proxy_utils.load_proxy_dicts(on_error='silent')`` which silently
dropped invalid lines, then indexed back into a parallel ``lines``
list. For a file with ``[good, good, bad, good]`` the parsed list had
length 3 while ``lines`` had length 4 — every row from the bad line
onwards got mis-aligned, so a "valid" parsed dict ended up paired with
the *next* line's literal text.

These tests assert the new aligned helper preserves a 1:1 mapping
between literal line text and parsed dict.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from mail_per_proxy_panel import _parse_proxy_file_aligned  # noqa: E402


def _write(text: str) -> Path:
    f = tempfile.NamedTemporaryFile(
        mode="w", suffix=".proxies", delete=False, encoding="utf-8"
    )
    f.write(text)
    f.close()
    return Path(f.name)


def test_all_valid_lines_round_trip_in_order() -> None:
    """Each valid entry pairs its OWN line text with its parsed dict."""
    p = _write(
        "1.2.3.4:8080:u1:p1\n"
        "5.6.7.8:9090:u2:p2\n"
        "9.10.11.12:3128:u3:p3\n"
    )
    valid, invalid = _parse_proxy_file_aligned(p)
    assert invalid == []
    assert len(valid) == 3
    # Line order preserved.
    assert valid[0][0] == "1.2.3.4:8080:u1:p1"
    assert valid[1][0] == "5.6.7.8:9090:u2:p2"
    assert valid[2][0] == "9.10.11.12:3128:u3:p3"
    # Each parsed dict matches its line.
    assert valid[0][1]["server"].endswith("1.2.3.4:8080")
    assert valid[1][1]["server"].endswith("5.6.7.8:9090")
    assert valid[2][1]["server"].endswith("9.10.11.12:3128")
    assert valid[0][1]["username"] == "u1"
    assert valid[1][1]["username"] == "u2"
    assert valid[2][1]["username"] == "u3"


def test_invalid_line_in_middle_does_not_misalign_others() -> None:
    """The exact scenario the Devin Review bot called out: a bad
    line in the middle of the file must not cause subsequent rows to
    get the wrong proxy_str."""
    p = _write(
        "1.2.3.4:8080:u1:p1\n"
        "5.6.7.8:9090:u2:p2\n"
        "not-a-proxy-line\n"
        "9.10.11.12:3128:u3:p3\n"
    )
    valid, invalid = _parse_proxy_file_aligned(p)
    # 3 valid, 1 invalid.
    assert len(valid) == 3
    assert invalid == ["not-a-proxy-line"]
    # Critical: the third valid entry must still pair with its own line.
    assert valid[0][0] == "1.2.3.4:8080:u1:p1"
    assert valid[1][0] == "5.6.7.8:9090:u2:p2"
    assert valid[2][0] == "9.10.11.12:3128:u3:p3", (
        "Bug #3182774684 regression: third valid line lost alignment "
        f"after invalid line was skipped — got {valid[2][0]!r}"
    )
    # And each parsed dict still matches its own line.
    assert valid[2][1]["server"].endswith("9.10.11.12:3128")
    assert valid[2][1]["username"] == "u3"


def test_blank_and_comment_lines_skipped_without_misalignment() -> None:
    p = _write(
        "# header comment\n"
        "\n"
        "1.2.3.4:8080:u1:p1\n"
        "   \n"
        "# another comment\n"
        "5.6.7.8:9090:u2:p2\n"
        "\n"
    )
    valid, invalid = _parse_proxy_file_aligned(p)
    assert invalid == []
    assert len(valid) == 2
    assert valid[0][0] == "1.2.3.4:8080:u1:p1"
    assert valid[1][0] == "5.6.7.8:9090:u2:p2"


def test_multiple_invalid_lines_collected_separately() -> None:
    p = _write(
        "garbage_one\n"
        "1.2.3.4:8080:u1:p1\n"
        "garbage two\n"
        "5.6.7.8:9090:u2:p2\n"
        "garbage_three\n"
    )
    valid, invalid = _parse_proxy_file_aligned(p)
    assert len(valid) == 2
    assert len(invalid) == 3
    assert invalid == ["garbage_one", "garbage two", "garbage_three"]
    assert valid[0][0] == "1.2.3.4:8080:u1:p1"
    assert valid[1][0] == "5.6.7.8:9090:u2:p2"


def test_password_with_special_chars_still_aligns() -> None:
    """Passwords containing ``@`` are still parsed correctly because
    ``parse_proxy_string`` uses the flat colon-delimited form."""
    p = _write(
        "1.2.3.4:8080:admin:p@ss\n"
        "5.6.7.8:9090:bob:secret\n"
    )
    valid, invalid = _parse_proxy_file_aligned(p)
    assert invalid == []
    assert len(valid) == 2
    assert valid[0][1]["password"] == "p@ss"
    assert valid[1][1]["password"] == "secret"


def test_empty_file_returns_empty_lists() -> None:
    p = _write("")
    valid, invalid = _parse_proxy_file_aligned(p)
    assert valid == []
    assert invalid == []


def test_only_comments_returns_empty_lists() -> None:
    p = _write("# nothing here\n# nothing there\n")
    valid, invalid = _parse_proxy_file_aligned(p)
    assert valid == []
    assert invalid == []


if __name__ == "__main__":
    test_all_valid_lines_round_trip_in_order()
    test_invalid_line_in_middle_does_not_misalign_others()
    test_blank_and_comment_lines_skipped_without_misalignment()
    test_multiple_invalid_lines_collected_separately()
    test_password_with_special_chars_still_aligns()
    test_empty_file_returns_empty_lists()
    test_only_comments_returns_empty_lists()
    print("ok all")
