"""Tkinter GUI for auto_form_filler.

Run:
    python auto_fill_gui.py
    python auto_fill_gui.py path/to/config.json   # open with a preloaded config

Features:
  * Visually edit any config.json — fields, values, target strategies.
  * Add / remove / reorder fields and targets.
  * Toggle headless / dry-run / debug / submit.
  * One-click Run — Playwright runs in a background thread, logs stream live
    into the window. Stop button cancels the run.
  * Save / Save As config without leaving the GUI.

No extra dependencies — Tkinter ships with Python.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import queue
import re
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from types import SimpleNamespace
from typing import Any, Optional

import auto_fill
from logger import get_logger
from proxy_utils import (
    build_playwright_proxy,
    load_proxy_dicts,
    mask_proxy,
    parse_proxy_string,
)

# --------------------------------------------------------------------------------------
#  Field-type / strategy dictionaries shown in dropdowns
# --------------------------------------------------------------------------------------

FIELD_TYPES = [
    "(auto)",
    "text",
    "email",
    "url",
    "textarea",
    "select",
    "radio",
    "checkbox",
    "file",
    "multi_textarea",
]

STRATEGIES = [
    "id",
    "name",
    "css",
    "aria_label",
    "aria_placeholder",
    "aria_labelledby",
    "label_text",
    "placeholder",
    "nearby_text",
    "type",
    "nth",
    "value",
    "data_testid",
    "role",
    "text",
]

EMPTY_CONFIG: dict[str, Any] = {
    "target_url": "",
    "wait_for_selector": "",
    "headless": False,
    "dry_run": True,
    "fields": [],
}


# --------------------------------------------------------------------------------------
#  Log handler that pipes log records into a queue (drained by Tk's main loop)
# --------------------------------------------------------------------------------------


class QueueLogHandler(logging.Handler):
    def __init__(self, q: "queue.Queue[str]") -> None:
        super().__init__()
        self.queue = q
        self.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s [%(levelname)s] %(message)s",
                datefmt="%H:%M:%S",
            )
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(self.format(record))
        except Exception:
            pass


# --------------------------------------------------------------------------------------
#  Main app
# --------------------------------------------------------------------------------------


# --------------------------------------------------------------------------------------
#  Theme
# --------------------------------------------------------------------------------------

THEMES: dict[str, dict[str, str]] = {
    "dark": {
        "bg":         "#181a20",
        "bg_alt":     "#222530",
        "bg_input":   "#0f1116",
        "fg":         "#e6e8ee",
        "fg_muted":   "#9aa0ad",
        "accent":     "#3b82f6",
        "accent_fg":  "#ffffff",
        "border":     "#2c3140",
        "select_bg":  "#2952a3",
        "select_fg":  "#ffffff",
        "log_bg":     "#0b0d12",
        "log_fg":     "#d6e3ff",
        "warning":    "#facc15",
        "error":      "#f87171",
    },
    "light": {
        "bg":         "#f5f6f8",
        "bg_alt":     "#ffffff",
        "bg_input":   "#ffffff",
        "fg":         "#1b1d22",
        "fg_muted":   "#555a66",
        "accent":     "#2563eb",
        "accent_fg":  "#ffffff",
        "border":     "#d2d6dd",
        "select_bg":  "#cfe1ff",
        "select_fg":  "#1b1d22",
        "log_bg":     "#101010",
        "log_fg":     "#d6e3ff",
        "warning":    "#b45309",
        "error":      "#b91c1c",
    },
}

THEME_PREF_FILE = Path.home() / ".auto_form_filler_theme"
LAYOUT_PREF_FILE = Path.home() / ".auto_form_filler_layout"
RECENT_FILES_FILE = Path.home() / ".auto_form_filler_recent"
RECENT_FILES_MAX = 8


# Window sizing constants -- the launcher computes a target geometry from the
# active screen so the GUI looks right on a 13" laptop and a 4K monitor alike.
_TARGET_W_FRACTION = 0.80   # 80% of screen width
_TARGET_H_FRACTION = 0.85   # 85% of screen height
_MAX_W = 1600               # cap so we don't sprawl on 4K
_MAX_H = 1100
_MIN_W = 900
_MIN_H = 600
_FADE_IN_STEPS = 12         # alpha 0 -> 1 in this many frames
_FADE_IN_INTERVAL_MS = 18   # ~16 ms = ~60 fps; 18 ms is comfortable on Tk


class AutoFillGUI(tk.Tk):
    def __init__(self, initial_config_path: Optional[str] = None) -> None:
        super().__init__()
        # Hide the window briefly so the user doesn't see the resize jump
        # before fade-in kicks off.
        try:
            self.attributes("-alpha", 0.0)
        except tk.TclError:
            pass

        self.title("AUTOMAtion")
        self._apply_screen_geometry()
        self.minsize(_MIN_W, _MIN_H)

        self.config_path: Optional[Path] = None
        self.config_data: dict[str, Any] = copy.deepcopy(EMPTY_CONFIG)
        self.selected_field_index: Optional[int] = None
        self.selected_target_index: Optional[int] = None

        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.runner_thread: Optional[threading.Thread] = None
        self.runner_loop: Optional[asyncio.AbstractEventLoop] = None
        self.runner_task: Optional[asyncio.Task] = None

        # Load theme preference (default: dark)
        self.current_theme: str = self._load_theme_pref()
        self.theme_var = tk.StringVar(value=self.current_theme)

        # Dirty-state + per-field run-status (✓ / ✗ / – icons in the list).
        self._dirty: bool = False
        self._field_status: dict[str, str] = {}  # field_id -> "ok" | "fail" | "pending"
        self._field_filter: str = ""
        self._suppress_dirty: int = 0           # used while we re-populate widgets
        self._recent_files: list[str] = self._load_recent_files()

        self._build_ui()
        self._bind_events()
        self._apply_theme(self.current_theme)
        self._install_shortcuts()

        if initial_config_path:
            self._load_config_from_path(Path(initial_config_path))
        else:
            self._refresh_all()
        # Initial title (no dirty marker yet).
        self._update_title()

        self.after(100, self._drain_log_queue)
        # Fade in once the layout has settled.
        self.after(40, self._fade_in)

    # ------------------------------------------------------------------ theme

    @staticmethod
    def _load_theme_pref() -> str:
        try:
            if THEME_PREF_FILE.exists():
                v = THEME_PREF_FILE.read_text(encoding="utf-8").strip().lower()
                if v in THEMES:
                    return v
        except Exception:
            pass
        return "dark"

    def _save_theme_pref(self) -> None:
        try:
            THEME_PREF_FILE.write_text(self.current_theme, encoding="utf-8")
        except Exception:
            pass

    # ------------------------------------------------------------------ layout
    #  Persist window geometry + paned-window sash positions across launches.
    #  Stored as JSON in ~/.auto_form_filler_layout.

    def _restore_layout(self) -> None:
        """Apply geometry + sash positions saved from the last session.

        Called once via ``after(80, ...)`` after the UI has been laid out so
        that ``winfo_width()`` / ``winfo_height()`` return real numbers.
        """
        data: dict = {}
        try:
            if LAYOUT_PREF_FILE.exists():
                data = json.loads(LAYOUT_PREF_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}

        geom = data.get("geometry")
        # Only honour the saved geometry if it still fits on the current
        # screen — avoids the window opening half off-screen when the user
        # plugs in a smaller display since last launch.
        if isinstance(geom, str) and "x" in geom and self._geometry_fits_screen(geom):
            try:
                self.geometry(geom)
                self.update_idletasks()
            except Exception:
                pass

        # Sash positions are RELATIVE to each PanedWindow's own size — never
        # the full toplevel — so we read each pane's height/width directly.
        try:
            self.update_idletasks()
            outer_h = max(self._outer_paned.winfo_height(), 1)
            middle_w = max(self._middle_paned.winfo_width(), 1)
            right_h = max(self._right_paned.winfo_height(), 1)
        except Exception:
            outer_h, middle_w, right_h = 600, 1100, 480

        outer = data.get("outer_sash")
        try:
            # Default: ~70% of vertical space to fields/editor, ~30% to log.
            self._outer_paned.sashpos(0, int(outer) if outer else int(outer_h * 0.70))
        except Exception:
            pass
        middle = data.get("middle_sash")
        try:
            # Default: ~25% to fields list, ~75% to editor+targets.
            self._middle_paned.sashpos(0, int(middle) if middle else int(middle_w * 0.25))
        except Exception:
            pass
        right = data.get("right_sash")
        try:
            # Default: ~40% to field-editor, ~60% to targets table.
            self._right_paned.sashpos(0, int(right) if right else int(right_h * 0.40))
        except Exception:
            pass

    def _geometry_fits_screen(self, geom: str) -> bool:
        """Return True if a saved ``WxH+X+Y`` string still fits the screen."""
        try:
            size, *rest = geom.split("+")
            w, h = (int(v) for v in size.split("x"))
            sw = self.winfo_screenwidth()
            sh = self.winfo_screenheight()
            return w <= sw and h <= sh
        except Exception:
            return False

    def _save_layout(self) -> None:
        try:
            data: dict[str, Any] = {"geometry": self.geometry()}
            try:
                data["outer_sash"] = self._outer_paned.sashpos(0)
            except Exception:
                pass
            try:
                data["middle_sash"] = self._middle_paned.sashpos(0)
            except Exception:
                pass
            try:
                data["right_sash"] = self._right_paned.sashpos(0)
            except Exception:
                pass
            LAYOUT_PREF_FILE.write_text(json.dumps(data), encoding="utf-8")
        except Exception:
            pass

    def _on_close(self) -> None:
        """Save layout (and prompt to save changes), then destroy the window."""
        if not self._confirm_discard():
            return
        self._save_layout()
        try:
            self.destroy()
        except Exception:
            pass

    # ------------------------------------------------------------------ dirty / title

    def _mark_dirty(self) -> None:
        if self._suppress_dirty:
            return
        if not self._dirty:
            self._dirty = True
            self._update_title()

    def _clear_dirty(self) -> None:
        if self._dirty:
            self._dirty = False
            self._update_title()

    def _update_title(self) -> None:
        name = self.config_path.name if self.config_path else "(unsaved)"
        marker = "•" if self._dirty else ""
        try:
            self.title(f"AUTOMAtion — {marker}{name}")
        except Exception:
            pass

    # ------------------------------------------------------------------ window sizing & fade-in

    def _apply_screen_geometry(self) -> None:
        """Pick a window size that fits the user's screen and centre it.

        Uses ``winfo_screenwidth/height`` (the active monitor under the
        cursor on most desktops) and falls back to a sane default if those
        calls error out (e.g. headless / weird WM).
        """
        try:
            sw = self.winfo_screenwidth()
            sh = self.winfo_screenheight()
        except Exception:
            sw, sh = 1366, 768
        w = min(int(sw * _TARGET_W_FRACTION), _MAX_W)
        h = min(int(sh * _TARGET_H_FRACTION), _MAX_H)
        w = max(w, _MIN_W)
        h = max(h, _MIN_H)
        x = max((sw - w) // 2, 0)
        y = max((sh - h) // 2, 0)
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _fade_in(self, step: int = 0) -> None:
        """Animate window alpha from 0 → 1 with smooth easing.

        Tk's ``-alpha`` attribute is only honoured by some window managers
        (it's a no-op on a few X11 setups). We try anyway; if it raises we
        just snap to fully visible.
        """
        try:
            t = (step + 1) / _FADE_IN_STEPS
            # Ease-out cubic for a natural feel.
            alpha = 1.0 - (1.0 - t) ** 3
            self.attributes("-alpha", min(1.0, max(0.0, alpha)))
        except tk.TclError:
            try:
                self.attributes("-alpha", 1.0)
            except tk.TclError:
                pass
            return
        if step + 1 < _FADE_IN_STEPS:
            self.after(_FADE_IN_INTERVAL_MS, self._fade_in, step + 1)

    # ------------------------------------------------------------------ recent files

    def _load_recent_files(self) -> list[str]:
        try:
            if RECENT_FILES_FILE.exists():
                items = json.loads(RECENT_FILES_FILE.read_text(encoding="utf-8"))
                if isinstance(items, list):
                    return [str(p) for p in items if isinstance(p, str)][:RECENT_FILES_MAX]
        except Exception:
            pass
        return []

    def _save_recent_files(self) -> None:
        try:
            RECENT_FILES_FILE.write_text(
                json.dumps(self._recent_files[:RECENT_FILES_MAX]),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _push_recent(self, path: Path) -> None:
        s = str(path)
        self._recent_files = [s] + [p for p in self._recent_files if p != s]
        self._recent_files = self._recent_files[:RECENT_FILES_MAX]
        self._save_recent_files()

    def cmd_show_recent(self) -> None:
        """Drop-down list of recent configs."""
        if not self._recent_files:
            messagebox.showinfo("Recent files", "No recent configs yet.")
            return
        if not self._confirm_discard():
            return
        menu = tk.Menu(self, tearoff=0)
        for p in self._recent_files:
            label = p if len(p) <= 80 else "…" + p[-78:]
            menu.add_command(
                label=label,
                command=lambda pp=p: self._load_config_from_path(Path(pp)),
            )
        menu.add_separator()
        menu.add_command(
            label="Clear recent files",
            command=lambda: (self._recent_files.clear(), self._save_recent_files()),
        )
        try:
            menu.tk_popup(self.recent_btn.winfo_rootx(),
                          self.recent_btn.winfo_rooty() + self.recent_btn.winfo_height())
        finally:
            menu.grab_release()

    # ------------------------------------------------------------------ shortcuts

    def _install_shortcuts(self) -> None:
        """Bind keyboard shortcuts at the toplevel level."""
        self.bind_all("<Control-s>",     lambda e: (self.cmd_save(), "break")[1])
        self.bind_all("<Control-S>",     lambda e: (self.cmd_save(), "break")[1])
        self.bind_all("<Control-Shift-s>", lambda e: (self.cmd_save_as(), "break")[1])
        self.bind_all("<Control-o>",     lambda e: (self.cmd_open(), "break")[1])
        self.bind_all("<Control-O>",     lambda e: (self.cmd_open(), "break")[1])
        self.bind_all("<Control-n>",     lambda e: (self.cmd_new(), "break")[1])
        self.bind_all("<Control-N>",     lambda e: (self.cmd_new(), "break")[1])
        self.bind_all("<F5>",            lambda e: (self.cmd_run(), "break")[1])
        self.bind_all("<Escape>",        lambda e: (self.cmd_stop(), "break")[1])
        self.bind_all("<Control-f>",     lambda e: (self._focus_filter(), "break")[1])
        self.bind_all("<Control-F>",     lambda e: (self._focus_filter(), "break")[1])
        self.bind_all("<Control-q>",     lambda e: (self._on_close(), "break")[1])

    def _focus_filter(self) -> None:
        try:
            self.filter_entry.focus_set()
            self.filter_entry.selection_range(0, "end")
        except Exception:
            pass

    # ------------------------------------------------------------------ field status icons

    @staticmethod
    def _status_glyph(status: Optional[str]) -> str:
        if status == "ok":
            return "✓ "
        if status == "fail":
            return "✗ "
        if status == "pending":
            return "… "
        return "  "

    def _set_field_status(self, field_id: str, status: str) -> None:
        self._field_status[field_id] = status
        self._refresh_field_list()

    def _reset_field_statuses(self) -> None:
        self._field_status.clear()
        self._refresh_field_list()

    # ------------------------------------------------------------------ validation

    def cmd_validate_config(self) -> None:
        """Run a dry sanity-check on the current config without launching a browser.

        Reports duplicate field_ids, fields with empty target lists, fields whose
        ``value`` is meant to be JSON-list (``multi_textarea``) but isn't valid
        JSON, missing ``target_url``, and fields with empty values that aren't
        explicit checkboxes/radios.
        """
        self._commit_field_value()
        self._commit_top_level()
        cfg = self.config_data
        issues: list[str] = []
        warnings: list[str] = []

        if not cfg.get("target_url"):
            issues.append("• target_url is empty")

        fields = cfg.get("fields", [])
        seen: dict[str, int] = {}
        for i, f in enumerate(fields):
            fid = f.get("field_id") or f"<unnamed #{i}>"
            seen[fid] = seen.get(fid, 0) + 1
            if not f.get("targets"):
                issues.append(f"• {fid}: no targets — engine has nothing to resolve")
            if f.get("field_type") == "multi_textarea":
                v = f.get("value")
                if isinstance(v, str):
                    try:
                        json.loads(v)
                    except Exception:
                        issues.append(f"• {fid}: multi_textarea value is not valid JSON")
            ftype = f.get("field_type") or ""
            if ftype not in ("checkbox", "radio") and (f.get("value") in (None, "")):
                warnings.append(f"• {fid}: empty value")
        for fid, n in seen.items():
            if n > 1:
                issues.append(f"• duplicate field_id: {fid!r} (×{n})")

        if not issues and not warnings:
            messagebox.showinfo(
                "Validate config",
                f"All good!  {len(fields)} field(s), no issues."
            )
            return
        text = ""
        if issues:
            text += "Issues (must fix):\n" + "\n".join(issues) + "\n\n"
        if warnings:
            text += "Warnings:\n" + "\n".join(warnings)
        if issues:
            messagebox.showwarning("Validate config", text)
        else:
            messagebox.showinfo("Validate config", text)

    # ------------------------------------------------------------------ test selector

    def cmd_test_selector(self) -> None:
        """Open a browser, navigate to ``target_url``, and try every target of
        the currently-selected field. Report which strategies match.
        """
        self._commit_field_value()
        self._commit_top_level()
        f = self._selected_field()
        if f is None:
            messagebox.showinfo("Test selector", "Select a field first.")
            return
        url = self.config_data.get("target_url", "").strip()
        if not url:
            messagebox.showwarning("Test selector", "Set target_url first.")
            return
        targets = f.get("targets", [])
        if not targets:
            messagebox.showinfo("Test selector", "This field has no targets to test.")
            return

        wait = self.config_data.get("wait_for_selector", "").strip() or None
        proxy = self._gui_proxy()
        fid = f.get("field_id", "<unnamed>")
        self._log_local(f"[TEST] {fid}: opening {url} to test {len(targets)} target(s)…")

        def _worker() -> None:
            try:
                from playwright.sync_api import sync_playwright
            except Exception as exc:
                self.log_queue.put_nowait(f"[TEST_ERR] Playwright not available: {exc}")
                return
            results: list[tuple[str, str, int, str]] = []
            try:
                with sync_playwright() as p:
                    launch_kw: dict = {"headless": False, "args": [
                        "--disable-blink-features=AutomationControlled",
                        "--no-default-browser-check",
                    ]}
                    if proxy:
                        launch_kw["proxy"] = proxy
                    browser = p.chromium.launch(**launch_kw)
                    page = browser.new_page()
                    page.goto(url, wait_until="domcontentloaded")
                    if wait:
                        try:
                            page.wait_for_selector(wait, timeout=10000)
                        except Exception:
                            pass
                    for t in targets:
                        strat = t.get("strategy", "?")
                        sel = t.get("selector", "")
                        try:
                            count = page.evaluate(
                                "(s) => document.querySelectorAll(s).length", sel
                            ) if strat in ("id", "name", "css", "placeholder",
                                           "aria_label", "aria_placeholder",
                                           "type", "data_testid") else None
                            if count is None:
                                # For label-text style strategies we just count via Playwright's get_by_*
                                if strat == "label_text":
                                    count = page.locator(f"label:has-text(\"{sel}\")").count()
                                elif strat == "role":
                                    count = page.get_by_role(sel).count()
                                elif strat == "text":
                                    count = page.get_by_text(sel).count()
                                else:
                                    count = page.locator(sel).count()
                            results.append((strat, sel, int(count), ""))
                        except Exception as exc:
                            results.append((strat, sel, -1, str(exc)[:120]))
                    browser.close()
            except Exception as exc:
                self.log_queue.put_nowait(f"[TEST_ERR] {exc!r}")
                return

            # Report.
            self.log_queue.put_nowait(f"[TEST] {fid}: results")
            any_hit = False
            for strat, sel, count, err in results:
                if count > 0:
                    any_hit = True
                    line = f"  ✓ {strat:14s} matches {count}× — {sel}"
                elif count == 0:
                    line = f"  ✗ {strat:14s} no match    — {sel}"
                else:
                    line = f"  ! {strat:14s} ERROR       — {sel}  ({err})"
                self.log_queue.put_nowait(line)
            self.log_queue.put_nowait(
                f"[TEST] {fid}: {'OK — at least one strategy matched' if any_hit else 'FAILED — no strategy matched'}"
            )

        threading.Thread(target=_worker, daemon=True).start()

    def _apply_theme(self, name: str) -> None:
        if name not in THEMES:
            name = "dark"
        t = THEMES[name]
        self.current_theme = name
        self.theme_var.set(name)

        # Root + tk widgets
        self.configure(bg=t["bg"])
        self.option_add("*Background", t["bg"])
        self.option_add("*Foreground", t["fg"])
        self.option_add("*selectBackground", t["select_bg"])
        self.option_add("*selectForeground", t["select_fg"])
        self.option_add("*Entry.Background", t["bg_input"])
        self.option_add("*Entry.Foreground", t["fg"])
        self.option_add("*Listbox.Background", t["bg_input"])
        self.option_add("*Listbox.Foreground", t["fg"])

        # ttk styling
        style = ttk.Style(self)
        try:
            style.theme_use("clam")  # 'clam' honors background colours on all platforms
        except tk.TclError:
            pass
        style.configure(".", background=t["bg"], foreground=t["fg"], fieldbackground=t["bg_input"], bordercolor=t["border"])
        style.configure("TFrame", background=t["bg"])
        style.configure("TLabel", background=t["bg"], foreground=t["fg"])
        style.configure("TLabelframe", background=t["bg"], foreground=t["fg"], bordercolor=t["border"])
        style.configure("TLabelframe.Label", background=t["bg"], foreground=t["fg_muted"])
        style.configure("TCheckbutton", background=t["bg"], foreground=t["fg"])
        style.map("TCheckbutton", background=[("active", t["bg"])])
        style.configure("TRadiobutton", background=t["bg"], foreground=t["fg"])
        style.configure("TButton", background=t["bg_alt"], foreground=t["fg"], borderwidth=1)
        style.map(
            "TButton",
            background=[("active", t["accent"]), ("pressed", t["accent"])],
            foreground=[("active", t["accent_fg"]), ("pressed", t["accent_fg"])],
        )
        style.configure("TEntry", fieldbackground=t["bg_input"], foreground=t["fg"], insertcolor=t["fg"])
        style.configure("TCombobox", fieldbackground=t["bg_input"], background=t["bg_alt"], foreground=t["fg"])
        style.map("TCombobox", fieldbackground=[("readonly", t["bg_input"])], foreground=[("readonly", t["fg"])])
        style.configure("TNotebook", background=t["bg"], borderwidth=0)
        style.configure("TNotebook.Tab", background=t["bg_alt"], foreground=t["fg_muted"], padding=(10, 4))
        style.map(
            "TNotebook.Tab",
            background=[("selected", t["accent"])],
            foreground=[("selected", t["accent_fg"])],
        )
        style.configure(
            "Treeview",
            background=t["bg_input"],
            fieldbackground=t["bg_input"],
            foreground=t["fg"],
            bordercolor=t["border"],
        )
        style.configure("Treeview.Heading", background=t["bg_alt"], foreground=t["fg"])
        style.map("Treeview", background=[("selected", t["select_bg"])], foreground=[("selected", t["select_fg"])])

        # Walk the widget tree and recolour raw tk widgets (Text, Listbox, Canvas)
        def _recolor(w: tk.Misc) -> None:
            try:
                cls = w.winfo_class()
                if cls in ("Text",):
                    w.configure(
                        bg=t["log_bg"] if getattr(w, "_is_log", False) else t["bg_input"],
                        fg=t["log_fg"] if getattr(w, "_is_log", False) else t["fg"],
                        insertbackground=t["fg"],
                        selectbackground=t["select_bg"],
                        selectforeground=t["select_fg"],
                    )
                elif cls == "Listbox":
                    w.configure(
                        bg=t["bg_input"], fg=t["fg"],
                        selectbackground=t["select_bg"], selectforeground=t["select_fg"],
                        highlightbackground=t["border"],
                    )
                elif cls == "Canvas":
                    w.configure(bg=t["bg"], highlightbackground=t["border"])
                elif cls in ("Toplevel", "Frame"):
                    w.configure(bg=t["bg"])
            except Exception:
                pass
            for child in w.winfo_children():
                _recolor(child)

        _recolor(self)
        # Update muted-foreground labels
        for attr in ("config_path_var",):
            pass  # values not affected
        # Status bar uses a custom foreground; refresh it.
        if hasattr(self, "_status_label"):
            self._status_label.configure(foreground=t["fg_muted"])
        if hasattr(self, "_path_label"):
            self._path_label.configure(foreground=t["fg_muted"])
        if hasattr(self, "theme_btn"):
            self.theme_btn.configure(text="☀ Light" if name == "dark" else "☽ Dark")
        self._save_theme_pref()

    def cmd_toggle_theme(self) -> None:
        new = "light" if self.current_theme == "dark" else "dark"
        self._apply_theme(new)

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        # ---------- Top: config file + global options ----------
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")

        row1 = ttk.Frame(top)
        row1.pack(fill="x")
        ttk.Label(row1, text="Config:").pack(side="left")
        self.config_path_var = tk.StringVar(value="(unsaved)")
        self._path_label = ttk.Label(row1, textvariable=self.config_path_var)
        self._path_label.pack(side="left", padx=(4, 8))
        ttk.Button(row1, text="New", width=6, command=self.cmd_new).pack(side="left")
        ttk.Button(row1, text="Open…", width=8, command=self.cmd_open).pack(side="left", padx=(4, 0))
        self.recent_btn = ttk.Button(row1, text="Recent ▾", width=10, command=self.cmd_show_recent)
        self.recent_btn.pack(side="left", padx=(2, 0))
        ttk.Button(row1, text="Save", width=6, command=self.cmd_save).pack(side="left", padx=(4, 0))
        ttk.Button(row1, text="Save As…", width=10, command=self.cmd_save_as).pack(side="left", padx=(4, 0))
        ttk.Separator(row1, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(row1, text="Validate", width=10, command=self.cmd_validate_config).pack(side="left")
        # Theme toggle (right side)
        self.theme_btn = ttk.Button(
            row1, text="☀ Light", width=10, command=self.cmd_toggle_theme
        )
        self.theme_btn.pack(side="right")

        row2 = ttk.Frame(top)
        row2.pack(fill="x", pady=(8, 0))
        ttk.Label(row2, text="Target URL:").pack(side="left")
        self.url_var = tk.StringVar()
        ttk.Entry(row2, textvariable=self.url_var).pack(side="left", fill="x", expand=True, padx=(4, 8))
        ttk.Label(row2, text="Wait for:").pack(side="left")
        self.wait_var = tk.StringVar()
        ttk.Entry(row2, textvariable=self.wait_var, width=24).pack(side="left", padx=(4, 0))

        row3 = ttk.Frame(top)
        row3.pack(fill="x", pady=(6, 0))
        self.headless_var = tk.BooleanVar()
        self.dry_run_var = tk.BooleanVar()
        self.debug_var = tk.BooleanVar()
        self.submit_var = tk.BooleanVar()
        ttk.Checkbutton(row3, text="headless", variable=self.headless_var).pack(side="left")
        ttk.Checkbutton(row3, text="dry-run", variable=self.dry_run_var).pack(side="left", padx=(8, 0))
        ttk.Checkbutton(row3, text="debug (highlight)", variable=self.debug_var).pack(side="left", padx=(8, 0))
        ttk.Checkbutton(row3, text="submit after fill", variable=self.submit_var).pack(side="left", padx=(8, 0))
        self.captcha_pause_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row3, text="pause on CAPTCHA", variable=self.captcha_pause_var).pack(side="left", padx=(8, 0))
        ttk.Label(row3, text="    Screenshot:").pack(side="left", padx=(20, 0))
        self.screenshot_var = tk.StringVar()
        ttk.Entry(row3, textvariable=self.screenshot_var, width=28).pack(side="left", padx=(4, 0))
        ttk.Button(row3, text="…", width=3, command=self.cmd_pick_screenshot).pack(side="left", padx=(4, 0))

        # Proxy row
        proxy_row = ttk.LabelFrame(top, text="Proxy", padding=6)
        proxy_row.pack(fill="x", pady=(8, 0))
        ttk.Label(proxy_row, text="server:").grid(row=0, column=0, sticky="w")
        self.proxy_server_var = tk.StringVar()
        ttk.Entry(proxy_row, textvariable=self.proxy_server_var, width=36).grid(
            row=0, column=1, sticky="we", padx=(4, 8)
        )
        ttk.Label(proxy_row, text="user:").grid(row=0, column=2, sticky="w")
        self.proxy_user_var = tk.StringVar()
        ttk.Entry(proxy_row, textvariable=self.proxy_user_var, width=14).grid(
            row=0, column=3, sticky="w", padx=(4, 8)
        )
        ttk.Label(proxy_row, text="pass:").grid(row=0, column=4, sticky="w")
        self.proxy_pass_var = tk.StringVar()
        ttk.Entry(proxy_row, textvariable=self.proxy_pass_var, width=14, show="•").grid(
            row=0, column=5, sticky="w", padx=(4, 8)
        )
        ttk.Button(proxy_row, text="Test", width=6, command=self.cmd_test_proxy).grid(
            row=0, column=6, sticky="w", padx=(4, 0)
        )
        # Row 1 — bypass (chỉ riêng dòng này, để dễ nhìn)
        ttk.Label(proxy_row, text="bypass:").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.proxy_bypass_var = tk.StringVar()
        ttk.Entry(proxy_row, textvariable=self.proxy_bypass_var, width=60).grid(
            row=1, column=1, columnspan=6, sticky="we", pady=(4, 0), padx=(4, 8)
        )
        ttk.Label(
            proxy_row,
            text="hosts đi thẳng (không qua proxy), cách nhau bằng dấu phẩy — vd: *.local, 127.0.0.1",
            foreground="#777",
        ).grid(row=2, column=1, columnspan=7, sticky="w", padx=(4, 0))

        # Row 3 — Import .txt (proxy rotation trong 1 tab) — dòng riêng theo yêu cầu
        ttk.Label(
            proxy_row, text="import .txt:",
        ).grid(row=3, column=0, sticky="w", pady=(8, 0))
        self.proxy_list_var = tk.StringVar()
        ttk.Entry(proxy_row, textvariable=self.proxy_list_var, width=44).grid(
            row=3, column=1, columnspan=3, sticky="we", pady=(8, 0), padx=(4, 4)
        )
        ttk.Button(
            proxy_row, text="…", width=3, command=self.cmd_pick_proxy_list,
        ).grid(row=3, column=4, sticky="w", pady=(8, 0))
        ttk.Label(proxy_row, text="rotate:").grid(
            row=3, column=5, sticky="w", pady=(8, 0), padx=(8, 0),
        )
        self.proxy_rotate_var = tk.StringVar(value="round_robin")
        ttk.Combobox(
            proxy_row,
            textvariable=self.proxy_rotate_var,
            values=["round_robin", "random", "none"],
            width=12,
            state="readonly",
        ).grid(row=3, column=6, sticky="w", pady=(8, 0), padx=(4, 0))
        ttk.Label(
            proxy_row,
            text=(
                "import file proxy .txt (host:port:user:pass / 1 dòng / một proxy) "
                "→ rotate trong CÙNG 1 tab. "
                "Muốn chạy nhiều tab song song: dùng hàng \"Proxy pool\" phía dưới."
            ),
            foreground="#777",
            wraplength=720,
            justify="left",
        ).grid(row=4, column=1, columnspan=7, sticky="w", padx=(4, 0), pady=(2, 0))
        proxy_row.columnconfigure(1, weight=1)

        # Chrome profile row
        profile_row = ttk.LabelFrame(top, text="Chrome Profile", padding=6)
        profile_row.pack(fill="x", pady=(8, 0))
        ttk.Label(profile_row, text="user-data-dir:").grid(row=0, column=0, sticky="w")
        self.chrome_profile_var = tk.StringVar()
        ttk.Entry(profile_row, textvariable=self.chrome_profile_var, width=50).grid(
            row=0, column=1, sticky="we", padx=(4, 8)
        )
        ttk.Button(profile_row, text="Browse…", width=8,
                   command=self.cmd_pick_chrome_profile).grid(
            row=0, column=2, sticky="w", padx=(0, 4)
        )
        ttk.Button(profile_row, text="Detect", width=7,
                   command=self.cmd_detect_chrome_profiles).grid(
            row=0, column=3, sticky="w", padx=(0, 0)
        )
        profile_row.columnconfigure(1, weight=1)

        row4 = ttk.Frame(top)
        row4.pack(fill="x", pady=(6, 0))
        self.pick_btn = ttk.Button(row4, text="🎯 Pick from page", command=self.cmd_pick_from_page)
        self.pick_btn.pack(side="left")
        self.record_btn = ttk.Button(row4, text="⏺ Record session", command=self.cmd_record_session)
        self.record_btn.pack(side="left", padx=(6, 0))
        self.mail_per_proxy_btn = ttk.Button(
            row4, text="📧 Mail-per-proxy", command=self.cmd_open_mail_per_proxy
        )
        self.mail_per_proxy_btn.pack(side="left", padx=(6, 0))
        ttk.Label(row4, text="(opens the target URL and adds fields you interact with)",
                  foreground="#777").pack(side="left", padx=(8, 0))

        # ---------- Middle column: drag-resizable nested PanedWindows ----------
        # Outer = vertical: [fields/editor area]  │  [log]
        # Inner top = horizontal: [Fields list]  │  [Field editor over Targets]
        # All sashes are draggable; positions are persisted on close.
        outer_paned = ttk.PanedWindow(self, orient="vertical")
        outer_paned.pack(fill="both", expand=True, padx=8, pady=4)
        self._outer_paned = outer_paned

        middle_paned = ttk.PanedWindow(outer_paned, orient="horizontal")
        outer_paned.add(middle_paned, weight=4)
        self._middle_paned = middle_paned

        # Fields list
        left_frame = ttk.LabelFrame(middle_paned, text="Fields", padding=6)
        middle_paned.add(left_frame, weight=1)

        # Filter row: type to narrow the list (Ctrl+F focuses this).
        filter_row = ttk.Frame(left_frame)
        filter_row.pack(fill="x", pady=(0, 4))
        ttk.Label(filter_row, text="Filter:").pack(side="left")
        self.filter_var = tk.StringVar()
        self.filter_entry = ttk.Entry(filter_row, textvariable=self.filter_var)
        self.filter_entry.pack(side="left", fill="x", expand=True, padx=(4, 0))

        def _on_filter_change(*_a: object) -> None:
            self._field_filter = self.filter_var.get()
            self._refresh_field_list()
        self.filter_var.trace_add("write", _on_filter_change)

        self.field_listbox = tk.Listbox(left_frame, exportselection=False, activestyle="dotbox")
        self.field_listbox.pack(fill="both", expand=True)
        fl_btns = ttk.Frame(left_frame)
        fl_btns.pack(fill="x", pady=(4, 0))
        ttk.Button(fl_btns, text="+ Add", command=self.cmd_add_field).pack(side="left")
        ttk.Button(fl_btns, text="− Remove", command=self.cmd_remove_field).pack(side="left", padx=(4, 0))
        ttk.Button(fl_btns, text="↑", width=3, command=lambda: self.cmd_move_field(-1)).pack(side="left", padx=(4, 0))
        ttk.Button(fl_btns, text="↓", width=3, command=lambda: self.cmd_move_field(1)).pack(side="left", padx=(2, 0))

        # Field editor + targets stacked vertically with their own draggable sash.
        right_paned = ttk.PanedWindow(middle_paned, orient="vertical")
        middle_paned.add(right_paned, weight=3)
        self._right_paned = right_paned

        right_frame = ttk.Frame(right_paned)
        right_paned.add(right_frame, weight=1)

        ed = ttk.LabelFrame(right_frame, text="Field", padding=6)
        ed.pack(fill="both", expand=True)

        ed1 = ttk.Frame(ed)
        ed1.pack(fill="x")
        ttk.Label(ed1, text="field_id:").grid(row=0, column=0, sticky="w")
        self.field_id_var = tk.StringVar()
        ttk.Entry(ed1, textvariable=self.field_id_var, width=30).grid(row=0, column=1, sticky="we", padx=(4, 12))
        ttk.Label(ed1, text="field_type:").grid(row=0, column=2, sticky="w")
        self.field_type_var = tk.StringVar()
        ttk.Combobox(ed1, textvariable=self.field_type_var, values=FIELD_TYPES, width=18, state="readonly").grid(
            row=0, column=3, sticky="w", padx=(4, 0)
        )
        ttk.Button(
            ed1, text="🔍 Test selectors", command=self.cmd_test_selector,
        ).grid(row=0, column=4, sticky="e", padx=(8, 0))
        ed1.columnconfigure(1, weight=1)

        ttk.Label(
            ed,
            text=(
                "value (one line; multi_textarea = JSON list e.g. [\"a\", \"b\"]; "
                "templates: {{date}}, {{uuid4}}, {{random_email}}, {{env:NAME}}):"
            ),
        ).pack(anchor="w", pady=(8, 2))
        self.value_text = tk.Text(ed, height=4, wrap="word")
        self.value_text.pack(fill="x")

        # Targets pane (own scope under right_paned)
        tg_outer = ttk.Frame(right_paned)
        right_paned.add(tg_outer, weight=2)
        tg = ttk.LabelFrame(tg_outer, text="Targets (tried in order — first match wins)", padding=6)
        tg.pack(fill="both", expand=True, pady=(6, 0))

        cols = ("strategy", "selector")
        self.target_tree = ttk.Treeview(tg, columns=cols, show="headings", height=8)
        self.target_tree.heading("strategy", text="strategy")
        self.target_tree.heading("selector", text="selector")
        self.target_tree.column("strategy", width=160, anchor="w")
        self.target_tree.column("selector", anchor="w")
        self.target_tree.pack(fill="both", expand=True)

        tg_edit = ttk.Frame(tg)
        tg_edit.pack(fill="x", pady=(6, 0))
        ttk.Label(tg_edit, text="strategy:").pack(side="left")
        self.target_strategy_var = tk.StringVar()
        ttk.Combobox(tg_edit, textvariable=self.target_strategy_var, values=STRATEGIES, width=18, state="readonly").pack(
            side="left", padx=(4, 12)
        )
        ttk.Label(tg_edit, text="selector:").pack(side="left")
        self.target_selector_var = tk.StringVar()
        ttk.Entry(tg_edit, textvariable=self.target_selector_var).pack(side="left", fill="x", expand=True, padx=(4, 0))

        tg_btns = ttk.Frame(tg)
        tg_btns.pack(fill="x", pady=(4, 0))
        ttk.Button(tg_btns, text="+ Add", command=self.cmd_add_target).pack(side="left")
        ttk.Button(tg_btns, text="↻ Update", command=self.cmd_update_target).pack(side="left", padx=(4, 0))
        ttk.Button(tg_btns, text="− Remove", command=self.cmd_remove_target).pack(side="left", padx=(4, 0))
        ttk.Button(tg_btns, text="↑", width=3, command=lambda: self.cmd_move_target(-1)).pack(side="left", padx=(4, 0))
        ttk.Button(tg_btns, text="↓", width=3, command=lambda: self.cmd_move_target(1)).pack(side="left", padx=(2, 0))

        # ---------- Multi-account row (above the log so it stays visible) ----------
        ma_row = ttk.LabelFrame(self, text="Multi-account", padding=6)
        ma_row.pack(fill="x", padx=8, pady=(2, 0), before=outer_paned)
        ttk.Label(ma_row, text="accounts.json:").grid(row=0, column=0, sticky="w")
        self.accounts_path_var = tk.StringVar()
        ttk.Entry(ma_row, textvariable=self.accounts_path_var, width=40).grid(
            row=0, column=1, sticky="we", padx=(4, 4)
        )
        ttk.Button(ma_row, text="…", width=3, command=self.cmd_pick_accounts).grid(
            row=0, column=2, sticky="w"
        )
        ttk.Label(ma_row, text="workers:").grid(row=0, column=3, sticky="w", padx=(8, 0))
        self.workers_var = tk.StringVar(value="")
        ttk.Entry(ma_row, textvariable=self.workers_var, width=4).grid(
            row=0, column=4, sticky="w", padx=(4, 0)
        )
        self.run_pool_btn = ttk.Button(ma_row, text="▶ Run pool", command=self.cmd_run_pool)
        self.run_pool_btn.grid(row=0, column=5, sticky="w", padx=(8, 0))
        ma_row.columnconfigure(1, weight=1)

        # ---------- Proxy pool row (multi-proxy parallel run) ----------
        pp_row = ttk.LabelFrame(
            self,
            text="Proxy pool — run N pages in parallel, one proxy per page",
            padding=6,
        )
        pp_row.pack(fill="x", padx=8, pady=(2, 0), before=outer_paned)
        ttk.Label(pp_row, text="proxies file:").grid(row=0, column=0, sticky="w")
        self.proxy_pool_path_var = tk.StringVar()
        ttk.Entry(pp_row, textvariable=self.proxy_pool_path_var, width=40).grid(
            row=0, column=1, sticky="we", padx=(4, 4)
        )
        ttk.Button(pp_row, text="…", width=3, command=self.cmd_pick_proxy_pool).grid(
            row=0, column=2, sticky="w"
        )
        ttk.Button(pp_row, text="Validate", width=9, command=self.cmd_validate_proxy_pool).grid(
            row=0, column=3, sticky="w", padx=(4, 0)
        )
        ttk.Label(pp_row, text="parallel:").grid(row=0, column=4, sticky="w", padx=(8, 0))
        self.proxy_pool_workers_var = tk.StringVar(value="")
        ttk.Entry(pp_row, textvariable=self.proxy_pool_workers_var, width=4).grid(
            row=0, column=5, sticky="w", padx=(4, 0)
        )
        self.proxy_pool_persistent_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            pp_row, text="separate profiles", variable=self.proxy_pool_persistent_var,
        ).grid(row=0, column=6, sticky="w", padx=(8, 0))
        self.run_proxy_pool_btn = ttk.Button(
            pp_row, text="▶ Run multi-proxy", command=self.cmd_run_proxy_pool,
        )
        self.run_proxy_pool_btn.grid(row=0, column=7, sticky="w", padx=(8, 0))
        ttk.Label(
            pp_row,
            text=(
                "Format per line: host:port:user:pass  (also accepts "
                "http://user:pass@host:port, host:port, etc.)"
            ),
            foreground="#777",
        ).grid(row=1, column=0, columnspan=8, sticky="w", pady=(4, 0))
        pp_row.columnconfigure(1, weight=1)

        # ---------- Bottom: run controls (above the log so they stay visible) ----------
        bot = ttk.Frame(self, padding=(8, 4))
        bot.pack(fill="x", before=outer_paned)
        self.run_btn = ttk.Button(bot, text="▶ Run", command=self.cmd_run)
        self.run_btn.pack(side="left")
        self.stop_btn = ttk.Button(bot, text="■ Stop", command=self.cmd_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(4, 0))
        ttk.Button(bot, text="Clear log", command=self.cmd_clear_log).pack(side="left", padx=(12, 0))
        self.use_v2_recorder_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            bot, text="Recorder v2 (frame-aware)", variable=self.use_v2_recorder_var
        ).pack(side="left", padx=(12, 0))
        self.status_var = tk.StringVar(value="Ready.")
        self._status_label = ttk.Label(bot, textvariable=self.status_var)
        self._status_label.pack(side="right")

        # Log lives inside the outer PanedWindow so the divider above it
        # (between fields/editor area and log) is draggable.
        log_frame = ttk.LabelFrame(outer_paned, text="Log", padding=6)
        outer_paned.add(log_frame, weight=1)
        self.log_text = tk.Text(
            log_frame, height=10, wrap="word", state="disabled",
            bg="#101010", fg="#d6e3ff",
        )
        self.log_text._is_log = True  # type: ignore[attr-defined]
        self.log_text.pack(fill="both", expand=True, side="left")
        log_sb = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        log_sb.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=log_sb.set)

        # Defer initial sash placement until the window has a real size.
        self.after(80, self._restore_layout)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ events

    def _bind_events(self) -> None:
        self.field_listbox.bind("<<ListboxSelect>>", lambda e: self._on_field_selected())
        self.target_tree.bind("<<TreeviewSelect>>", lambda e: self._on_target_selected())

        # Auto-commit field-editor changes (and mark dirty when the user edits).
        def _meta_changed(*_a: object) -> None:
            self._commit_field_metadata()
            self._mark_dirty()

        def _top_changed(*_a: object) -> None:
            self._commit_top_level()
            self._mark_dirty()

        def _value_changed(_e: object) -> None:
            self._commit_field_value()
            self._mark_dirty()

        self.field_id_var.trace_add("write", _meta_changed)
        self.field_type_var.trace_add("write", _meta_changed)
        self.value_text.bind("<FocusOut>", _value_changed)
        self.value_text.bind("<KeyRelease>", lambda e: self._mark_dirty())

        # Auto-commit top-level config changes
        for var in (
            self.url_var, self.wait_var, self.headless_var, self.dry_run_var,
            self.proxy_server_var, self.proxy_user_var, self.proxy_pass_var,
            self.proxy_bypass_var, self.proxy_list_var, self.proxy_rotate_var,
        ):
            var.trace_add("write", _top_changed)

    # ------------------------------------------------------------------ helpers

    def _refresh_all(self) -> None:
        # Don't let trace_add callbacks flip the dirty flag while we re-populate.
        self._suppress_dirty += 1
        try:
            self._refresh_all_unguarded()
        finally:
            self._suppress_dirty -= 1

    def _refresh_all_unguarded(self) -> None:
        self.url_var.set(self.config_data.get("target_url", ""))
        self.wait_var.set(self.config_data.get("wait_for_selector", "") or "")
        self.headless_var.set(bool(self.config_data.get("headless", False)))
        self.dry_run_var.set(bool(self.config_data.get("dry_run", True)))
        # Auto-enable "submit after fill" when the loaded config carries a
        # captured submit block.  Users can still untick it before running.
        # Honour an explicit ``submit_after_fill`` flag in the config when present.
        if "submit_after_fill" in self.config_data:
            self.submit_var.set(bool(self.config_data.get("submit_after_fill")))
        else:
            submit_block = self.config_data.get("submit")
            self.submit_var.set(bool(submit_block))
        proxy = self.config_data.get("proxy")
        if isinstance(proxy, dict):
            self.proxy_server_var.set(proxy.get("server", ""))
            self.proxy_user_var.set(proxy.get("username", ""))
            self.proxy_pass_var.set(proxy.get("password", ""))
            self.proxy_bypass_var.set(proxy.get("bypass", ""))
        elif isinstance(proxy, str):
            self.proxy_server_var.set(proxy)
            self.proxy_user_var.set("")
            self.proxy_pass_var.set("")
            self.proxy_bypass_var.set("")
        else:
            self.proxy_server_var.set("")
            self.proxy_user_var.set("")
            self.proxy_pass_var.set("")
            self.proxy_bypass_var.set("")
        self.proxy_list_var.set(self.config_data.get("proxy_list", "") or "")
        self.proxy_rotate_var.set(self.config_data.get("proxy_rotate", "round_robin") or "round_robin")
        self.chrome_profile_var.set(self.config_data.get("chrome_profile", "") or "")
        self._refresh_field_list()
        self._on_field_selected()

    def _refresh_field_list(self) -> None:
        """Repopulate the Fields listbox respecting the current filter and
        prefixing each entry with a status glyph (✓ / ✗ / … / blank).

        The listbox indices map 1:1 to ``self._visible_field_indexes``, which
        we keep in sync with ``self.config_data['fields']``.
        """
        self.field_listbox.delete(0, "end")
        flt = self._field_filter.lower().strip()
        self._visible_field_indexes: list[int] = []
        for i, f in enumerate(self.config_data.get("fields", [])):
            fid = f.get("field_id", "<unnamed>")
            if flt:
                hay = " ".join([
                    str(fid),
                    str(f.get("field_type", "") or ""),
                    str(f.get("value", "") or ""),
                ]).lower()
                if flt not in hay:
                    continue
            glyph = self._status_glyph(self._field_status.get(fid))
            self.field_listbox.insert("end", glyph + fid)
            self._visible_field_indexes.append(i)
        # Re-highlight selected.
        if self.selected_field_index is not None:
            try:
                visible_pos = self._visible_field_indexes.index(self.selected_field_index)
                self.field_listbox.selection_set(visible_pos)
                self.field_listbox.activate(visible_pos)
            except ValueError:
                pass

    def _selected_field(self) -> Optional[dict]:
        if self.selected_field_index is None:
            return None
        fields = self.config_data.get("fields", [])
        if 0 <= self.selected_field_index < len(fields):
            return fields[self.selected_field_index]
        return None

    def _on_field_selected(self) -> None:
        sel = self.field_listbox.curselection()
        if not sel:
            return
        # Translate listbox row → real field index, accounting for the filter.
        row = sel[0]
        try:
            self.selected_field_index = self._visible_field_indexes[row]
        except (AttributeError, IndexError):
            self.selected_field_index = row
        f = self._selected_field()
        if f is None:
            return
        # Populating editor widgets fires trace_add — keep dirty flag stable.
        self._suppress_dirty += 1
        try:
            self._populate_editor_for(f)
        finally:
            self._suppress_dirty -= 1
        return

    def _populate_editor_for(self, f: dict) -> None:
        # Populate editor
        self.field_id_var.set(f.get("field_id", ""))
        ftype = f.get("field_type") or "(auto)"
        if ftype not in FIELD_TYPES:
            ftype = "(auto)"
        self.field_type_var.set(ftype)
        self.value_text.delete("1.0", "end")
        val = f.get("value", "")
        if isinstance(val, list):
            self.value_text.insert("1.0", json.dumps(val, ensure_ascii=False))
        else:
            self.value_text.insert("1.0", "" if val is None else str(val))
        # Targets
        self._refresh_target_tree()

    def _refresh_target_tree(self) -> None:
        for iid in self.target_tree.get_children():
            self.target_tree.delete(iid)
        f = self._selected_field()
        if f is None:
            return
        for i, t in enumerate(f.get("targets", [])):
            self.target_tree.insert(
                "", "end", iid=str(i),
                values=(t.get("strategy", ""), t.get("selector", "")),
            )

    def _on_target_selected(self) -> None:
        sel = self.target_tree.selection()
        if not sel:
            self.selected_target_index = None
            return
        self.selected_target_index = int(sel[0])
        f = self._selected_field()
        if f is None:
            return
        targets = f.get("targets", [])
        if 0 <= self.selected_target_index < len(targets):
            t = targets[self.selected_target_index]
            self.target_strategy_var.set(t.get("strategy", ""))
            self.target_selector_var.set(t.get("selector", ""))

    def _commit_top_level(self) -> None:
        # NOTE: only mark dirty when invoked from a user-driven trace_add
        # callback, never when the engine flushes state before save/run.
        self.config_data["target_url"] = self.url_var.get()
        self.config_data["wait_for_selector"] = self.wait_var.get()
        self.config_data["headless"] = bool(self.headless_var.get())
        self.config_data["dry_run"] = bool(self.dry_run_var.get())
        self.config_data["submit_after_fill"] = bool(self.submit_var.get())

        # Proxy: store as a dict if any field is set, otherwise drop the key.
        server = self.proxy_server_var.get().strip()
        if server:
            proxy_dict: dict = {"server": server}
            user = self.proxy_user_var.get().strip()
            pwd = self.proxy_pass_var.get()
            bypass = self.proxy_bypass_var.get().strip()
            if user:
                proxy_dict["username"] = user
            if pwd:
                proxy_dict["password"] = pwd
            if bypass:
                proxy_dict["bypass"] = bypass
            self.config_data["proxy"] = proxy_dict
        else:
            self.config_data.pop("proxy", None)

        plist = self.proxy_list_var.get().strip()
        if plist:
            self.config_data["proxy_list"] = plist
            self.config_data["proxy_rotate"] = self.proxy_rotate_var.get() or "round_robin"
        else:
            self.config_data.pop("proxy_list", None)
            self.config_data.pop("proxy_rotate", None)

        # Chrome profile
        chrome_profile = self.chrome_profile_var.get().strip()
        if chrome_profile:
            self.config_data["chrome_profile"] = chrome_profile
        else:
            self.config_data.pop("chrome_profile", None)

    def _commit_field_metadata(self) -> None:
        f = self._selected_field()
        if f is None:
            return
        new_id = self.field_id_var.get().strip()
        if new_id:
            f["field_id"] = new_id
        ftype = self.field_type_var.get()
        if ftype == "(auto)" or ftype == "":
            f.pop("field_type", None)
        else:
            f["field_type"] = ftype
        # Re-render the entire list so glyphs/filter stay correct.
        self._refresh_field_list()

    def _commit_field_value(self) -> None:
        f = self._selected_field()
        if f is None:
            return
        raw = self.value_text.get("1.0", "end-1c")
        if f.get("field_type") == "multi_textarea":
            try:
                parsed = json.loads(raw) if raw.strip() else []
                if not isinstance(parsed, list):
                    raise ValueError("multi_textarea value must be a JSON list")
                f["value"] = parsed
                return
            except Exception:
                # Fall through to string — user may still be editing
                f["value"] = raw
                return
        # auto-detect: keep bool/list if obviously json
        s = raw.strip()
        if s.lower() == "true":
            f["value"] = True
            return
        if s.lower() == "false":
            f["value"] = False
            return
        if s.startswith("[") or s.startswith("{"):
            try:
                f["value"] = json.loads(s)
                return
            except Exception:
                pass
        f["value"] = raw

    # ------------------------------------------------------------------ commands

    def cmd_new(self) -> None:
        if not self._confirm_discard():
            return
        self.config_path = None
        self.config_path_var.set("(unsaved)")
        self.config_data = copy.deepcopy(EMPTY_CONFIG)
        self.selected_field_index = None
        self._field_status.clear()
        self._refresh_all()
        self._clear_dirty()
        self._update_title()

    def cmd_open(self) -> None:
        if not self._confirm_discard():
            return
        path = filedialog.askopenfilename(
            title="Open config", filetypes=[("JSON", "*.json"), ("All files", "*.*")]
        )
        if path:
            self._load_config_from_path(Path(path))

    def _load_config_from_path(self, path: Path) -> None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            messagebox.showerror("Open failed", f"{path}\n{exc}")
            return
        # Merge with defaults so missing keys don't break the UI
        merged = copy.deepcopy(EMPTY_CONFIG)
        merged.update(data)
        merged.setdefault("fields", [])
        self.config_data = merged
        self.config_path = path
        self.config_path_var.set(str(path))
        self.selected_field_index = 0 if merged["fields"] else None
        self._field_status.clear()
        self._refresh_all()
        self._clear_dirty()
        self._update_title()
        self._push_recent(path)
        self._log_local(f"Loaded {path}")

    def cmd_save(self) -> None:
        self._commit_field_value()
        self._commit_top_level()
        if self.config_path is None:
            self.cmd_save_as()
            return
        try:
            self.config_path.write_text(json.dumps(self.config_data, indent=2, ensure_ascii=False), encoding="utf-8")
            self._log_local(f"Saved {self.config_path}")
            self.status_var.set(f"Saved {self.config_path.name}")
            self._push_recent(self.config_path)
            self._clear_dirty()
            self._update_title()
        except Exception as exc:
            messagebox.showerror("Save failed", str(exc))

    def cmd_save_as(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Save config as", defaultextension=".json",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        self.config_path = Path(path)
        self.config_path_var.set(path)
        self.cmd_save()

    # ------------------------------------------------------------------ proxy helpers

    def _gui_proxy(self) -> Optional[dict]:
        """Build a Playwright proxy dict from the current GUI fields."""
        server = self.proxy_server_var.get().strip()
        if not server:
            return None
        try:
            return build_playwright_proxy(
                server=server,
                username=self.proxy_user_var.get().strip() or None,
                password=self.proxy_pass_var.get() or None,
                bypass=self.proxy_bypass_var.get().strip() or None,
            )
        except Exception as exc:
            self._log_local(f"[PROXY] invalid proxy: {exc}")
            return None

    def cmd_pick_proxy_list(self) -> None:
        path = filedialog.askopenfilename(
            title="Select proxy list (one URL per line)",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if path:
            self.proxy_list_var.set(path)

    def cmd_test_proxy(self) -> None:
        """Validate the proxy fields and try a quick HTTP request through it."""
        server = self.proxy_server_var.get().strip()
        if not server:
            messagebox.showinfo("Test proxy", "No proxy server set — runs will go direct.")
            return
        try:
            proxy = build_playwright_proxy(
                server=server,
                username=self.proxy_user_var.get().strip() or None,
                password=self.proxy_pass_var.get() or None,
            )
        except Exception as exc:
            messagebox.showerror("Test proxy", f"Invalid proxy: {exc}")
            return
        if not proxy:
            messagebox.showerror("Test proxy", "Proxy is empty after parsing.")
            return

        self.status_var.set(f"Testing proxy {mask_proxy(proxy)}…")
        self._log_local(f"[PROXY] testing {mask_proxy(proxy)}")

        def worker() -> None:
            try:
                import urllib.request
                from urllib.parse import urlparse

                u = urlparse(proxy["server"])
                if u.scheme.startswith("socks"):
                    self.log_queue.put_nowait(
                        "[PROXY] SOCKS proxies can't be tested via urllib — "
                        "saved anyway, will be used by Playwright."
                    )
                    return

                user = proxy.get("username", "")
                pwd = proxy.get("password", "")
                netloc = u.hostname + (f":{u.port}" if u.port else "")
                if user:
                    creds = f"{user}:{pwd}@" if pwd else f"{user}@"
                    proxy_url = f"{u.scheme}://{creds}{netloc}"
                else:
                    proxy_url = f"{u.scheme}://{netloc}"

                handler = urllib.request.ProxyHandler(
                    {"http": proxy_url, "https": proxy_url}
                )
                opener = urllib.request.build_opener(handler)
                req = urllib.request.Request(
                    "https://api.ipify.org?format=text",
                    headers={"User-Agent": "auto_form_filler/proxy-test"},
                )
                with opener.open(req, timeout=10) as resp:
                    body = resp.read().decode("utf-8", errors="replace").strip()
                self.log_queue.put_nowait(f"[PROXY] OK — egress IP: {body}")
            except Exception as exc:
                self.log_queue.put_nowait(f"[PROXY] FAILED: {exc!r}")
            finally:
                self.after(0, lambda: self.status_var.set("Ready."))

        threading.Thread(target=worker, daemon=True).start()

    def cmd_pick_chrome_profile(self) -> None:
        """Open a directory picker for the Chrome user-data-dir."""
        path = filedialog.askdirectory(title="Select Chrome user-data-dir")
        if path:
            self.chrome_profile_var.set(path)
            self._mark_dirty()

    def cmd_detect_chrome_profiles(self) -> None:
        """Auto-detect common Chrome profile directories on this system."""
        import platform
        candidates: list[str] = []
        home = Path.home()
        system = platform.system()
        if system == "Windows":
            local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
            candidates = [
                str(local / "Google" / "Chrome" / "User Data"),
                str(local / "Chromium" / "User Data"),
                str(local / "Google" / "Chrome Beta" / "User Data"),
            ]
        elif system == "Darwin":
            candidates = [
                str(home / "Library" / "Application Support" / "Google" / "Chrome"),
                str(home / "Library" / "Application Support" / "Chromium"),
            ]
        else:  # Linux
            candidates = [
                str(home / ".config" / "google-chrome"),
                str(home / ".config" / "chromium"),
                str(home / ".config" / "google-chrome-beta"),
                str(home / "snap" / "chromium" / "common" / "chromium"),
            ]
        found: list[str] = []
        for c in candidates:
            p = Path(c)
            if p.is_dir():
                found.append(c)
                # Also list sub-profiles (Default, Profile 1, etc.)
                for sub in sorted(p.iterdir()):
                    if sub.is_dir() and (sub / "Preferences").exists():
                        found.append(str(sub))
        if not found:
            messagebox.showinfo(
                "Chrome profiles",
                "No Chrome/Chromium profile directories found on this system.\n\n"
                "Use Browse… to pick a directory manually.",
            )
            return
        # Show a selection popup
        win = tk.Toplevel(self)
        win.title("Detected Chrome Profiles")
        win.geometry("500x300")
        win.transient(self)
        win.grab_set()
        lb = tk.Listbox(win, selectmode="browse", font=("Consolas", 10))
        lb.pack(fill="both", expand=True, padx=8, pady=8)
        for item in found:
            lb.insert("end", item)

        def _pick() -> None:
            sel = lb.curselection()
            if sel:
                self.chrome_profile_var.set(lb.get(sel[0]))
                self._mark_dirty()
            win.destroy()

        ttk.Button(win, text="Select", command=_pick).pack(pady=(0, 8))

    def cmd_pick_screenshot(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Screenshot path", defaultextension=".png",
            filetypes=[("PNG", "*.png"), ("All files", "*.*")],
        )
        if path:
            self.screenshot_var.set(path)

    def cmd_add_field(self) -> None:
        new = {"field_id": f"field_{len(self.config_data.get('fields', [])) + 1}", "value": "", "targets": []}
        self.config_data.setdefault("fields", []).append(new)
        self.selected_field_index = len(self.config_data["fields"]) - 1
        self._refresh_field_list()
        self._on_field_selected()
        self._mark_dirty()

    def cmd_remove_field(self) -> None:
        if self.selected_field_index is None:
            return
        del self.config_data["fields"][self.selected_field_index]
        self.selected_field_index = max(0, self.selected_field_index - 1) if self.config_data["fields"] else None
        self._refresh_field_list()
        self._on_field_selected()
        self._mark_dirty()

    def cmd_move_field(self, delta: int) -> None:
        if self.selected_field_index is None:
            return
        i = self.selected_field_index
        j = i + delta
        fs = self.config_data["fields"]
        if 0 <= j < len(fs):
            fs[i], fs[j] = fs[j], fs[i]
            self.selected_field_index = j
            self._refresh_field_list()
            self._mark_dirty()

    def cmd_add_target(self) -> None:
        f = self._selected_field()
        if f is None:
            return
        strat = self.target_strategy_var.get() or "id"
        sel = self.target_selector_var.get() or ""
        f.setdefault("targets", []).append({"strategy": strat, "selector": sel})
        self._refresh_target_tree()
        last = len(f["targets"]) - 1
        self.target_tree.selection_set(str(last))
        self.target_tree.see(str(last))
        self._mark_dirty()

    def cmd_update_target(self) -> None:
        f = self._selected_field()
        if f is None or self.selected_target_index is None:
            return
        targets = f.get("targets", [])
        if 0 <= self.selected_target_index < len(targets):
            targets[self.selected_target_index] = {
                "strategy": self.target_strategy_var.get(),
                "selector": self.target_selector_var.get(),
            }
            self._refresh_target_tree()
            self.target_tree.selection_set(str(self.selected_target_index))
            self._mark_dirty()

    def cmd_remove_target(self) -> None:
        f = self._selected_field()
        if f is None or self.selected_target_index is None:
            return
        del f["targets"][self.selected_target_index]
        self.selected_target_index = None
        self._refresh_target_tree()
        self._mark_dirty()

    def cmd_move_target(self, delta: int) -> None:
        f = self._selected_field()
        if f is None or self.selected_target_index is None:
            return
        i = self.selected_target_index
        j = i + delta
        ts = f.get("targets", [])
        if 0 <= j < len(ts):
            ts[i], ts[j] = ts[j], ts[i]
            self.selected_target_index = j
            self._refresh_target_tree()
            self.target_tree.selection_set(str(j))

    # -------------------- Pick / Record (browser-driven helpers) --------------------

    def _set_pickrecord_buttons(self, state: str) -> None:
        try:
            self.pick_btn.configure(state=state)
            self.record_btn.configure(state=state)
        except Exception:
            pass

    def cmd_pick_from_page(self) -> None:
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("Missing URL", "Set the target URL first.")
            return
        self._set_pickrecord_buttons("disabled")
        self.status_var.set("Pick mode — opening browser, click any element…")
        self._log_local("[PICK] launching browser…")
        wait = self.wait_var.get().strip() or None

        proxy = self._gui_proxy()

        def worker() -> None:
            try:
                import picker
                # Newer picker.pick_one_sync supports a `proxy` kwarg; older ones don't.
                try:
                    snap = picker.pick_one_sync(url, wait, proxy=proxy)
                except TypeError:
                    snap = picker.pick_one_sync(url, wait)
            except Exception as exc:
                self.log_queue.put_nowait(f"[PICK_ERR] {exc!r}")
                snap = None
            self.after(0, lambda: self._on_pick_done(snap))

        threading.Thread(target=worker, daemon=True).start()

    def _on_pick_done(self, snap: Optional[dict]) -> None:
        self._set_pickrecord_buttons("normal")
        self.status_var.set("Ready.")
        if not snap:
            self._log_local("[PICK] cancelled.")
            return
        import picker
        field = picker.snapshot_to_field(snap)
        self.config_data.setdefault("fields", []).append(field)
        self.selected_field_index = len(self.config_data["fields"]) - 1
        self._refresh_field_list()
        self._on_field_selected()
        self._log_local(
            f"[PICK] added '{field['field_id']}' "
            f"({field.get('field_type', 'auto')}) with {len(field['targets'])} target(s)"
        )

    def cmd_open_mail_per_proxy(self) -> None:
        """Open the Mail-per-proxy Manager dialog.

        Pre-fills with ``accounts.json`` next to the active config when
        present, otherwise opens empty and lets the user pick a file.
        """
        try:
            from mail_per_proxy_panel import MailPerProxyDialog
        except Exception as exc:
            messagebox.showerror(
                "Mail-per-proxy",
                f"Failed to import dialog module:\n{exc}",
                parent=self,
            )
            return
        candidate: Optional[Path] = None
        if self.config_path is not None:
            sibling = self.config_path.parent / "accounts.json"
            if sibling.exists():
                candidate = sibling
        MailPerProxyDialog(self, accounts_path=candidate)

    def cmd_record_session(self) -> None:
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("Missing URL", "Set the target URL first.")
            return
        self._set_pickrecord_buttons("disabled")
        self.status_var.set("Recording — fill the form, then click 'Done' in the browser overlay.")
        self._log_local("[REC ] launching browser…")
        wait = self.wait_var.get().strip() or None

        # Auto-save config + events into ./recordings/recorded_<ts>.json next to the GUI.
        from datetime import datetime
        recordings_dir = Path.cwd() / "recordings"
        recordings_dir.mkdir(exist_ok=True)
        out_path = recordings_dir / f"recorded_{datetime.now():%Y%m%d_%H%M%S}.json"

        proxy = self._gui_proxy()
        chrome_profile = self.chrome_profile_var.get().strip() or None

        use_v2 = bool(self.use_v2_recorder_var.get())

        def worker() -> None:
            try:
                if use_v2:
                    import recorder_v2
                    config, events = recorder_v2.record_to_config_sync(
                        target_url=url,
                        wait_for_selector=wait,
                        out_path=str(out_path),
                        proxy=proxy,
                        chrome_profile=chrome_profile,
                    )
                else:
                    import recorder
                    if hasattr(recorder, "record_to_config_sync"):
                        try:
                            config, events = recorder.record_to_config_sync(
                                target_url=url,
                                wait_for_selector=wait,
                                out_path=str(out_path),
                                proxy=proxy,
                            )
                        except TypeError:
                            config, events = recorder.record_to_config_sync(
                                target_url=url,
                                wait_for_selector=wait,
                                out_path=str(out_path),
                            )
                    else:
                        fields = recorder.record_session_sync(url, wait)
                        config = {"fields": fields}
                        events = []
            except Exception as exc:
                self.log_queue.put_nowait(f"[REC_ERR] {exc!r}")
                config, events = {"fields": []}, []
            self.after(0, lambda: self._on_record_done(config, events, out_path))

        threading.Thread(target=worker, daemon=True).start()

    def _on_record_done(self, config: dict, events: list, out_path: Path) -> None:
        self._set_pickrecord_buttons("normal")
        self.status_var.set("Ready.")
        if not isinstance(config, dict):
            self._log_local("[REC ] no config returned.")
            return

        # v2 recorder produces "actions". v1 recorder produces "fields".
        actions = config.get("actions") or []
        fields = config.get("fields") or []
        if not actions and not fields:
            self._log_local("[REC ] nothing captured.")
            return

        if actions:
            # v2: replace the whole config_data so we don't mix v1 fields with v2 actions.
            self.config_data = config
            self._log_local(f"[REC ] v2 config with {len(actions)} action(s) loaded")
            if config.get("submit"):
                self._log_local("[REC ] submit captured")
            self._refresh_all()
            self._log_local(f"[REC ] auto-saved → {out_path}")
            return

        # ---- v1 path ----
        # Add captured fields to the current config_data.
        self.config_data.setdefault("fields", []).extend(fields)

        # If the user hasn't set wait_for_selector / submit_selectors yet, fill them in
        # from the recording (saves a manual step).
        if not (self.config_data.get("wait_for_selector") or "").strip():
            ws = config.get("wait_for_selector", "")
            if ws:
                self.config_data["wait_for_selector"] = ws
                if hasattr(self, "wait_var"):
                    self.wait_var.set(ws)
        if config.get("submit_selectors") and not self.config_data.get("submit_selectors"):
            self.config_data["submit_selectors"] = list(config["submit_selectors"])

        self.selected_field_index = len(self.config_data["fields"]) - 1
        self._refresh_field_list()
        self._refresh_all() if hasattr(self, "_refresh_all") else self._on_field_selected()
        self._log_local(
            f"[REC ] added {len(fields)} field(s) "
            f"({len(config.get('submit_selectors') or [])} submit selector(s), "
            f"{len(events)} raw event(s)) from session"
        )
        self._log_local(f"[REC ] auto-saved → {out_path}")
        self._show_record_toast(out_path, len(fields), len(events))

    def _show_record_toast(self, out_path: Path, n_fields: int, n_events: int) -> None:
        """Small non-blocking popup with a one-click 'Open file' / 'Open folder' action."""
        try:
            top = tk.Toplevel(self)
        except Exception:
            return
        top.title("Recording saved")
        top.transient(self)
        top.resizable(False, False)
        # Position near the top-right of the main window.
        try:
            self.update_idletasks()
            x = self.winfo_rootx() + self.winfo_width() - 360
            y = self.winfo_rooty() + 60
            top.geometry(f"340x140+{max(x, 20)}+{max(y, 20)}")
        except Exception:
            pass

        ttk.Label(top, text="✓ Recording saved", font=("", 11, "bold")).pack(
            anchor="w", padx=12, pady=(10, 2)
        )
        ttk.Label(
            top,
            text=f"{n_fields} field(s), {n_events} event(s)",
            foreground="#555",
        ).pack(anchor="w", padx=12)
        ttk.Label(top, text=str(out_path), foreground="#1e6", wraplength=310).pack(
            anchor="w", padx=12, pady=(2, 8)
        )

        btns = ttk.Frame(top)
        btns.pack(fill="x", padx=8, pady=(0, 8))

        def _open_file() -> None:
            try:
                self._load_config_from_path(out_path)
                self._log_local(f"[REC ] loaded {out_path}")
            except Exception as exc:
                messagebox.showerror("Open failed", str(exc))
            top.destroy()

        def _open_folder() -> None:
            try:
                import subprocess, sys as _sys
                folder = str(out_path.parent)
                if _sys.platform == "darwin":
                    subprocess.Popen(["open", folder])
                elif _sys.platform.startswith("win"):
                    subprocess.Popen(["explorer", folder])
                else:
                    subprocess.Popen(["xdg-open", folder])
            except Exception:
                pass

        ttk.Button(btns, text="Load this config", command=_open_file).pack(side="left")
        ttk.Button(btns, text="Open folder", command=_open_folder).pack(side="left", padx=(6, 0))
        ttk.Button(btns, text="Dismiss", command=top.destroy).pack(side="right")

        # Auto-dismiss after 8s if the user doesn't click.
        top.after(8000, lambda: top.winfo_exists() and top.destroy())

    def cmd_clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    # ------------------------------------------------------------------ run / stop

    def cmd_run(self) -> None:
        if self.runner_thread and self.runner_thread.is_alive():
            return
        self._commit_field_value()
        self._commit_top_level()
        if not self.config_data.get("target_url"):
            messagebox.showwarning("Missing URL", "Please set a target_url before running.")
            return

        # Build args namespace consumed by auto_fill.run
        chrome_profile = self.chrome_profile_var.get().strip() or None
        args = SimpleNamespace(
            headless=bool(self.headless_var.get()),
            dry_run=bool(self.dry_run_var.get()),
            debug=bool(self.debug_var.get()),
            submit=bool(self.submit_var.get()),
            screenshot=self.screenshot_var.get() or None,
            config=str(self.config_path) if self.config_path else "(in-memory)",
            profile=None,
            chrome_profile=chrome_profile,
            no_captcha_pause=not bool(self.captcha_pause_var.get()),
            proxy=None,                         # GUI uses config["proxy"] directly
            proxy_list=None,
            proxy_rotate=None,
            proxy_bypass=None,
            no_proxy=False,
        )
        config = copy.deepcopy(self.config_data)
        if chrome_profile:
            config["chrome_profile"] = chrome_profile

        # Wire a queue handler into the engine's logger
        engine_logger = get_logger(debug=args.debug)
        for h in list(engine_logger.handlers):
            if isinstance(h, QueueLogHandler):
                engine_logger.removeHandler(h)
        engine_logger.addHandler(QueueLogHandler(self.log_queue))

        self.run_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status_var.set("Running…")
        # Reset per-field status icons; they'll be re-populated as the run streams logs.
        self._current_log_field = None  # type: ignore[attr-defined]
        self._reset_field_statuses()

        self.runner_thread = threading.Thread(
            target=self._runner_thread_main, args=(config, args, engine_logger), daemon=True
        )
        self.runner_thread.start()

    def _runner_thread_main(self, config, args, logger) -> None:
        try:
            loop = asyncio.new_event_loop()
            self.runner_loop = loop
            asyncio.set_event_loop(loop)
            self.runner_task = loop.create_task(auto_fill.run(config, args, logger))
            loop.run_until_complete(self.runner_task)
        except asyncio.CancelledError:
            self.log_queue.put_nowait("[CANCELLED] run stopped by user")
        except Exception as exc:
            self.log_queue.put_nowait(f"[ERROR] {exc!r}")
        finally:
            try:
                if self.runner_loop and not self.runner_loop.is_closed():
                    self.runner_loop.close()
            except Exception:
                pass
            self.runner_loop = None
            self.runner_task = None
            self.after(0, self._on_run_finished)

    def _on_run_finished(self) -> None:
        self.run_btn.configure(state="normal")
        if hasattr(self, "run_proxy_pool_btn"):
            self.run_proxy_pool_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.status_var.set("Ready.")

    def cmd_stop(self) -> None:
        if self.runner_loop and self.runner_task and not self.runner_task.done():
            self.runner_loop.call_soon_threadsafe(self.runner_task.cancel)

    # ------------------------------------------------------------------ multi-account run

    def cmd_pick_accounts(self) -> None:
        path = filedialog.askopenfilename(
            title="Select accounts.json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if path:
            self.accounts_path_var.set(path)

    def cmd_run_pool(self) -> None:
        accounts_path = self.accounts_path_var.get().strip()
        if not accounts_path or not Path(accounts_path).exists():
            messagebox.showwarning("Multi-account", "Pick a valid accounts.json first.")
            return
        if self.runner_thread and self.runner_thread.is_alive():
            return

        self._commit_field_value()
        self._commit_top_level()
        if not self.config_data.get("target_url"):
            messagebox.showwarning("Missing URL", "Please set a target_url before running.")
            return

        try:
            workers = int(self.workers_var.get().strip() or 0)
        except ValueError:
            workers = 0

        config_snapshot = copy.deepcopy(self.config_data)
        engine_logger = get_logger(debug=False)
        engine_logger.addHandler(QueueLogHandler(self.log_queue))

        self.run_btn.configure(state="disabled")
        self.run_pool_btn.configure(state="disabled")
        # Mirrors cmd_run_proxy_pool — _on_pool_finished re-enables
        # all three buttons, so disabling all three on entry keeps
        # the disable/enable cycle symmetric. Otherwise this button
        # stays visually clickable mid-run; ``runner_thread.is_alive()``
        # guards against double execution but the UI still misleads
        # the user.
        if hasattr(self, "run_proxy_pool_btn"):
            self.run_proxy_pool_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status_var.set("Running pool…")
        self._log_local(f"[POOL] starting with accounts={accounts_path} workers={workers or 'auto'}")

        def worker() -> None:
            try:
                from accounts import load_accounts
                from worker_pool import Task, WorkerPool

                accts = load_accounts(accounts_path)
                self.log_queue.put_nowait(f"[POOL] loaded {len(accts)} account(s)")

                tasks = [
                    # Each Task gets its own deep copy so that any
                    # worker-local mutation of nested config (e.g.
                    # ``actions`` lists for retries, ``submit`` overrides
                    # for self-healing) cannot leak across concurrent
                    # workers. Mirrors auto_fill.run_multi_proxy.
                    Task(config=copy.deepcopy(config_snapshot), vars=dict(a.vars), label=a.name)
                    for a in accts
                ]

                def report(evt: str, payload: dict) -> None:
                    bits = [f"[{evt}]"]
                    for k in ("account", "label", "ok", "filled", "skipped",
                              "attempts", "duration_s", "error", "proxy"):
                        if k in payload and payload[k] is not None:
                            bits.append(f"{k}={payload[k]!r}")
                    self.log_queue.put_nowait(" ".join(bits))

                pool = WorkerPool(
                    accts,
                    max_concurrency=workers or len(accts),
                    report_cb=report,
                    dry_run=bool(self.dry_run_var.get()),
                    debug=False,
                )

                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self.runner_loop = loop
                self.runner_task = loop.create_task(pool.run_tasks(tasks))
                results = loop.run_until_complete(self.runner_task)
                ok = sum(1 for r in results if r.ok)
                self.log_queue.put_nowait(
                    f"[POOL] done — {ok}/{len(results)} task(s) succeeded"
                )
            except Exception as exc:
                self.log_queue.put_nowait(f"[POOL_ERR] {exc!r}")
            finally:
                # Close the per-run event loop so its selector / fd
                # resources don't accumulate across repeated button
                # clicks. Mirrors the single-run path's ``finally``
                # block (auto_fill_gui.py: cmd_run inner worker).
                try:
                    if self.runner_loop and not self.runner_loop.is_closed():
                        self.runner_loop.close()
                except Exception:
                    pass
                self.runner_loop = None
                self.runner_task = None
                self.after(0, self._on_pool_finished)

        self.runner_thread = threading.Thread(target=worker, daemon=True)
        self.runner_thread.start()

    def _on_pool_finished(self) -> None:
        self.run_btn.configure(state="normal")
        self.run_pool_btn.configure(state="normal")
        self.run_proxy_pool_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.status_var.set("Ready.")

    # ------------------------------------------------------------------ multi-proxy run

    def cmd_pick_proxy_pool(self) -> None:
        path = filedialog.askopenfilename(
            title="Select proxies file (host:port:user:pass per line)",
            filetypes=[
                ("Text / proxy list", "*.txt *.list *.proxies *.csv"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self.proxy_pool_path_var.set(path)

    def cmd_validate_proxy_pool(self) -> None:
        """Parse the selected proxy file and report how many proxies are valid."""
        path = self.proxy_pool_path_var.get().strip()
        if not path:
            messagebox.showinfo("Proxy pool", "Pick a proxies file first.")
            return
        if not Path(path).exists():
            messagebox.showwarning("Proxy pool", f"File not found:\n{path}")
            return
        try:
            proxies = load_proxy_dicts(path, on_error="silent")
        except Exception as exc:
            messagebox.showerror("Proxy pool", f"Failed to parse:\n{exc}")
            return
        if not proxies:
            messagebox.showwarning(
                "Proxy pool",
                "No valid proxies parsed. Each line must be like\n"
                "host:port:user:pass  (or http://user:pass@host:port).",
            )
            return
        sample = "\n".join(f"  {i + 1}. {mask_proxy(p)}" for i, p in enumerate(proxies[:8]))
        more = "" if len(proxies) <= 8 else f"\n  … {len(proxies) - 8} more"
        self._log_local(f"[PROXY-POOL] {len(proxies)} proxy/ies parsed from {path}")
        messagebox.showinfo(
            "Proxy pool",
            f"Parsed {len(proxies)} proxy/ies:\n\n{sample}{more}",
        )

    def cmd_run_proxy_pool(self) -> None:
        """Run the current config in parallel — one BrowserContext per proxy."""
        if self.runner_thread and self.runner_thread.is_alive():
            return
        path = self.proxy_pool_path_var.get().strip()
        if not path or not Path(path).exists():
            messagebox.showwarning("Proxy pool", "Pick a valid proxies file first.")
            return

        self._commit_field_value()
        self._commit_top_level()
        if not self.config_data.get("target_url"):
            messagebox.showwarning("Missing URL", "Please set a target_url before running.")
            return

        try:
            workers = int(self.proxy_pool_workers_var.get().strip() or 0)
        except ValueError:
            workers = 0

        config_snapshot = copy.deepcopy(self.config_data)
        # Don't double-apply the single-proxy fields when running the pool —
        # each worker uses its own proxy from the file.
        config_snapshot.pop("proxy", None)
        config_snapshot.pop("proxy_list", None)
        config_snapshot.pop("proxy_rotate", None)

        engine_logger = get_logger(debug=bool(self.debug_var.get()))
        for h in list(engine_logger.handlers):
            if isinstance(h, QueueLogHandler):
                engine_logger.removeHandler(h)
        engine_logger.addHandler(QueueLogHandler(self.log_queue))

        headless = bool(self.headless_var.get())
        dry_run = bool(self.dry_run_var.get())
        debug = bool(self.debug_var.get())
        persistent = bool(self.proxy_pool_persistent_var.get())

        self.run_btn.configure(state="disabled")
        self.run_pool_btn.configure(state="disabled")
        self.run_proxy_pool_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status_var.set("Running multi-proxy…")
        self._log_local(
            f"[PROXY-POOL] starting with proxies={path} parallel={workers or 'auto'} "
            f"profiles={'separate' if persistent else 'ephemeral'}"
        )

        def worker() -> None:
            try:
                from accounts import accounts_from_proxies
                from worker_pool import Task, WorkerPool

                proxies = load_proxy_dicts(path, on_error="warn")
                if not proxies:
                    self.log_queue.put_nowait(
                        "[PROXY-POOL] no valid proxies — aborting"
                    )
                    return
                self.log_queue.put_nowait(
                    f"[PROXY-POOL] loaded {len(proxies)} proxy/ies"
                )

                udd_template = None
                if persistent:
                    base = Path.home() / ".auto_form_filler_profiles"
                    base.mkdir(parents=True, exist_ok=True)
                    udd_template = str(base / "{name}")

                accts = accounts_from_proxies(
                    proxies,
                    headless=headless,
                    user_data_dir_template=udd_template,
                )
                tasks = [
                    # Each Task gets its own deep copy so that any
                    # worker-local mutation of nested config (e.g.
                    # ``actions`` lists for retries, ``submit`` overrides
                    # for self-healing) cannot leak across concurrent
                    # workers. Mirrors auto_fill.run_multi_proxy.
                    Task(config=copy.deepcopy(config_snapshot), vars=dict(a.vars), label=a.name)
                    for a in accts
                ]
                self.log_queue.put_nowait(
                    f"[PROXY-POOL] {len(tasks)} task(s) queued, "
                    f"max concurrency={workers or len(accts)}"
                )

                def report(evt: str, payload: dict) -> None:
                    bits = [f"[{evt}]"]
                    for k in ("account", "label", "ok", "filled", "skipped",
                              "attempts", "duration_s", "error", "proxy"):
                        if k in payload and payload[k] is not None:
                            bits.append(f"{k}={payload[k]!r}")
                    self.log_queue.put_nowait(" ".join(bits))

                pool = WorkerPool(
                    accts,
                    max_concurrency=workers or len(accts),
                    report_cb=report,
                    dry_run=dry_run,
                    debug=debug,
                )

                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self.runner_loop = loop
                self.runner_task = loop.create_task(pool.run_tasks(tasks))
                results = loop.run_until_complete(self.runner_task)
                ok = sum(1 for r in results if r.ok)
                self.log_queue.put_nowait(
                    f"[PROXY-POOL] done — {ok}/{len(results)} task(s) succeeded"
                )
            except Exception as exc:
                self.log_queue.put_nowait(f"[PROXY-POOL_ERR] {exc!r}")
            finally:
                # Close the per-run event loop so its selector / fd
                # resources don't accumulate across repeated button
                # clicks. Mirrors the single-run path's ``finally``
                # block (auto_fill_gui.py: cmd_run inner worker).
                try:
                    if self.runner_loop and not self.runner_loop.is_closed():
                        self.runner_loop.close()
                except Exception:
                    pass
                self.runner_loop = None
                self.runner_task = None
                self.after(0, self._on_pool_finished)

        self.runner_thread = threading.Thread(target=worker, daemon=True)
        self.runner_thread.start()

    # ------------------------------------------------------------------ log queue draining

    def _drain_log_queue(self) -> None:
        try:
            while True:
                line = self.log_queue.get_nowait()
                self._append_log(line)
                self._maybe_update_status_from_log(line)
        except queue.Empty:
            pass
        self.after(80, self._drain_log_queue)

    # Regexes used by _maybe_update_status_from_log.
    _RE_FIELD_START = re.compile(r"\[FIELD\]\s+(\S+)")
    _RE_ACT_START = re.compile(r"\[ACT \]\s+#\d+\s+\S+\s+(\S+)")
    _RE_FILL_OK = re.compile(r"\[FILL_OK\]|\[SUCCESS\]")
    _RE_FILL_FAIL = re.compile(r"\[FILL_FAIL\]")
    _RE_HIT = re.compile(r"\[HIT \]")
    _RE_SKIP = re.compile(r"\[SKIP\]")
    _RE_DRY = re.compile(r"\[DRY \]")

    def _maybe_update_status_from_log(self, line: str) -> None:
        """Watch the engine log to keep per-field ✓/✗ status icons in sync."""
        m = self._RE_FIELD_START.search(line)
        if m:
            self._current_log_field = m.group(1)
            self._set_field_status(self._current_log_field, "pending")
            return
        m = self._RE_ACT_START.search(line)
        if m:
            self._current_log_field = m.group(1)
            self._set_field_status(self._current_log_field, "pending")
            return
        cur = getattr(self, "_current_log_field", None)
        if not cur:
            return
        if self._RE_FILL_OK.search(line) or self._RE_DRY.search(line):
            self._set_field_status(cur, "ok")
        elif self._RE_HIT.search(line):
            # tentatively OK — will be overwritten if FILL_FAIL follows
            if self._field_status.get(cur) != "ok":
                self._set_field_status(cur, "ok")
        elif self._RE_FILL_FAIL.search(line) or self._RE_SKIP.search(line):
            self._set_field_status(cur, "fail")

    def _append_log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _log_local(self, text: str) -> None:
        self._append_log(text)

    # ------------------------------------------------------------------ misc

    def _confirm_discard(self) -> bool:
        """Ask the user to save / discard / cancel when they have unsaved changes."""
        if not self._dirty:
            return True
        ans = messagebox.askyesnocancel(
            "Unsaved changes",
            "You have unsaved changes. Save before continuing?",
        )
        if ans is None:           # Cancel
            return False
        if ans:                   # Yes, save
            self.cmd_save()
            return not self._dirty   # if save was cancelled by Save-As dialog, abort
        return True               # No, discard


# --------------------------------------------------------------------------------------
#  Entry point
# --------------------------------------------------------------------------------------


def _enable_windows_dpi_awareness() -> None:
    """Make the GUI render crisply on Windows 11 high-DPI displays.

    Tkinter without DPI awareness gets bitmap-stretched by Windows, which
    makes text look blurry on 1.5x / 2.0x scaled monitors. Calling
    ``SetProcessDpiAwareness(2)`` (PROCESS_PER_MONITOR_DPI_AWARE) before
    the first ``tk.Tk()`` tells Windows we'll handle scaling ourselves and
    hands us back full-resolution rendering. Best-effort — silently no-ops
    on non-Windows hosts and on older Windows where the API is missing.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes  # type: ignore
        # Per-monitor DPI awareness (Windows 8.1+); falls back to system
        # awareness on Windows 7 via SetProcessDPIAware.
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
            return
        except Exception:
            pass
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    except Exception:
        pass


def main() -> None:
    _enable_windows_dpi_awareness()
    initial = sys.argv[1] if len(sys.argv) > 1 else None
    app = AutoFillGUI(initial_config_path=initial)
    app.mainloop()


if __name__ == "__main__":
    main()
