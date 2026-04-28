# Changelog

## Windows 11 polish

A pass over the recorder + GUI to make the tool feel native on Windows
11. None of these are bug fixes per se — the tool already ran on
Windows — but they remove the most common rough edges users hit:

- `auto_fill_gui` now opts into per-monitor DPI awareness on Windows
  (`SetProcessDpiAwareness(2)`) before the first `tk.Tk()` so text is
  crisp on 1.5x / 2.0x displays instead of bitmap-stretched.
- All preference reads/writes (`THEME_PREF_FILE`, `LAYOUT_PREF_FILE`,
  `RECENT_FILES_FILE`) explicitly use `encoding="utf-8"` so the GUI no
  longer depends on the user's `cp1252` codepage.
- `recorder_v2._is_chrome_user_data_dir` accepts both casings of
  `Local State` (Chrome occasionally writes mixed case across versions)
  so passing `C:\Users\...\google\chrome\user data` is fine even when
  Windows preserves it lowercased.
- `recorder_v2._check_chrome_profile_lock` detects an in-use Chrome
  profile (`SingletonLock` / `SingletonCookie` / `SingletonSocket` /
  `lockfile`) BEFORE Playwright tries to launch and raises a clear
  message — no more 30 second hang ending in `ProcessSingleton`.
- New `recorder_v2._default_chrome_user_data_dir` returns the most
  likely User Data path for the current OS, so the GUI's recorder
  dialog can pre-fill the field on Windows
  (`%LOCALAPPDATA%\Google\Chrome\User Data`).
- New `--shots-dir` CLI flag (and `shots_dir_override=` kwarg) lets the
  user redirect per-step screenshots out of the config's parent. The
  default path now also probes for write access and falls back to the
  system temp dir when the parent is read-only — typical for configs
  living inside a OneDrive sync folder on Windows.
- The recorder's floating panel adds `pointer-events: auto`,
  `isolation: isolate` and `transform: translateZ(0)` to its CSS so
  iframes / parent transforms can't hide it on Edge in Windows.

## Recorder accuracy: multi-checkbox groups & dynamic IDs

The recorder used to mis-identify the *target* of a click whenever a form
had several radios/checkboxes sharing a single `name` attribute (e.g.
Facebook's trademark report form, where four `content_type[]` checkboxes
sit inside one fieldset). The first input under the click's container was
always picked, so all four "Content You Want to Report" actions ended up
pointing at the *same* checkbox at replay time.

The fix touches the recorder, the resolver, the fingerprint, and the GUI:

- `recorder_v2.OVERLAY_JS`
  - `_findAssociatedInput()` now picks the right hidden input even when
    several share a parent: it prefers the input whose own wrapping label's
    bounding box contains the click point, then falls back to the closest
    input by Euclidean distance to the click coordinates.
  - The proxy-click branch always carries the `value` attribute now (not
    just for radios), so checkboxes that share a `name` (e.g.
    `content_type[]`) are disambiguated at replay time.
  - The change handler also carries `_hidden_input_name` /
    `_hidden_input_type` / `radio_value` so the replay engine's
    JS-direct-set fallback can target the right sibling.
  - New compound selector `name_value` →
    `[name="..."][value="..."]` (weight 92) emitted for every
    radio/checkbox with both attributes — the strongest natural identity
    short of an `id`.
  - Click coordinates are tracked from `pointerdown` and `click`; a new
    `click_position` field (% within the target's bounding box) is shipped
    on every `click` / `submit` / `check` action as a future-proof
    tiebreaker.
  - `looksRandom()` now rejects Facebook React internals (`u_0_K3`,
    `u_0_12_D3`, `u_0_h_K8`, `u_0_2_G/`), long all-digit ids and
    decimal-suffixed numerics (`1112475925434379.0`), so the recorder no
    longer commits FB's volatile dynamic ids as `stable_id` selectors.
  - `labelTextFor()` now strips nested form controls before reading
    `innerText`, so each checkbox in a fieldset gets its own distinct
    accessible name (previously all four read out the same combined
    label).
  - The click handler short-circuits when its target is a *real* input
    (`<input type=checkbox|radio>`) — labels emit a synthetic click on
    the underlying input which used to make the proxy branch fire twice.

- `element_fingerprint.FINGERPRINT_JS`: the captured attributes set now
  includes `value` for radios/checkboxes only, so the resolver's
  attribute-match score correctly differentiates siblings.

- `resolver_v2._STRATEGY_WEIGHT`: `name_value` is registered at weight
  92, slotting it just below `data_testid` / `id` and above the bare
  `name` strategy.

- `auto_fill_gui`: the "submit after fill" checkbox auto-enables when
  the loaded config carries a captured `submit` block. The state is
  persisted to the JSON as `submit_after_fill` so re-loading restores
  the user's preference.

A regression test (`test_recorder_checkbox_group.py`) reproduces the
Facebook-style 4-checkbox group end-to-end and asserts that all four
options are checked after replay.

## Multi-proxy parallel runs (Proxy pool)

The pool runner can now drive **N parallel browser contexts, one proxy per
context**, straight from a flat proxy list. No more hand-crafting an
`accounts.json` just to test multiple proxies.

### Proxy file parser
- `proxy_utils.parse_proxy_string` now accepts the **flat colon format**
  used by most commercial proxy providers in addition to the existing URL
  forms:
  - `host:port:user:pass`  ← most common
  - `host:port`            ← anonymous
  - `user:pass@host:port`  ← creds-before-host
  - everything from before still works (`http://user:pass@host:port`,
    `socks5://...`, etc.)
  - passwords containing `:` are preserved (only the first 3 colons are
    treated as field separators).
- New `proxy_utils.load_proxy_dicts(path)` returns a list of
  Playwright-shaped proxy dicts in one call. Bad lines are skipped with
  a warning by default (configurable: `on_error="raise" | "silent"`).
- `proxies.example.txt` ships as a copy-paste template.

### Multi-proxy runner
- New `accounts.accounts_from_proxies(proxies, ...)` synthesises one
  `Account` per proxy so the existing `WorkerPool` can fan out N parallel
  pages with no `accounts.json` needed. Optional
  `user_data_dir_template` gives every worker its own persistent profile.
- New CLI flag `auto_fill.py --proxy-pool proxies.txt` runs the current
  config across every proxy in parallel. `--workers N` caps concurrency,
  `--proxy-pool-persistent` enables per-proxy profiles.
- New GUI section **Proxy pool** (right below Multi-account):
  - File picker + **Validate** button (parses without running, shows the
    first 8 proxies with passwords masked).
  - **parallel** workers entry, **separate profiles** toggle.
  - **▶ Run multi-proxy** button — streams per-proxy `task_start` /
    `task_done` events into the same log used by single-proxy runs.

See README → "Multi-proxy parallel runs" for full usage.

## Quality-of-life pass: dirty-state, shortcuts, filter, status icons, recent files, validate, value templates, test selectors

A coordinated polish round across the GUI and engine to make the tool feel
production-ready.

### GUI polish

- **Dirty-state tracking** — the title bar now shows `•` whenever the
  config has unsaved edits. New / Open / Recent / window-close all prompt
  before discarding unsaved work (Yes saves, No discards, Cancel aborts).
- **Keyboard shortcuts**:
  * `Ctrl+S` save · `Ctrl+Shift+S` save-as · `Ctrl+O` open
  * `Ctrl+N` new · `F5` run · `Esc` stop
  * `Ctrl+F` focus the field-list filter · `Ctrl+Q` quit
- **Field-list filter** — a Filter entry at the top of the Fields panel
  narrows the list as you type (matched against `field_id`, `field_type`,
  and the value). Selecting a filtered row still maps back to the right
  field even when items are hidden.
- **Per-field status icons** after a run — every field shows ✓ (filled),
  ✗ (skipped/failed), or … (running) directly in the Fields list. Status
  is parsed live from the engine log so you can scroll through 50+ fields
  and immediately see which ones failed.
- **Recent files menu** — a `Recent ▾` button next to Open opens a popup
  with the last 8 configs you touched (saved to
  `~/.auto_form_filler_recent`). Includes a *Clear recent files* item.
- **Validate Config button** — runs a dry-check on the loaded config:
  duplicate `field_id`, empty `targets`, unparsable `multi_textarea`
  JSON, blank values, and missing target URL. Reports issues in a single
  dialog or *"All good"* when clean.
- **Per-field "🔍 Test selectors" button** in the Field editor — opens a
  Chromium tab, navigates to the target URL, and tries each of the
  selected field's `targets` in order. Reports each strategy as
  `✓ matches Nx`, `✗ no match`, or `! ERROR`, so you can see exactly
  which selector breaks before kicking off a real run.

### Engine

- **Value templates** — embed `{{date}}`, `{{date:%d/%m/%Y}}`,
  `{{datetime}}`, `{{timestamp}}`, `{{uuid4}}` / `{{uuid}}`,
  `{{random_str:N}}`, `{{random_int:LO:HI}}`, `{{random_email}}`,
  `{{random_email:domain.com}}`, `{{env:VAR}}`, or `{{env:VAR:default}}`
  inside any `value` field. The engine expands each placeholder fresh on
  every replay (so batched DMCA / contact reports get unique IDs and
  timestamps automatically). Implemented in a zero-dependency
  `value_templates.py` module and wired into both the v1 (`auto_fill.py`
  → `fill_one_field`) and v2 (`replay_engine.py` → `_resolve_value`)
  engines.

### Files touched

- `auto_fill_gui.py` — most of the work; ~250 lines added for the new
  state, callbacks, keybindings, filter, status, validate, recent menu,
  and test-selector dialog.
- `value_templates.py` — new module (no external deps).
- `auto_fill.py` — calls `value_templates.expand` on every field's
  `value` before fill.
- `replay_engine.py` — calls `value_templates.expand` inside
  `_resolve_value` so v2 actions also get template expansion.

## Resizable GUI layout

The Tk GUI is now fully resize-friendly. Every major panel can be widened,
narrowed, or hidden by dragging the divider between it and its neighbours,
and the layout you settle on is restored next time you launch the app.

### What's new

- **Three nested `ttk.PanedWindow`s** replace fixed-height stacking:
  * Outer (vertical): fields/editor area  ⇕  Log
  * Inner top (horizontal): Fields list  ⇔  Field-editor + Targets
  * Inner right (vertical): Field editor  ⇕  Targets table
- **Multi-account row + Run/Stop bar packed *above* the resizable area** so
  they stay visible even when the editor or log is dragged to fill the
  whole window.
- **Sash positions + window geometry persisted** to
  `~/.auto_form_filler_layout` (JSON). Restored on next launch.
- **Sensible defaults** when no preference exists: ~70 % of vertical space
  to fields/editor, ~30 % to log; ~25 % to fields list, ~75 % to editor +
  targets; ~40 % to field editor, ~60 % to targets table.
- **`minsize(900, 600)` retained** so the layout never collapses past a
  usable point.

The whole flow still works at any window size — drag the bottom-right
corner of the window, then drag any sash to give the panel you're editing
more room. Close the window normally (X button) and your layout is saved.

## Smooth-fill upgrade for Facebook-style React forms

The replay engine now fills React/Relay-controlled inputs (Facebook DMCA /
Trademark, Help Center contact forms, etc.) reliably — to the millimeter.

### What's new

- **Self-healing fill** — `_do_fill` now reads the live value back after
  filling. If it didn't stick (common on React/Relay-controlled inputs),
  it automatically escalates: `fill` → real keystroke `type` → native
  `HTMLInputElement.value` setter + native `input` event. The first
  method that produces a matching live value wins, and the chosen method
  is logged as `[FILL_OK] healed via 'type'`.
- **Wait-for-actionable** — every action now waits for the target to be
  visible, enabled, and layout-stable (bbox drift < 4px) before
  interacting. Solves "click registered but did nothing" on lazy-rendered
  FB cards.
- **Smarter combobox** — instead of a fixed 200ms `sleep`, the engine
  actively polls for any `[role=listbox]`/`[role=menu]` to appear (up to
  3s), then tries five different option-pick strategies (exact role+name,
  partial name, listbox text=, option text=, page text=).
- **Human pacing between actions** — `action_delay_ms` (default 120) +
  `action_jitter_ms` (default 80, random 0..jitter). Both configurable
  per-task via the JSON config. Reduces anti-bot heuristic flags.
- **Real-keystroke typing with delay** — when typing, presses use a
  per-key delay (default 25ms) and clear with `Ctrl+A` + `Delete`
  before typing — matches real-user keystroke patterns.
- **`blur` event always fired** — Facebook validates many fields on blur;
  the engine now dispatches a final `blur` after any fill.
- **Browser stealth** — Chromium launches with
  `--disable-blink-features=AutomationControlled`, the `navigator.webdriver`
  flag is stripped via `add_init_script`, and a real Chrome user-agent
  is set by default. Override via `config["user_agent"]` /
  `config["viewport"]` / `config["locale"]` / `config["timezone_id"]`,
  or per-account `user_agent` / `viewport`.

### Bug fixes

- `resolver_v2` and `worker_pool` were using `async with asyncio.timeout(...)`
  which is Python 3.11+ only. On Python 3.10 (used by tkinter on most
  Linuxes), this raised `AttributeError`, the broad except caught it, and
  EVERY locator returned 0 candidates → all fields skipped. Replaced with
  `asyncio.wait_for(...)` (cross-version).
- `recorder_v2` was capturing clicks on its own overlay panel
  (`#__af2_panel` — Done / Undo / Cancel buttons) as user actions.
  Recorder now ignores any element whose id starts with `__af2_` or
  is inside `#__af2_panel`.

### New config keys (all optional)

```json
{
    "action_delay_ms":  120,
    "action_jitter_ms": 80,
    "user_agent":       "Mozilla/5.0 (X11; Linux x86_64) ... Chrome/124.0.0.0 ...",
    "viewport":         {"width": 1366, "height": 820},
    "locale":           "en-US",
    "timezone_id":      "Asia/Ho_Chi_Minh"
}
```

### New per-account fields

```json
{
    "name":       "alpha",
    "user_agent": "Mozilla/5.0 (Macintosh; ...) ... Safari/605.1.15",
    "viewport":   {"width": 1440, "height": 900}
}
```

## Dark theme

`auto_fill_gui.py` now ships with both **dark** (default) and **light**
themes. The toggle button in the top-right of the window switches
between them; the chosen theme is persisted to
`~/.auto_form_filler_theme` and reloaded on next launch. All ttk
widgets (Entry, Treeview, Listbox, Notebook, Button, Combobox, etc.)
plus the raw tk Text/Listbox log areas are recoloured. Verified with
xvfb pixel-sampling: dark `bg=#181a20`, input `#0f1116`; light
`bg=#f5f6f8`, input `#ffffff`.

## v2 rewrite — accurate record + replay, multi-account concurrency

This release is a substantial rewrite. The core problem it addresses
is **the recorder/resolver clicking the wrong element or filling the
wrong field**. Root causes are documented in `ANALYSIS.md`.

### What's new

- **`recorder_v2.py`** — new recorder that captures, for every action:
  - The full **iframe URL chain** (so nested forms — Facebook
    DMCA / Trademark, embedded widgets — replay correctly).
  - An **element fingerprint** (tag, role, accessible name, key
    attributes, surrounding text, viewport bbox) used at replay time
    to verify the resolver picked the right element.
  - The actual **input method** (`fill` / `type` / `paste` / `select` /
    `check` / `set_files` / `combobox` / `contenteditable`).
  - 6–10 redundant **selectors with weights** (`data-testid`=100,
    `id`=95, `name`=90, `role[name]`=85, `aria-label`=80, …, `text`=20)
    so the resolver has multiple fallbacks ordered by reliability.
  - Custom widgets (`div[role=combobox]`, `[role=radio]`,
    `[role=checkbox]`, `[contenteditable]`) as first-class actions.
  - Submit button captured with the same fingerprint+selectors as
    fields, so two "Send" buttons on the same page no longer collide.

- **`element_fingerprint.py`** — fingerprint capture (browser-side JS)
  + Python scoring with weighted fields
  (`accessible_name`=3.0 > `tag`=2.0 > `attributes`=2.0 > `role`=1.5 >
  `neighbour_text`=1.5 > `viewport_hint`=0.5 > `type`=1.0). Default
  match threshold = 0.55 — below that the resolver moves on to the
  next strategy instead of acting on the wrong element.

- **`resolver_v2.py`** — frame-aware locator resolution. Walks the
  recorded iframe chain, evaluates **every** candidate locator (not
  just `.first`), scores each one against the recorded fingerprint,
  and returns the best-scoring `ResolveResult` across all strategies.

- **`replay_engine.py`** — faithful action replay. Each action kind is
  dispatched to a dedicated handler (`_do_fill`, `_do_click`,
  `_do_check`, `_do_select`, `_do_set_files`, `_do_combobox`,
  `_do_contenteditable`). `value_template` substitution happens here.

- **`worker_pool.py`** + **`accounts.py`** — multi-account concurrent
  runner. One persistent `BrowserContext` per account (separate
  cookies, separate proxy, separate `user_data_dir`). All workers pull
  from a single `asyncio.Queue` of tasks; per-task `vars` are merged
  on top of per-account `vars`; each task can be retried up to N
  times on failure. Per-event reporting callback lets the GUI render
  a live dashboard.

- **`auto_fill.py`** — auto-detects v1 vs v2 config and routes to the
  right code path. New CLI flags: `--accounts`, `--tasks`, `--workers`.

- **`auto_fill_gui.py`** — the GUI now defaults to recorder v2 (toggle
  via the *"Recorder v2 (frame-aware)"* checkbox). New
  **Multi-account** row lets you pick `accounts.json` and run the
  whole config concurrently across all accounts.

### Backward compatibility

- v1 configs (`{"fields": [...]}`) still work. The engine logs
  `[CONFIG] format=v1 (legacy)` when it falls back to the v1 code
  path.
- The v1 `recorder.py` and old `target_resolver.py` remain in the
  source tree and on disk, so any custom integrations keep running.

### Files

| New                       | Purpose                                                  |
|---------------------------|----------------------------------------------------------|
| `ANALYSIS.md`             | Root-cause analysis of the click-wrong-target bugs.      |
| `recorder_v2.py`          | Frame-aware recorder with fingerprints + action kinds.   |
| `resolver_v2.py`          | Multi-strategy + fingerprint-verifying resolver.         |
| `replay_engine.py`        | Action replay engine with input-method fidelity.         |
| `element_fingerprint.py`  | Fingerprint capture + scoring.                           |
| `worker_pool.py`          | Multi-account concurrent runner.                         |
| `accounts.py`             | `accounts.json` schema + loader.                         |
| `accounts.example.json`   | Sample multi-account config.                             |

### Verified end-to-end

`test_v2_e2e.py` records a session against `test_form.html`,
replays it on a fresh page, and asserts every field's DOM state
matches what was recorded — including the radio-button case where
two `[name=plan]` radios share a selector but the fingerprint
correctly distinguishes "Pro" from "Free". The same config is then
driven through `WorkerPool` with two synthetic accounts (`alpha`
and `beta`) using `value_template` substitution; both workers run
concurrently and each fills its own values.

```
[capture] 6 action(s); submit=yes
[replay] filled=6  skipped=0
[replay] DOM state: {'email':'phu@example.com', 'full_name':'Nguyen Phu',
                     'message':'hello v2', 'country':'vn',
                     'agree':True, 'plan':'pro'}
[pool] 2/2 task(s) OK
       alpha ok=True filled=6 skipped=0 attempts=1
        beta ok=True filled=6 skipped=0 attempts=1
```

`_test_cli_pool.py` does the same via `python auto_fill.py
--config ... --accounts ... --workers 3` (3 accounts in parallel,
each ~0.35 s, all 3/3 OK).

## Proxy support

Every browser launch in the project now goes through `proxy_utils.resolve_proxy()`,
so a single setting controls the engine, the recorder, the picker and the OTP
orchestrator at once.

### What's new

- **New file** — `proxy_utils.py`: URL parsing, list loading, round-robin /
  random rotation, env-var fallback, and a `mask_proxy()` helper that prints
  proxies safely in logs (no passwords).
- **`auto_fill.py`** — `--proxy`, `--proxy-list`, `--proxy-rotate`,
  `--proxy-bypass`, `--no-proxy` CLI flags. Reads `proxy` / `proxy_list` /
  `proxy_rotate` from the config JSON. Logs the proxy in use at run start.
- **`run_with_email_otp.py`** — same CLI flags as `auto_fill.py`. The same
  proxy is used for both phase 1 (form fill) and phase 2 (OTP confirmation).
- **`recorder.py`** — `record_session(_sync)` and `record_to_config(_sync)`
  now take an optional `proxy=` kwarg (string, dict, or `None`). The CLI
  also gets the standard proxy flags.
- **`auto_fill_gui.py`** — new **Proxy** panel at the top with `server` /
  `user` / `pass` / `bypass` / `list file` / `rotate` controls plus a
  **Test** button that round-trips through `https://api.ipify.org` and
  prints the egress IP into the log. The proxy is persisted in the config
  JSON when you Save.
- **README.md** — full **Proxy** section with CLI / config / env-var
  examples, list-file format, and the SOCKS-auth caveat.

### Configuration

Three ways to configure a proxy, checked in priority order:

1. CLI: `--proxy http://user:pass@host:8080` or `--proxy-list proxies.txt`.
2. Config: `"proxy": "http://user:pass@host:8080"` (or a dict), and/or
   `"proxy_list": "proxies.txt"` + `"proxy_rotate": "round_robin"`.
3. Env: `HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY`.

`--no-proxy` overrides all of them.

### Test

`test_smoke.py` now also covers proxy parsing / rotation logic — purely
in-process, no network — so CI catches regressions in the parser.

---

# `recorder.py` — what changed

## TL;DR

`recorder.py` is now self-contained, streaming, and writes a ready-to-use
`config.json` automatically. Drop the new file into your project, no other
files need to change. The GUI still calls `record_session_sync(url, wait)` and
gets back the same `list[dict]` shape it always did.

## Breaking changes

**None.** Every existing call site keeps working:

```python
from recorder import record_session_sync
fields = record_session_sync(url, wait_for_selector)   # same as before
```

## New behaviour

| What | Before | After |
|------|--------|-------|
| Overlay JS | Loaded from `_browser_overlay.js` (crashed if missing) | Embedded in `recorder.py` (no external file needed) |
| Crash recovery | One final dump on Done — close the tab early and you lose everything | Streamed event-by-event to Python, so you only ever lose the *current* event |
| Output | `list[dict]` only | Optional auto-write of `config.json` + `config.events.json` |
| Submit button | Not captured (`auto_fill.py` had to guess) | Captured automatically as `submit_selectors` |
| Multi-step forms | Lost the overlay after navigation | Overlay re-injects on every page load |
| Iframes | Ignored | Same-origin iframes get the listeners too |
| Selectors per field | Whatever `picker.snapshot_to_field` produced (often just 1–2) | Up to 7 per field: id, name, data-testid, aria-label, aria-placeholder, label_text, placeholder, role, css-path |
| `field_id` | Sometimes empty / collided | Always non-empty, deduplicated with `_2`, `_3`, … suffixes |
| Live UI | Just a Done button | Done / Cancel / **Undo last** + live counter + last-captured preview |
| CLI | None — only callable from the GUI | `python recorder.py --url <url> --out my_form.json` |

## New public API

```python
# Old (kept):
record_session(url, wait_for_selector)                           -> list[fields]
record_session_sync(url, wait_for_selector)                      -> list[fields]

# New (additive):
record_to_config(url, wait_for_selector, out_path, headless,
                 save_events)                                     -> (config, events)
record_to_config_sync(...)                                        -> (config, events)
build_config(session, target_url, wait_for_selector, fields_override) -> dict
```

`record_session(*, out_path=…)` now also accepts the `out_path` kwarg, so the
GUI can ask the recorder to also save the config straight to disk if you want
that path in the future.

## How to upgrade

1. Replace the old `recorder.py` with the new one (drop-in).
2. (Optional) Delete `_browser_overlay.js` — no longer used by `recorder.py`.
   `picker.py` may still need it; check before deleting.
3. (Optional) In the GUI, when you call the recorder, pass an `out_path` so the
   user gets a saved config without having to remember to click Save:

   ```python
   # in auto_fill_gui.py: cmd_record_session worker
   import recorder
   from datetime import datetime
   out = f"recorded_{datetime.now():%Y%m%d_%H%M%S}.json"
   fields = recorder.record_session_sync(url, wait, out_path=out)
   self.log_queue.put_nowait(f"[REC ] saved → {out}")
   ```

## Test

A self-contained smoke test ships with this bundle:

```
python test_smoke.py
```

It launches a headless browser against `test_form.html`, drives every input
type (text / email / textarea / select / checkbox / radio / submit), and
asserts the recorder produced the right structure. Output: 6 fields, 5+
selector strategies on most of them, 1 submit selector, all dedup'd.
