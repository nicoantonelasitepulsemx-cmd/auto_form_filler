# auto_form_filler

A robust, async-Playwright form-filling tool that uses **multiple targeting
strategies** simultaneously to identify and fill any web form field — even
when fields lack clear `id`s or `name`s.

For each field the resolver tries every target you list, in order, and stops
at the first one that matches. If they all fail it logs a warning and skips
the field instead of crashing.

## Files

| File                   | Purpose                                                                    |
|------------------------|----------------------------------------------------------------------------|
| `auto_fill.py`         | CLI + Playwright engine: opens the page, fills, submits.                   |
| `auto_fill_gui.py`     | Tkinter GUI on top of the engine — edit configs visually.                 |
| `target_resolver.py`   | Implements every targeting strategy.                                       |
| `picker.py`            | "Pick from page" — open URL, click element, auto-build target list.       |
| `recorder.py`          | "Record session" — fill the form once, get back a complete config.        |
| `captcha.py`           | CAPTCHA detection + floating "Continue" banner that pauses for a human.    |
| `_browser_overlay.js`  | Shared in-page JS used by picker and recorder.                             |
| `logger.py`            | Stdout logger with `[SUCCESS]` / `[MISS]` / `[SKIP]` lines.                |
| `config.json`          | Sample config (Facebook copyright report).                                 |
| `proxy_utils.py`       | Proxy parsing / rotation helpers shared by the engine, recorder, picker.  |
| `recorder_v2.py`       | **v2 recorder** — frame-aware, fingerprint-capturing, action-based.       |
| `resolver_v2.py`       | **v2 resolver** — multi-strategy + fingerprint verification + iframes.    |
| `replay_engine.py`     | Action replay (fill / click / check / select / combobox / contenteditable). |
| `element_fingerprint.py` | Element fingerprint capture + score (replay-time verification).         |
| `worker_pool.py`       | Multi-account concurrent runner (one BrowserContext per account).         |
| `accounts.py`          | `accounts.json` schema + loader.                                          |
| `accounts.example.json`| Sample multi-account config.                                              |
| `build.py`             | PyInstaller build script — produces a single executable.                  |
| `requirements.txt`     | Python dependencies.                                                       |

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

On Linux you also need Tk for the GUI: `sudo apt install python3-tk`
(macOS / Windows ship Tk with Python).

## GUI usage

```bash
python auto_fill_gui.py                  # blank config
python auto_fill_gui.py config.json      # open with a config preloaded
```

The window has four panels:

1. **Top bar** — config file path + `New / Open / Save / Save As`,
   target URL, `wait_for_selector`, and the run flags
   (`headless`, `dry-run`, `debug`, `submit after fill`, screenshot path).
2. **Fields list (left)** — every field in the config; `+ Add` / `− Remove`
   / `↑ ↓` to reorder.
3. **Field editor (right)** — `field_id`, `field_type`, `value`, plus a
   targets table you can append to / reorder. Pick a strategy from the
   dropdown, type the selector, click `+ Add`. Click `↻ Update` to overwrite
   the currently-selected target row.
4. **Run panel + log** — `▶ Run` starts the filler in a background thread
   and streams the engine's logs straight into the window;
   `■ Stop` cancels mid-run.

Tips:
- For `multi_textarea` fields, the `value` box accepts a JSON list, e.g.
  `["url1", "url2", "url3"]`.
- For `checkbox` fields, set `value` to `true` or `false` — the GUI parses
  it automatically.
- The Save button writes the config back to disk in the canonical schema
  used by the CLI, so you can hand-edit it later or check it into git.

### Pick from page (auto-build a field)

Click **🎯 Pick from page** in the toolbar. A new browser window opens at
your `target_url`. Hover over any input/textarea/select — it will be
outlined in red — and click it. The browser closes automatically and a
new field is appended to your config with up to 5 fallback targets
auto-generated from the element (id → aria_label → label_text →
placeholder → type). Click Save to persist.

### Record session (build the whole config in one go)

Click **⏺ Record session**. The browser opens at your `target_url` with
a floating "✓ Done recording" button in the top-right corner. Fill the
form normally — every input you touch (text, textarea, select, checkbox,
radio) is captured along with its value. When you're finished, click
**Done recording**. The browser closes and every field you interacted
with is appended to the GUI as a complete config entry (including the
value you typed). Save to disk and you're done.

### One-time codes via kuku.lu disposable mail

The recorder can mint a disposable address per session and paste OTP
codes for you. m.kuku.lu is a free Japanese throw-away mail service —
no signup, you keep an "account" by saving two cookies.

1. **Provision an account** (one-time, per proxy/identity):
   ```
   python kuku_lu_cli.py mint --out kuku_acct1.json
   ```
   Save `kuku_acct1.json` somewhere safe — it contains the
   `csrf_token` + `sessionhash` that identify this kuku.lu account
   plus the disposable address that was minted.

2. **Record with OTP support** by passing `--kuku-creds`:
   ```
   python recorder_v2.py --url https://m.facebook.com/help/contact \
       --out trademark_form.json \
       --kuku-creds kuku_acct1.json \
       --kuku-from facebook
   ```
   The recorder panel gains a blue "✎ Get OTP → paste" button. While
   recording: when you reach the confirmation-code field, click the
   field once, then click the panel button. The recorder polls
   kuku.lu, types the matching code into the input, and ships an
   `kind="otp_paste"` action — so replay can re-fetch a fresh code
   per account on every run.

3. **Per-proxy mailboxes (multi-account replay).** Add a `kuku` block
   next to each account in `accounts.json` so each proxy drains its
   own inbox during parallel replay:
   ```json
   {
     "name": "acct1",
     "proxy": "http://user:pass@1.2.3.4:8080",
     "kuku": { "csrf_token": "...", "sessionhash": "...", "current_address": "abc@kpay.be" }
   }
   ```
   The replay engine (`replay_engine.run_action`) automatically
   resolves OTP actions through the account's kuku creds — pass
   `ctx={"_kuku_creds": account.kuku, ...}` from the orchestrator.

### Mail-per-proxy Manager (GUI)

Instead of editing `accounts.json` by hand, click **📧 Mail-per-proxy**
in the main toolbar to open a dedicated dialog that:

- Loads / saves `accounts.json` with the same schema described above.
- Lists every account with a masked proxy preview, the bound kuku
  mailbox, and a status badge.
- Buttons per account:
  - **🆕 Mint mailbox** — provisions a fresh kuku.lu identity through
    the account's proxy (so the egress IP matches what replay will
    use).
  - **🔄 New address** — rotates the disposable alias when the
    previous one is spammed.
  - **📥 Test inbox** — peeks at the latest 3 messages for debugging.
  - **⏳ Wait OTP** — polls until a code arrives, then copies it to
    the clipboard. Same regex / from-filter / timeout logic as the
    replay engine.
- **Domain** — combobox replicating the m.kuku.lu "Add email
  address" dropdown (`@boxfi.uk`, `@haren.uk`, `@bangban.uk`,
  `@catgroup.uk`, `@goatmail.uk`, `@sendnow.win`, `@ccmail.uk`,
  `@exdonuts.com`, `@tensi.org`, `@kpay.be`, `@neko2.net`). Leave
  blank to let kuku.lu pick a random domain. Editable: type any new
  domain that kuku.lu adds in the future. The selected domain is
  applied to both **Mint mailbox** and **New address**, and saved as
  part of the OTP defaults.
- **From proxy file…** — pick a `host:port:user:pass`-per-line file
  and one account row is created per proxy, ready for bulk-mint.
- **OTP defaults** — saved in `~/.auto_form_filler_otp_defaults` so
  every recorder/replay run picks up your preferred regex and
  from-filter without editing JSON.

The dialog reuses the parent GUI's theme (dark / light) and runs all
kuku.lu calls in a background thread so the main window stays
responsive.

> **Cloudflare note:** kuku.lu sometimes ships a Cloudflare challenge
> on datacenter IPs. The recorder uses the same browser context as
> the form recording itself, so it inherits any clearance cookie you
> already solved interactively. For headless replay, run from a
> residential IP or use `kuku_lu_cli.py --via-browser`.

## CAPTCHA detection + pause-for-human

When the engine loads the form (and again right before/after submit), it
scans the page for known CAPTCHA fingerprints:

| Kind                     | Matched by                                                         |
|--------------------------|--------------------------------------------------------------------|
| `recaptcha-v2`           | `iframe[src*='recaptcha']`, `.g-recaptcha`, `#g-recaptcha-response` |
| `hcaptcha`               | `iframe[src*='hcaptcha.com']`, `.h-captcha`, `#h-captcha-response`  |
| `cloudflare-turnstile`   | `iframe[src*='challenges.cloudflare.com']`, `.cf-turnstile`         |
| `funcaptcha` / Arkose    | `iframe[src*='funcaptcha.com']`, `iframe[src*='arkoselabs.com']`    |
| `image-captcha`          | `img[src*='captcha' i]`, `input[name*='captcha' i]`                 |
| `text-challenge`         | Visible text "I'm not a robot" / "verify you are human" / "Vui lòng xác minh" |

When a CAPTCHA is detected, the engine injects a fixed red banner at the
top of the page with a **"✓ Continue"** button. The run **pauses** until
you solve the CAPTCHA in the same browser window and click Continue. The
default timeout is 300s (configurable via `captcha_timeout` in the config).

Behaviour:
- **Visible browser** (default): pauses and waits.
- **Headless**: cannot pause (no human present) — logs an error and continues.
  Re-run without `--headless` if your form has a CAPTCHA.
- **Opt out**: pass `--no-captcha-pause` on the CLI, set
  `pause_on_captcha: false` in the config, or uncheck **pause on CAPTCHA**
  in the GUI.

This tool **does not solve CAPTCHAs automatically** — by design. CAPTCHAs
exist to require human interaction; bypassing them is against most sites'
terms of service. The pause-for-human flow is a pragmatic middle ground:
you stay in the loop for the part that requires a human, the tool handles
everything else.

## Standalone executable (PyInstaller)

To distribute the tool to people who don't have Python installed:

```bash
pip install pyinstaller
python build.py            # produces dist/auto_form_filler[.exe]
python build.py --no-cli   # Windows: hide the console window (GUI only)
```

PyInstaller is **platform-specific** — to get a Windows `.exe` you must
run `build.py` on a Windows machine; running it on Linux gives a Linux
ELF binary, on macOS a macOS Mach-O.

The build does **not** bundle the Chromium browser (~150 MB). After
distributing the binary, the end-user runs once:

```bash
python -m playwright install chromium
```

(or, on a machine with no Python, install the standalone `playwright`
CLI from the official site).

## CLI usage

```bash
# Try the resolver, but don't actually type anything (safe).
python auto_fill.py --config config.json --dry-run --debug

# Run for real, headless.
python auto_fill.py --config config.json --headless

# Run for real and click submit when done.
python auto_fill.py --config config.json --submit

# Use a named profile (loads ./profiles/<name>.json instead of --config).
python auto_fill.py --profile facebook_dmca --debug

# Save a screenshot after the run.
python auto_fill.py --config config.json --screenshot out.png
```

CLI flags:

| Flag             | Meaning                                                      |
|------------------|--------------------------------------------------------------|
| `--config PATH`  | Path to config JSON (default `config.json`).                 |
| `--profile NAME` | Loads `./profiles/<name>.json`, overrides `--config`.        |
| `--headless`     | Run without a visible browser window.                        |
| `--dry-run`      | Resolve every field, log the strategy used — but don't type. |
| `--submit`       | After all fields are filled, click the submit button.        |
| `--debug`        | Verbose logs + visually highlight every matched field.       |
| `--screenshot P` | Save a full-page PNG to `P` after the run.                   |
| `--proxy URL`    | Route the browser through a proxy. See **Proxy** below.       |
| `--proxy-list F` | Path to a newline-separated file of proxy URLs (rotation).    |
| `--proxy-rotate M` | Rotation mode for `--proxy-list`: `round_robin` (default) / `random` / `none`. |
| `--proxy-bypass H` | Comma-separated hosts to bypass, e.g. `*.local, 127.0.0.1`. |
| `--no-proxy`     | Disable proxies entirely, even if config / env defines one.   |
| `--accounts F`   | Run concurrently across multiple accounts. See **Multi-account**. |
| `--tasks F`      | Per-task config + vars. See **Multi-account**.                |
| `--workers N`    | Max concurrent workers (default = number of accounts).        |

## Dark / light theme

The GUI defaults to a dark theme. Toggle via the **☀ Light / ☽ Dark**
button at the top-right of the window. The choice persists at
`~/.auto_form_filler_theme`.

## v4 — radio targeting + extension APIs

v4 fixes a bug where picking "I am the rights owner" on
Facebook's trademark form was being replayed as "I am reporting on
behalf of someone else". The recorder, replay engine, and resolver
all received hardening:

- **Recorder**: role=radio clicks ship `checked=true` (radios are
  never deselected by clicking) instead of the pre-React
  `aria-checked` value. Sibling-deselect ghosts on `aria-checked`
  *and* `<input type=radio>` `change` events are dropped silently.
- **Resolver**: `accessible_name` weight bumped 3.0 → 5.0 with a
  soft mismatch penalty; exact `value` attribute match earns
  dedicated weight so two siblings sharing tag/role/neighbour text
  but different `value`s can no longer tie.
- **Replay**: refuses to call `uncheck()` on a radio; verifies the
  live element's accessible name *before* clicking and re-resolves
  via `page.get_by_role("radio", name=...)` if the resolver picked
  the wrong sibling; verifies post-click outcome and falls through
  the escalation ladder on a sibling-mismatch.

A new module **`ai_features.py`** ships three opt-in hooks:

| API | Purpose |
|-----|---------|
| `ai_heal(...)`            | LLM-backed selector self-heal. Replay calls it when the resolver returns no candidate. Pure no-op without `OPENAI_API_KEY`; configurable model via `AUTOFORM_AI_HEAL_MODEL`. |
| `vision_match(rec, cands)`| Perceptual-hash tiebreaker (pure-Python dHash, no Pillow) for selecting between visually similar candidates. |
| `codegen_export(config)`  | Render a captured recording into a runnable standalone Playwright Python script (no `auto_form_filler` dependency). |
| `vision_hash(png)`        | Stand-alone perceptual hash helper. |
| `stable_hash(payload)`    | Deterministic SHA-256 over any JSON-serialisable payload. |

```python
from ai_features import codegen_export
print(codegen_export(json.load(open("trademark1.json"))))
```

See `CHANGELOG.md` for the full v4 release notes.

## v2 — accurate record + replay

The default recorder is now `recorder_v2.py`, which fixes the
*"clicked the wrong element / filled the wrong field"* class of bugs.
`ANALYSIS.md` lists every root cause and how it's addressed; the short
version:

- Every recorded action carries a **frame chain** (so iframe-based
  forms — Facebook DMCA, Trademark, embedded widgets — replay correctly).
- Every recorded action carries an **element fingerprint**: tag, role,
  accessible name, key attributes, surrounding text, and a
  viewport-relative bbox. At replay time the resolver computes the live
  fingerprint of every candidate locator and only acts on a candidate
  whose fingerprint score clears a configurable threshold (default 0.55).
- Each action records the **input method** that was actually used
  (`fill` / `type` / `paste` / `select` / `check` / `set_files` /
  `combobox` / `contenteditable`) so replay reproduces it instead of
  collapsing everything to `Locator.fill()`.
- Custom widgets (`div[role=combobox]`, `[role=radio]`, `[role=checkbox]`,
  `[contenteditable]`) are first-class.
- Submit buttons are recorded with the same fingerprint+selectors as
  fields, so two "Send" buttons on the same page no longer collide.

The v1 schema (`{"fields": [...]}`) is still accepted for backward
compatibility — if a config doesn't have `"version": 2` the engine falls
back to the original code path.

### Recording with v2

```bash
python recorder_v2.py --url https://example.com/contact --out recorded.json
```

Or in the GUI: the **Recorder v2** checkbox at the bottom of the window
is on by default. Click **⏺ Record session**, fill the form like a real
user, click **Done** in the recorder overlay.

## Multi-account concurrency

Run the same config concurrently across N accounts, each with its own
BrowserContext, cookies, proxy and per-account variable substitution.

### accounts.json

See `accounts.example.json`. Minimum schema:

```json
[
  {
    "name": "alpha",
    "user_data_dir": "./profiles/alpha",
    "proxy": "http://user:pass@1.2.3.4:8080",
    "vars": { "email": "alpha@example.com", "full_name": "Alpha Account" }
  },
  {
    "name": "beta",
    "user_data_dir": "./profiles/beta",
    "proxy": "socks5://5.6.7.8:1080",
    "vars": { "email": "beta@example.com",  "full_name": "Beta Account" }
  }
]
```

`vars` is the substitution dict. Any action with
`"value_template": "{email}"` is filled with the current account's
`email`.

### CLI

```bash
# One task per account, all using the same config:
python auto_fill.py --config config.json --accounts accounts.json --workers 5

# Different tasks per account (tasks.json carries per-task config / vars):
python auto_fill.py --accounts accounts.json --tasks tasks.json --workers 5
```

`--workers` defaults to the number of accounts. Each worker owns one
persistent `BrowserContext`. Cookies and login state are persisted to
the account's `user_data_dir` between runs.

### GUI

The **Multi-account** row (between the field list and the run controls)
lets you pick `accounts.json` and click **▶ Run pool**. Per-account
events stream into the log: `task_start`, `task_done`, `worker_done`.

## Proxy

The engine, recorder and picker all support proxies through Playwright's
built-in `proxy=` option. You can configure it three ways — they're
checked in priority order, top wins:

1. **CLI flag**
   ```bash
   python auto_fill.py --config config.json --proxy http://user:pass@1.2.3.4:8080
   python auto_fill.py --config config.json --proxy socks5://1.2.3.4:1080
   python auto_fill.py --config config.json --proxy-list proxies.txt --proxy-rotate random
   ```

2. **Config file** (`config.json`):
   ```jsonc
   {
     "proxy": "http://user:pass@1.2.3.4:8080",
     // …or as an object if you prefer separate fields:
     // "proxy": {
     //   "server":   "http://1.2.3.4:8080",
     //   "username": "user",
     //   "password": "pass",
     //   "bypass":   "*.local, 127.0.0.1"
     // },
     "proxy_list":   "proxies.txt",     // optional — overridden by `proxy`
     "proxy_rotate": "round_robin"      // round_robin | random | none
   }
   ```

3. **Environment variables** (`HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY`).
   Used as a last resort when no CLI flag and no config-level proxy is set.

`--no-proxy` overrides every source above and forces a direct connection.

### GUI

The GUI has a dedicated **Proxy** panel at the top with `server`, `user`,
`pass`, `bypass`, `list file` and `rotate` controls plus a **Test**
button. The Test button parses the proxy, fetches `https://api.ipify.org`
through it, and prints the egress IP into the log so you can verify the
proxy actually works before clicking Run.

### Proxy list format

`proxies.txt` is a plain newline-separated list. Blank lines and `#`
comments are skipped. Each line can use **any** of these shapes (mix
freely in the same file):

```
# host:port:user:pass — most common in commercial proxy lists
1.2.3.4:8080:alice:apass

# host:port — anonymous proxy (no auth)
9.10.11.12:3128

# user:pass@host:port
bob:bpass@5.6.7.8:9090

# explicit scheme + URL form
http://user:pass@1.2.3.4:8080
socks5://9.10.11.12:1080
```

A ready-to-edit template is provided as `proxies.example.txt`.

## Multi-proxy parallel runs

If you have a pool of proxies and want to run the **same** config across
all of them at the same time (one BrowserContext per proxy), use the
**Proxy pool** feature.

### CLI

```bash
# Run config.json with one parallel page per proxy
python auto_fill.py --config config.json --proxy-pool proxies.txt

# Cap concurrency to 5 even if proxies.txt has more lines
python auto_fill.py --config config.json --proxy-pool proxies.txt --workers 5

# Headless + dry-run is great for smoke-testing the pool itself
python auto_fill.py --config config.json --proxy-pool proxies.txt --headless --dry-run

# Each worker gets its own persistent Chromium profile
python auto_fill.py --config config.json --proxy-pool proxies.txt --proxy-pool-persistent
```

The pool prints `task_start` / `task_done` events for every proxy; final
line is `[PROXY-POOL] done — N/M task(s) succeeded`.

### GUI

The GUI has a dedicated **Proxy pool** row (just below **Multi-account**):

* **proxies file** — pick a `.txt` / `.list` file in the format above.
* **Validate** — parse the file and show how many proxies are valid +
  the first few (passwords masked) without running anything.
* **parallel** — max concurrent browser contexts (defaults to the number
  of proxies in the file).
* **separate profiles** — when checked, each worker uses its own
  persistent profile under `~/.auto_form_filler_profiles/proxy_<i>` so
  cookies / storage stay isolated across runs. Leave unchecked for
  ephemeral one-shot runs.
* **▶ Run multi-proxy** — fan the current config out across all proxies.
  Per-proxy events stream into the log.

### SOCKS authentication caveat

Playwright's bundled Chromium does **not** support username/password
auth on SOCKS proxies (only on HTTP/HTTPS). HTTP/HTTPS proxies with
auth, and SOCKS proxies without auth, both work fine.

## Targeting strategies (priority chain)

`auto_fill` walks the `targets` list in order. The first strategy that
returns a non-empty Locator wins.

| #  | Strategy           | `selector` is …                                                                          |
|----|--------------------|------------------------------------------------------------------------------------------|
| 1  | `id` / `name` / `css` | A raw CSS selector, e.g. `input[name='email']`                                        |
| 2  | `aria_label`       | Raw CSS, e.g. `input[aria-label='Email']`                                                |
| 2  | `aria_placeholder` | Raw CSS, e.g. `input[aria-placeholder='Enter email']`                                    |
| 2  | `aria_labelledby`  | Either the *id* of the labeller element, or the *text* the labeller contains.            |
| 3  | `label_text`       | The literal text of a `<label>` (or `<legend>`); resolves to the associated control.    |
| 4  | `placeholder`      | Raw CSS, e.g. `input[placeholder*='email' i]`                                            |
| 5  | `nearby_text`      | Free-text near the input — resolver finds the nearest input following that text.        |
| 6  | `type` / `nth`     | Raw CSS like `input[type='email']` or `textarea:nth-of-type(2)`                          |
| 7  | `text`             | Free-text on the page (`page.get_by_text`).                                              |
| 7  | `role`             | `role` or `role:Name`, e.g. `button:Submit` (uses `page.get_by_role`).                  |
| 7  | `data_testid`      | Raw CSS like `[data-testid='email-input']`                                              |
| 7  | `value`            | Raw CSS like `input[value='photo_video']` (good for radios/checkboxes).                 |

Anything else is treated as a raw CSS selector.

## Field types

`field_type` (optional — auto-detected if omitted):

| `field_type`     | Strategy                                                                |
|------------------|-------------------------------------------------------------------------|
| `text` / `email` / `url` | `loc.fill(value)`                                               |
| `textarea`       | `loc.fill(value)` (newlines preserved).                                 |
| `select` / `dropdown` | `loc.select_option(label=value)` then falls back to `value=value`. |
| `radio`          | `loc.check()` (target a *specific* radio via `value`, `label_text`, …). |
| `checkbox`       | `loc.check()` if `value` is truthy, else `loc.uncheck()`.               |
| `file`           | `loc.set_input_files(value)` (string or list).                          |
| `multi_textarea` | `value` must be a list — fills the i-th value into the i-th textarea matched by the first list-capable target. |

## Config format

```jsonc
{
  "target_url": "https://example.com/form",
  "wait_for_selector": "#form-container",  // wait until form mounted
  "headless": false,
  "dry_run": false,
  "submit_selectors": ["button[type='submit']"],  // optional override
  "fields": [
    {
      "field_id": "email",
      "value": "you@example.com",
      "field_type": "email",                // optional, auto-detected otherwise
      "targets": [
        {"strategy": "type",        "selector": "input[type='email']"},
        {"strategy": "label_text",  "selector": "Email address"},
        {"strategy": "placeholder", "selector": "input[placeholder*='email' i]"}
      ]
    }
  ]
}
```

## Adding new targets

Open `target_resolver.py` and either:

1. Add a new entry to `_RAW_CSS_STRATEGIES` if your strategy maps directly to
   a CSS selector — no code needed beyond that.
2. Or write `async def resolve_<name>(page, selector)` and dispatch it inside
   `try_strategy`.

That's it — the engine will pick it up automatically.

## Behaviour & robustness notes

- Each field is retried up to **3 times** before being marked skipped.
- Resolution failures never crash the run — only `WARNING [SKIP]` lines.
- `--debug` injects a 1.2s red outline + box-shadow on every matched field
  so you can visually confirm targeting before enabling submit.
- `--dry-run` exercises the whole resolver path (and logs which strategy
  *would* have been used) without typing anything — perfect for tuning a
  config against a tricky form.
- The browser stays open for 5 seconds at the end of every non-headless run
  so you can inspect state.

## Roadmap

- **Round 2:** per-field screenshot capture
- **Round 3:** CAPTCHA detection + pause-for-human fallback
- **Round 4:** support filling forms inside `<iframe>`s
- **Round 5:** export the run as a replayable script
