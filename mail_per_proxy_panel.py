"""Mail-per-proxy Manager — Tk dialog for binding kuku.lu mailboxes to accounts.

Opens from the main GUI's toolbar (``📧 Mail-per-proxy``) and provides a
visual editor for ``accounts.json`` focused on the kuku.lu disposable
mail integration:

* List view of accounts (name, proxy preview, mailbox address, status).
* **Mint** a fresh kuku.lu identity for the selected account.
* **New address** to rotate the alias when the previous one is spammed.
* **Test inbox** to peek at messages (debug helper).
* **Wait code** to poll for a one-time code with the same logic the
  recorder/replay engine uses.
* **Bind proxy** to attach a ``host:port:user:pass`` string to an
  account.
* **Generate from proxy file** to bulk-create one account per proxy
  line — minting all kuku mailboxes in the background.

The dialog reads/writes the same ``accounts.json`` schema understood by
``accounts.load_accounts`` so the rest of the tool keeps working
unchanged.

The kuku operations themselves are delegated to :class:`kuku_lu.Kuku`;
this module only owns the UI + threading. All blocking calls run in a
worker thread and post results back via a Tk queue, mirroring the
log-handling pattern in ``auto_fill_gui.py``.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import queue
import re
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, Optional

from accounts import Account, load_accounts
from kuku_lu import Kuku, KukuCreds, KukuError, KukuLocalPartTaken
from proxy_utils import load_proxy_dicts, mask_proxy, parse_proxy_string


# Where we persist the per-user OTP defaults (regex / from-filter /
# timeout / poll). Mirrors the ``THEME_PREF_FILE`` pattern used by
# ``auto_fill_gui.py``.
OTP_PREF_FILE = Path.home() / ".auto_form_filler_otp_defaults"

DEFAULT_OTP_REGEX = r"(?<!\d)(\d{5,8})(?!\d)"
DEFAULT_OTP_FROM_FILTER = ""
DEFAULT_OTP_TIMEOUT = 180.0
DEFAULT_OTP_POLL = 4.0

# Known kuku.lu domains as of 2026 — mirrors the dropdown in the
# m.kuku.lu "Add email address" UI. The list is editable in the GUI
# combobox so future-added domains can be typed in directly. ``""`` =
# let kuku.lu pick a random domain (the original ``addMailAddrByAuto``
# endpoint).
KNOWN_KUKU_DOMAINS: list[str] = [
    "",  # auto / random
    "boxfi.uk",
    "haren.uk",
    "bangban.uk",
    "catgroup.uk",
    "goatmail.uk",
    "sendnow.win",
    "ccmail.uk",
    "exdonuts.com",
    "tensi.org",
    "kpay.be",
    "neko2.net",
]


@dataclasses.dataclass
class _AccountRow:
    """In-memory editable representation of one account.

    We don't reuse :class:`accounts.Account` directly because the user
    is mid-edit and may have invalid intermediate state.

    The ``local`` and ``domain`` fields are *desired* values for the
    next mint — they're separate from the live ``kuku.current_address``
    so the user can pre-fill many rows at once and then run “Mint all
    empty”. They persist to the on-disk JSON under ``_pending_local`` /
    ``_pending_domain`` so reloading the file keeps the user's intent.
    The ``status`` field is purely runtime UI — never serialised.
    """
    name: str
    proxy: Optional[str] = None
    user_data_dir: Optional[str] = None
    kuku: Optional[KukuCreds] = None
    raw: dict = dataclasses.field(default_factory=dict)  # untouched extra keys
    local: Optional[str] = None
    domain: Optional[str] = None
    status: str = "idle"  # idle | queued | minting | ok | err: <msg>

    @classmethod
    def from_account(cls, acct: Account, raw: dict) -> "_AccountRow":
        proxy_str: Optional[str] = None
        if acct.proxy is not None:
            if isinstance(acct.proxy, str):
                proxy_str = acct.proxy
            elif isinstance(acct.proxy, dict):
                # Render the Playwright proxy dict back to the canonical
                # ``http://user:pass@host:port`` form for display.
                server = acct.proxy.get("server", "")
                user = acct.proxy.get("username")
                pwd = acct.proxy.get("password")
                if user and pwd and "://" in server:
                    scheme, rest = server.split("://", 1)
                    proxy_str = f"{scheme}://{user}:{pwd}@{rest}"
                else:
                    proxy_str = server
        # Restore pending local/domain hints if present (round-trips so the
        # user's row-form choices survive Save → reopen).
        local_hint = raw.get("_pending_local") if isinstance(raw, dict) else None
        domain_hint = raw.get("_pending_domain") if isinstance(raw, dict) else None
        return cls(
            name=acct.name,
            proxy=proxy_str,
            user_data_dir=acct.user_data_dir,
            kuku=acct.kuku,
            raw=raw,
            local=local_hint or None,
            domain=domain_hint or None,
        )

    def to_dict(self) -> dict:
        out: dict[str, Any] = dict(self.raw)
        out["name"] = self.name
        if self.proxy is not None:
            out["proxy"] = self.proxy
        else:
            out.pop("proxy", None)
        if self.user_data_dir:
            out["user_data_dir"] = self.user_data_dir
        else:
            out.pop("user_data_dir", None)
        if self.kuku is not None:
            out["kuku"] = self.kuku.to_dict()
        else:
            out.pop("kuku", None)
        # Persist pending local/domain so the row's intent survives reloads.
        if self.local:
            out["_pending_local"] = self.local
        else:
            out.pop("_pending_local", None)
        if self.domain:
            out["_pending_domain"] = self.domain
        else:
            out.pop("_pending_domain", None)
        return out


def _format_proxy(proxy: Optional[str]) -> str:
    """Render a proxy string for the table — masks any password.

    ``proxy_utils.mask_proxy`` operates on a Playwright dict; we parse
    the user's string into that shape first so credentials show as
    ``user:***``.
    """
    if not proxy:
        return "—"
    try:
        d = parse_proxy_string(proxy)
        if d:
            return mask_proxy(d)
    except Exception:
        pass
    # Couldn't parse — fall back to a coarse password redaction so we
    # never echo a literal password back to the UI.
    redacted = proxy
    if "@" in redacted and "://" in redacted:
        scheme, rest = redacted.split("://", 1)
        if "@" in rest:
            authpart, hostpart = rest.rsplit("@", 1)
            if ":" in authpart:
                user, _ = authpart.split(":", 1)
                redacted = f"{scheme}://{user}:***@{hostpart}"
    return redacted


def _format_kuku(creds: Optional[KukuCreds]) -> str:
    if creds is None:
        return "—"
    addr = creds.current_address or "(no address)"
    return addr


def load_otp_defaults() -> dict[str, Any]:
    """Read user-saved OTP defaults; tolerant to missing/corrupt file."""
    base = {
        "regex": DEFAULT_OTP_REGEX,
        "from_filter": DEFAULT_OTP_FROM_FILTER,
        "timeout": DEFAULT_OTP_TIMEOUT,
        "poll": DEFAULT_OTP_POLL,
        "domain": "",
    }
    if not OTP_PREF_FILE.exists():
        return base
    try:
        data = json.loads(OTP_PREF_FILE.read_text(encoding="utf-8"))
        return {
            "regex": str(data.get("regex") or DEFAULT_OTP_REGEX),
            "from_filter": str(data.get("from_filter") or DEFAULT_OTP_FROM_FILTER),
            "timeout": float(data.get("timeout") or DEFAULT_OTP_TIMEOUT),
            "poll": float(data.get("poll") or DEFAULT_OTP_POLL),
            "domain": str(data.get("domain") or ""),
        }
    except Exception:
        return base


def save_otp_defaults(defaults: dict[str, Any]) -> None:
    try:
        OTP_PREF_FILE.write_text(
            json.dumps(defaults, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        # GUI prefs are non-critical; never raise from a save.
        pass


def _proxy_to_playwright_dict(proxy_str: str) -> Optional[dict]:
    """Turn ``host:port:user:pass`` (or any form) into a Playwright dict.

    Returns ``None`` if parsing fails so the caller can skip mailbox-mint.
    """
    try:
        return parse_proxy_string(proxy_str)
    except Exception:
        return None


@dataclasses.dataclass
class _BulkSpec:
    """One line parsed out of the Bulk-register textarea.

    The user pastes one entry per line. We accept three shapes so the
    same textbox covers casual usage ("just give me 5 random kpay.be
    aliases") and per-account control ("acct1 | alice@kpay.be | proxy"):

    1. ``local``                                 → name=local, domain=default
    2. ``local@domain``                          → name=local, domain=domain
    3. ``name | local@domain | proxy``           → all three fields explicit
       (any field may be empty / "auto" to defer to defaults)
    """
    name: str
    local_part: Optional[str] = None  # None → kuku.lu picks at random
    domain: Optional[str] = None      # None → kuku.lu picks at random
    proxy: Optional[str] = None       # None → no proxy
    raw: str = ""                     # original line, for error messages


def _parse_bulk_lines(text: str, default_domain: str = "") -> list[_BulkSpec]:
    """Parse the Bulk-register textarea into structured specs.

    Blank lines and ``# comment`` lines are skipped silently. Lines
    that don't fit any shape become a spec with ``name=raw`` and no
    other hints — the bulk runner will surface them as parse errors
    rather than silently drop them.
    """
    out: list[_BulkSpec] = []
    seen_names: set[str] = set()

    def _uniq(base: str) -> str:
        if not base:
            base = "acct"
        n = base
        i = 2
        while n in seen_names:
            n = f"{base}_{i}"
            i += 1
        seen_names.add(n)
        return n

    def _sanitize_local(s: str) -> str:
        s = s.strip().lower()
        s = re.sub(r"[^a-z0-9._\-]+", ".", s).strip("._-")
        return s

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # Three-field form: ``name | local@domain | proxy``
        if "|" in line:
            parts = [p.strip() for p in line.split("|")]
            # Pad to 3
            while len(parts) < 3:
                parts.append("")
            name_raw, addr_raw, proxy_raw = parts[0], parts[1], parts[2]
            local: Optional[str] = None
            domain: Optional[str] = None
            addr_lower = addr_raw.lower() if addr_raw else ""
            if addr_lower in ("auto", "random", "-"):
                # Explicit opt-out — no local_part / domain hint regardless
                # of default_domain. kuku.lu picks both.
                pass
            elif addr_raw:
                if "@" in addr_raw:
                    local, _, domain = addr_raw.partition("@")
                    local = _sanitize_local(local)
                    domain = domain.strip() or None
                else:
                    local = _sanitize_local(addr_raw)
                    domain = default_domain or None
            elif default_domain:
                # Empty alias field — apply default domain so the row
                # uses a known domain rather than a random kuku.lu one.
                domain = default_domain
            name = name_raw or local or _uniq("acct")
            name = _uniq(name)
            proxy = proxy_raw or None
            if proxy and proxy.lower() in ("auto", "none", "-"):
                proxy = None
            out.append(_BulkSpec(
                name=name, local_part=local, domain=domain,
                proxy=proxy, raw=raw_line,
            ))
            continue
        # Single-field forms.
        if "@" in line:
            local, _, domain = line.partition("@")
            local = _sanitize_local(local)
            domain = domain.strip() or None
            name = _uniq(local or "acct")
            out.append(_BulkSpec(
                name=name, local_part=local or None, domain=domain,
                proxy=None, raw=raw_line,
            ))
            continue
        # Bare local part — use default domain.
        local = _sanitize_local(line)
        if not local:
            # Couldn't parse — surface to user with no hints set.
            out.append(_BulkSpec(name=_uniq("acct"), raw=raw_line))
            continue
        name = _uniq(local)
        out.append(_BulkSpec(
            name=name, local_part=local,
            domain=(default_domain or None), proxy=None, raw=raw_line,
        ))
    return out


# --------------------------------------------------------------------------------------
#  Async worker plumbing
# --------------------------------------------------------------------------------------

class _AsyncRunner:
    """Run async kuku.lu calls in a background thread, post results to Tk.

    The dialog has many buttons that each spawn a one-shot coroutine.
    Sharing one event loop in a daemon thread avoids the cost of
    ``asyncio.run()`` per call.
    """

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        def runner() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._ready.set()
            try:
                self._loop.run_forever()
            finally:
                try:
                    self._loop.close()
                except Exception:
                    pass

        self._thread = threading.Thread(target=runner, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=2.0)

    def submit(
        self,
        coro_factory: Callable[[], Any],
        on_done: Callable[[Any, Optional[BaseException]], None],
    ) -> None:
        """Run ``coro_factory()`` on the loop; report the result/exception.

        ``coro_factory`` is a no-arg callable that returns a coroutine —
        we wrap creation so each call runs on the loop's thread (avoids
        the ``loop bound to other thread`` warning).
        """
        if self._loop is None:
            self.start()
        assert self._loop is not None

        def schedule() -> None:
            assert self._loop is not None
            # Bug B8 fix: previously a synchronous failure inside
            # ``coro_factory`` (e.g. closure raised before the coroutine
            # was scheduled) would propagate unhandled into the
            # event-loop thread, killing the runner for ALL subsequent
            # buttons. Catch it here and report through ``on_done`` so
            # the dialog stays usable.
            try:
                coro = coro_factory()
                fut = asyncio.ensure_future(coro, loop=self._loop)
            except BaseException as exc:  # noqa: BLE001
                on_done(None, exc)
                return

            def done_cb(f: asyncio.Future) -> None:
                try:
                    exc = f.exception()
                except BaseException as e:  # noqa: BLE001
                    on_done(None, e)
                    return
                if exc is not None:
                    on_done(None, exc)
                    return
                try:
                    result = f.result()
                except BaseException as e:  # noqa: BLE001
                    on_done(None, e)
                    return
                try:
                    on_done(result, None)
                except BaseException:  # noqa: BLE001
                    # The Tk callback itself raised; we don't want that
                    # to leak into the asyncio loop. The traceback is
                    # already visible because Tk prints it.
                    pass

            fut.add_done_callback(done_cb)

        self._loop.call_soon_threadsafe(schedule)

    def stop(self) -> None:
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop = None
        self._thread = None


# --------------------------------------------------------------------------------------
#  The dialog
# --------------------------------------------------------------------------------------

class MailPerProxyDialog(tk.Toplevel):
    """Modal-ish Toplevel; the parent GUI keeps running in the background."""

    def __init__(self, parent: tk.Misc, *, accounts_path: Optional[Path] = None) -> None:
        super().__init__(parent)
        self.title("Mail-per-proxy Manager")
        self.geometry("1100x640")
        self.minsize(900, 480)
        # Inherit theme by reusing parent's ttk.Style — no extra theme
        # config needed.

        self._parent = parent
        self._accounts_path: Optional[Path] = Path(accounts_path) if accounts_path else None
        self._rows: list[_AccountRow] = []
        self._dirty: bool = False

        self._otp_defaults = load_otp_defaults()

        self._log_queue: "queue.Queue[str]" = queue.Queue()
        self._async_runner = _AsyncRunner()
        self._async_runner.start()

        self._build_ui()
        self._bind_events()

        if self._accounts_path and self._accounts_path.exists():
            self._load_accounts(self._accounts_path)

        self.after(100, self._drain_log)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ ui

    def _build_ui(self) -> None:
        # Top toolbar
        bar = ttk.Frame(self, padding=(8, 6))
        bar.pack(fill="x")
        self.path_var = tk.StringVar(value="(no file loaded)")
        ttk.Label(bar, text="accounts.json:").pack(side="left")
        ttk.Label(bar, textvariable=self.path_var, foreground="#777").pack(side="left", padx=(4, 8))
        ttk.Button(bar, text="Open…", command=self._cmd_open, width=8).pack(side="left")
        ttk.Button(bar, text="Save", command=self._cmd_save, width=6).pack(side="left", padx=(4, 0))
        ttk.Button(bar, text="Save As…", command=self._cmd_save_as, width=10).pack(side="left", padx=(4, 0))
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(bar, text="+ Add", command=self._cmd_add_account, width=8).pack(side="left")
        ttk.Button(bar, text="− Remove", command=self._cmd_remove_account, width=10).pack(side="left", padx=(4, 0))
        ttk.Button(bar, text="From proxy file…", command=self._cmd_from_proxy_file, width=18).pack(side="left", padx=(4, 0))

        # Main split: accounts table on the left, action panel on the right.
        outer = ttk.PanedWindow(self, orient="horizontal")
        outer.pack(fill="both", expand=True, padx=8, pady=4)

        # ----------------- left: accounts table (per-row inline editor)
        # Bug B1/B3/B6 refactor: replaced the [tree + side-form] split with
        # an inline-editable table where each row IS the form for that
        # worker. Empty cells (Local / Domain) act as the “ô trống đăng
        # ký email” the user requested — paste a target alias right into
        # the row, click “Mint all”, and the multi-threaded runner mints
        # everything in parallel through each row's proxy.
        left = ttk.LabelFrame(outer, text="Accounts (double-click cell to edit)", padding=6)
        outer.add(left, weight=4)

        cols = ("name", "local", "domain", "proxy", "kuku", "status")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="extended")
        self.tree.heading("name", text="Name")
        self.tree.heading("local", text="Local part (blank=auto)")
        self.tree.heading("domain", text="Domain (blank=default)")
        self.tree.heading("proxy", text="Proxy (masked)")
        self.tree.heading("kuku", text="Mailbox")
        self.tree.heading("status", text="Status")
        self.tree.column("name", width=110, anchor="w")
        self.tree.column("local", width=140, anchor="w")
        self.tree.column("domain", width=110, anchor="w")
        self.tree.column("proxy", width=220, anchor="w")
        self.tree.column("kuku", width=200, anchor="w")
        self.tree.column("status", width=100, anchor="w")
        self.tree.pack(fill="both", expand=True, side="left")
        # Tag-driven row colouring keeps status visible at a glance.
        self.tree.tag_configure("ok", foreground="#22c55e")
        self.tree.tag_configure("err", foreground="#ef4444")
        self.tree.tag_configure("minting", foreground="#3b82f6")
        self.tree.tag_configure("queued", foreground="#a78bfa")
        self.tree.tag_configure("invalid_proxy", background="#3f1d1d")

        sb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        sb.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=sb.set)

        # Per-row inline edit + context menu — wired up in _bind_events.
        self._inline_editor: Optional[tk.Widget] = None
        self._row_menu = tk.Menu(self.tree, tearoff=0)
        self._row_menu.add_command(label="🆕 Mint mailbox", command=self._cmd_mint_selected_rows)
        self._row_menu.add_command(label="🔄 New address (rotate)", command=self._cmd_new_address_selected)
        self._row_menu.add_command(label="📥 Test inbox", command=self._cmd_test_inbox_selected)
        self._row_menu.add_command(label="⏳ Wait OTP", command=self._cmd_wait_code_selected)
        self._row_menu.add_separator()
        self._row_menu.add_command(label="📋 Copy address", command=self._cmd_copy_address)
        self._row_menu.add_command(label="− Remove row", command=self._cmd_remove_account)

        # ----------------- right: shared defaults + bulk actions
        right_outer = ttk.Frame(outer)
        outer.add(right_outer, weight=2)

        # Quick hint panel (replaces the per-row “Selected account” form
        # — row editing now happens directly in the table).
        hint = ttk.LabelFrame(right_outer, text="How it works", padding=8)
        hint.pack(fill="x", pady=(0, 6))
        ttk.Label(
            hint,
            text=(
                "• Double-click any cell (Name / Local / Domain / Proxy)\n"
                "  to edit it inline.\n"
                "• Right-click a row for per-row Mint / Wait OTP /\n"
                "  Test inbox / Copy address.\n"
                "• Empty Local / Domain ⇒ falls back to the defaults\n"
                "  on the right; mint multi-thread at the bottom."
            ),
            foreground="#777",
            justify="left",
        ).pack(anchor="w")

        # kuku.lu actions
        ka = ttk.LabelFrame(right_outer, text="kuku.lu actions", padding=8)
        ka.pack(fill="x", pady=(0, 6))

        # Domain picker — replicates the dropdown from m.kuku.lu's
        # "Add email address" UI. The combobox is editable so newly
        # added kuku.lu domains can be typed in even if they aren't in
        # ``KNOWN_KUKU_DOMAINS``.
        ttk.Label(ka, text="Domain:").grid(row=0, column=0, sticky="w", padx=(0, 4))
        self.domain_var = tk.StringVar(value=str(self._otp_defaults.get("domain") or ""))
        domain_box = ttk.Combobox(
            ka,
            textvariable=self.domain_var,
            values=KNOWN_KUKU_DOMAINS,
            width=22,
        )
        domain_box.grid(row=0, column=1, sticky="we", padx=(0, 4))
        ttk.Label(
            ka,
            text="(blank = let kuku.lu pick)",
            foreground="#777",
        ).grid(row=0, column=2, columnspan=2, sticky="w", padx=(2, 0))

        # Local part picker — replicates the "Enter an account name of
        # your choice" textbox in the m.kuku.lu UI. Empty = kuku.lu
        # generates a random local part.
        ttk.Label(ka, text="Local part:").grid(row=1, column=0, sticky="w", padx=(0, 4), pady=(4, 0))
        self.local_part_var = tk.StringVar(value="")
        ttk.Entry(ka, textvariable=self.local_part_var, width=24).grid(
            row=1, column=1, sticky="we", padx=(0, 4), pady=(4, 0)
        )
        ttk.Button(
            ka, text="Use Name", command=self._cmd_local_from_name, width=12,
        ).grid(row=1, column=2, sticky="w", pady=(4, 0))
        ttk.Label(
            ka,
            text="(blank = random)",
            foreground="#777",
        ).grid(row=1, column=3, sticky="w", padx=(2, 0), pady=(4, 0))

        ttk.Button(ka, text="🆕 Mint selected", command=self._cmd_mint_selected_rows, width=18).grid(
            row=2, column=0, padx=2, pady=2, columnspan=2, sticky="w"
        )
        ttk.Button(ka, text="🔄 New address", command=self._cmd_new_address_selected, width=18).grid(
            row=2, column=2, padx=2, pady=2, columnspan=2, sticky="w"
        )
        ttk.Button(ka, text="📥 Test inbox", command=self._cmd_test_inbox_selected, width=18).grid(
            row=3, column=0, padx=2, pady=2, columnspan=2, sticky="w"
        )
        ttk.Button(ka, text="⏳ Wait OTP", command=self._cmd_wait_code_selected, width=18).grid(
            row=3, column=2, padx=2, pady=2, columnspan=2, sticky="w"
        )
        ttk.Label(
            ka,
            text=(
                "These act on the SELECTED row(s). Default Domain/Local\n"
                "are used when a row leaves those cells blank. Cloudflare\n"
                "403? Use Recorder to open m.kuku.lu in real Chromium\n"
                "once — cookies are reused."
            ),
            foreground="#777",
            justify="left",
        ).grid(row=4, column=0, columnspan=4, sticky="w", padx=2, pady=(4, 0))
        ka.columnconfigure(1, weight=1)

        # OTP defaults
        otp = ttk.LabelFrame(right_outer, text="OTP defaults (saved across launches)", padding=8)
        otp.pack(fill="x", pady=(0, 6))
        ttk.Label(otp, text="Regex:").grid(row=0, column=0, sticky="w")
        self.otp_regex_var = tk.StringVar(value=str(self._otp_defaults["regex"]))
        ttk.Entry(otp, textvariable=self.otp_regex_var).grid(row=0, column=1, sticky="we", padx=(4, 0))
        ttk.Label(otp, text="From filter:").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.otp_from_var = tk.StringVar(value=str(self._otp_defaults["from_filter"]))
        ttk.Entry(otp, textvariable=self.otp_from_var).grid(row=1, column=1, sticky="we", padx=(4, 0), pady=(4, 0))
        ttk.Label(otp, text="Timeout (s):").grid(row=2, column=0, sticky="w", pady=(4, 0))
        self.otp_timeout_var = tk.StringVar(value=str(self._otp_defaults["timeout"]))
        ttk.Entry(otp, textvariable=self.otp_timeout_var, width=8).grid(
            row=2, column=1, sticky="w", padx=(4, 0), pady=(4, 0)
        )
        ttk.Label(otp, text="Poll (s):").grid(row=3, column=0, sticky="w", pady=(4, 0))
        self.otp_poll_var = tk.StringVar(value=str(self._otp_defaults["poll"]))
        ttk.Entry(otp, textvariable=self.otp_poll_var, width=8).grid(
            row=3, column=1, sticky="w", padx=(4, 0), pady=(4, 0)
        )
        ttk.Button(otp, text="Save defaults", command=self._cmd_save_otp_defaults).grid(
            row=4, column=1, sticky="e", padx=(4, 0), pady=(6, 0)
        )
        # Bug B9: explicit “Clear OTP cache” wipes the in-memory
        # since-timestamps so the next Wait OTP starts fresh.
        ttk.Button(otp, text="Clear OTP cache", command=self._cmd_clear_otp_cache).grid(
            row=4, column=0, sticky="w", pady=(6, 0)
        )
        otp.columnconfigure(1, weight=1)

        # ----------------- Multi-threaded mint controls
        bulk = ttk.LabelFrame(right_outer, text="⚡ Multi-threaded mint", padding=8)
        bulk.pack(fill="x", pady=(0, 6))

        ctrl = ttk.Frame(bulk)
        ctrl.pack(fill="x")
        ttk.Label(ctrl, text="Concurrency:").pack(side="left")
        self.bulk_concurrency_var = tk.IntVar(value=4)
        tk.Spinbox(
            ctrl, from_=1, to=16, increment=1, width=4,
            textvariable=self.bulk_concurrency_var,
        ).pack(side="left", padx=(4, 12))
        ttk.Button(
            ctrl, text="▶ Mint all empty", command=self._cmd_mint_all_empty, width=18,
        ).pack(side="right", padx=(0, 4))
        ttk.Button(
            ctrl, text="▶ Mint selected", command=self._cmd_mint_selected_rows, width=16,
        ).pack(side="right")

        self.bulk_status_var = tk.StringVar(value="idle")
        ttk.Label(bulk, textvariable=self.bulk_status_var, foreground="#3b82f6").pack(
            anchor="w", pady=(4, 0)
        )

        # ----------------- Optional: legacy textarea import (collapsible)
        # Keep the textarea importer behind a toggle button so power users
        # can still paste “name | local@domain | proxy” shorthand. The
        # default workflow is now per-row table edits + Mint all empty.
        paste_box = ttk.LabelFrame(right_outer, text="📋 Paste import (optional)", padding=8)
        paste_box.pack(fill="both", expand=True, pady=(0, 6))
        ttk.Button(
            paste_box, text="▼ Show / hide paste textarea",
            command=self._toggle_paste_box, width=30,
        ).pack(anchor="w")
        self._paste_inner = ttk.Frame(paste_box)
        # Hidden by default; toggled on demand.
        ttk.Label(
            self._paste_inner,
            text=(
                "One per line. Formats:\n"
                "   alice                        → name=alice, default domain\n"
                "   alice@kpay.be                → exact alias\n"
                "   acct1 | alice@kpay.be | host:port:user:pass\n"
                "Empty fields = use defaults. Lines starting with # are skipped."
            ),
            foreground="#777",
            justify="left",
        ).pack(fill="x", anchor="w")
        self.bulk_text = tk.Text(self._paste_inner, height=5, wrap="none",
                                 bg="#0f1115", fg="#d6e3ff", insertbackground="#fff")
        self.bulk_text.pack(fill="both", expand=True, pady=(4, 4))
        ttk.Button(
            self._paste_inner, text="▶ Import as rows + mint",
            command=self._cmd_bulk_register, width=24,
        ).pack(side="right")

        # Log
        log_frame = ttk.LabelFrame(self, text="Log", padding=4)
        log_frame.pack(fill="both", expand=False, padx=8, pady=(0, 6))
        self.log_text = tk.Text(log_frame, height=8, wrap="word", state="disabled",
                                bg="#101010", fg="#d6e3ff")
        self.log_text.pack(fill="both", expand=True, side="left")
        log_sb = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        log_sb.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=log_sb.set)

        # Bottom status
        bot = ttk.Frame(self, padding=(8, 4))
        bot.pack(fill="x")
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(bot, textvariable=self.status_var).pack(side="left")
        ttk.Button(bot, text="Close", command=self._on_close, width=8).pack(side="right")

        # Legacy compatibility shims: external scripts (and the smoke
        # tests) still reference ``sel_name_var`` / ``sel_proxy_var`` /
        # ``sel_mailbox_var`` from the pre-table layout. We keep them as
        # plain StringVars and refresh them inside
        # ``_on_selection_changed`` so reading still works.
        self.sel_name_var = tk.StringVar(value="")
        self.sel_proxy_var = tk.StringVar(value="")
        self.sel_mailbox_var = tk.StringVar(value="")

    def _bind_events(self) -> None:
        # Inline-edit on double-click; row context menu on right-click /
        # macOS Ctrl-click. We also intercept <Escape> to dismiss any
        # active inline editor without committing.
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Button-3>", self._on_right_click)
        self.tree.bind("<Control-Button-1>", self._on_right_click)  # macOS
        # Refresh the legacy ``sel_*_var`` shims + status bar whenever
        # the user picks a different row. Without this, the status
        # label stayed pinned to "Ready." and the compat StringVars
        # never updated, so external callers that watch them couldn't
        # tell which row was active.
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._on_selection_changed())
        self.bind_all("<Escape>", lambda e: self._cancel_inline_edit())

    # ------------------------------------------------------------------ paste box

    def _toggle_paste_box(self) -> None:
        if self._paste_inner.winfo_ismapped():
            self._paste_inner.pack_forget()
        else:
            self._paste_inner.pack(fill="both", expand=True, pady=(4, 0))

    # ------------------------------------------------------------------ logging

    def _log(self, msg: str) -> None:
        self._log_queue.put_nowait(msg)

    def _drain_log(self) -> None:
        try:
            while True:
                line = self._log_queue.get_nowait()
                self.log_text.configure(state="normal")
                self.log_text.insert("end", line + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(100, self._drain_log)

    # ------------------------------------------------------------------ table state

    def _refresh_table(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for i, row in enumerate(self._rows):
            tags = [self._row_tag(row)]
            self.tree.insert(
                "",
                "end",
                iid=str(i),
                values=(
                    row.name,
                    row.local or "",
                    row.domain or "",
                    _format_proxy(row.proxy),
                    _format_kuku(row.kuku),
                    self._status_text(row),
                ),
                tags=tags,
            )

    def _refresh_row(self, idx: int) -> None:
        """Update a single row in-place — cheaper than rebuilding the table."""
        if not (0 <= idx < len(self._rows)):
            return
        row = self._rows[idx]
        try:
            self.tree.item(
                str(idx),
                values=(
                    row.name,
                    row.local or "",
                    row.domain or "",
                    _format_proxy(row.proxy),
                    _format_kuku(row.kuku),
                    self._status_text(row),
                ),
                tags=[self._row_tag(row)],
            )
        except tk.TclError:
            # Row scrolled off / table being rebuilt — fall back to full refresh.
            self._refresh_table()

    def _status_text(self, row: _AccountRow) -> str:
        s = row.status
        if s == "idle":
            return "✓" if row.kuku else "no mailbox"
        if s == "queued":
            return "… queued"
        if s == "minting":
            return "⊙ minting…"
        if s == "ok":
            return "✓ minted"
        if s.startswith("err:"):
            return s[:60]
        return s

    def _row_tag(self, row: _AccountRow) -> str:
        if row.proxy and _proxy_to_playwright_dict(row.proxy) is None:
            return "invalid_proxy"
        s = row.status
        if s == "ok":
            return "ok"
        if s == "minting":
            return "minting"
        if s == "queued":
            return "queued"
        if s.startswith("err:"):
            return "err"
        return ""

    def _selected_index(self) -> Optional[int]:
        sel = self.tree.selection()
        if not sel:
            return None
        try:
            return int(sel[0])
        except ValueError:
            return None

    def _selected_indices(self) -> list[int]:
        out: list[int] = []
        for s in self.tree.selection():
            try:
                out.append(int(s))
            except ValueError:
                pass
        return out

    # ------------------------------------------------------------------ inline edit

    # Map column names to the row attribute they edit; only these are
    # editable. ``kuku`` (mailbox) and ``status`` are read-only.
    _EDITABLE_COLS = {
        "name": "name",
        "local": "local",
        "domain": "domain",
        "proxy": "proxy",
    }

    def _cancel_inline_edit(self) -> None:
        if self._inline_editor is not None:
            try:
                self._inline_editor.destroy()
            except tk.TclError:
                pass
            self._inline_editor = None

    def _on_double_click(self, event: tk.Event) -> str:
        """Open an inline Entry overlay over the clicked cell."""
        self._cancel_inline_edit()
        region = self.tree.identify_region(event.x, event.y)
        if region != "cell":
            return ""
        item_id = self.tree.identify_row(event.y)
        col_id = self.tree.identify_column(event.x)
        if not item_id or not col_id:
            return ""
        try:
            idx = int(item_id)
        except ValueError:
            return ""
        col_idx = int(col_id.replace("#", "")) - 1
        col_name = self.tree["columns"][col_idx]
        if col_name not in self._EDITABLE_COLS:
            return ""
        bbox = self.tree.bbox(item_id, col_id)
        if not bbox:
            return ""
        x, y, w, h = bbox
        # Use a Combobox for the domain (so the kuku list is one click away);
        # plain Entry for the rest.
        attr = self._EDITABLE_COLS[col_name]
        row = self._rows[idx]
        current = getattr(row, attr) or ""
        if col_name == "domain":
            ed = ttk.Combobox(
                self.tree, values=KNOWN_KUKU_DOMAINS, takefocus=True,
            )
            ed.set(current)
        else:
            ed = ttk.Entry(self.tree)
            ed.insert(0, current)
        ed.place(x=x, y=y, width=w, height=h)
        ed.focus_set()
        ed.select_range(0, "end") if hasattr(ed, "select_range") else None
        self._inline_editor = ed

        def commit(_e=None) -> None:
            new_val = ed.get().strip()
            ed.destroy()
            if self._inline_editor is ed:
                self._inline_editor = None
            self._apply_cell_edit(idx, attr, new_val or None)

        ed.bind("<Return>", commit)
        ed.bind("<FocusOut>", commit)
        ed.bind("<Escape>", lambda e: self._cancel_inline_edit())
        return "break"

    def _apply_cell_edit(self, idx: int, attr: str, new_val: Optional[str]) -> None:
        if not (0 <= idx < len(self._rows)):
            return
        row = self._rows[idx]
        if attr == "name":
            new_val = (new_val or "").strip()
            if not new_val:
                self._log("[edit] ignored: name cannot be empty")
                return
            # Reject duplicate names.
            if any(i != idx and r.name == new_val for i, r in enumerate(self._rows)):
                messagebox.showerror(
                    "Mail-per-proxy",
                    f"Account name {new_val!r} already exists.",
                    parent=self,
                )
                return
            row.name = new_val
        elif attr == "local":
            row.local = (new_val or "").strip().lower() or None
            if row.local:
                row.local = re.sub(r"[^a-z0-9._\-]+", ".", row.local).strip("._-") or None
        elif attr == "domain":
            row.domain = (new_val or "").strip() or None
        elif attr == "proxy":
            new_proxy = (new_val or "").strip() or None
            if new_proxy and _proxy_to_playwright_dict(new_proxy) is None:
                # B5: validate at edit-time so the user notices NOW.
                row.status = "err: bad proxy"
                messagebox.showwarning(
                    "Mail-per-proxy",
                    f"Proxy {new_proxy!r} could not be parsed; row will be "
                    "flagged as invalid until you fix it.",
                    parent=self,
                )
            elif row.status.startswith("err: bad proxy"):
                # Clearing or fixing the proxy clears the bad-proxy flag so
                # subsequent mint attempts don't keep showing the old error.
                row.status = "idle"
            row.proxy = new_proxy
        self._refresh_row(idx)
        self._mark_dirty()

    def _on_right_click(self, event: tk.Event) -> str:
        item_id = self.tree.identify_row(event.y)
        if item_id and item_id not in self.tree.selection():
            self.tree.selection_set(item_id)
        if not self.tree.selection():
            return ""
        try:
            self._row_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._row_menu.grab_release()
        return "break"

    def _on_selection_changed(self) -> None:
        # The side-form is gone, but legacy callers (and the smoke
        # tests) still expect ``sel_*_var`` to track the focused row.
        # We refresh them here so ``hasattr`` / read-only consumers
        # keep working.
        idx = self._selected_index()
        if idx is None:
            self.status_var.set("Ready.")
            self.sel_name_var.set("")
            self.sel_proxy_var.set("")
            self.sel_mailbox_var.set("")
            return
        row = self._rows[idx]
        self.status_var.set(
            f"{row.name}: {_format_kuku(row.kuku)}  \u2022  {_format_proxy(row.proxy)}"
        )
        self.sel_name_var.set(row.name)
        self.sel_proxy_var.set(row.proxy or "")
        self.sel_mailbox_var.set(_format_kuku(row.kuku))

    def _mark_dirty(self) -> None:
        self._dirty = True
        title = self.title()
        if not title.endswith("*"):
            self.title(title + " *")

    def _clear_dirty(self) -> None:
        self._dirty = False
        title = self.title()
        if title.endswith(" *"):
            self.title(title[:-2])

    # ------------------------------------------------------------------ load / save

    def _load_accounts(self, path: Path) -> None:
        try:
            raw_text = path.read_text(encoding="utf-8")
            raw_data = json.loads(raw_text)
            if isinstance(raw_data, dict) and "accounts" in raw_data:
                raw_data = raw_data["accounts"]
            if not isinstance(raw_data, list):
                raise ValueError("expected a JSON list")
            accts = load_accounts(path)  # validation
        except Exception as exc:
            messagebox.showerror("Mail-per-proxy", f"Failed to load:\n{exc}", parent=self)
            return
        self._rows = [
            _AccountRow.from_account(a, raw=raw_data[i] if i < len(raw_data) else {})
            for i, a in enumerate(accts)
        ]
        self._accounts_path = path
        self.path_var.set(str(path))
        self._refresh_table()
        self._clear_dirty()
        self._log(f"[load] {len(self._rows)} account(s) from {path}")

    def _cmd_open(self) -> None:
        path = filedialog.askopenfilename(
            title="Open accounts.json",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
            parent=self,
        )
        if path:
            self._load_accounts(Path(path))

    def _save_to(self, path: Path) -> bool:
        # Validate name uniqueness before writing.
        names = [r.name for r in self._rows]
        if len(set(names)) != len(names):
            messagebox.showerror(
                "Mail-per-proxy",
                "Account names must be unique.",
                parent=self,
            )
            return False
        try:
            path.write_text(
                json.dumps([r.to_dict() for r in self._rows], indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:
            messagebox.showerror("Mail-per-proxy", f"Save failed:\n{exc}", parent=self)
            return False
        self._accounts_path = path
        self.path_var.set(str(path))
        self._clear_dirty()
        self._log(f"[save] wrote {len(self._rows)} account(s) → {path}")
        return True

    def _cmd_save(self) -> None:
        if self._accounts_path is None:
            self._cmd_save_as()
            return
        self._save_to(self._accounts_path)

    def _cmd_save_as(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Save accounts.json as",
            defaultextension=".json",
            filetypes=[("JSON", "*.json")],
            parent=self,
        )
        if path:
            self._save_to(Path(path))

    # ------------------------------------------------------------------ row commands

    def _cmd_add_account(self) -> None:
        # Find a unique default name.
        existing = {r.name for r in self._rows}
        i = len(self._rows) + 1
        while f"acct{i}" in existing:
            i += 1
        self._rows.append(_AccountRow(name=f"acct{i}"))
        self._refresh_table()
        self._mark_dirty()
        self.tree.selection_set(str(len(self._rows) - 1))

    def _cmd_remove_account(self) -> None:
        idx = self._selected_index()
        if idx is None:
            return
        if not messagebox.askyesno(
            "Mail-per-proxy",
            f"Remove account {self._rows[idx].name!r}?",
            parent=self,
        ):
            return
        del self._rows[idx]
        self._refresh_table()
        self._mark_dirty()

    # _cmd_apply_edit removed in the per-row table refactor — editing now
    # happens inline via _on_double_click / _apply_cell_edit.

    def _cmd_copy_address(self) -> None:
        idx = self._selected_index()
        if idx is None:
            return
        row = self._rows[idx]
        addr = row.kuku.current_address if row.kuku else None
        if not addr:
            messagebox.showinfo("Mail-per-proxy", "No mailbox on this account.", parent=self)
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(addr)
            self.update()
            self._log(f"[clip] copied {addr}")
        except tk.TclError:
            pass

    def _cmd_from_proxy_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Pick proxies file (one host:port:user:pass per line)",
            filetypes=[("Text / proxy list", "*.txt *.list *.proxies"), ("All", "*.*")],
            parent=self,
        )
        if not path:
            return
        try:
            proxies_dicts = load_proxy_dicts(path, on_error="silent")
        except Exception as exc:
            messagebox.showerror("Mail-per-proxy", f"Failed to parse:\n{exc}", parent=self)
            return
        if not proxies_dicts:
            messagebox.showwarning(
                "Mail-per-proxy",
                "No valid proxies parsed.",
                parent=self,
            )
            return
        # Extract proxy strings from the original file so the row keeps
        # the user's literal input. Fall back to the dict server URL if
        # we can't re-read the line.
        try:
            lines = [
                ln.strip() for ln in Path(path).read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.strip().startswith("#")
            ]
        except Exception:
            lines = []
        existing = {r.name for r in self._rows}
        added = 0
        for i, _ in enumerate(proxies_dicts):
            name_base = f"proxy_{i + 1}"
            n = name_base
            j = 1
            while n in existing:
                j += 1
                n = f"{name_base}_{j}"
            existing.add(n)
            proxy_str = lines[i] if i < len(lines) else proxies_dicts[i].get("server", "")
            row = _AccountRow(name=n, proxy=proxy_str)
            # B5: validate the proxy at row-add time. Invalid strings still
            # make it onto the table (so the user can fix them inline) but
            # are colour-tagged ``invalid_proxy`` and tagged with an
            # explanatory status so they don't silently start mints with
            # broken settings.
            if proxy_str and _proxy_to_playwright_dict(proxy_str) is None:
                row.status = "err: bad proxy"
                self._log(f"[bulk] invalid proxy on {n}: {proxy_str!r}")
            self._rows.append(row)
            added += 1
        self._refresh_table()
        self._mark_dirty()
        self._log(f"[bulk] added {added} account(s) from {path}")
        messagebox.showinfo(
            "Mail-per-proxy",
            f"Added {added} account(s). Use 'Mint mailbox' on each (or "
            "select all + run a future bulk-mint command) to bind kuku creds.",
            parent=self,
        )

    # ------------------------------------------------------------------ kuku ops

    def _selected_row(self) -> Optional[_AccountRow]:
        idx = self._selected_index()
        if idx is None:
            return None
        return self._rows[idx]

    def _busy(self, msg: str) -> None:
        self.status_var.set(msg)

    def _idle(self) -> None:
        self.status_var.set("Ready.")

    def _cmd_local_from_name(self) -> None:
        """Copy each selected row's Name into its Local part column.

        Sanitises whitespace so the value is a valid mailbox local part
        (kuku.lu rejects spaces and most punctuation other than ``.`` /
        ``-`` / ``_``). When the local box at the top is empty AND no
        rows are selected, this also seeds the global Local part field.
        """
        idxs = self._selected_indices()
        if not idxs:
            row = self._selected_row()
            if row is None:
                messagebox.showinfo(
                    "Mail-per-proxy", "Select one or more accounts first.", parent=self,
                )
                return
            local = re.sub(r"[^a-z0-9._\-]+", ".", row.name.strip().lower()).strip("._-")
            if not local:
                messagebox.showinfo(
                    "Mail-per-proxy",
                    "Account name has no valid characters for a local part.",
                    parent=self,
                )
                return
            self.local_part_var.set(local)
            self._log(f"[local] copied name \u2192 {local!r}")
            return
        for i in idxs:
            row = self._rows[i]
            local = re.sub(r"[^a-z0-9._\-]+", ".", row.name.strip().lower()).strip("._-")
            row.local = local or None
        # Back-compat: when exactly one row is selected, also seed the
        # global Local part field so the legacy single-row workflow
        # (and existing tests) keep producing the same result.
        if len(idxs) == 1:
            row = self._rows[idxs[0]]
            local = re.sub(r"[^a-z0-9._\-]+", ".", row.name.strip().lower()).strip("._-")
            if local:
                self.local_part_var.set(local)
            self._log(f"[local] copied name \u2192 {local!r}")
            self._refresh_row(i)
        self._mark_dirty()
        self._log(f"[local] populated {len(idxs)} row(s) from name")

    # ---------------------------------------------------------------- per-row dispatch

    def _effective_domain(self, row: _AccountRow) -> Optional[str]:
        return row.domain or self.domain_var.get().strip() or None

    def _effective_local(self, row: _AccountRow) -> Optional[str]:
        return row.local or self.local_part_var.get().strip() or None

    def _cmd_mint_selected_rows(self) -> None:
        """Multi-thread mint for every selected row.

        Per row: row.local / row.domain take precedence over the global
        Local / Domain inputs. Effective spec.local requires a domain;
        if neither is set we fall back to kuku.lu's auto-pick.
        """
        idxs = self._selected_indices()
        if not idxs:
            messagebox.showinfo("Mail-per-proxy", "Select one or more rows first.", parent=self)
            return
        rows: list[_AccountRow] = []
        specs: list[_BulkSpec] = []
        for i in idxs:
            row = self._rows[i]
            local = self._effective_local(row)
            domain = self._effective_domain(row)
            if local and not domain:
                # kuku.lu rejects a local part without a domain — drop the
                # local hint and let the service auto-pick. The user still
                # gets a mailbox; the row Status will note the fallback.
                local = None
            specs.append(_BulkSpec(
                name=row.name, local_part=local, domain=domain,
                proxy=row.proxy, raw=row.name,
            ))
            rows.append(row)
        concurrency = max(1, min(16, int(self.bulk_concurrency_var.get() or 1)))
        self._log(
            f"[mint] selected: {len(rows)} row(s), concurrency={concurrency}"
        )
        self._run_bulk_mint(rows, specs, concurrency)

    def _cmd_mint_mailbox(self) -> None:
        """Legacy single-row mint — delegates to the multi-thread runner.

        Kept so older code paths / keyboard shortcuts still work, but
        the new in-table workflow is preferred.
        """
        idxs = self._selected_indices()
        if not idxs:
            messagebox.showinfo("Mail-per-proxy", "Select an account first.", parent=self)
            return
        self._cmd_mint_selected_rows()

    def _on_mint_done(self, row: _AccountRow, creds: Any, exc: Optional[BaseException]) -> None:
        """Legacy single-row mint completion handler.

        Bulk mints route through ``_on_bulk_one_done`` instead; this
        handler is kept only for the manual “retry alternative” path
        which still uses ``_async_runner.submit`` directly.
        """
        self._idle()
        if isinstance(exc, KukuLocalPartTaken):
            self._log(
                f"[mint] taken — {exc.requested!r}; alternatives: {exc.alternatives!r}"
            )
            self._prompt_offer_alternatives(row, exc)
            return
        if exc is not None:
            row.status = f"err: {exc}"
            self._log(f"[mint] FAIL — {exc}")
            messagebox.showerror("Mail-per-proxy", f"Mint failed:\n{exc}", parent=self)
            self._refresh_table()
            return
        assert isinstance(creds, KukuCreds)
        row.kuku = creds
        row.status = "ok"
        self._refresh_table()
        # Restore selection.
        for i, r in enumerate(self._rows):
            if r is row:
                self.tree.selection_set(str(i))
                break
        self._mark_dirty()
        self._log(f"[mint] OK {row.name} → {creds.current_address}")

    def _prompt_offer_alternatives(
        self, row: _AccountRow, exc: KukuLocalPartTaken
    ) -> None:
        """Modal dialog asking the user to pick a kuku.lu-suggested address.

        kuku.lu's ``checkNewMailUser`` returns ``OFFER:...`` when the
        requested local part is unavailable but the service can offer
        a free alternative. We show the alternatives in a small dialog
        with a Listbox + Use / Cancel buttons; selecting one re-runs
        :py:meth:`_cmd_mint_mailbox` with that local part / domain.
        """
        win = tk.Toplevel(self)
        win.title("kuku.lu — choose alternative")
        win.transient(self)
        win.grab_set()
        win.geometry("420x260")

        ttk.Label(
            win,
            text=(
                f"kuku.lu says {exc.requested!r} is taken.\n"
                "Pick one of the suggested alternatives to mint instead:"
            ),
            wraplength=400,
            justify="left",
            padding=(10, 8),
        ).pack(fill="x")

        listbox = tk.Listbox(win, height=8)
        listbox.pack(fill="both", expand=True, padx=10, pady=(0, 4))
        for alt in exc.alternatives:
            listbox.insert("end", alt)
        if exc.alternatives:
            listbox.selection_set(0)

        def use_selected() -> None:
            sel = listbox.curselection()
            if not sel:
                return
            chosen = exc.alternatives[sel[0]]
            if "@" not in chosen:
                messagebox.showinfo(
                    "kuku.lu",
                    f"Cannot parse alternative: {chosen!r}",
                    parent=win,
                )
                return
            local, _, dom = chosen.partition("@")
            # Apply the chosen alt to the originating row so the retry
            # multi-thread runner picks it up. We also seed the global
            # fields for visibility.
            row.local = local
            row.domain = dom
            self.local_part_var.set(local)
            self.domain_var.set(dom)
            for i, r in enumerate(self._rows):
                if r is row:
                    self._refresh_row(i)
                    self.tree.selection_set(str(i))
                    break
            win.destroy()
            self._log(f"[mint] retry with chosen alternative {chosen!r}")
            self._cmd_mint_selected_rows()

        btn = ttk.Frame(win, padding=(10, 0, 10, 10))
        btn.pack(fill="x")
        ttk.Button(btn, text="Use this", command=use_selected).pack(side="right")
        ttk.Button(btn, text="Cancel", command=win.destroy).pack(side="right", padx=(0, 6))

    def _cmd_new_address(self) -> None:
        """Rotate the address for the selected row(s).

        Wrapped over ``_cmd_new_address_selected`` so older toolbar wiring
        keeps working.
        """
        self._cmd_new_address_selected()

    def _cmd_new_address_selected(self) -> None:
        idxs = self._selected_indices()
        if not idxs:
            messagebox.showinfo(
                "Mail-per-proxy", "Select one or more rows first.", parent=self,
            )
            return
        targets = [self._rows[i] for i in idxs if self._rows[i].kuku is not None]
        if not targets:
            messagebox.showinfo(
                "Mail-per-proxy",
                "None of the selected rows have a minted mailbox — use Mint first.",
                parent=self,
            )
            return
        for row in targets:
            self._submit_new_address_one(row)

    def _submit_new_address_one(self, row: _AccountRow) -> None:
        proxy_dict = _proxy_to_playwright_dict(row.proxy) if row.proxy else None
        kuku_proxy = _playwright_to_requests_proxy(proxy_dict) if proxy_dict else None
        creds_in = row.kuku
        domain = self._effective_domain(row)
        local_part = self._effective_local(row)
        if local_part and not domain:
            local_part = None  # auto-pick fallback
        row.status = "minting"
        for i, r in enumerate(self._rows):
            if r is row:
                self._refresh_row(i); break
        self._busy(f"Rotating address for {row.name}…")
        self._log(
            f"[new-address] {row.name} "
            f"local={local_part or '(auto)'} domain={domain or '(auto)'}"
        )

        async def task() -> KukuCreds:
            k = await Kuku.from_requests(creds=creds_in, proxy=kuku_proxy)
            kwargs: dict = {}
            if domain:
                kwargs["domain"] = domain
            if local_part:
                kwargs["local_part"] = local_part
            addr = await k.create_address(**kwargs) if kwargs else await k.create_address()
            creds = k.credentials()
            creds.current_address = addr
            await k._backend.aclose()
            return creds

        def done(result: Any, exc: Optional[BaseException]) -> None:
            self.after(0, lambda: self._on_mint_done(row, result, exc))

        self._async_runner.submit(task, done)

    def _cmd_test_inbox(self) -> None:
        self._cmd_test_inbox_selected()

    def _cmd_test_inbox_selected(self) -> None:
        idxs = self._selected_indices()
        if not idxs:
            messagebox.showinfo(
                "Mail-per-proxy", "Select one or more rows first.", parent=self,
            )
            return
        for i in idxs:
            row = self._rows[i]
            if row.kuku is None:
                self._log(f"[inbox] {row.name}: no mailbox — skipping")
                continue
            self._submit_inbox_one(row)

    def _submit_inbox_one(self, row: _AccountRow) -> None:
        proxy_dict = _proxy_to_playwright_dict(row.proxy) if row.proxy else None
        kuku_proxy = _playwright_to_requests_proxy(proxy_dict) if proxy_dict else None
        creds_in = row.kuku
        self._busy(f"Reading inbox of {row.name}…")
        self._log(f"[inbox] {row.name} addr={creds_in.current_address}")

        async def task() -> int:
            k = await Kuku.from_requests(creds=creds_in, proxy=kuku_proxy)
            try:
                mails = await k.list_mails()
                count = len(mails)
                # Read up to 3 most recent for the log.
                for entry in mails[:3]:
                    body = await k.read_mail(entry)
                    snippet = " ".join(body.split())[:120]
                    self._log(f"  · {entry.num}: {snippet}")
                return count
            finally:
                await k._backend.aclose()

        def done(result: Any, exc: Optional[BaseException]) -> None:
            self.after(0, lambda: self._on_inbox_done(row, result, exc))

        self._async_runner.submit(task, done)

    def _on_inbox_done(self, row: _AccountRow, count: Any, exc: Optional[BaseException]) -> None:
        self._idle()
        if exc is not None:
            self._log(f"[inbox] FAIL — {exc}")
            messagebox.showerror("Mail-per-proxy", f"Inbox failed:\n{exc}", parent=self)
            return
        self._log(f"[inbox] {row.name}: {count} message(s)")

    def _cmd_wait_code(self) -> None:
        self._cmd_wait_code_selected()

    def _cmd_wait_code_selected(self) -> None:
        idxs = self._selected_indices()
        if not idxs:
            messagebox.showinfo(
                "Mail-per-proxy", "Select one or more rows first.", parent=self,
            )
            return
        try:
            timeout = float(self.otp_timeout_var.get() or DEFAULT_OTP_TIMEOUT)
            poll = float(self.otp_poll_var.get() or DEFAULT_OTP_POLL)
        except ValueError:
            messagebox.showerror("Mail-per-proxy", "Timeout / poll must be numeric.", parent=self)
            return
        regex = self.otp_regex_var.get() or DEFAULT_OTP_REGEX
        from_filter = self.otp_from_var.get().strip() or None
        for i in idxs:
            row = self._rows[i]
            if row.kuku is None:
                self._log(f"[wait] {row.name}: no mailbox — skipping")
                continue
            self._submit_wait_one(row, regex, from_filter, timeout, poll)

    def _submit_wait_one(
        self,
        row: _AccountRow,
        regex: str,
        from_filter: Optional[str],
        timeout: float,
        poll: float,
    ) -> None:
        proxy_dict = _proxy_to_playwright_dict(row.proxy) if row.proxy else None
        kuku_proxy = _playwright_to_requests_proxy(proxy_dict) if proxy_dict else None
        creds_in = row.kuku
        self._busy(f"Waiting OTP for {row.name} (timeout={timeout:.0f}s)…")
        self._log(f"[wait] {row.name} regex={regex!r} from={from_filter!r} timeout={timeout}")

        # Bug B4: lock the start time on the GUI thread BEFORE we kick
        # off the background task. Passing this as ``since`` to
        # wait_for_code makes it ignore stale codes that were already
        # in the inbox when the user clicked Wait. Without this the
        # button effectively returns the most recent OTP from any past
        # registration over the same mailbox.
        import time as _time
        since = _time.time()

        async def task() -> str:
            k = await Kuku.from_requests(creds=creds_in, proxy=kuku_proxy)
            try:
                return await k.wait_for_code(
                    regex=regex,
                    from_filter=from_filter,
                    timeout=timeout,
                    poll_interval=poll,
                    since=since,
                )
            finally:
                await k._backend.aclose()

        def done(result: Any, exc: Optional[BaseException]) -> None:
            self.after(0, lambda: self._on_wait_done(row, result, exc))

        self._async_runner.submit(task, done)

    def _on_wait_done(self, row: _AccountRow, code: Any, exc: Optional[BaseException]) -> None:
        self._idle()
        if exc is not None:
            self._log(f"[wait] FAIL — {exc}")
            messagebox.showerror("Mail-per-proxy", f"Wait failed:\n{exc}", parent=self)
            return
        self._log(f"[wait] {row.name} got code: {code}")
        try:
            self.clipboard_clear()
            self.clipboard_append(str(code))
            self.update()
            self._log("[clip] copied code to clipboard")
        except tk.TclError:
            pass
        messagebox.showinfo("Mail-per-proxy", f"OTP code: {code}\n(copied to clipboard)", parent=self)

    def _cmd_clear_otp_cache(self) -> None:
        """Reset any per-row “since” timestamps used by Wait OTP.

        Today the panel passes ``since=time.time()`` per click so there
        is no panel-level cache to clear; this button still has value
        because it logs an explicit checkpoint that downstream callers
        can rely on (“I told it to forget anything older than NOW”).
        """
        import time as _time
        self._otp_since_floor = _time.time()
        self._log(
            f"[otp] cache cleared — future Wait OTP ignores codes "
            f"older than t={self._otp_since_floor:.0f}"
        )

    def _cmd_save_otp_defaults(self) -> None:
        try:
            timeout = float(self.otp_timeout_var.get() or DEFAULT_OTP_TIMEOUT)
            poll = float(self.otp_poll_var.get() or DEFAULT_OTP_POLL)
        except ValueError:
            messagebox.showerror(
                "Mail-per-proxy",
                "Timeout / poll must be numeric.",
                parent=self,
            )
            return
        defaults = {
            "regex": self.otp_regex_var.get() or DEFAULT_OTP_REGEX,
            "from_filter": self.otp_from_var.get().strip(),
            "domain": self.domain_var.get().strip(),
            "timeout": timeout,
            "poll": poll,
        }
        save_otp_defaults(defaults)
        self._otp_defaults = defaults
        self._log(f"[prefs] saved OTP defaults → {OTP_PREF_FILE}")

    # ------------------------------------------------------------------ bulk register
    #
    # Both bulk paths (Register all + Mint all empty) share the same
    # plumbing: a list of (row, proxy_dict, kwargs) tasks that we drain
    # through a fixed-size pool of asyncio workers. The worker count is
    # the user-set Concurrency value clamped to [1, 16].

    def _cmd_bulk_register(self) -> None:
        """Parse the textarea, create rows, and mint in parallel.

        Each parsed line becomes a fresh ``_AccountRow`` with the
        provided proxy. Mint requests fan out across N workers; results
        are reported into the row table as they complete.
        """
        text = self.bulk_text.get("1.0", "end")
        default_domain = self.domain_var.get().strip()
        try:
            specs = _parse_bulk_lines(text, default_domain=default_domain)
        except Exception as exc:
            messagebox.showerror("Bulk register", f"Parse failed:\n{exc}", parent=self)
            return
        if not specs:
            messagebox.showinfo(
                "Bulk register",
                "Textarea is empty (or only contains comments).",
                parent=self,
            )
            return

        # Reject duplicates against existing names so the user sees
        # the conflict before we kick off any network work.
        existing_names = {r.name for r in self._rows}
        renamed = 0
        for s in specs:
            base = s.name
            n = s.name
            i = 2
            while n in existing_names:
                n = f"{base}_{i}"
                i += 1
            if n != s.name:
                renamed += 1
            s.name = n
            existing_names.add(n)

        # Validate proxies — bad ones still create a row, but we log
        # so the user knows which proxy failed to parse.
        rows: list[_AccountRow] = []
        for s in specs:
            row = _AccountRow(name=s.name, proxy=s.proxy)
            rows.append(row)
            self._rows.append(row)
        self._refresh_table()
        self._mark_dirty()
        if renamed:
            self._log(f"[bulk] renamed {renamed} duplicate(s) to avoid collisions")

        concurrency = max(1, min(16, int(self.bulk_concurrency_var.get() or 1)))
        self._log(
            f"[bulk] starting register: {len(specs)} line(s), "
            f"concurrency={concurrency}, default_domain={default_domain or '(auto)'}"
        )
        self._run_bulk_mint(rows, specs, concurrency)

    def _cmd_mint_all_empty(self) -> None:
        """Mint a mailbox for every existing row that doesn't have one yet.

        Useful after ``From proxy file…`` populates many rows: instead
        of clicking Mint mailbox per row, this fans them out in
        parallel using the proxy attached to each row.
        """
        targets = [r for r in self._rows if r.kuku is None]
        if not targets:
            messagebox.showinfo(
                "Bulk register",
                "All accounts already have mailboxes.",
                parent=self,
            )
            return
        # Honour any per-row Local / Domain values the user typed
        # into the inline cell editor — they are the whole point of
        # the inline-edit UI. Falls back to the global ``Local part`` /
        # ``Domain`` boxes via ``_effective_*`` when a row left those
        # cells blank.
        specs = [
            _BulkSpec(
                name=r.name,
                local_part=self._effective_local(r),
                domain=self._effective_domain(r),
                proxy=r.proxy,
                raw=r.name,
            )
            for r in targets
        ]
        concurrency = max(1, min(16, int(self.bulk_concurrency_var.get() or 1)))
        self._log(
            f"[bulk] mint-empty: {len(targets)} row(s), concurrency={concurrency}"
        )
        self._run_bulk_mint(targets, specs, concurrency)

    def _run_bulk_mint(
        self,
        rows: list[_AccountRow],
        specs: list[_BulkSpec],
        concurrency: int,
    ) -> None:
        """Drive ``len(rows)`` mint coroutines through ``concurrency`` workers.

        Why one big coroutine instead of many ``submit`` calls? We want
        a real concurrency cap (otherwise a paste of 200 lines fires
        200 simultaneous requests at kuku.lu — Cloudflare instantly
        bans the IP). ``asyncio.Semaphore`` enforces the cap inside a
        single ``asyncio.gather`` and the existing ``_AsyncRunner``
        loop handles thread-safety.
        """
        if not rows:
            return
        total = len(rows)
        # Mutable counters for the progress label — Tk only reads them
        # via ``after``, so plain lists are sufficient.
        counters = {"done": 0, "ok": 0, "err": 0}
        # Mark every queued row up-front so the table immediately
        # reflects the user's intent. Workers later flip them to
        # ``minting`` and finally ``ok`` / ``err``.
        for row in rows:
            row.status = "queued"
        for i, r in enumerate(self._rows):
            if r in rows:
                self._refresh_row(i)
        # Disable the launch button so the user can't start a second
        # bulk mid-flight (would interleave creds wrongly).
        self._bulk_set_busy(True)
        self._update_bulk_status(counters, total)

        async def task() -> dict:
            sem = asyncio.Semaphore(concurrency)

            async def one(row: _AccountRow, spec: _BulkSpec) -> tuple[
                _AccountRow, Optional[KukuCreds], Optional[BaseException]
            ]:
                async with sem:
                    proxy_dict = (
                        _proxy_to_playwright_dict(spec.proxy) if spec.proxy else None
                    )
                    kuku_proxy = (
                        _playwright_to_requests_proxy(proxy_dict) if proxy_dict else None
                    )
                    # Flip the row to ``minting`` on the GUI thread so the
                    # spinner state is visible while the network call runs.
                    def _mark_minting(r=row):
                        r.status = "minting"
                        for j, rr in enumerate(self._rows):
                            if rr is r:
                                self._refresh_row(j); break
                    self.after(0, _mark_minting)
                    try:
                        k = await Kuku.from_requests(proxy=kuku_proxy)
                        try:
                            kwargs: dict = {}
                            if spec.domain:
                                kwargs["domain"] = spec.domain
                            if spec.local_part:
                                kwargs["local_part"] = spec.local_part
                                if not spec.domain:
                                    # kuku.lu requires both — defer to auto.
                                    kwargs.pop("local_part", None)
                            addr = (
                                await k.create_address(**kwargs)
                                if kwargs
                                else await k.create_address()
                            )
                            creds = k.credentials()
                            creds.current_address = addr
                            return (row, creds, None)
                        finally:
                            await k._backend.aclose()
                    except BaseException as exc:  # noqa: BLE001
                        return (row, None, exc)

            tasks = [
                asyncio.ensure_future(one(r, s)) for r, s in zip(rows, specs)
            ]
            for fut in asyncio.as_completed(tasks):
                row, creds, exc = await fut
                # Push the per-row outcome back to Tk on the main thread.
                self.after(
                    0,
                    lambda r=row, c=creds, e=exc:
                        self._on_bulk_one_done(r, c, e, counters, total),
                )
            return counters

        def done(_result: Any, exc: Optional[BaseException]) -> None:
            def finish() -> None:
                self._bulk_set_busy(False)
                if exc is not None:
                    self._log(f"[bulk] FAIL — runner crashed: {exc!r}")
                    messagebox.showerror(
                        "Bulk register",
                        f"Bulk runner crashed:\n{exc}",
                        parent=self,
                    )
                self._log(
                    f"[bulk] finished: {counters['ok']} ok, "
                    f"{counters['err']} err, {counters['done']}/{total}"
                )
            self.after(0, finish)

        self._async_runner.submit(task, done)

    def _on_bulk_one_done(
        self,
        row: _AccountRow,
        creds: Optional[KukuCreds],
        exc: Optional[BaseException],
        counters: dict,
        total: int,
    ) -> None:
        """Per-row mint completion: update row, refresh table, log."""
        counters["done"] += 1
        if exc is not None:
            counters["err"] += 1
            row.status = f"err: {type(exc).__name__}"
            # Surface KukuLocalPartTaken with its alternatives so the
            # user can see which local parts failed instead of a wall
            # of generic errors.
            if isinstance(exc, KukuLocalPartTaken):
                row.status = "err: local taken"
                self._log(
                    f"[bulk] {row.name}: local part taken — "
                    f"alts={exc.alternatives!r}"
                )
            else:
                self._log(f"[bulk] {row.name}: FAIL — {exc}")
        else:
            counters["ok"] += 1
            assert creds is not None
            row.kuku = creds
            row.status = "ok"
            self._log(f"[bulk] {row.name} → {creds.current_address}")
        # Per-row refresh is much cheaper than rebuilding the table on
        # every completion — the latter would visibly stutter at high
        # concurrency. Fall back to full refresh only on TclError.
        for i, r in enumerate(self._rows):
            if r is row:
                self._refresh_row(i); break
        self._mark_dirty()
        self._update_bulk_status(counters, total)

    def _update_bulk_status(self, counters: dict, total: int) -> None:
        self.bulk_status_var.set(
            f"{counters['done']}/{total} done · "
            f"{counters['ok']} ok · {counters['err']} err"
        )

    def _bulk_set_busy(self, busy: bool) -> None:
        if busy:
            self._busy("Bulk minting…")
        else:
            self._idle()

    # ------------------------------------------------------------------ close

    def _on_close(self) -> None:
        if self._dirty:
            ok = messagebox.askyesnocancel(
                "Mail-per-proxy",
                "Unsaved changes — save before closing?",
                parent=self,
            )
            if ok is None:
                return
            if ok and not self._save_or_save_as():
                return
        try:
            self._async_runner.stop()
        except Exception:
            pass
        try:
            self.destroy()
        except tk.TclError:
            pass

    def _save_or_save_as(self) -> bool:
        if self._accounts_path is not None:
            return self._save_to(self._accounts_path)
        path = filedialog.asksaveasfilename(
            title="Save accounts.json",
            defaultextension=".json",
            filetypes=[("JSON", "*.json")],
            parent=self,
        )
        if not path:
            return False
        return self._save_to(Path(path))


def _playwright_to_requests_proxy(proxy_dict: Optional[dict]) -> Optional[dict]:
    """Convert a Playwright proxy dict to the ``requests`` proxies dict.

    Playwright form::

        {"server": "http://1.2.3.4:8080", "username": "u", "password": "p"}

    requests form::

        {"http":  "http://u:p@1.2.3.4:8080",
         "https": "http://u:p@1.2.3.4:8080"}
    """
    if not proxy_dict:
        return None
    server = proxy_dict.get("server")
    if not server:
        return None
    user = proxy_dict.get("username")
    pwd = proxy_dict.get("password")
    if user and pwd and "://" in server:
        scheme, rest = server.split("://", 1)
        # SOCKS proxies need the requests[socks] extra; we still emit
        # the URL because callers may have it installed.
        url = f"{scheme}://{user}:{pwd}@{rest}"
    else:
        url = server
    return {"http": url, "https": url}


def open_dialog(parent: tk.Misc, *, accounts_path: Optional[str] = None) -> MailPerProxyDialog:
    """Convenience entrypoint for the main GUI's toolbar button."""
    return MailPerProxyDialog(parent, accounts_path=Path(accounts_path) if accounts_path else None)


__all__ = [
    "MailPerProxyDialog",
    "open_dialog",
    "load_otp_defaults",
    "save_otp_defaults",
    "OTP_PREF_FILE",
    "_BulkSpec",
    "_parse_bulk_lines",
]
