"""value_templates.py — runtime expansion of `{{token}}` placeholders.

The replay engine and ``auto_fill.py`` call :func:`expand` on every field
value just before it is typed into the page. This lets users include
ergonomic placeholders in their config so each run produces a fresh value:

  {{date}}                    → today as ``YYYY-MM-DD``
  {{date:%d/%m/%Y %H:%M}}     → today, formatted by ``strftime``
  {{timestamp}}               → unix timestamp (seconds since epoch)
  {{datetime}}                → ISO-8601 ``YYYY-MM-DDTHH:MM:SS``
  {{uuid}} / {{uuid4}}        → random uuid4 (lowercase, with dashes)
  {{random_str:N}}            → random alphanumeric string of length N (default 8)
  {{random_int:LO:HI}}        → random integer LO..HI inclusive
  {{random_email}}            → ``rand@example.com`` style throwaway email
  {{env:NAME}} / {{env:NAME:default}} → value of ``$NAME`` (or default)

Templates are escapable: ``\\{{date}}`` becomes the literal string ``{{date}}``.
Unknown templates are left untouched so an honest mistake doesn't silently
delete the whole value — the caller can grep for ``{{`` afterwards.

This module has zero third-party dependencies — it is safe to import from
anywhere in the engine.
"""
from __future__ import annotations

import datetime as _dt
import os
import random
import re
import string
import time
import uuid
from typing import Any

# {{name}} or {{name:argument}}  — argument may itself contain `:` (e.g. for date format strings).
_TOKEN_RE = re.compile(r"\\?\{\{\s*([a-zA-Z_][\w]*)\s*(?::([^}]*))?\s*\}\}")


def _resolve(name: str, arg: str | None) -> str | None:
    """Return the expanded text for one template, or None to leave it alone."""
    n = name.lower()
    if n == "date":
        fmt = arg if arg else "%Y-%m-%d"
        return _dt.date.today().strftime(fmt)
    if n == "datetime":
        fmt = arg if arg else "%Y-%m-%dT%H:%M:%S"
        return _dt.datetime.now().strftime(fmt)
    if n == "timestamp":
        return str(int(time.time()))
    if n in ("uuid", "uuid4"):
        return str(uuid.uuid4())
    if n == "random_str":
        try:
            length = int(arg) if arg else 8
        except ValueError:
            length = 8
        length = max(1, min(length, 256))
        alphabet = string.ascii_letters + string.digits
        return "".join(random.choice(alphabet) for _ in range(length))
    if n == "random_int":
        if arg and ":" in arg:
            lo_s, hi_s = arg.split(":", 1)
            try:
                lo = int(lo_s)
                hi = int(hi_s)
            except ValueError:
                return None
        else:
            lo, hi = 0, 999999
        if lo > hi:
            lo, hi = hi, lo
        return str(random.randint(lo, hi))
    if n == "random_email":
        local = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(8))
        domain = arg if arg else "example.com"
        return f"{local}@{domain}"
    if n == "env":
        if not arg:
            return None
        if ":" in arg:
            varname, default = arg.split(":", 1)
        else:
            varname, default = arg, ""
        return os.environ.get(varname, default)
    return None  # unknown — leave the literal token intact


def expand(value: Any) -> Any:
    """Expand template tokens in ``value`` and return the result.

    Strings are processed through the template regex; lists/tuples/dicts are
    walked recursively (handy for ``multi_textarea`` values which are lists
    of strings). Anything else (None, bool, int, float) is returned as-is.
    """
    if isinstance(value, str):
        return _expand_string(value)
    if isinstance(value, list):
        return [expand(v) for v in value]
    if isinstance(value, tuple):
        return tuple(expand(v) for v in value)
    if isinstance(value, dict):
        return {k: expand(v) for k, v in value.items()}
    return value


def _expand_string(s: str) -> str:
    def _sub(m: re.Match[str]) -> str:
        # Escaped: `\{{x}}`  →  `{{x}}`
        if m.group(0).startswith("\\"):
            return m.group(0)[1:]
        name = m.group(1)
        arg = m.group(2)
        out = _resolve(name, arg)
        return out if out is not None else m.group(0)
    return _TOKEN_RE.sub(_sub, s)


__all__ = ["expand"]


# --------------------------------------------------------------------------------------
#  Self-test (run as a script): `python value_templates.py`
# --------------------------------------------------------------------------------------

if __name__ == "__main__":
    samples = [
        "today is {{date}}",
        "{{date:%d/%m/%Y}}",
        "{{datetime}}",
        "{{timestamp}}",
        "ID-{{uuid4}}",
        "{{random_str}} / {{random_str:16}}",
        "{{random_int:1:100}}",
        "{{random_email}} / {{random_email:test.com}}",
        "{{env:HOME}}",
        "{{env:NOPE:fallback-here}}",
        "literal: \\{{date}} should NOT expand",
        ["batch", "{{date}}", {"x": "{{uuid4}}"}],
    ]
    for s in samples:
        print(f"{s!r:60s}  →  {expand(s)!r}")
