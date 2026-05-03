"""recorder_v2.py — capture-everything recorder for the v2 config format.

What's new vs `recorder.py`:

  * **Frame chain** — every event records the full chain of frame URLs
    leading to the element, so the player can reach the same iframe.
  * **Element fingerprint** — captured at action time (tag, role,
    accessible name, attributes, neighbour text, viewport-relative bbox).
    The replay-time resolver uses this to confirm the picked element is
    the right one before acting on it.
  * **Multiple selectors per action** — six to ten redundant strategies
    each with a default weight, so a single missing attribute doesn't
    sink the whole action.
  * **Action kind, not "field"** — every captured event is an *action*:
    fill / click / check / select / combobox / contenteditable / set_files.
    Replay uses the same method.
  * **Custom widgets** — `div[role=combobox]`, `[role=radio]`,
    `[role=checkbox]`, `[contenteditable]` are first-class.
  * **Streaming + crash-safe** — events are pushed to Python on every
    interaction, so a tab crash only loses the *last* action.
  * **Submit detected by structure** — submit fingerprint + selectors,
    not just the visible text.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from playwright.async_api import (
    ElementHandle,
    Frame,
    Page,
    async_playwright,
)

from element_fingerprint import FINGERPRINT_JS, fingerprint_handle
from proxy_utils import (
    add_cli_args as _add_proxy_cli_args,
    mask_proxy,
    normalize_proxy,
    resolve_proxy,
)


# --------------------------------------------------------------------------------------
# Browser-side overlay
# --------------------------------------------------------------------------------------

OVERLAY_JS = r"""
(() => {
  if (window.__af2_installed) return;
  window.__af2_installed = true;

  const ts = () => Date.now();

  // ---------------------- selector generation ----------------------
  // Reject IDs/classes that look auto-generated (React/Relay/FB internal,
  // long hex blobs, decimal-suffix numerics).  These rarely survive a page
  // refresh, so picking them as a `stable_id` selector is a tarpit.
  const looksRandom = (s) => {
    if (!s) return true;
    if (s.length > 60) return true;
    // Long hex blob, low alpha density (e.g. abc123def456...)
    if (/[0-9a-f]{8,}/i.test(s) && !/[a-z]{4,}/i.test(s)) return true;
    // Generic prefix-hex (e.g. ember42, react-x9f8a1c)
    if (/^[a-z]+[-_][0-9a-f]{6,}/i.test(s)) return true;
    // Facebook React internals: u_0_K3, u_0_12_D3, u_0_2_G/, ...
    if (/^u_\d+(?:_|$)/i.test(s)) return true;
    // Long all-digit ids and decimal-suffixed numeric ids (1112475925434379.0)
    if (/^\d{6,}(?:\.\d+)?$/.test(s)) return true;
    // Multiple underscore segments where the longest alpha run is < 4 chars
    // (catches u_0_h_K8 type strings that the patterns above miss).
    if ((s.match(/_/g) || []).length >= 2 && /^[A-Za-z0-9_/+\-]+$/.test(s)) {
      const longestAlpha = (s.match(/[A-Za-z]+/g) || [])
        .reduce((m, p) => Math.max(m, p.length), 0);
      if (longestAlpha < 4) return true;
    }
    return false;
  };

  const cssEscape = (s) =>
    (window.CSS && CSS.escape) ? CSS.escape(s) : String(s).replace(/[^\w-]/g, "\\$&");

  // Read the visible label text for an element.  When walking up to a wrapping
  // <label>, strip nested form controls so we don't pollute the label with the
  // current `value` of a sibling input — that's especially important when the
  // form has multiple checkboxes inside the same fieldset and we need each
  // checkbox to have a *different* accessible name.
  const _labelInnerText = (lab) => {
    if (!lab) return "";
    try {
      const clone = lab.cloneNode(true);
      clone.querySelectorAll("input, textarea, select, script, style").forEach((n) => n.remove());
      return (clone.innerText || clone.textContent || "").replace(/\s+/g, " ").trim();
    } catch (e) {
      return (lab.innerText || lab.textContent || "").replace(/\s+/g, " ").trim();
    }
  };

  const labelTextFor = (el) => {
    if (!el) return "";
    if (el.id) {
      const lab = document.querySelector(`label[for="${cssEscape(el.id)}"]`);
      const t = _labelInnerText(lab);
      if (t) return t;
    }
    let p = el.parentElement;
    while (p && p !== document.body) {
      if (p.tagName === "LABEL") return _labelInnerText(p);
      p = p.parentElement;
    }
    if (el.getAttribute("aria-labelledby")) {
      const ref = document.getElementById(el.getAttribute("aria-labelledby"));
      if (ref) return (ref.innerText || ref.textContent || "").replace(/\s+/g, " ").trim();
    }
    return "";
  };

  const accessibleName = (el) => {
    if (!el) return "";
    if (el.getAttribute("aria-label")) return el.getAttribute("aria-label").trim();
    const lt = labelTextFor(el);
    if (lt) return lt;
    if (el.getAttribute("placeholder")) return el.getAttribute("placeholder").trim();
    if (el.getAttribute("title")) return el.getAttribute("title").trim();
    if (el.tagName === "BUTTON" || el.getAttribute("role") === "button") {
      return ((el.innerText || el.textContent || "").trim()).slice(0, 60);
    }
    return "";
  };

  const cssPathOf = (el) => {
    if (!(el instanceof Element)) return "";
    const parts = [];
    // Cap depth at 6 (was 8) — beyond that the path is rarely unique
    // anyway and the extra walk adds layout-thrash on hot click paths.
    while (el && el.nodeType === 1 && parts.length < 6) {
      let part = el.tagName.toLowerCase();
      if (el.id && !looksRandom(el.id)) {
        parts.unshift(`${part}#${cssEscape(el.id)}`);
        return parts.join(" > ");
      }
      const cls = (typeof el.className === "string")
        ? el.className.split(/\s+/).filter(c => c && !looksRandom(c)).slice(0, 2)
        : [];
      if (cls.length) part += "." + cls.map(cssEscape).join(".");
      const parent = el.parentElement;
      if (parent) {
        const same = Array.from(parent.children).filter(c => c.tagName === el.tagName);
        if (same.length > 1) part += `:nth-of-type(${same.indexOf(el) + 1})`;
      }
      parts.unshift(part);
      el = el.parentElement;
    }
    return parts.join(" > ");
  };

  const buildSelectors = (el) => {
    const out = [];
    const seen = new Set();
    const add = (strategy, selector, weight) => {
      if (!selector) return;
      const key = strategy + "::" + selector;
      if (seen.has(key)) return;
      seen.add(key);
      out.push({ strategy, selector, weight });
    };

    const dti = el.getAttribute("data-testid");
    if (dti) add("data_testid", `[data-testid="${cssEscape(dti)}"]`, 100);

    if (el.id && !looksRandom(el.id)) {
      add("stable_id", el.id, 95);
      add("css", `#${cssEscape(el.id)}`, 90);
    }

    if (el.name) {
      add("name", `[name="${cssEscape(el.name)}"]`, 90);
      // For radio/checkbox the value attribute is what disambiguates siblings
      // sharing a name (e.g. content_type[] with options Photo/Ad/Page/Other).
      // Capture a name+value compound selector at higher weight than plain name.
      const t = (el.getAttribute && (el.getAttribute("type") || "").toLowerCase()) || "";
      if ((t === "radio" || t === "checkbox") && el.value != null && el.value !== "") {
        add("name_value",
            `[name="${cssEscape(el.name)}"][value="${cssEscape(el.value)}"]`,
            92);
      }
    }

    const role = el.getAttribute("role")
      || (el.tagName === "BUTTON" ? "button"
        : (el.tagName === "INPUT" && /^(text|email|url|tel|search|password|number)$/i.test(el.type || "")) ? "textbox"
        : (el.tagName === "TEXTAREA") ? "textbox"
        : (el.tagName === "SELECT") ? "combobox"
        : null);
    const an = accessibleName(el);
    if (role && an) add("role_name", `${role}|${an}`, 85);
    if (role) add("role", role, 75);

    const al = el.getAttribute("aria-label");
    if (al) add("aria_label", `[aria-label="${cssEscape(al)}"]`, 80);
    const aph = el.getAttribute("aria-placeholder");
    if (aph) add("aria_placeholder", `[aria-placeholder="${cssEscape(aph)}"]`, 75);
    const albby = el.getAttribute("aria-labelledby");
    if (albby) add("aria_labelledby", albby, 70);

    const ph = el.getAttribute("placeholder");
    if (ph) add("placeholder", `[placeholder="${cssEscape(ph)}"]`, 70);

    const lt = labelTextFor(el);
    if (lt) add("label_text", lt, 65);

    const path = cssPathOf(el);
    if (path) add("css", path, 55);

    return out;
  };

  // ---------------------- frame chain ----------------------
  const frameChain = () => {
    const chain = [];
    let w = window;
    while (w && w !== window.top) {
      try {
        chain.unshift(w.location && w.location.href ? w.location.href : (w.name || "frame"));
      } catch (e) {
        chain.unshift("(cross-origin frame)");
      }
      w = w.parent;
    }
    chain.unshift("top");
    return chain;
  };

  // ---------------------- field-id / kind detection ----------------------
  const fieldIdFor = (el, sels) => {
    const candidates = [];
    if (el.name) candidates.push(el.name);
    const dti = el.getAttribute("data-testid"); if (dti) candidates.push(dti);
    const al = el.getAttribute("aria-label");    if (al)  candidates.push(al);
    const lt = labelTextFor(el);                 if (lt)  candidates.push(lt);
    const ph = el.getAttribute("placeholder");   if (ph)  candidates.push(ph);
    if (el.id && !/^[0-9a-f]{8,}/i.test(el.id))  candidates.push(el.id);
    for (const c of candidates) {
      const norm = String(c).toLowerCase()
        .replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 60);
      if (norm) return norm;
    }
    return "field_" + (window.__af2_anon = (window.__af2_anon || 0) + 1);
  };

  const interactionKind = (el, evType) => {
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute("role") || "";
    const t = (el.getAttribute("type") || "").toLowerCase();

    if (tag === "select") return "select";
    if (tag === "textarea") return "fill";
    if (tag === "input") {
      if (t === "checkbox") return "check";
      if (t === "radio")    return "check";
      if (t === "file")     return "set_files";
      if (t === "submit" || t === "button") return "click";
      return "fill";
    }
    if (role === "checkbox" || role === "radio") return "check";
    if (role === "combobox") return "combobox";
    if (role === "button" || tag === "button") return "click";
    if (el.isContentEditable) return "contenteditable";
    return evType === "click" ? "click" : "fill";
  };

  // ---------------------- viewport / fingerprint helpers ----------------------
  const viewportHint = (el) => {
    const r = el.getBoundingClientRect();
    if (!r) return null;
    const vw = window.innerWidth || 1, vh = window.innerHeight || 1;
    return {
      x_pct: +(r.left / vw).toFixed(3),
      y_pct: +(r.top  / vh).toFixed(3),
      w_pct: +(r.width  / vw).toFixed(3),
      h_pct: +(r.height / vh).toFixed(3),
    };
  };

  const fingerprintOf = (el) => {
    const interesting = ["id","name","type","role","data-testid","aria-label",
                         "placeholder","title","autocomplete","value"];
    const attrs = {};
    for (const k of interesting) {
      // For radios/checkboxes el.value is part of identity; for free-text
      // inputs we deliberately skip it so the fingerprint does not depend on
      // what the user happens to have typed at record time.
      if (k === "value") {
        const t = ((el.getAttribute && el.getAttribute("type")) || "").toLowerCase();
        if (t !== "radio" && t !== "checkbox") continue;
      }
      const v = el.getAttribute(k);
      if (v != null) attrs[k] = v;
    }
    // accessibleName + neighbour_text both force layout via innerText reads.
    // Compute accessible_name once; only read neighbour_text when we actually
    // need it (no strong accessible name) — that alone halves the layout
    // thrash on the click hot-path for large pages.
    const an = accessibleName(el);
    let neighbour = "";
    if (!an || an.length < 3) {
      const parent = el.closest("label,fieldset,section") || el.parentElement;
      if (parent) {
        try {
          neighbour = (parent.textContent || "").replace(/\s+/g," ").trim().slice(0,200);
        } catch (e) { /* ignore */ }
      }
    }
    let textContent = "";
    try {
      textContent = ((el.textContent || "").trim().slice(0,120)) || "";
    } catch (e) { /* ignore */ }
    return {
      tag: el.tagName.toLowerCase(),
      type: (el.getAttribute("type") || "").toLowerCase() || null,
      role: el.getAttribute("role") || null,
      accessible_name: an,
      attributes: attrs,
      text_content: textContent,
      neighbour_text: neighbour,
      viewport_hint: viewportHint(el),
    };
  };

  // ---------------------- ship action to Python ----------------------
  // Track the most recent click coordinates so any synchronously-shipped
  // action that wants to record a click_position has access to them.
  const _lastClick = { x: null, y: null, ts: 0 };
  document.addEventListener("pointerdown", (ev) => {
    if (typeof ev.clientX === "number") {
      _lastClick.x = ev.clientX;
      _lastClick.y = ev.clientY;
      _lastClick.ts = ts();
    }
  }, true);
  document.addEventListener("click", (ev) => {
    if (typeof ev.clientX === "number") {
      _lastClick.x = ev.clientX;
      _lastClick.y = ev.clientY;
      _lastClick.ts = ts();
    }
  }, true);

  const _clickPositionWithin = (el) => {
    if (!el || _lastClick.x == null) return null;
    if (ts() - _lastClick.ts > 1500) return null;
    let r;
    try { r = el.getBoundingClientRect(); } catch (e) { return null; }
    if (!r || r.width <= 0 || r.height <= 0) return null;
    return {
      x_pct: +((_lastClick.x - r.left) / r.width).toFixed(3),
      y_pct: +((_lastClick.y - r.top) / r.height).toFixed(3),
    };
  };

  const ship = (kind, el, extra) => {
    try {
      const sels = buildSelectors(el);
      const action = {
        kind,
        ts: ts(),
        url: location.href,
        frame_chain: frameChain(),
        field_id: fieldIdFor(el, sels),
        field_type: (el.getAttribute && el.getAttribute("type")) || el.tagName.toLowerCase(),
        selectors: sels,
        fingerprint: fingerprintOf(el),
        ...(extra || {}),
      };
      // Attach a click_position when the action is a click-shaped one and we
      // have a recent clientX/Y from the pointer/click stream.  The replay
      // engine uses this as a tiebreaker when several siblings match the
      // same selectors.
      if (action.click_position == null) {
        const navKinds = (kind === "click" || kind === "submit" || kind === "check");
        if (navKinds) {
          const pos = _clickPositionWithin(el);
          if (pos) action.click_position = pos;
        }
      }
      if (window.__afRecord) window.__afRecord(JSON.stringify(action));
    } catch (e) { /* never break the host page */ }
  };

  // ---------------------- listeners ----------------------

  // Track the last value typed/changed per element so blur doesn't ship empty.
  const lastValue = new WeakMap();
  // Elements that fired `input` but haven't shipped a `fill` action yet.
  // Flushed on `submit` / `beforeunload` so the last keystrokes survive a
  // navigation (React-controlled inputs often lose `change` in that window).
  const pendingFills = new Set();
  // Paste timestamps per element — used to annotate the next change event as
  // `input_method="paste"` so replay pastes instead of typing keystrokes.
  const pasted = new WeakMap();

  document.addEventListener("input", (ev) => {
    const el = ev.target;
    if (!el || !el.tagName) return;
    if (el.tagName === "INPUT" || el.tagName === "TEXTAREA") {
      lastValue.set(el, el.value);
      const t = (el.getAttribute("type") || "").toLowerCase();
      // Only remember fillable inputs as "pending ship" — checkboxes /
      // radios / buttons fire `change` of their own and don't need flushing.
      if (t !== "checkbox" && t !== "radio" && t !== "submit"
          && t !== "button" && t !== "reset" && t !== "file") {
        pendingFills.add(el);
      }
    }
  }, true);

  document.addEventListener("paste", (ev) => {
    const el = ev.target;
    if (!el || !el.tagName) return;
    if (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable) {
      pasted.set(el, ts());
    }
  }, true);

  document.addEventListener("change", (ev) => {
    const el = ev.target;
    if (!el || !el.tagName) return;
    const tag = el.tagName.toLowerCase();
    const t = (el.getAttribute("type") || "").toLowerCase();
    // Bug A15 fix: React/SPA forms (Facebook trademark, GDPR consent forms,
    // anything controlled-input) re-render their radio/checkbox groups on
    // every state transition (e.g. while the user types in another field).
    // Each re-render dispatches a synthetic `change` event whose `isTrusted`
    // is false because it didn't originate from a user gesture. Without this
    // filter the recorder shipped a `check` action for every ghost event,
    // and replay processed them in order — overwriting the user's actual
    // selection with whichever radio React happened to re-render last.
    //
    // The filter is INTENTIONALLY narrow — only radio/checkbox change
    // events are dropped when isTrusted=false. Selects and text inputs
    // are routinely populated programmatically via `page.select_option()`
    // / `page.fill()` (which also dispatch isTrusted=false events); a
    // blanket drop would silently break form capture in test harnesses
    // and template-driven workflows.
    if (ev.isTrusted === false && tag === "input" && (t === "radio" || t === "checkbox")) {
      return;
    }

    if (tag === "select") {
      const opt = el.options[el.selectedIndex];
      ship("select", el, { value: el.value, value_label: opt ? opt.text : null });
      return;
    }
    if (tag === "input" && t === "checkbox") {
      // Bug A1 fix: timestamp-based dedup window (was: stale boolean flag).
      // The proxy-click branch sets __af_proxy_ts; if `change` fires within
      // the 800ms window we treat it as a duplicate. After the window the
      // flag effectively expires, so a second user toggle on the SAME input
      // is not silently swallowed (the boolean version was sticky forever).
      if (el.__af_proxy_ts && (ts() - el.__af_proxy_ts) < 800) {
        delete el.__af_proxy_ts;
        return;
      }
      // Always carry the value attribute so siblings sharing a name can be
      // distinguished at replay time (content_type[] = Photo / Ad / ...).
      ship("check", el, {
        checked: !!el.checked,
        radio_value: el.value || undefined,
        _hidden_input_name: el.name || undefined,
        _hidden_input_type: "checkbox",
      });
      return;
    }
    if (tag === "input" && t === "radio" && el.checked) {
      if (el.__af_proxy_ts && (ts() - el.__af_proxy_ts) < 800) {
        delete el.__af_proxy_ts;
        return;
      }
      ship("check", el, {
        checked: true,
        radio_value: el.value,
        _hidden_input_name: el.name || undefined,
        _hidden_input_type: "radio",
      });
      return;
    }
    if (tag === "input" && t === "file") {
      const names = Array.from(el.files || []).map(f => f.name);
      ship("set_files", el, { files: names, _files_warning: "absolute paths required at replay time" });
      return;
    }
    if (tag === "input" || tag === "textarea") {
      const pastedAt = pasted.get(el);
      const wasPasted = (pastedAt != null) && (ts() - pastedAt) < 1500;
      pendingFills.delete(el);
      ship("fill", el, {
        value: el.value,
        input_method: wasPasted ? "paste" : "fill",
      });
      return;
    }
  }, true);

  // ---------------------- hidden radio/checkbox helpers ----------------------
  // Facebook (and many React apps) hide the real <input type=radio|checkbox>
  // behind a styled <span>::before or <label> wrapper.  The click lands on
  // the visual proxy (span, div, label) — not on the input.  We need to
  // detect this pattern and record the action on the *clickable* element
  // (so replay clicks it), while noting the associated hidden input.

  const _isHiddenInput = (el) => {
    if (!el || el.tagName !== "INPUT") return false;
    const t = (el.type || "").toLowerCase();
    if (t !== "radio" && t !== "checkbox") return false;
    const st = window.getComputedStyle(el);
    if (st.display === "none" || st.visibility === "hidden" || st.opacity === "0") return true;
    const r = el.getBoundingClientRect();
    if (r.width <= 2 || r.height <= 2) return true;
    if (st.position === "absolute" || st.position === "fixed") {
      if (st.clip && st.clip !== "auto") return true;
      if (st.clipPath && st.clipPath !== "none") return true;
      if (parseInt(st.left) < -9000 || parseInt(st.top) < -9000) return true;
    }
    return false;
  };

  // When the user clicks a styled span/div proxy, walk up looking for the
  // SPECIFIC hidden radio/checkbox the click belongs to.  We must NOT just
  // grab the first input we find under a parent container — if the container
  // wraps a fieldset of multiple checkboxes (Facebook's content_type[]), the
  // first input is the wrong target ~75% of the time.
  //
  // Strategy:
  //   1. closest <label> wrapping the click — that's the row label, return
  //      its input.  This is the dominant case for well-structured forms.
  //   2. label[for=ID] reference.
  //   3. Walk up; among ALL inputs in the container, prefer one whose own
  //      label or bounding box contains the click coordinates.
  //   4. Fallback: closest input by Euclidean distance to the click point.
  const _findAssociatedInput = (clicked, clickX, clickY) => {
    if (!clicked) return null;

    // 1. Nearest enclosing <label>: by definition this is THIS row's label.
    const label = clicked.closest("label");
    if (label) {
      const inp = label.querySelector('input[type="radio"], input[type="checkbox"]');
      if (inp) return inp;
      const f = label.getAttribute("for");
      if (f) {
        const ref = document.getElementById(f);
        if (ref && ref.tagName === "INPUT") return ref;
      }
    }

    // Fast-path bail-out: if the click target isn't inside any structure
    // that *could* be a hidden-input proxy (a label, a styled control row,
    // a form, a fieldset), don't waste time walking the ancestor chain.
    // This skips the heavy querySelectorAll-per-parent path for the vast
    // majority of clicks (links, dropdown options, scrolling, etc.).
    if (!clicked.closest("label,fieldset,form,[role=group],[role=radiogroup]," +
                          "[data-testid*=checkbox i],[data-testid*=radio i]")) {
      return null;
    }

    // 2. Click coordinate hit-test against every nearby input's wrapping label.
    //    This handles the case where the visual click target is OUTSIDE the
    //    label (e.g. a container span) but inside the label's bounding box.
    //    Walk depth capped at 4 (was 8) — beyond that we're almost certainly
    //    matching unrelated inputs, and the per-level querySelectorAll +
    //    bbox reads are the dominant cost on hot click paths.
    const haveCoords = (typeof clickX === "number" && typeof clickY === "number");
    let parent = clicked.parentElement;
    for (let depth = 0; parent && depth < 4; depth++, parent = parent.parentElement) {
      const inputs = parent.querySelectorAll('input[type="radio"], input[type="checkbox"]');
      if (!inputs.length) continue;
      if (inputs.length === 1) return inputs[0];

      // Multiple inputs: pick by hit-test if we have click coords.
      if (haveCoords) {
        for (const inp of inputs) {
          const ownLabel = inp.closest("label");
          if (ownLabel) {
            const r = ownLabel.getBoundingClientRect();
            if (clickX >= r.left && clickX <= r.right &&
                clickY >= r.top  && clickY <= r.bottom) {
              return inp;
            }
          }
        }
      }
      // Bug A2 fix: when ALL inputs are off-screen by a wide margin (Facebook
      // pattern: position:absolute;left:-9999px), every input has the SAME
      // bbox. Euclidean distance ranks them effectively at random and we
      // mis-attribute the click. In that case, label hit-test is the only
      // reliable signal — if it didn't match, return null so the caller
      // ships a generic click instead of guessing.
      let allOffScreen = true;
      for (const inp of inputs) {
        let r;
        try { r = inp.getBoundingClientRect(); } catch (e) { r = null; }
        if (!r) continue;
        if (r.left > -1000 && r.top > -1000 && r.right < (window.innerWidth + 1000) && r.bottom < (window.innerHeight + 1000)) {
          allOffScreen = false;
          break;
        }
      }
      if (allOffScreen) {
        // Hit-test already failed (we'd have returned above). Bail out at
        // this depth and let the caller try the next ancestor.
        continue;
      }
      // Otherwise: closest input by Euclidean distance to the click point
      //            (or the clicked element's own centre if no coords).
      const cx = haveCoords ? clickX : ((() => {
        const r = clicked.getBoundingClientRect();
        return r ? r.left + r.width / 2 : 0;
      })());
      const cy = haveCoords ? clickY : ((() => {
        const r = clicked.getBoundingClientRect();
        return r ? r.top + r.height / 2 : 0;
      })());
      let best = null, bestDist = Infinity;
      for (const inp of inputs) {
        let r;
        try { r = inp.getBoundingClientRect(); } catch (e) { continue; }
        if (!r) continue;
        const ix = r.left + r.width / 2;
        const iy = r.top + r.height / 2;
        const d = Math.hypot(ix - cx, iy - cy);
        if (d < bestDist) { bestDist = d; best = inp; }
      }
      if (best) return best;
    }
    return null;
  };

  // Capture custom-widget interactions via clicks.
  document.addEventListener("click", (ev) => {
    // Ignore clicks anywhere inside the recorder's own overlay panel.
    if (ev.target && ev.target.closest && ev.target.closest("#__af2_panel")) return;
    // Bug A15 (companion to the change-handler guard): React libraries
    // sometimes call `el.click()` on the previously-checked radio when a
    // new option is picked. That synthetic click is isTrusted=false.
    //
    // Narrow filter — only drop isTrusted=false clicks when the target
    // (or any ancestor up to the form) looks like a radio/checkbox or
    // a role-based widget. Other programmatic clicks (Playwright's
    // `page.click()` on submit buttons, links, etc.) ARE legitimately
    // captured by automated capture flows and must keep working.
    if (ev.isTrusted === false) {
      const t0 = ev.target;
      if (t0 && t0.closest && t0.closest(
        'input[type="radio"], input[type="checkbox"],' +
        '[role="radio"], [role="checkbox"]'
      )) {
        return;
      }
    }
    // When the click target is a *real* radio/checkbox input, skip — the
    // change handler will fire on this same interaction and ship the right
    // action.  We must skip BOTH visible and hidden inputs here, because the
    // browser synthesises a duplicate click on the wrapped input whenever the
    // user clicks a label/span proxy (label-bound default action).  Without
    // this guard, every proxy click ships twice and toggles the checkbox
    // back to its starting state at replay time.
    {
      const tgt = ev.target;
      if (tgt && tgt.tagName === "INPUT") {
        const tt = (tgt.getAttribute("type") || "").toLowerCase();
        if (tt === "checkbox" || tt === "radio") return;
      }
    }
    const path = ev.composedPath ? ev.composedPath() : [ev.target];
    let el = null;
    for (const n of path) {
      if (!n || !n.getAttribute) continue;
      // Skip any element that is part of the overlay (id prefix __af2_).
      const eid = n.id || "";
      if (typeof eid === "string" && eid.indexOf("__af2_") === 0) return;
      const role = n.getAttribute("role");
      if (role === "checkbox" || role === "radio") { el = n; break; }
      if (role === "combobox" || role === "option") { el = n; break; }
      if (role === "button" || n.tagName === "BUTTON" || n.tagName === "A") { el = n; break; }
    }

    // --- NEW: detect clicks on visual proxies for hidden radio/checkbox ---
    // When el is null the click target was a plain <span>/<div>/<label> that
    // wraps a hidden <input type=radio|checkbox>.  We record the action on
    // the *clickable* visual proxy so the replay engine can click it.
    if (!el) {
      const clicked = ev.target;
      const assocInput = _findAssociatedInput(clicked, ev.clientX, ev.clientY);
      if (assocInput && _isHiddenInput(assocInput)) {
        const t = (assocInput.type || "").toLowerCase();
        const checked = t === "radio" ? true : !assocInput.checked;
        // Record on the hidden input itself (which has stable name/value
        // selectors) rather than the proxy label (which often only has CSS
        // selectors with dynamic IDs).  The _click_proxy flag tells the
        // replay engine to click the parent label instead of force-clicking.
        // Always include the input's `value` (radio_value) regardless of type
        // — for checkboxes that share a `name` (content_type[]), the value is
        // what disambiguates Photo / Ad / Page / Other at replay time.
        ship("check", assocInput, {
          checked,
          radio_value: assocInput.value || undefined,
          _hidden_input_name: assocInput.name || undefined,
          _hidden_input_type: t,
          _click_proxy: true,
        });
        // Bug A1: timestamp-based dedup. The browser synthesises a duplicate
        // change event when a label/proxy is clicked; we want to skip *that*
        // change but NOT a future legitimate user toggle on the same input.
        // Using a wall-clock timestamp instead of a boolean ensures the flag
        // self-expires after 800ms, so a second tick is recorded normally.
        assocInput.__af_proxy_ts = ts();
        return;
      }
    }

    if (!el) return;
    const role = el.getAttribute("role") || "";
    const tag = el.tagName.toLowerCase();

    if (role === "checkbox" || role === "radio") {
      const checked = el.getAttribute("aria-checked") === "true";
      ship("check", el, { checked });
      return;
    }
    if (role === "option") {
      // Combobox option pick — ship the parent combobox's "combobox" action.
      const combo = el.closest("[role=combobox]") || el.closest("[aria-haspopup='listbox']");
      if (combo) ship("combobox", combo, { value: (el.innerText || "").trim() });
      return;
    }
    if (looksLikeSubmit(el)) {
      // Flush any pending input first so replay captures the last keystrokes.
      // Bug A3 fix: react controlled inputs may have `pf.value === ""` at
      // submit time (state was lifted to a parent + the DOM input cleared
      // before re-render). Use the lastValue map (populated by `input` event)
      // as the source of truth, falling back to pf.value only when present.
      try {
        for (const pf of pendingFills) {
          if (!pf || !pf.isConnected) continue;
          const v = (pf.value && pf.value.length > 0)
            ? pf.value
            : (lastValue.get(pf) || "");
          if (!v) continue; // skip empties — nothing to replay
          ship("fill", pf, {
            value: v, input_method: "fill", _via: "pre_submit_flush",
          });
        }
        pendingFills.clear();
      } catch (e) { /* swallow */ }
      window.__af2_last_submit_ts = ts();
      ship("submit", el, {});
      return;
    }
    // generic click capture (custom buttons we don't know what they do)
    ship("click", el, {});
  }, true);

  // Form submit event — catches Enter-key submissions, form.submit() calls
  // from JS, and any click-submit the click handler missed.  De-duplicates
  // against the click branch via __af2_last_submit_ts so we don't double-ship.
  document.addEventListener("submit", (ev) => {
    const form = ev.target;
    if (!form || form.tagName !== "FORM") return;
    // Flush pending text fills so the last keystrokes aren't lost if the
    // form navigates away before `change` fires (common on Enter submit).
    // Bug A3: prefer lastValue snapshot when DOM value already cleared.
    try {
      for (const pf of pendingFills) {
        if (!pf || !pf.isConnected) continue;
        const v = (pf.value && pf.value.length > 0)
          ? pf.value
          : (lastValue.get(pf) || "");
        if (!v) continue;
        ship("fill", pf, {
          value: v, input_method: "fill", _via: "pre_submit_flush",
        });
      }
      pendingFills.clear();
    } catch (e) { /* swallow */ }
    if (window.__af2_last_submit_ts && (ts() - window.__af2_last_submit_ts) < 600) {
      return; // click branch already shipped this submit
    }
    window.__af2_last_submit_ts = ts();
    // Prefer the form's own submit button — its selectors are stable and
    // replay can click it the same way a user would.
    let target = form.querySelector("button[type=submit], input[type=submit]");
    if (!target) {
      const buttons = form.querySelectorAll("button, [role=button]");
      for (const b of buttons) { if (looksLikeSubmit(b)) { target = b; break; } }
    }
    // Bug A13: HTML5 buttons with form="<id>" attribute submit the form
    // from OUTSIDE its DOM tree. Without this lookup the recorder shipped
    // the form element itself, whose selectors are often less stable than
    // the button's.
    if (!target && form.id) {
      const ext = document.querySelectorAll(
        `button[form="${form.id}"], input[type=submit][form="${form.id}"]`);
      for (const b of ext) { if (looksLikeSubmit(b)) { target = b; break; } }
      if (!target && ext.length === 1) target = ext[0];
    }
    ship("submit", target || form, { _via: "form_submit_event" });
  }, true);

  // Best-effort flush on unload — React inputs can navigate before their
  // `change` event fires, and the submit handler above may not have run
  // if the navigation was triggered programmatically (location.href = ...).
  window.addEventListener("beforeunload", () => {
    try {
      for (const pf of pendingFills) {
        if (!pf || !pf.isConnected) continue;
        const v = (pf.value && pf.value.length > 0)
          ? pf.value
          : (lastValue.get(pf) || "");
        if (!v) continue;
        ship("fill", pf, {
          value: v, input_method: "fill", _via: "beforeunload",
        });
      }
      pendingFills.clear();
    } catch (e) { /* swallow */ }
  }, true);

  // Contenteditable: capture on blur with the final innerHTML.
  document.addEventListener("blur", (ev) => {
    const el = ev.target;
    if (!el || !el.isContentEditable) return;
    ship("contenteditable", el, { value: el.innerHTML });
  }, true);

  // MutationObserver on aria-checked — custom checkbox/radio widgets
  // (React headless-ui, Radix, FB internal) toggle state by flipping
  // `aria-checked` without a DOM click landing on the role=checkbox/radio
  // element.  We ship a synthetic `check` whenever the attribute flips.
  // De-duped against the click-based path via per-element timestamp.
  const _ariaObserver = new MutationObserver((muts) => {
    for (const m of muts) {
      if (m.type !== "attributes" || m.attributeName !== "aria-checked") continue;
      const el = m.target;
      if (!(el instanceof Element)) continue;
      const role = el.getAttribute("role") || "";
      if (role !== "checkbox" && role !== "radio") continue;
      if (el.__af2_last_aria_ts && ts() - el.__af2_last_aria_ts < 600) continue;
      el.__af2_last_aria_ts = ts();
      ship("check", el, { checked: el.getAttribute("aria-checked") === "true" });
    }
  });
  const _installAriaObserver = () => {
    const root = document.body || document.documentElement;
    if (!root) { requestAnimationFrame(_installAriaObserver); return; }
    try {
      _ariaObserver.observe(root, {
        subtree: true, attributes: true, attributeFilter: ["aria-checked"],
      });
    } catch (e) { /* swallow */ }
  };
  _installAriaObserver();

  // Bug A6 fix: previous regex used \b which doesn't recognise non-ASCII
  // word chars (đ, ă, ư...) so "đăng ký" / "tiếp tục" did NOT actually
  // match. Replaced with a substring-based scan plus simple boundary check
  // that works for both English and Vietnamese with diacritics.
  // Also adds a form-singleton heuristic so an icon-only button that is the
  // ONLY submit-shaped button in its <form> is treated as submit.
  const SUBMIT_KEYWORDS = [
    // English
    "submit","send","continue","next","save","post","register","sign up",
    "log in","login","sign in","verify","confirm","create","finish",
    "complete","done","apply","accept","agree","go","proceed","report",
    "subscribe","place order","pay now","order now","check out","checkout",
    // Vietnamese (with + without diacritics for typo-friendliness)
    "gửi","gui","tiếp","tiep","tiếp tục","tiep tuc","nộp","nop",
    "đăng ký","dang ky","đăng nhập","dang nhap","xác nhận","xac nhan",
    "xác minh","xac minh","hoàn tất","hoan tat","lưu","luu",
    "tạo","tao","đồng ý","dong y","chấp nhận","chap nhan",
    "báo cáo","bao cao","tiep tuc","tiếp tục",
  ];
  // Buttons whose label clearly is NOT submit — guards against a "Send help"
  // / "Cancel and continue" false positive.
  const SUBMIT_NEGATIVE = [
    "cancel","close","back","skip","later","edit","delete","remove",
    "huỷ","huy","bỏ","bo","đóng","dong","quay lại","quay lai","sửa","sua",
    "xoá","xoa","trở lại","tro lai",
  ];
  const _normSubmitText = (s) => {
    if (!s) return "";
    // Lowercase, replace anything that's not letter/digit/space with space,
    // collapse multiple spaces, pad with spaces so word-boundary checks
    // become trivial substring matches against " keyword ".
    let t = s.toLowerCase();
    // Strip emoji + control chars but keep diacritics. \p{L}/\p{N} need /u.
    t = t.replace(/[^\p{L}\p{N} _\-]/gu, " ");
    t = t.replace(/\s+/g, " ").trim();
    return " " + t + " ";
  };
  const _hasKeyword = (txt, kws) => {
    for (const kw of kws) {
      if (txt.indexOf(" " + kw + " ") !== -1) return true;
    }
    return false;
  };

  function looksLikeSubmit(el) {
    if (!el || !el.tagName) return false;
    const tag = el.tagName.toLowerCase();
    const t = (el.getAttribute("type") || "").toLowerCase();
    const role = el.getAttribute("role") || "";
    if (tag === "input" && t === "submit") return true;
    if (!(tag === "button" || role === "button" || (tag === "a" && el.getAttribute("href") === "#"))) {
      return false;
    }
    if (tag === "button" && t && t !== "submit" && t !== "button") {
      // type="reset" / "menu" — never submit-ish.
      return false;
    }
    const rawTxt = (el.innerText || el.textContent || "") + " "
                 + (el.getAttribute("aria-label") || "") + " "
                 + (el.getAttribute("title") || "") + " "
                 + (el.getAttribute("data-action") || "");
    const norm = _normSubmitText(rawTxt);
    if (_hasKeyword(norm, SUBMIT_NEGATIVE)) return false;
    if (_hasKeyword(norm, SUBMIT_KEYWORDS)) return true;

    // Bug A6 / A13: form-singleton heuristic. An icon-only button with no
    // text but inside a <form> that has it as the only submit-shaped button
    // is overwhelmingly the submit. Lets us catch FB's icon-only "Send"
    // arrow without false positives on busy pages.
    if (tag === "button" && (t === "submit" || t === "")) {
      const form = el.closest("form");
      if (form) {
        const candidates = form.querySelectorAll(
          "button[type=submit], button:not([type]), input[type=submit]");
        if (candidates.length === 1 && candidates[0] === el) return true;
      }
    }
    return false;
  }

  // ---------------------- floating control panel ----------------------
  const installPanel = () => {
    if (document.getElementById("__af2_panel")) return;
    if (!document.body) { requestAnimationFrame(installPanel); return; }
    const wrap = document.createElement("div");
    wrap.id = "__af2_panel";
    wrap.style.cssText = `
      position: fixed; top: 12px; right: 12px; z-index: 2147483647;
      background: rgba(20,20,30,0.96); color: #fff;
      font: 13px/1.4 system-ui, sans-serif;
      padding: 10px 12px; border-radius: 10px;
      box-shadow: 0 8px 28px rgba(0,0,0,0.4);
      width: 260px; user-select: none;
      pointer-events: auto; isolation: isolate;
      transform: translateZ(0); will-change: transform;
    `;
    wrap.innerHTML = `
      <div style="display:flex;align-items:center;gap:6px;margin-bottom:6px;">
        <span style="display:inline-block;width:9px;height:9px;border-radius:50%;
          background:#ff3c3c;box-shadow:0 0 6px #ff3c3c;animation:__af2_blink 1s infinite;"></span>
        <strong>Recording (v2)</strong>
        <span id="__af2_count" style="margin-left:auto;color:#9af;">0 actions</span>
      </div>
      <div id="__af2_last" style="font-size:11px;color:#bbb;
        white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-bottom:6px;"></div>
      <div style="display:flex;gap:6px;margin-bottom:6px;">
        <button id="__af2_otp" style="flex:1;padding:6px 8px;background:#1f5d8b;color:#fff;
          border:0;border-radius:6px;cursor:pointer;font-weight:600;"
          title="Click an OTP input first, then press this button to fetch the latest code from kuku.lu and paste it.">✎ Get OTP → paste</button>
      </div>
      <div id="__af2_otp_status" style="font-size:11px;color:#aaa;margin-bottom:6px;min-height:14px;"></div>
      <div style="display:flex;gap:6px;">
        <button id="__af2_done" style="flex:1;padding:6px 8px;background:#1f8b3a;color:#fff;
          border:0;border-radius:6px;cursor:pointer;font-weight:600;">Done</button>
        <button id="__af2_undo" style="padding:6px 8px;background:#444;color:#fff;
          border:0;border-radius:6px;cursor:pointer;">Undo</button>
        <button id="__af2_cancel" style="padding:6px 8px;background:#7a1f1f;color:#fff;
          border:0;border-radius:6px;cursor:pointer;">Cancel</button>
      </div>
      <style>@keyframes __af2_blink { 50% { opacity:0.25; } }</style>
    `;
    document.body.appendChild(wrap);
    document.getElementById("__af2_done").addEventListener("click", () => window.__afFinish && window.__afFinish());
    document.getElementById("__af2_cancel").addEventListener("click", () => window.__afCancel && window.__afCancel());
    document.getElementById("__af2_undo").addEventListener("click", () => window.__afUndo && window.__afUndo());
    // The OTP button captures the current focus FIRST (mousedown, before the
    // browser shifts focus to the panel) so we know which input the user wants
    // the code typed into.
    const otpBtn = document.getElementById("__af2_otp");
    if (otpBtn) {
      otpBtn.addEventListener("mousedown", () => {
        try { window.__af2_otp_target = document.activeElement || null; }
        catch (e) { window.__af2_otp_target = null; }
      }, true);
      otpBtn.addEventListener("click", async () => {
        const stat = document.getElementById("__af2_otp_status");
        if (stat) stat.textContent = "… fetching code from kuku.lu";
        try {
          if (window.__afOtp) await window.__afOtp();
          else if (stat) stat.textContent = "OTP integration not configured (recorder started without --kuku-creds)";
        } catch (e) {
          if (stat) stat.textContent = "OTP error: " + (e && e.message || e);
        }
      });
    }
  };
  // Build an action stub describing the input the user just clicked into.
  // Called from the Python side once the OTP code has been fetched.
  window.__af_describeOtpTarget = () => {
    const el = window.__af2_otp_target;
    if (!el || el === document.body) return null;
    try {
      const sels = buildSelectors(el);
      return {
        field_id: fieldIdFor(el, sels),
        field_type: (el.getAttribute && el.getAttribute("type")) || el.tagName.toLowerCase(),
        selectors: sels,
        fingerprint: fingerprintOf(el),
        frame_chain: frameChain(),
        url: location.href,
      };
    } catch (e) { return null; }
  };
  window.__af_setOtpStatus = (msg) => {
    const el = document.getElementById("__af2_otp_status");
    if (el) el.textContent = msg || "";
  };
  window.__af_updatePanel = (count, last) => {
    const c = document.getElementById("__af2_count");
    const l = document.getElementById("__af2_last");
    if (c) c.textContent = `${count} action${count === 1 ? "" : "s"}`;
    if (l) l.textContent = last || "";
  };
  installPanel();
  document.addEventListener("DOMContentLoaded", installPanel);
  setTimeout(installPanel, 800);
})();
"""


# --------------------------------------------------------------------------------------
# Python-side session
# --------------------------------------------------------------------------------------


class _Session:
    def __init__(self) -> None:
        self.actions: list[dict] = []

    def add(self, snap: dict) -> str:
        # De-dup consecutive identical fills on the same field (common when both
        # `input` (keystrokes) and `change` (blur) ship the same value).
        if self.actions and snap.get("kind") == "fill":
            last = self.actions[-1]
            if (last.get("kind") == "fill"
                    and last.get("field_id") == snap.get("field_id")
                    and last.get("frame_chain") == snap.get("frame_chain")):
                last.update(snap)
                return f"~{snap.get('field_id')}"
        self.actions.append(snap)
        label = f"{snap.get('kind')} {snap.get('field_id', '')}"
        return label[:60]

    def undo_last(self) -> None:
        if self.actions:
            self.actions.pop()


def _is_chrome_user_data_dir(p: Path) -> bool:
    """True if ``p`` looks like a Chrome "User Data" directory.

    Filesystems on Windows are case-insensitive but can preserve case in
    different ways across Chrome versions/locales ("Local State" vs
    "local state"). We tolerate both casings and also accept lowercase
    on Linux/macOS.
    """
    try:
        if not p.is_dir():
            return False
        # Fast path: the canonical capitalisation Chrome writes.
        if (p / "Local State").exists():
            return True
        # Case-insensitive scan as a safety net.
        for entry in p.iterdir():
            if entry.name.lower() == "local state":
                return True
    except Exception:
        return False
    return False


def _resolve_chrome_profile(profile_path: str) -> tuple[str, Optional[str]]:
    """Resolve a Chrome profile path into (user_data_dir, profile_directory).

    Chrome stores profiles as sub-folders (``Default``, ``Profile 1``, …)
    inside a *User Data* directory.  Playwright's
    ``launch_persistent_context`` expects the **User Data** path and an
    optional ``--profile-directory=…`` arg.

    If the user gives us ``…/User Data/Profile 36``, we split it:
      → user_data_dir = ``…/User Data``
      → profile_directory = ``Profile 36``

    If the user gives us ``…/User Data`` directly (or any path that contains
    ``Local State`` – the marker file Chrome writes in User Data), we use
    it as-is and let Chromium pick the Default profile.
    """
    p = Path(profile_path)
    if _is_chrome_user_data_dir(p):
        return str(p), None
    # If the path contains "Preferences" and a parent has "Local State",
    # it is a sub-profile.
    if (p / "Preferences").exists():
        parent = p.parent
        if _is_chrome_user_data_dir(parent):
            return str(parent), p.name
    # Fallback: use as-is (might be a standalone Playwright profile).
    return str(p), None


def _check_chrome_profile_lock(user_data_dir: str) -> Optional[str]:
    """Return a human-readable error string when a Chrome profile is in use.

    Chrome (and Chromium) writes a ``SingletonLock`` symlink/file in the
    ``User Data`` directory while a browser is attached. Launching
    Playwright on the same path would either fail with an opaque
    "ProcessSingleton" error or, worse, silently corrupt the profile.
    On Windows the equivalent marker is ``lockfile`` plus the absence of
    ``First Run`` write access.
    """
    try:
        p = Path(user_data_dir)
        for marker in ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"):
            if (p / marker).exists():
                return (
                    f"Chrome profile at {user_data_dir!s} is currently in use "
                    f"(found {marker}). Close any running Chrome / Edge windows "
                    f"that share this profile before starting the recorder."
                )
    except Exception:
        return None
    return None


def _default_chrome_user_data_dir() -> Optional[str]:
    """Best-effort default location of the user's main Chrome User Data dir.

    Used as a hint in error messages and as the auto-fill suggestion in
    the GUI's recorder dialog. Returns ``None`` when no plausible folder
    exists.
    """
    candidates: list[Path] = []
    home = Path.home()
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates.append(Path(local) / "Google" / "Chrome" / "User Data")
            candidates.append(Path(local) / "Microsoft" / "Edge" / "User Data")
    elif sys.platform == "darwin":
        candidates.append(home / "Library" / "Application Support" / "Google" / "Chrome")
    else:
        candidates.append(home / ".config" / "google-chrome")
        candidates.append(home / ".config" / "chromium")
    for c in candidates:
        if _is_chrome_user_data_dir(c):
            return str(c)
    return None


def _resolve_shots_dir(
    out_path: Optional[str],
    override: Optional[str],
) -> Optional[Path]:
    """Pick the directory we'll write per-step screenshots into.

    Priority:
      1. ``override`` if the caller passed one explicitly.
      2. ``<out_stem>_shots`` next to the config (the historical default).
      3. ``<tempdir>/auto_form_filler_<stem>_shots`` if (2) is unwritable
         (e.g. the config lives in OneDrive on Windows and the parent has
         a sync lock on writes).

    Returns ``None`` when ``out_path`` is missing AND no override was
    given — in that case the caller skips screenshots entirely.
    """
    if override:
        try:
            d = Path(override).expanduser()
            d.mkdir(parents=True, exist_ok=True)
            return d
        except Exception:
            pass
    if not out_path:
        return None
    out = Path(out_path)
    primary = out.parent / f"{out.stem}_shots"
    try:
        primary.mkdir(parents=True, exist_ok=True)
        # Probe writability — OneDrive sometimes lets you mkdir but blocks
        # the next file write.
        probe = primary / ".__af2_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return primary
    except Exception:
        pass
    import tempfile
    fallback = Path(tempfile.gettempdir()) / f"auto_form_filler_{out.stem}_shots"
    try:
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback
    except Exception:
        return None


def build_config(
    session: _Session,
    *,
    target_url: str,
    wait_for_selector: Optional[str] = None,
    auto_template: bool = True,
) -> dict:
    """Construct a v2 config dict from a finished recording session.

    When ``auto_template=True`` (the default) the captured ``fill`` /
    ``contenteditable`` actions whose values look like emails / phones /
    dates / full names get a ``value_template`` field of ``"{var}"``,
    and a side ``variables`` dict is added so the user knows which
    fields are templatised. The literal ``value`` is preserved as a
    fallback so old replay code paths keep working.
    """
    cfg: dict = {
        "version": 2,
        "target_url": target_url,
        "wait_for_selector": wait_for_selector,
        "actions": [],
        "submit": None,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
    }
    # Bug A5 fix: previously every recorded `submit` action was lifted out
    # of the action stream and the LAST one became cfg["submit"]. Multi-step
    # forms (Next → Next → Submit) lost their intermediate submits, so
    # replay never advanced past step 1.
    #
    # New behaviour:
    #   - When >1 submit was recorded (multi-step form), ALL submits stay
    #     INLINE in cfg["actions"] in their original order so replay
    #     processes them along with surrounding fills. cfg["submit"] still
    #     points at the LAST submit for callers that want to know where
    #     the flow finishes.
    #   - When exactly 1 submit was recorded (single-step form), it is
    #     LIFTED OUT of cfg["actions"] and exposed only via cfg["submit"]
    #     so callers can decide when (and whether) to click it. This
    #     preserves the long-standing behaviour where ``run_actions``
    #     fills fields but does NOT auto-submit.
    #   - cfg["submits"] is a NEW list of all submits (ordered) for callers
    #     that want to enumerate every step.
    submits: list[dict] = []
    raw_actions: list[dict] = []
    submit_actions_inline: list[dict] = []
    for a in session.actions:
        if a.get("kind") == "submit":
            submits.append({
                "selectors": a.get("selectors") or [],
                "fingerprint": a.get("fingerprint") or {},
                "frame_chain": a.get("frame_chain") or ["top"],
                "ts": a.get("ts"),
            })
            submit_actions_inline.append(a)
            raw_actions.append(a)
            continue
        raw_actions.append(a)
    # Single-submit case: drop the lone inline submit so replay does not
    # auto-click. Multi-submit case: keep them all inline.
    if len(submit_actions_inline) <= 1:
        raw_actions = [a for a in raw_actions if a.get("kind") != "submit"]

    if auto_template:
        try:
            from auto_template import extract_variables
            templated, variables = extract_variables(raw_actions)
        except Exception:
            templated, variables = raw_actions, {}
        cfg["actions"] = templated
        if variables:
            cfg["variables"] = variables
    else:
        cfg["actions"] = raw_actions
    # Back-compat: cfg["submit"] = LAST submit (None if no submit captured).
    cfg["submit"] = submits[-1] if submits else None
    if submits:
        cfg["submits"] = submits
    return cfg


# --------------------------------------------------------------------------------------
# Public async entry points
# --------------------------------------------------------------------------------------


async def record_to_config(
    target_url: str,
    *,
    wait_for_selector: Optional[str] = None,
    out_path: Optional[str] = None,
    headless: bool = False,
    save_events: bool = True,
    proxy: Optional[Any] = None,
    chrome_profile: Optional[str] = None,
    capture_screenshots: bool = False,
    shots_dir_override: Optional[str] = None,
    kuku_creds_path: Optional[str] = None,
    kuku_otp_options: Optional[dict] = None,
) -> tuple[dict, list[dict]]:
    """Drive a browser, record the user's clicks/fills, return a v2 config.

    Screenshots are **off by default**. Every ``await page.screenshot()``
    call routes through CDP ``Page.captureScreenshot`` which briefly
    stalls the renderer (100-500 ms) — the user sees a visible flash on
    every click. Replay only needs selectors + fingerprint, not the
    image, so the default behaviour is now to skip them entirely.

    Pass ``capture_screenshots=True`` (or ``--screenshots`` on the CLI)
    to opt into per-step PNGs; they will be debounced (one shot per
    quiet period, not one per click) and saved next to the config in
    ``<stem>_shots/step_NNNN_<kind>.png`` with the relative path stored
    on each action under the ``screenshot`` key. Screenshot failures
    (page closed mid-click, frame detached, etc.) are silently swallowed
    — the recorded action is unaffected.

    When ``kuku_creds_path`` points at a JSON file containing
    ``{csrf_token, sessionhash, current_address?}`` (see
    :py:class:`kuku_lu.KukuCreds`), the recorder enables the floating
    "Get OTP → paste" button in the panel: clicking it fetches the
    latest matching code from kuku.lu and types it into whichever input
    the user just clicked into. The action is recorded as
    ``kind="otp_paste"`` so replay can re-fetch the code per-account.
    ``kuku_otp_options`` is a dict like
    ``{"regex": ..., "from_filter": "facebook", "timeout_ms": 180000,
       "address": "abc@kpay.be"}`` overriding the defaults.
    """
    fut: asyncio.Future[str] = asyncio.get_event_loop().create_future()
    session = _Session()
    proxy_dict = normalize_proxy(proxy) if proxy else None

    # Resolve the screenshots directory eagerly so we don't pay the cost
    # again per click. ``shots_dir`` stays None when screenshots are off
    # or no out_path was given (e.g. ad-hoc CLI smoke runs).
    shots_dir: Optional[Path] = None
    if capture_screenshots:
        shots_dir = _resolve_shots_dir(out_path, shots_dir_override)

    # Smart-wait scratch state. Whenever the user fires a click/submit-ish
    # action, we mark ``_recently_acted_at`` and the framenavigated handler
    # treats any navigation within the next ``_NAV_WINDOW_MS`` as a
    # consequence of that action — it then injects a synthetic ``wait``
    # action right after it so the replay engine waits for the load.
    _NAV_WINDOW_MS = 1500
    _recently_acted_at: list[int] = [0]   # mutable cell so closures can update it

    async with async_playwright() as p:
        browser = None
        if chrome_profile:
            user_data_dir, profile_dir = _resolve_chrome_profile(chrome_profile)
            lock_msg = _check_chrome_profile_lock(user_data_dir)
            if lock_msg:
                # Surface the message via stderr AND raise so the GUI can
                # show it in a dialog instead of letting Playwright die
                # 30 s later with an unhelpful ProcessSingleton error.
                print(f"[recorder] {lock_msg}", file=sys.stderr)
                raise RuntimeError(lock_msg)
            print(f"[recorder] user_data_dir: {user_data_dir}")
            if profile_dir:
                print(f"[recorder] profile_directory: {profile_dir}")
            launch_kwargs: dict[str, Any] = {"headless": headless}
            if profile_dir:
                launch_kwargs.setdefault("args", [])
                launch_kwargs["args"].append(f"--profile-directory={profile_dir}")
            if proxy_dict:
                launch_kwargs["proxy"] = proxy_dict
                print(f"[recorder] proxy: {mask_proxy(proxy_dict)}")
            context = await p.chromium.launch_persistent_context(
                user_data_dir, **launch_kwargs,
            )
        else:
            launch_kwargs = {"headless": headless}
            if proxy_dict:
                launch_kwargs["proxy"] = proxy_dict
                print(f"[recorder] proxy: {mask_proxy(proxy_dict)}")
            browser = await p.chromium.launch(**launch_kwargs)
            context = await browser.new_context()

        # Debounced screenshot pipeline.
        #
        # `await page.screenshot()` is the single biggest source of the
        # "giật như reload" jitter the user sees while clicking through a
        # form: the call routes through CDP `Page.captureScreenshot`,
        # which briefly stalls the renderer thread while it composes the
        # surface and encodes the PNG (100–500 ms on busy pages). Even
        # though the asyncio side is fire-and-forget, the renderer itself
        # is what flickers — every captured click pays that stall, and on
        # consecutive clicks the page visibly flashes.
        #
        # The fix: don't screenshot on every action. Coalesce them. After
        # each action we mark the latest snapshot as "needs an image",
        # then a single background task fires the actual screenshot only
        # after `_SHOT_DEBOUNCE_S` of quiet. Rapid click bursts collapse
        # into one screenshot at the end (or zero, if the user keeps
        # interacting), eliminating mid-click flashes entirely.
        _SHOT_DEBOUNCE_S = 0.35  # quiet period before we actually shoot
        _shot_pending: list[Optional[tuple[dict, int]]] = [None]
        _shot_task: list[Optional[asyncio.Task]] = [None]

        async def _do_screenshot(snap: dict, idx: int) -> None:
            if shots_dir is None:
                return
            try:
                # Prefer the most-recently-active page. Playwright orders
                # ``context.pages`` roughly by creation; the user's flow
                # is almost always on the last opened page (form pop-ups,
                # OAuth tabs, etc.).
                active = context.pages[-1] if context.pages else None
                if active is None or active.is_closed():
                    return
                kind = str(snap.get("kind", "action"))[:20].replace("/", "_")
                fname = f"step_{idx:04d}_{kind}.png"
                fpath = shots_dir / fname
                await active.screenshot(
                    path=str(fpath),
                    full_page=False,
                    timeout=2500,
                )
                # Store as POSIX-relative so the same config replays on
                # both Linux and Windows replay hosts.
                snap["screenshot"] = f"{shots_dir.name}/{fname}"
            except Exception as exc:
                snap["screenshot_error"] = repr(exc)[:120]

        async def _flush_screenshot() -> None:
            try:
                # Sleep, then re-check the pending slot every iteration:
                # if a newer action arrived during the sleep, restart the
                # quiet timer so we never fire mid-burst.
                while True:
                    await asyncio.sleep(_SHOT_DEBOUNCE_S)
                    target = _shot_pending[0]
                    if target is None:
                        return
                    # Snapshot the slot value, then atomically clear it.
                    # If a new action lands AFTER this read but BEFORE we
                    # finish the screenshot, the next _schedule_screenshot
                    # call will spin up a fresh task.
                    _shot_pending[0] = None
                    snap, idx = target
                    await _do_screenshot(snap, idx)
                    if _shot_pending[0] is None:
                        return
            finally:
                _shot_task[0] = None

        def _capture_screenshot(snap: dict, idx: int) -> None:
            """Schedule a debounced screenshot for this action.

            Synchronous + non-blocking: just updates the pending slot and
            (re)starts the background flusher. Replaces the previous
            ``_capture_screenshot`` coroutine that callers had to launch
            with ``asyncio.create_task`` — the result is the same
            (fire-and-forget background work) but with no per-click PNG
            stall on the renderer.
            """
            if shots_dir is None:
                return
            # Latest action wins — supersede any pending screenshot.
            _shot_pending[0] = (snap, idx)
            if _shot_task[0] is None or _shot_task[0].done():
                _shot_task[0] = asyncio.create_task(_flush_screenshot())

        # Coalesced panel update: when several actions ship in quick
        # succession, we only want ONE round-trip to JS to refresh the
        # "N actions / last: …" panel. ``_panel_pending`` holds the latest
        # (count, label) pair we still need to display; the background task
        # picks it up after a short sleep and clears the flag.
        _panel_pending: list[Optional[tuple[int, str]]] = [None]
        _panel_task: list[Optional[asyncio.Task]] = [None]

        async def _flush_panel() -> None:
            try:
                # Tiny delay lets bursts of actions (e.g. fill + change
                # firing back-to-back) collapse into one update.
                await asyncio.sleep(0.04)
                pending = _panel_pending[0]
                _panel_pending[0] = None
                if pending is None:
                    return
                count, label = pending
                for pg in context.pages:
                    try:
                        await pg.evaluate(
                            "(args) => window.__af_updatePanel && window.__af_updatePanel(args[0], args[1])",
                            [count, label],
                        )
                    except Exception:
                        pass
            finally:
                _panel_task[0] = None

        def _schedule_panel_update(count: int, label: str) -> None:
            _panel_pending[0] = (count, label)
            if _panel_task[0] is None or _panel_task[0].done():
                _panel_task[0] = asyncio.create_task(_flush_panel())

        async def on_record(payload: str) -> None:
            try:
                snap = json.loads(payload)
            except Exception:
                return
            # Add to the session FIRST: the dedup logic in `session.add()`
            # merges consecutive fills on the same field, and we want to
            # screenshot the surviving dict (so the path key isn't lost
            # when the merge overwrites it). The fire-and-forget screenshot
            # task runs in the background — it never blocks the recorder
            # binding callback, which is the main source of "giật lag"
            # the user feels while clicking through the form.
            label = session.add(snap)
            target = session.actions[-1] if session.actions else snap
            _capture_screenshot(target, len(session.actions))
            # Mark this moment so framenavigated knows whose navigation
            # this is. Only navigation-prone kinds count.
            kind = (snap.get("kind") or "").lower()
            if kind in ("submit", "click"):
                _recently_acted_at[0] = int(time.time() * 1000)
            _schedule_panel_update(len(session.actions), label)

        def _on_frame_navigated(frame: Any) -> None:
            """Inject a synthetic wait action when a navigation follows a click/submit.

            Only the *main* frame counts; subframe navigations are too
            chatty (analytics iframes etc.) and would pollute the action
            stream.
            """
            try:
                if frame.parent_frame is not None:
                    return  # not the top frame
                now = int(time.time() * 1000)
                if now - _recently_acted_at[0] > _NAV_WINDOW_MS:
                    return  # no recent action — likely the initial page.goto
                # Only inject one wait per click burst.
                if session.actions and session.actions[-1].get("kind") == "wait" \
                        and session.actions[-1].get("wait_kind") == "navigation":
                    return
                session.actions.append({
                    "kind": "wait",
                    "wait_kind": "navigation",
                    "timeout_ms": 15000,
                    "ts": now,
                    "_synthetic": True,
                })
                # Reset so we don't double-inject for chained redirects.
                _recently_acted_at[0] = 0
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
            _schedule_panel_update(len(session.actions), "(undo)")

        # ---- OTP integration (kuku.lu) ---------------------------------
        # Loaded lazily so the recorder still imports cleanly even if the
        # optional ``requests`` / ``beautifulsoup4`` deps are missing.
        kuku_creds_obj = None
        kuku_options = dict(kuku_otp_options or {})
        if kuku_creds_path:
            try:
                from kuku_lu import KukuCreds
                kuku_creds_obj = KukuCreds.from_dict(
                    json.loads(Path(kuku_creds_path).read_text(encoding="utf-8"))
                )
            except Exception as exc:
                print(
                    f"[recorder] could not load kuku creds from {kuku_creds_path!r}: {exc!r}",
                    file=sys.stderr,
                )

        async def on_otp() -> None:
            """Fetch a code from kuku.lu, type it into the focused input,
            and ship an ``otp_paste`` action describing the input."""
            if kuku_creds_obj is None:
                for pg in context.pages:
                    try:
                        await pg.evaluate(
                            "(m) => window.__af_setOtpStatus && window.__af_setOtpStatus(m)",
                            "no kuku creds — pass --kuku-creds <path>",
                        )
                    except Exception:
                        pass
                return
            # Find the page whose panel button was clicked. The most
            # recently active page is overwhelmingly the right one.
            target_page = context.pages[-1] if context.pages else None
            if target_page is None:
                return
            # Capture the focused element's selectors BEFORE we navigate or
            # do anything that could shift focus.
            try:
                desc = await target_page.evaluate(
                    "() => window.__af_describeOtpTarget && window.__af_describeOtpTarget()"
                )
            except Exception as exc:
                desc = None
                print(f"[recorder] otp: describe failed: {exc!r}", file=sys.stderr)
            if not desc or not desc.get("selectors"):
                try:
                    await target_page.evaluate(
                        "(m) => window.__af_setOtpStatus && window.__af_setOtpStatus(m)",
                        "click on the OTP input first, then press the button",
                    )
                except Exception:
                    pass
                return

            from kuku_lu import Kuku, KukuError
            address = kuku_options.get("address") or kuku_creds_obj.current_address
            regex = kuku_options.get("regex") or r"(?<!\d)(\d{5,8})(?!\d)"
            from_filter = kuku_options.get("from_filter")
            timeout_ms = int(kuku_options.get("timeout_ms") or 180000)

            try:
                # Use Playwright backend so we share the user's browser
                # cookies + proxy + Cloudflare clearance.
                async with await Kuku.from_playwright(
                    target_page, creds=kuku_creds_obj,
                ) as k:
                    if not address:
                        try:
                            await target_page.evaluate(
                                "(m) => window.__af_setOtpStatus && window.__af_setOtpStatus(m)",
                                "minting kuku.lu address…",
                            )
                        except Exception:
                            pass
                        address = await k.create_address()
                        kuku_creds_obj.current_address = address
                    try:
                        await target_page.evaluate(
                            "(m) => window.__af_setOtpStatus && window.__af_setOtpStatus(m)",
                            f"waiting for code at {address}…",
                        )
                    except Exception:
                        pass
                    code = await k.wait_for_code(
                        address,
                        regex=regex,
                        timeout=timeout_ms / 1000.0,
                        from_filter=from_filter,
                    )
            except KukuError as exc:
                try:
                    await target_page.evaluate(
                        "(m) => window.__af_setOtpStatus && window.__af_setOtpStatus(m)",
                        f"kuku.lu error: {exc}",
                    )
                except Exception:
                    pass
                return
            except Exception as exc:
                try:
                    await target_page.evaluate(
                        "(m) => window.__af_setOtpStatus && window.__af_setOtpStatus(m)",
                        f"unexpected error: {exc!r}",
                    )
                except Exception:
                    pass
                return

            # Type the code into the focused input — same as the user
            # pressing the keys themselves.
            try:
                await target_page.keyboard.insert_text(code)
            except Exception:
                try:
                    await target_page.keyboard.type(code, delay=30)
                except Exception:
                    pass

            action = {
                "kind": "otp_paste",
                "ts": int(time.time() * 1000),
                "value": code,  # last-known good for replay-debug fallback
                "input_method": "type",
                "source": {
                    "kind": "kuku.lu",
                    "address": address,
                    "regex": regex,
                    "from_filter": from_filter,
                    "timeout_ms": timeout_ms,
                },
                **{k: v for k, v in desc.items() if k not in ("frame_chain",)},
                "frame_chain": desc.get("frame_chain") or ["top"],
            }
            label = session.add(action)
            stored = session.actions[-1] if session.actions else action
            _capture_screenshot(stored, len(session.actions))
            _schedule_panel_update(len(session.actions), f"otp:{address} → {label}")
            try:
                await target_page.evaluate(
                    "(m) => window.__af_setOtpStatus && window.__af_setOtpStatus(m)",
                    f"got code {code} from {address}",
                )
            except Exception:
                pass

        # expose_function may raise if already registered (persistent context
        # remembers bindings from previous sessions).  Wrap each call so we
        # don't abort the whole recording.
        for fn_name, fn_ref in [
            ("__afRecord", on_record),
            ("__afFinish", on_finish),
            ("__afCancel", on_cancel),
            ("__afUndo", on_undo),
            ("__afOtp", on_otp),
        ]:
            try:
                await context.expose_function(fn_name, fn_ref)
            except Exception:
                pass  # already registered — OK

        await context.add_init_script(OVERLAY_JS)

        # Persistent contexts may already have pages open.  Close stale ones
        # so the user is not confused by old tabs.
        if chrome_profile:
            for stale in context.pages:
                try:
                    await stale.close()
                except Exception:
                    pass

        page = await context.new_page()

        def _on_close(_p: Any) -> None:
            if not fut.done():
                fut.set_result("closed")

        page.on("close", _on_close)
        context.on("close", lambda _c: _on_close(None))
        # Smart-wait: detect navigations that follow a captured click/submit
        # and inject a synthetic wait action so replay knows to wait too.
        page.on("framenavigated", _on_frame_navigated)
        # Also attach the handler to any future page (multi-tab flows).
        context.on("page", lambda _p: _p.on("framenavigated", _on_frame_navigated))

        try:
            await page.goto(target_url, wait_until="domcontentloaded")
        except Exception as exc:
            print(f"[recorder] navigation failed: {exc}", file=sys.stderr)

        # For persistent contexts, add_init_script may not fire on the first
        # navigation if the bindings were stale.  Inject the overlay directly
        # as a safety net so the Done / Cancel / Undo buttons always appear.
        try:
            has_overlay = await page.evaluate("() => !!window.__af_updatePanel")
        except Exception:
            has_overlay = False
        if not has_overlay:
            try:
                await page.evaluate(OVERLAY_JS)
            except Exception as exc:
                print(f"[recorder] overlay inject fallback failed: {exc}",
                      file=sys.stderr)

        if wait_for_selector:
            try:
                await page.wait_for_selector(wait_for_selector, timeout=15000)
            except Exception:
                pass

        outcome = await fut

        # Bug A7 fix: previously this awaited the running task, but a
        # screenshot scheduled microseconds before fut resolved was still
        # sleeping out its debounce window — the task would be cancelled
        # by browser teardown, dropping the final PNG. Now we (a) await
        # the running task, then (b) force-shoot anything left in the
        # pending slot so the LAST action always lands on disk.
        try:
            t = _shot_task[0]
            if t is not None and not t.done():
                await asyncio.wait_for(t, timeout=2.0)
        except Exception:
            pass
        try:
            pending = _shot_pending[0]
            if pending is not None:
                _shot_pending[0] = None
                await asyncio.wait_for(_do_screenshot(*pending), timeout=2.0)
        except Exception:
            pass

        try:
            await context.close()
            if browser:
                await browser.close()
        except Exception:
            pass

    actions = session.actions if outcome != "cancel" else []
    config = build_config(
        _Session() if outcome == "cancel" else session,
        target_url=target_url,
        wait_for_selector=wait_for_selector,
    )

    if out_path:
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
        if save_events:
            ev_path = out.with_name(out.stem + ".events.json")
            ev_path.write_text(
                json.dumps(actions, indent=2, ensure_ascii=False), encoding="utf-8"
            )

    return config, actions


def record_to_config_sync(
    target_url: str,
    *,
    wait_for_selector: Optional[str] = None,
    out_path: Optional[str] = None,
    headless: bool = False,
    save_events: bool = True,
    capture_screenshots: bool = False,
    proxy: Optional[Any] = None,
    chrome_profile: Optional[str] = None,
    shots_dir_override: Optional[str] = None,
    kuku_creds_path: Optional[str] = None,
    kuku_otp_options: Optional[dict] = None,
) -> tuple[dict, list[dict]]:
    return asyncio.run(record_to_config(
        target_url,
        wait_for_selector=wait_for_selector,
        out_path=out_path,
        headless=headless,
        save_events=save_events,
        proxy=proxy,
        chrome_profile=chrome_profile,
        capture_screenshots=capture_screenshots,
        shots_dir_override=shots_dir_override,
        kuku_creds_path=kuku_creds_path,
        kuku_otp_options=kuku_otp_options,
    ))


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def _default_out_path() -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"recorded_v2_{stamp}.json"


def main() -> None:
    p = argparse.ArgumentParser(description="Record a form-filling session into a v2 config JSON.")
    p.add_argument("--url", required=True)
    p.add_argument("--out", default=None)
    p.add_argument("--wait-for", default=None)
    p.add_argument("--headless", action="store_true")
    p.add_argument("--no-events", action="store_true")
    p.add_argument(
        "--screenshots",
        action="store_true",
        help="Capture a per-step viewport PNG into <out>_shots/. "
             "Off by default because every screenshot briefly stalls "
             "the Chromium renderer and produces a visible click flash.",
    )
    # Back-compat alias: old --no-screenshots still works (was the
    # opt-out before the default flipped). Now it's a no-op.
    p.add_argument("--no-screenshots", action="store_true", help=argparse.SUPPRESS)
    p.add_argument(
        "--chrome-profile",
        help="Path to a Chrome user-data-dir to reuse cookies/proxy/extensions.",
    )
    p.add_argument(
        "--shots-dir",
        help="Override directory for per-step screenshots. "
             "Defaults to <out>_shots/ (with a tempdir fallback when the parent "
             "is read-only, e.g. inside OneDrive on Windows).",
    )
    p.add_argument(
        "--kuku-creds",
        help="Path to a JSON file with kuku.lu credentials "
             "({csrf_token, sessionhash, current_address?}). When set, the "
             "recorder panel exposes a button that fetches the latest code "
             "and pastes it into the focused input.",
    )
    p.add_argument(
        "--kuku-from",
        dest="kuku_from",
        default=None,
        help="Substring filter applied to mail bodies before code extraction "
             "(e.g. 'facebook'). Matches case-insensitively.",
    )
    p.add_argument(
        "--kuku-regex",
        dest="kuku_regex",
        default=None,
        help=r"Regex with one capture group used to extract the code. "
             r"Defaults to (?<!\d)(\d{5,8})(?!\d).",
    )
    p.add_argument(
        "--kuku-timeout-ms",
        dest="kuku_timeout_ms",
        type=int,
        default=180000,
        help="How long to poll kuku.lu before giving up. Default 180000 (3 min).",
    )
    p.add_argument(
        "--kuku-address",
        dest="kuku_address",
        default=None,
        help="Force a specific kuku.lu alias (e.g. abc@kpay.be). Default: "
             "reuse current_address from the creds file, or mint a fresh one.",
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
    kuku_otp_options = {
        "regex": getattr(args, "kuku_regex", None),
        "from_filter": getattr(args, "kuku_from", None),
        "timeout_ms": getattr(args, "kuku_timeout_ms", None),
        "address": getattr(args, "kuku_address", None),
    }
    # Drop None entries so on_otp falls back to its own defaults.
    kuku_otp_options = {k: v for k, v in kuku_otp_options.items() if v is not None}
    config, events = record_to_config_sync(
        args.url,
        wait_for_selector=args.wait_for,
        out_path=out_path,
        headless=args.headless,
        save_events=not args.no_events,
        capture_screenshots=bool(args.screenshots) and not args.no_screenshots,
        proxy=proxy,
        chrome_profile=getattr(args, 'chrome_profile', None),
        shots_dir_override=getattr(args, 'shots_dir', None),
        kuku_creds_path=getattr(args, 'kuku_creds', None),
        kuku_otp_options=kuku_otp_options or None,
    )
    print(f"[recorder_v2] {len(config.get('actions', []))} action(s), "
          f"{len(events)} event(s)")
    print(f"[recorder_v2] config → {out_path}")


if __name__ == "__main__":
    main()
