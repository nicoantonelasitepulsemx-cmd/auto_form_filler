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
    while (el && el.nodeType === 1 && parts.length < 8) {
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
    const parent = el.closest("form,fieldset,section,div") || el.parentElement;
    const neighbour = parent
      ? (parent.innerText || parent.textContent || "").replace(/\s+/g," ").trim().slice(0,200)
      : "";
    return {
      tag: el.tagName.toLowerCase(),
      type: (el.getAttribute("type") || "").toLowerCase() || null,
      role: el.getAttribute("role") || null,
      accessible_name: accessibleName(el),
      attributes: attrs,
      text_content: ((el.innerText || el.textContent || "").trim().slice(0,120)) || "",
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

  document.addEventListener("input", (ev) => {
    const el = ev.target;
    if (!el || !el.tagName) return;
    if (el.tagName === "INPUT" || el.tagName === "TEXTAREA") {
      lastValue.set(el, el.value);
    }
  }, true);

  document.addEventListener("change", (ev) => {
    const el = ev.target;
    if (!el || !el.tagName) return;
    const tag = el.tagName.toLowerCase();
    const t = (el.getAttribute("type") || "").toLowerCase();

    if (tag === "select") {
      const opt = el.options[el.selectedIndex];
      ship("select", el, { value: el.value, value_label: opt ? opt.text : null });
      return;
    }
    if (tag === "input" && t === "checkbox") {
      if (el.__af_proxy_handled) { delete el.__af_proxy_handled; return; }
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
      if (el.__af_proxy_handled) { delete el.__af_proxy_handled; return; }
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
      ship("fill", el, { value: el.value, input_method: "fill" });
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

    // 2. Click coordinate hit-test against every nearby input's wrapping label.
    //    This handles the case where the visual click target is OUTSIDE the
    //    label (e.g. a container span) but inside the label's bounding box.
    const haveCoords = (typeof clickX === "number" && typeof clickY === "number");
    let parent = clicked.parentElement;
    for (let depth = 0; parent && depth < 8; depth++, parent = parent.parentElement) {
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
        // Mark this input so the change handler won't record a duplicate.
        assocInput.__af_proxy_handled = true;
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
      ship("submit", el, {});
      return;
    }
    // generic click capture (custom buttons we don't know what they do)
    ship("click", el, {});
  }, true);

  // Contenteditable: capture on blur with the final innerHTML.
  document.addEventListener("blur", (ev) => {
    const el = ev.target;
    if (!el || !el.isContentEditable) return;
    ship("contenteditable", el, { value: el.innerHTML });
  }, true);

  function looksLikeSubmit(el) {
    if (!el || !el.tagName) return false;
    const tag = el.tagName.toLowerCase();
    const t = (el.getAttribute("type") || "").toLowerCase();
    const role = el.getAttribute("role") || "";
    if (tag === "input" && t === "submit") return true;
    if (tag === "button" && (t === "submit" || t === "")) {
      const txt = ((el.innerText || el.textContent || "") + " " + (el.getAttribute("aria-label") || "")).toLowerCase();
      return /\b(submit|send|continue|next|gửi|gui|tiếp|tiep|xác nhận|xac nhan|đăng ký|dang ky|nộp|nop)\b/.test(txt);
    }
    if (role === "button") {
      const txt = ((el.innerText || el.textContent || "") + " " + (el.getAttribute("aria-label") || "")).toLowerCase();
      return /\b(submit|send|continue|next|gửi|gui|tiếp|tiep|nộp|nop)\b/.test(txt);
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
    submit = None
    raw_actions: list[dict] = []
    for a in session.actions:
        if a.get("kind") == "submit":
            submit = {
                "selectors": a.get("selectors") or [],
                "fingerprint": a.get("fingerprint") or {},
                "frame_chain": a.get("frame_chain") or ["top"],
            }
            continue
        raw_actions.append(a)

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
    cfg["submit"] = submit
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
    capture_screenshots: bool = True,
    shots_dir_override: Optional[str] = None,
) -> tuple[dict, list[dict]]:
    """Drive a browser, record the user's clicks/fills, return a v2 config.

    When ``capture_screenshots=True`` (the default) and ``out_path`` is
    provided, every captured action also gets a viewport PNG saved next
    to the config in ``<stem>_shots/step_NNNN_<kind>.png`` and the
    relative path stored on the action under the ``screenshot`` key.
    Screenshot failures (page closed mid-click, frame detached, etc.)
    are silently swallowed — the recorded action is unaffected.
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

        async def _capture_screenshot(snap: dict, idx: int) -> None:
            """Best-effort viewport snapshot of whichever page is active.

            We try the page that owns the topmost (last) frame chain; that
            page is overwhelmingly the one the user just clicked. On any
            failure we annotate ``screenshot_error`` and move on so the
            action itself is never lost.
            """
            if shots_dir is None:
                return
            try:
                # Prefer the most-recently-active page. Playwright orders
                # ``context.pages`` roughly by creation; the user's flow is
                # almost always on the last opened page (form pop-ups,
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

        async def on_record(payload: str) -> None:
            try:
                snap = json.loads(payload)
            except Exception:
                return
            # Take the screenshot BEFORE session.add(): the dedup logic in
            # session.add() may merge two consecutive fills, and we want
            # the screenshot tied to the dict that survives the merge.
            await _capture_screenshot(snap, len(session.actions) + 1)
            label = session.add(snap)
            # Mark this moment so framenavigated knows whose navigation
            # this is. Only navigation-prone kinds count.
            kind = (snap.get("kind") or "").lower()
            if kind in ("submit", "click"):
                _recently_acted_at[0] = int(time.time() * 1000)
            count = len(session.actions)
            for pg in context.pages:
                try:
                    await pg.evaluate(
                        "(args) => window.__af_updatePanel && window.__af_updatePanel(args[0], args[1])",
                        [count, label],
                    )
                except Exception:
                    pass

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
            for pg in context.pages:
                try:
                    await pg.evaluate(
                        "(args) => window.__af_updatePanel && window.__af_updatePanel(args[0], args[1])",
                        [len(session.actions), "(undo)"],
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
    capture_screenshots: bool = True,
    proxy: Optional[Any] = None,
    chrome_profile: Optional[str] = None,
    shots_dir_override: Optional[str] = None,
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
        "--no-screenshots",
        action="store_true",
        help="Disable per-step viewport screenshots (default: capture into <out>_shots/).",
    )
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
        args.url,
        wait_for_selector=args.wait_for,
        out_path=out_path,
        headless=args.headless,
        save_events=not args.no_events,
        capture_screenshots=not args.no_screenshots,
        proxy=proxy,
        chrome_profile=getattr(args, 'chrome_profile', None),
        shots_dir_override=getattr(args, 'shots_dir', None),
    )
    print(f"[recorder_v2] {len(config.get('actions', []))} action(s), "
          f"{len(events)} event(s)")
    print(f"[recorder_v2] config → {out_path}")


if __name__ == "__main__":
    main()
