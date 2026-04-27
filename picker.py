"""picker.py — interactive single-element picker (legacy v1 path).

The GUI's "🎯 Pick from page" button calls into this module:

    snap = picker.pick_one_sync(url, wait_for_selector, proxy=proxy_dict)
    field = picker.snapshot_to_field(snap)

Modern users should use `recorder_v2.py` ("Record session") instead — it
captures multiple actions with frame chains and fingerprints. This module
only exists to keep the legacy "Pick from page" GUI button alive.

Implementation notes
--------------------
* Opens Chromium via Playwright (sync API) so it can be called from a Tk
  worker thread without nested-event-loop pain.
* Injects a small overlay that highlights whatever element is currently
  under the mouse, plus a header bar telling the user what to do.
* On click, snapshots the element's identifying attributes, accessible
  name, label text, etc., and returns it. We synthesize a v1-compatible
  `targets` list (strategy + selector) using the highest-confidence
  attributes available.
"""
from __future__ import annotations

import json
from typing import Any, Optional

try:  # Playwright sync API — only required at runtime.
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover
    sync_playwright = None  # type: ignore


# --------------------------------------------------------------------------------------
#  Browser-side script — injected into the target page.
# --------------------------------------------------------------------------------------

_PICKER_JS = r"""
(() => {
  if (window.__af_picker_installed) return;
  window.__af_picker_installed = true;

  // ---------- Style the overlay ----------
  const css = `
    .__afpk_hover { outline: 2px solid #ef4444 !important;
                    outline-offset: 1px !important;
                    box-shadow: 0 0 0 4px rgba(239,68,68,.18) !important; }
    #__afpk_bar  { position:fixed; top:0; left:0; right:0; z-index:2147483647;
                   background:#111827; color:#fff; font:14px/1.4 system-ui,sans-serif;
                   padding:8px 12px; display:flex; gap:8px; align-items:center;
                   border-bottom:2px solid #ef4444; }
    #__afpk_bar b{color:#fbbf24;}
    #__afpk_bar button { background:#374151; color:#fff; border:0; padding:6px 10px;
                         border-radius:4px; cursor:pointer; }
    #__afpk_bar button:hover { background:#4b5563; }
  `;
  const style = document.createElement('style');
  style.textContent = css;
  document.head.appendChild(style);

  // ---------- Header bar with Cancel button ----------
  const bar = document.createElement('div');
  bar.id = '__afpk_bar';
  bar.innerHTML = `<span>🎯 <b>Pick mode</b> — hover an element, click to capture it. Press <b>Esc</b> or click ✕ to cancel.</span>
                   <span style="flex:1"></span>
                   <button id="__afpk_cancel">✕ Cancel</button>`;
  document.documentElement.appendChild(bar);
  document.body && (document.body.style.paddingTop = (bar.offsetHeight + 4) + 'px');

  // ---------- Track hover ----------
  let lastHover = null;
  const inOverlay = (el) =>
    el && el.closest && (el.closest('#__afpk_bar') || el.id === '__afpk_bar');

  document.addEventListener('mousemove', (ev) => {
    const el = ev.target;
    if (!el || inOverlay(el)) return;
    if (lastHover && lastHover !== el) lastHover.classList.remove('__afpk_hover');
    el.classList.add('__afpk_hover');
    lastHover = el;
  }, true);

  // ---------- Helpers ----------
  const cssEscape = (s) =>
    (window.CSS && CSS.escape) ? CSS.escape(s) : String(s).replace(/(["\\])/g, '\\$1');

  const labelTextFor = (el) => {
    if (!el) return null;
    if (el.id) {
      const lbl = document.querySelector(`label[for="${cssEscape(el.id)}"]`);
      if (lbl) return (lbl.innerText || lbl.textContent || '').trim();
    }
    const wrap = el.closest && el.closest('label');
    if (wrap) {
      const clone = wrap.cloneNode(true);
      clone.querySelectorAll('input,textarea,select,button').forEach(n => n.remove());
      return (clone.innerText || clone.textContent || '').trim();
    }
    return null;
  };

  const accessibleName = (el) => {
    if (!el) return null;
    const al = el.getAttribute && el.getAttribute('aria-label');
    if (al) return al.trim();
    const labelledby = el.getAttribute && el.getAttribute('aria-labelledby');
    if (labelledby) {
      const refs = labelledby.split(/\s+/)
        .map(id => document.getElementById(id))
        .filter(Boolean)
        .map(n => (n.innerText || n.textContent || '').trim())
        .filter(Boolean);
      if (refs.length) return refs.join(' ');
    }
    return labelTextFor(el);
  };

  const fieldType = (el) => {
    if (!el || !el.tagName) return 'unknown';
    const tag = el.tagName.toLowerCase();
    const t = (el.getAttribute('type') || '').toLowerCase();
    if (tag === 'select')   return 'select';
    if (tag === 'textarea') return 'textarea';
    if (tag === 'button')   return 'button';
    if (tag === 'input') {
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio')    return 'radio';
      if (t === 'file')     return 'file';
      if (t === 'submit')   return 'button';
      return t || 'text';
    }
    if (el.isContentEditable) return 'contenteditable';
    const role = el.getAttribute && el.getAttribute('role');
    if (role) return role;
    return tag;
  };

  const snapshot = (el) => {
    if (!el) return null;
    const attrs = {};
    for (const k of ['id','name','type','role','data-testid','aria-label',
                     'aria-labelledby','aria-placeholder','placeholder',
                     'autocomplete','title']) {
      const v = el.getAttribute && el.getAttribute(k);
      if (v != null) attrs[k] = v;
    }
    const r = el.getBoundingClientRect();
    return {
      tag: el.tagName.toLowerCase(),
      type: (el.getAttribute && el.getAttribute('type')) || null,
      attributes: attrs,
      label_text: labelTextFor(el) || null,
      accessible_name: accessibleName(el) || null,
      field_type: fieldType(el),
      placeholder: (el.getAttribute && el.getAttribute('placeholder')) || null,
      text: (el.innerText || el.textContent || '').trim().slice(0, 120),
      bbox: r ? {x: r.left, y: r.top, w: r.width, h: r.height} : null,
      url: location.href,
    };
  };

  // ---------- Click / cancel handling ----------
  document.getElementById('__afpk_cancel').addEventListener('click', (ev) => {
    ev.preventDefault(); ev.stopPropagation();
    window.__af_picker_result = {cancelled: true};
  });
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape') window.__af_picker_result = {cancelled: true};
  }, true);

  document.addEventListener('click', (ev) => {
    const el = ev.target;
    if (!el || inOverlay(el)) return;
    ev.preventDefault();
    ev.stopPropagation();
    ev.stopImmediatePropagation();
    window.__af_picker_result = {snapshot: snapshot(el)};
  }, true);

  // Suppress form submission while picking.
  document.addEventListener('submit', (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
  }, true);
})();
"""


# --------------------------------------------------------------------------------------
#  Public API
# --------------------------------------------------------------------------------------


def pick_one_sync(
    url: str,
    wait_for_selector: Optional[str] = None,
    *,
    proxy: Optional[Any] = None,
    timeout_s: int = 300,
) -> Optional[dict]:
    """Open a browser, let the user click ONE element, return its snapshot.

    Returns ``None`` if the user cancels (Esc / ✕ / closes the window).
    Raises ``RuntimeError`` if Playwright isn't installed.
    """
    if sync_playwright is None:
        raise RuntimeError(
            "Playwright is not installed. Run `pip install playwright && "
            "python -m playwright install chromium`."
        )

    launch_kwargs: dict = {
        "headless": False,
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--no-default-browser-check",
        ],
    }
    if proxy:
        launch_kwargs["proxy"] = proxy

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 820},
        )
        context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:
            browser.close()
            raise RuntimeError(f"failed to load {url!r}: {exc}") from exc

        if wait_for_selector:
            try:
                page.wait_for_selector(wait_for_selector, timeout=15000)
            except Exception:
                pass

        # Inject the picker every time the page navigates / reloads.
        def _inject(_frame=None) -> None:
            try:
                page.evaluate(_PICKER_JS)
            except Exception:
                pass

        _inject()
        page.on("framenavigated", lambda _f: _inject())

        # Poll for the result.
        result: Optional[dict] = None
        deadline = page.evaluate("Date.now()") + (timeout_s * 1000)
        try:
            while True:
                # Check if the user closed the browser window.
                if page.is_closed():
                    break
                try:
                    raw = page.evaluate("window.__af_picker_result || null")
                except Exception:
                    raw = None
                if raw:
                    result = raw
                    break
                if page.evaluate("Date.now()") > deadline:
                    break
                page.wait_for_timeout(150)
        finally:
            try:
                browser.close()
            except Exception:
                pass

    if not result or result.get("cancelled"):
        return None
    return result.get("snapshot")


# --------------------------------------------------------------------------------------
#  Snapshot → v1 field dict
# --------------------------------------------------------------------------------------


def _safe_id(s: str, fallback: str = "field") -> str:
    norm = "".join(c if c.isalnum() else "_" for c in s.lower()).strip("_")
    return norm[:60] or fallback


def snapshot_to_field(snap: dict) -> dict:
    """Convert a picker snapshot into a v1 field dict.

    Output schema (consumed by ``auto_fill.fill_one_field``):

        {
          "field_id":   "<stable id>",
          "field_type": "email" | "text" | "textarea" | "select"
                       | "checkbox" | "radio" | "file" | ...,
          "value":      "<placeholder — fill in via GUI>",
          "targets":    [{"strategy": "...", "selector": "..."}, ...]
        }
    """
    if not snap:
        raise ValueError("empty snapshot")

    attrs = snap.get("attributes") or {}
    label = snap.get("label_text") or snap.get("accessible_name") or ""
    placeholder = snap.get("placeholder") or attrs.get("placeholder")
    field_type = snap.get("field_type") or "text"

    # Pick a stable, human-readable field_id.
    fid_src = (
        attrs.get("data-testid")
        or attrs.get("name")
        or label
        or attrs.get("id")
        or attrs.get("aria-label")
        or placeholder
        or snap.get("tag", "field")
    )
    field_id = _safe_id(str(fid_src))

    # Build the targets list — best strategy first.
    targets: list[dict[str, str]] = []

    if attrs.get("data-testid"):
        targets.append({
            "strategy": "data_testid",
            "selector": f'[data-testid="{attrs["data-testid"]}"]',
        })
    if attrs.get("id") and not _looks_random(attrs["id"]):
        targets.append({"strategy": "id", "selector": f'#{_css_escape(attrs["id"])}'})
    if attrs.get("name"):
        targets.append({
            "strategy": "name",
            "selector": f'[name="{attrs["name"]}"]',
        })
    if attrs.get("aria-label"):
        targets.append({
            "strategy": "aria_label",
            "selector": f'[aria-label="{attrs["aria-label"]}"]',
        })
    if placeholder:
        targets.append({
            "strategy": "placeholder",
            "selector": f'[placeholder="{placeholder}"]',
        })
    if label:
        targets.append({"strategy": "label_text", "selector": label})
    if snap.get("accessible_name") and not label:
        targets.append({"strategy": "label_text", "selector": snap["accessible_name"]})

    # Fallback: tag + first attribute we have, so we never end up with [].
    if not targets:
        tag = snap.get("tag", "input")
        if attrs:
            k, v = next(iter(attrs.items()))
            targets.append({"strategy": "css", "selector": f'{tag}[{k}="{v}"]'})
        else:
            targets.append({"strategy": "css", "selector": tag})

    field: dict[str, Any] = {
        "field_id": field_id,
        "field_type": field_type,
        "value": "",  # user fills via GUI
        "targets": targets,
    }
    if label:
        field["label"] = label
    return field


# --------------------------------------------------------------------------------------
#  Helpers
# --------------------------------------------------------------------------------------


def _css_escape(s: str) -> str:
    out = []
    for c in s:
        if c.isalnum() or c in ("-", "_"):
            out.append(c)
        else:
            out.append(f"\\{c}")
    return "".join(out)


def _looks_random(s: str) -> bool:
    """Crude heuristic: ids with long hex/digit runs are framework-generated."""
    if len(s) < 6:
        return False
    if any(c.isdigit() for c in s) and len(s) >= 12:
        # e.g. "u_0_5_aF" or ":r1l:" — react-style.
        return True
    if s.startswith(":") and s.endswith(":"):
        return True
    return False


__all__ = ["pick_one_sync", "snapshot_to_field"]


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: python picker.py <URL> [wait_selector]", file=sys.stderr)
        sys.exit(2)
    url = sys.argv[1]
    wait = sys.argv[2] if len(sys.argv) > 2 else None
    snap = pick_one_sync(url, wait)
    if not snap:
        print("[PICK] cancelled", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(snapshot_to_field(snap), indent=2, ensure_ascii=False))
