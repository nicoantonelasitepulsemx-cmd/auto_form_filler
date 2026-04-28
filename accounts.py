"""accounts.py — load + validate the multi-account configuration.

JSON schema (`accounts.json`)::

    [
        {
            "name":          "acct1",
            "user_data_dir": "./profiles/acct1",   // persistent Chromium profile
            "proxy":         "http://user:pass@1.2.3.4:8080",
            "vars":          { "email": "phu1@example.com", "phone": "0900000001" },
            "cookies":       [],                   // optional pre-set cookies
            "storage_state": "./profiles/acct1/state.json",  // optional
            "headless":      false,
            "viewport":      {"width": 1280, "height": 800}
        },
        ...
    ]

`vars` is the per-account substitution dict that flows into the replay engine
so a recorded action with `value_template="{email}"` fills with each account's
own value.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class Account:
    name: str
    user_data_dir: Optional[str] = None
    proxy: Optional[Any] = None
    vars: dict[str, Any] = field(default_factory=dict)
    cookies: list[dict[str, Any]] = field(default_factory=list)
    storage_state: Optional[str] = None
    headless: bool = False
    viewport: dict[str, int] = field(default_factory=lambda: {"width": 1280, "height": 800})
    user_agent: Optional[str] = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Account":
        if "name" not in d or not d["name"]:
            raise ValueError("account is missing 'name'")
        return cls(
            name=str(d["name"]),
            user_data_dir=d.get("user_data_dir"),
            proxy=d.get("proxy"),
            vars=dict(d.get("vars") or {}),
            cookies=list(d.get("cookies") or []),
            storage_state=d.get("storage_state"),
            headless=bool(d.get("headless", False)),
            viewport=dict(d.get("viewport") or {"width": 1280, "height": 800}),
            user_agent=d.get("user_agent"),
        )


def load_accounts(path: str | Path) -> list[Account]:
    """Load accounts from a JSON file. Accepts either a list or {"accounts": [...]}.

    Validates name uniqueness; raises ValueError on duplicates.
    """
    text = Path(path).read_text(encoding="utf-8")
    data = json.loads(text)
    if isinstance(data, dict) and "accounts" in data:
        data = data["accounts"]
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a list of accounts (got {type(data).__name__})")

    seen: set[str] = set()
    out: list[Account] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"{path}[{i}]: account must be a JSON object")
        acct = Account.from_dict(item)
        if acct.name in seen:
            raise ValueError(f"duplicate account name: {acct.name!r}")
        seen.add(acct.name)
        out.append(acct)
    return out


def accounts_from_proxies(
    proxies: list[Any],
    *,
    name_prefix: str = "proxy",
    headless: bool = False,
    base_vars: Optional[dict[str, Any]] = None,
    user_data_dir_template: Optional[str] = None,
) -> list[Account]:
    """Synthesise one :class:`Account` per proxy.

    Used by the multi-proxy parallel runner — each worker in the pool gets
    its own ``BrowserContext`` bound to one proxy, so N proxies → N parallel
    pages running the same task config.

    Args:
        proxies: list of proxy values (strings or Playwright dicts) — anything
            accepted by ``proxy_utils.normalize_proxy``.
        name_prefix: account name template; the index is appended (``proxy_1``).
        headless: run each context headless.
        base_vars: optional vars dict copied into every account.
        user_data_dir_template: optional path template; ``{i}`` is replaced
            with the 1-based proxy index, ``{name}`` with the account name.
            When set, every account gets its own persistent profile so
            cookies/storage don't bleed across proxies.

    Returns:
        list[Account] — one per proxy, in input order.
    """
    base_vars = dict(base_vars or {})
    out: list[Account] = []
    for i, proxy in enumerate(proxies, start=1):
        name = f"{name_prefix}_{i}"
        udd: Optional[str] = None
        if user_data_dir_template:
            udd = user_data_dir_template.format(i=i, name=name)
        out.append(
            Account(
                name=name,
                user_data_dir=udd,
                proxy=proxy,
                vars=dict(base_vars),
                headless=headless,
            )
        )
    return out


__all__ = ["Account", "accounts_from_proxies", "load_accounts"]
