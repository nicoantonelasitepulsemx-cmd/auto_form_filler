"""recorder.py — record a form-filling session and emit a ready-to-use config.json.

Backwards-compatible with the previous version (record_session / record_session_sync
keep the same signatures), but with several upgrades:

  1. Self-contained: overlay JS is embedded — no external _browser_overlay.js needed.
  2. Streaming: every input/click is pushed to Python the moment it happens, so a
     browser crash or accidental tab close never loses progress.
  3. Auto-save: when the session ends the recorder writes
        - <out>            → full config.json ready for auto_fill.py
        - <out>.events.json → raw event log (for debug / replay)
       e.g. --out my_form.json → my_form.json + my_form.events.json
  4. Navigation-aware: the overlay is re-injected on every navigation, so multi-step
     forms (page1 → Next → page2) are recorded as a single config.
  5. Iframe-aware: the overlay is injected into every same-origin frame.
  6. Multi-strategy selectors: each captured field carries up to ~7 fallback target
     strategies (id, name, aria_label, label_text, placeholder, data_testid,
     css-path, nth-of-type).
  7. Submit detection: clicks on submit buttons / role=button with text "Submit",
     "Send", "Continue", etc. are recorded as `submit_selectors` in the config.
  8. CLI:
        python recorder.py --url https://example.com/form --out my_form.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from playwright.async_api import async_playwright

from proxy_utils import (
    add_cli_args as _add_proxy_cli_args,
    mask_proxy,
    normalize_proxy,
    resolve_proxy,
)


# --------------------------------------------------------------------------------------
#  Browser-side overlay (injected into every page + frame)
# --------------------------------------------------------------------------------------
#
#  The overlay:
#    * Draws a small fixed-position panel (top-right) showing live capture counts and
#      Done / Cancel / Undo buttons.
#    * Listens (in capture phase) for `change`, `blur`, and `click` events on the
#      whole document.
#    * For each event it builds a snapshot { selectors: [...], field_id, field_type,
#      value, checked, tag, ts } and ships it to Python via window.__afRecord(...).
#    * The Done button calls window.__afFinish(); Cancel calls window.__afCancel().
#
#  All names are namespaced (window.__af_*) to avoid clashing with the host page.

OVERLAY_JS = r"""
(() => {
  if (window.__af_recorder_installed) return;
  window.__af_recorder_installed = true;

  // -------------------- helpers --------------------
  const ts = () => Date.now();

  const looksRandom = (s) => {
    if (!s) return true;
    if (s.length > 40) return true;
    // long hex / base64-ish chunks
    if (/[0-9a-f]{8,}/i.test(s) && !/[a-z]{4,}/i.test(s)) return true;
    if (/^[a-z]+[-_][0-9a-f]{6,}/i.test(s)) return true;
    return false;
  };

  const cssEscape = (s) =>
    (window.CSS && CSS.escape) ? CSS.escape(s) : String(s).replace(/"/g, '\\"');

  const labelFor = (el) => {
    if (!el) return "";
    if (el.id) {
      const lab = document.querySelector(`label[for="${cssEscape(el.id)}"]`);
      if (lab) return (lab.innerText || lab.textContent || "").trim();
    }
    let p = el.parentElement;
    while (p && p !== document.body) {
      if (p.tagName === "LABEL") return (p.innerText || p.textContent || "").trim();
      p = p.parentElement;
    }
    if (el.getAttribute("aria-labelledby")) {
      const ref = document.getElementById(el.getAttribute("aria-labelledby"));
      if (ref) return (ref.innerText || ref.textContent || "").trim();
    }
    return "";
  };

  const cssPath = (el) => {
    if (!(el instanceof Element)) return "";
    const parts = [];
    while (el && el.nodeType === 1 && parts.length < 6) {
      let part = el.tagName.toLowerCase();
      if (el.id && !looksRandom(el.id)) {
        part += `#${cssEscape(el.id)}`;
        parts.unshift(part);
        break;
      }
      const cls = (el.className && typeof el.className === "string")
        ? el.className.split(/\s+/).filter(c => c && !looksRandom(c)).slice(0, 2)
        : [];
      if (cls.length) part += "." + cls.map(cssEscape).join(".");
      const parent = el.parentElement;
      if (parent) {
        const same = Array.from(parent.children).filter(c => c.tagName === el.tagName);
        if (same.length > 1) {
          part += `:nth-of-type(${same.indexOf(el) + 1})`;
        }
      }
      parts.unshift(part);
      el = el.parentElement;
    }
    return parts.join(" > ");
  };

  const buildTargets = (el) => {
    const t = [];
    const seen = new Set();
    const add = (strategy, selector) => {
      if (!selector) return;
      const key = strategy + "::" + selector;
      if (seen.has(key)) return;
      seen.add(key);
      t.push({ strategy, selector });
    };

    if (el.id && !looksRandom(el.id)) add("id", el.id);
    if (el.name) add("name", el.name);
    const dti = el.getAttribute("data-testid");
    if (dti) add("data_testid", dti);
    const aria = el.getAttribute("aria-label");
    if (aria) add("aria_label", aria);
    const placeholder = el.getAttribute("placeholder");
    if (placeholder) add("placeholder", placeholder);
    const ariaPh = el.getAttribute("aria-placeholder");
    if (ariaPh) add("aria_placeholder", ariaPh);
    const ariaLb = el.getAttribute("aria-labelledby");
    if (ariaLb) add("aria_labelledby", ariaLb);
    const lbl = labelFor(el);
    if (lbl) add("label_text", lbl);
    const role = el.getAttribute("role");
    if (role) add("role", role);
    const path = cssPath(el);
    if (path) add("css", path);
    return t;
  };

  const fieldIdFor = (el, targets) => {
    const pickFrom = (strat) => {
      const m = targets.find(x => x.strategy === strat);
      return m ? m.selector : null;
    };
    const candidates = [
      el.name,
      pickFrom("data_testid"),
      pickFrom("aria_label"),
      pickFrom("label_text"),
      pickFrom("placeholder"),
      el.id,
    ].filter(Boolean);
    for (const c of candidates) {
      const norm = String(c)
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, "_")
        .replace(/^_+|_+$/g, "")
        .slice(0, 60);
      if (norm) return norm;
    }
    return "field_" + (window.__af_anon_counter = (window.__af_anon_counter || 0) + 1);
  };

  const fieldTypeOf = (el) => {
    const tag = el.tagName.toLowerCase();
    if (tag === "textarea") return "textarea";
    if (tag === "select") return "select";
    if (tag === "input") {
      const t = (el.getAttribute("type") || "text").toLowerCase();
      if (t === "checkbox") return "checkbox";
      if (t === "radio") return "radio";
      if (t === "file") return "file";
      if (t === "submit" || t === "button") return "button";
      return ["email", "url", "tel", "search", "password", "number"].includes(t) ? t : "text";
    }
    if (tag === "button") return "button";
    return "text";
  };

  const isFormField = (el) => {
    if (!el || !el.tagName) return false;
    const tag = el.tagName.toLowerCase();
    if (["input", "textarea", "select"].includes(tag)) {
      const t = (el.getAttribute && (el.getAttribute("type") || "")).toLowerCase();
      if (tag === "input" && (t === "submit" || t === "button" || t === "reset")) return false;
      return true;
    }
    return false;
  };

  const looksLikeSubmit = (el) => {
    if (!el || !el.tagName) return false;
    const tag = el.tagName.toLowerCase();
    const t = (el.getAttribute && (el.getAttribute("type") || "")).toLowerCase();
    if (tag === "input" && t === "submit") return true;
    if (tag === "button" && (t === "submit" || t === "")) {
      const txt = ((el.innerText || el.textContent || "") + " " + (el.getAttribute("aria-label") || "")).toLowerCase();
      return /\b(submit|send|continue|next|gửi|gui|tiếp|tiep|xác nhận|xac nhan|đăng ký|dang ky)\b/.test(txt);
    }
    if (el.getAttribute && el.getAttribute("role") === "button") {
      const txt = ((el.innerText || el.textContent || "") + " " + (el.getAttribute("aria-label") || "")).toLowerCase();
      return /\b(submit|send|continue|next|gửi|gui|tiếp|tiep)\b/.test(txt);
    }
    return false;
  };

  // -------------------- snapshot ship-out --------------------
  const ship = (kind, el, extra = {}) => {
    try {
      const targets = buildTargets(el);
      const snap = {
        kind, // "field" | "submit"
        ts: ts(),
        url: location.href,
        frame: (window.top === window) ? "top" : (window.name || "frame"),
        tag: el.tagName.toLowerCase(),
        field_type: fieldTypeOf(el),
        field_id: fieldIdFor(el, targets),
        targets,
        value: ("value" in el) ? el.value : null,
        checked: ("checked" in el) ? !!el.checked : null,
        ...extra,
      };
      if (window.__afRecord) window.__afRecord(JSON.stringify(snap));
    } catch (e) {
      // swallow — never break the host page
    }
  };

  // -------------------- event listeners (capture phase) --------------------
  // We use capture so we still see events even if the page calls stopPropagation.
  document.addEventListener("change", (ev) => {
    const el = ev.target;
    if (!isFormField(el)) return;
    ship("field", el);
  }, true);

  document.addEventListener("blur", (ev) => {
    const el = ev.target;
    if (!isFormField(el)) return;
    // Only ship blur for text-y inputs (change already covers select/checkbox/radio).
    const ft = fieldTypeOf(el);
    if (["text", "email", "url", "tel", "search", "password", "number", "textarea"].includes(ft)) {
      ship("field", el);
    }
  }, true);

  document.addEventListener("click", (ev) => {
    const el = ev.target.closest("button, input[type=submit], input[type=button], [role=button]");
    if (el && looksLikeSubmit(el)) ship("submit", el);
  }, true);

  // -------------------- floating panel UI --------------------
  const installPanel = () => {
    if (document.getElementById("__af_panel")) return;
    if (!document.body) {
      // body not ready yet — try again soon
      requestAnimationFrame(installPanel);
      return;
    }

    const wrap = document.createElement("div");
    wrap.id = "__af_panel";
    wrap.style.cssText = `
      position: fixed; top: 12px; right: 12px; z-index: 2147483647;
      background: rgba(20,20,30,0.95); color: #fff; font: 13px/1.4 system-ui, sans-serif;
      padding: 10px 12px; border-radius: 10px; box-shadow: 0 6px 24px rgba(0,0,0,0.35);
      width: 240px; user-select: none;
    `;
    wrap.innerHTML = `
      <div style="display:flex;align-items:center;gap:6px;margin-bottom:6px;">
        <span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:#ff3c3c;
          box-shadow:0 0 6px #ff3c3c;animation:__af_blink 1s infinite;"></span>
        <strong style="font-size:13px;">Recording</strong>
        <span id="__af_count" style="margin-left:auto;color:#9af;">0 fields</span>
      </div>
      <div id="__af_last" style="font-size:11px;color:#bbb;min-height:14px;
        white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-bottom:6px;"></div>
      <div style="display:flex;gap:6px;">
        <button id="__af_done" style="flex:1;padding:6px 8px;background:#1f8b3a;color:#fff;
          border:0;border-radius:6px;cursor:pointer;font-weight:600;">Done</button>
        <button id="__af_undo" style="padding:6px 8px;background:#444;color:#fff;
          border:0;border-radius:6px;cursor:pointer;">Undo</button>
        <button id="__af_cancel" style="padding:6px 8px;background:#7a1f1f;color:#fff;
          border:0;border-radius:6px;cursor:pointer;">Cancel</button>
      </div>
      <style>@keyframes __af_blink { 50% { opacity: 0.25; } }</style>
    `;
    document.body.appendChild(wrap);

    document.getElementById("__af_done").addEventListener("click", () => {
      if (window.__afFinish) window.__afFinish();
    });
    document.getElementById("__af_cancel").addEventListener("click", () => {
      if (window.__afCancel) window.__afCancel();
    });
    document.getElementById("__af_undo").addEventListener("click", () => {
      if (window.__afUndo) window.__afUndo();
    });
  };

  // Public API used by Python to update the panel after each event.
  window.__af_updatePanel = (count, last) => {
    const c = document.getElementById("__af_count");
    const l = document.getElementById("__af_last");
    if (c) c.textContent = `${count} field${count === 1 ? "" : "s"}`;
    if (l && last) l.textContent = `↳ ${last}`;
  };

  // Install panel only in the top frame (iframes don't need their own UI).
  if (window.top === window) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", installPanel, { once: true });
    } else {
      installPanel();
    }
  }
})();
"""


# --------------------------------------------------------------------------------------
#  Python-side accumulator
# --------------------------------------------------------------------------------------


def _signature(targets: list[dict[str, Any]]) -> str:
    """Stable identity for a recorded element across multiple events."""
    if not targets:
        return ""
    # prefer id / name / data_testid / aria_label / css for the signature
    priority = ["id", "name", "data_testid", "aria_label", "label_text", "css"]
    by_strat = {t["strategy"]: t["selector"] for t in targets}
    for k in priority:
        if k in by_strat:
            return f"{k}::{by_strat[k]}"
    return f"{targets[0]['strategy']}::{targets[0]['selector']}"


class _Session:
    """Accumulates streamed snapshots into a dedup'd list of fields + submit selectors."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.fields_by_sig: dict[str, dict[str, Any]] = {}
        self.fields_order: list[str] = []
        self.submit_selectors: list[str] = []
        self.first_field_target: Optional[dict[str, str]] = None

    def add(self, snap: dict[str, Any]) -> str:
        """Returns a short label describing what was just captured (for the panel)."""
        self.events.append(snap)
        kind = snap.get("kind", "field")

        if kind == "submit":
            targets = snap.get("targets") or []
            for t in targets:
                if t["strategy"] == "css" and t["selector"] not in self.submit_selectors:
                    self.submit_selectors.append(t["selector"])
                    break
            return f"submit ({snap.get('field_id', '?')})"

        sig = _signature(snap.get("targets") or [])
        if not sig:
            return ""

        field = self._snap_to_field(snap)
        if sig in self.fields_by_sig:
            # Update the value (latest wins) but keep the original targets list.
            existing = self.fields_by_sig[sig]
            if "value" in field:
                existing["value"] = field["value"]
            existing["field_type"] = field.get("field_type", existing.get("field_type"))
        else:
            self.fields_by_sig[sig] = field
            self.fields_order.append(sig)
            if self.first_field_target is None:
                # Pick the most stable target as a wait_for_selector hint.
                self.first_field_target = field["targets"][0] if field["targets"] else None

        return f"{field.get('field_id', '?')} = {field.get('value', '')!r}"[:80]

    def undo_last(self) -> Optional[str]:
        if not self.events:
            return None
        ev = self.events.pop()
        if ev.get("kind") == "submit":
            tgts = ev.get("targets") or []
            for t in tgts:
                if t["strategy"] == "css" and t["selector"] in self.submit_selectors:
                    self.submit_selectors.remove(t["selector"])
                    break
            return "submit"
        sig = _signature(ev.get("targets") or [])
        # If no other event remains for this signature, drop the field entirely.
        # Otherwise just rebuild from remaining events (cheap because the list is small).
        self._rebuild()
        return sig

    def _rebuild(self) -> None:
        old_events = list(self.events)
        self.events.clear()
        self.fields_by_sig.clear()
        self.fields_order.clear()
        self.submit_selectors.clear()
        self.first_field_target = None
        for ev in old_events:
            self.add(ev)

    @staticmethod
    def _snap_to_field(snap: dict[str, Any]) -> dict[str, Any]:
        ftype = snap.get("field_type", "text")
        field: dict[str, Any] = {
            "field_id": snap.get("field_id") or "field",
            "field_type": ftype,
            "targets": snap.get("targets") or [],
        }
        if ftype == "checkbox":
            field["value"] = bool(snap.get("checked"))
        else:
            v = snap.get("value")
            if v is not None:
                field["value"] = v
        return field

    def suggested_wait_selector(self) -> str:
        """Pick a CSS selector for the first field the user touched."""
        if not self.first_field_target:
            return ""
        t = self.first_field_target
        if t["strategy"] == "id":
            return f"#{t['selector']}"
        if t["strategy"] == "name":
            return f"[name=\"{t['selector']}\"]"
        if t["strategy"] == "data_testid":
            return f"[data-testid=\"{t['selector']}\"]"
        if t["strategy"] == "css":
            return t["selector"]
        # fallback: look for a css target on that same field
        if self.fields_order:
            first_field = self.fields_by_sig.get(self.fields_order[0])
            if first_field:
                for tt in first_field.get("targets", []):
                    if tt["strategy"] == "css":
                        return tt["selector"]
        return ""

    def fields(self) -> list[dict[str, Any]]:
        """Return fields in insertion order, with disambiguated field_ids."""
        out: list[dict[str, Any]] = []
        seen_ids: dict[str, int] = {}
        for sig in self.fields_order:
            f = dict(self.fields_by_sig[sig])
            fid = f["field_id"]
            if fid in seen_ids:
                seen_ids[fid] += 1
                f["field_id"] = f"{fid}_{seen_ids[fid]}"
            else:
                seen_ids[fid] = 1
            out.append(f)
        return out


def build_config(
    session: "_Session",
    target_url: str,
    wait_for_selector: Optional[str] = None,
    fields_override: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Build a full auto_fill.py-compatible config dict from a recording session."""
    fields = fields_override if fields_override is not None else session.fields()
    cfg: dict[str, Any] = {
        "target_url": target_url,
        "wait_for_selector": wait_for_selector or session.suggested_wait_selector(),
        "headless": False,
        "dry_run": True,
        "fields": fields,
    }
    if session.submit_selectors:
        cfg["submit_selectors"] = session.submit_selectors
    return cfg


# --------------------------------------------------------------------------------------
#  Recording entrypoints
# --------------------------------------------------------------------------------------


async def record_session(
    target_url: str,
    wait_for_selector: Optional[str] = None,
    out_path: Optional[str] = None,
    headless: bool = False,
    save_events: bool = True,
    proxy: Optional[Any] = None,
) -> list[dict[str, Any]]:
    """Open `target_url`, record interactions, return a list of fields.

    If `out_path` is given, also writes:
        <out_path>            → full config.json (target_url, fields, submit_selectors, …)
        <out_path>.events.json → raw event log
    """
    config, _ = await record_to_config(
        target_url=target_url,
        wait_for_selector=wait_for_selector,
        out_path=out_path,
        headless=headless,
        save_events=save_events,
        proxy=proxy,
    )
    return config.get("fields", [])


async def record_to_config(
    target_url: str,
    wait_for_selector: Optional[str] = None,
    out_path: Optional[str] = None,
    headless: bool = False,
    save_events: bool = True,
    proxy: Optional[Any] = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Like record_session, but returns (full_config, raw_events).

    `proxy` accepts the same shapes as `proxy_utils.normalize_proxy`:
    a URL string, a dict, or None to disable.
    """
    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    session = _Session()

    proxy_dict = normalize_proxy(proxy) if proxy else None

    async with async_playwright() as p:
        launch_kwargs: dict = {"headless": headless}
        if proxy_dict:
            launch_kwargs["proxy"] = proxy_dict
            print(f"[recorder] proxy: {mask_proxy(proxy_dict)}")
        browser = await p.chromium.launch(**launch_kwargs)
        context = await browser.new_context()

        async def on_record(payload: str) -> None:
            try:
                snap = json.loads(payload)
            except Exception:
                return
            label = session.add(snap)
            count = len(session.fields_order)
            try:
                for pg in context.pages:
                    await pg.evaluate(
                        "(args) => window.__af_updatePanel && window.__af_updatePanel(args[0], args[1])",
                        [count, label],
                    )
            except Exception:
                pass

        async def on_finish() -> None:
            if not fut.done():
                fut.set_result("done")

        async def on_cancel() -> None:
            if not fut.done():
                fut.set_result("cancel")

        async def on_undo() -> None:
            session.undo_last()
            count = len(session.fields_order)
            try:
                for pg in context.pages:
                    await pg.evaluate(
                        "(args) => window.__af_updatePanel && window.__af_updatePanel(args[0], args[1])",
                        [count, "(undid last event)"],
                    )
            except Exception:
                pass

        await context.expose_function("__afRecord", on_record)
        await context.expose_function("__afFinish", on_finish)
        await context.expose_function("__afCancel", on_cancel)
        await context.expose_function("__afUndo", on_undo)

        # Inject overlay into every frame of every page (current + future).
        await context.add_init_script(OVERLAY_JS)

        page = await context.new_page()

        def _on_close(_p: Any) -> None:
            if not fut.done():
                fut.set_result("closed")

        page.on("close", _on_close)
        context.on("close", lambda _c: _on_close(None))

        try:
            await page.goto(target_url, wait_until="domcontentloaded")
        except Exception as exc:
            print(f"[recorder] navigation failed: {exc}", file=sys.stderr)

        if wait_for_selector:
            try:
                await page.wait_for_selector(wait_for_selector, timeout=15000)
            except Exception:
                pass

        # Wait until the user clicks Done/Cancel or closes the window.
        outcome = await fut

        try:
            await context.close()
            await browser.close()
        except Exception:
            pass

    fields = session.fields() if outcome != "cancel" else []
    raw_events = list(session.events) if outcome != "cancel" else []

    config = build_config(
        session=session,
        target_url=target_url,
        wait_for_selector=wait_for_selector,
        fields_override=fields,
    )

    if out_path:
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
        if save_events:
            ev_path = out.with_name(out.stem + ".events.json")
            ev_path.write_text(
                json.dumps(raw_events, indent=2, ensure_ascii=False), encoding="utf-8"
            )

    return config, raw_events


def record_session_sync(
    target_url: str,
    wait_for_selector: Optional[str] = None,
    out_path: Optional[str] = None,
    headless: bool = False,
    save_events: bool = True,
    proxy: Optional[Any] = None,
) -> list[dict[str, Any]]:
    return asyncio.run(
        record_session(
            target_url=target_url,
            wait_for_selector=wait_for_selector,
            out_path=out_path,
            headless=headless,
            save_events=save_events,
            proxy=proxy,
        )
    )


def record_to_config_sync(
    target_url: str,
    wait_for_selector: Optional[str] = None,
    out_path: Optional[str] = None,
    headless: bool = False,
    save_events: bool = True,
    proxy: Optional[Any] = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    return asyncio.run(
        record_to_config(
            target_url=target_url,
            wait_for_selector=wait_for_selector,
            out_path=out_path,
            headless=headless,
            save_events=save_events,
            proxy=proxy,
        )
    )


# --------------------------------------------------------------------------------------
#  CLI
# --------------------------------------------------------------------------------------


def _default_out_path() -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"recorded_{stamp}.json"


def main() -> None:
    p = argparse.ArgumentParser(
        description="Record a form-filling session and write a ready-to-use config.json."
    )
    p.add_argument("--url", required=True, help="Target form URL.")
    p.add_argument(
        "--out",
        default=None,
        help="Output config path (default: recorded_<timestamp>.json in cwd).",
    )
    p.add_argument(
        "--wait-for",
        default=None,
        help="Optional selector to wait for after page load (e.g. '#email').",
    )
    p.add_argument(
        "--headless", action="store_true", help="Run without a visible browser (rarely useful here)."
    )
    p.add_argument(
        "--no-events",
        action="store_true",
        help="Don't write the .events.json raw-events file.",
    )
    _add_proxy_cli_args(p)
    args = p.parse_args()

    proxy = resolve_proxy(
        cli_proxy=args.proxy,
        cli_proxy_list=args.proxy_list,
        cli_no_proxy=args.no_proxy,
        cli_rotate=args.proxy_rotate,
        cli_bypass=args.proxy_bypass,
        config=None,
    )

    out_path = args.out or _default_out_path()
    config, events = record_to_config_sync(
        target_url=args.url,
        wait_for_selector=args.wait_for,
        out_path=out_path,
        headless=args.headless,
        save_events=not args.no_events,
        proxy=proxy,
    )

    print(f"[recorder] captured {len(config.get('fields', []))} field(s), "
          f"{len(events)} event(s)")
    print(f"[recorder] config → {out_path}")
    if not args.no_events:
        print(f"[recorder] events → {out_path}.events.json")


if __name__ == "__main__":
    main()
