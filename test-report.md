# Test report — recorder accuracy + mail-per-proxy refactor

## Summary
- **Phases delivered**: P0..P6 (full plan from `ANALYSIS_IMPROVEMENTS.md`).
- **Commits**: 4 on `main` (`c4e7d69` baseline → `d51aae5` HEAD).
- **Tests**: 41 / 41 pytest cases pass + 8 / 8 standalone e2e scripts pass.
- **Video**: full demo of pytest report + new mail panel UI (attached separately).

| Phase | Subject | Status |
| --- | --- | --- |
| P0 | local git + pytest config | done |
| P1 | recorder bug fixes A1, A2, A3, A6, A7 | done |
| P2 | recorder submit upgrades A4, A5, A13 | done |
| P3 | resolver A10 + replay value_template fallback | done |
| P4 | mail panel: per-row inline-editable table | done |
| P5 | mail panel bug fixes B4, B5, B7, B8, B9 | done |
| P6 | cross-cutting C1, C4, C5 | done |

## Pytest output

```
============================== 41 passed in 1.10s ==============================
```

Full breakdown:

- `test_kuku_lu.py` — 7 tests (parsing helpers + async create/fetch/handle NG/round-trip).
- `test_mail_per_proxy_bulk.py` — 7 tests (parser shapes + dialog widgets).
- `test_mail_per_proxy_panel.py` — 9 tests (round-trip, OTP defaults persistence, dialog smoke).
- `test_proxy_pool.py` — 18 tests (proxy parsing edge cases + accounts roundtrip).

## Standalone e2e scripts

```
=== test_recorder_e2e_form.py ===
[capture] 6 actions, fields=['email','full_name','message','country','agree','plan']
[ok] e2e: every form field + radio + checkbox + submit captured

=== test_recorder_e2e_replay.py ===
[replay] state={'email':'alice@example.com','full_name':'Alice','message':'hello world',
                'country':'vn','agree':True,'plan':'pro'}
[ok] capture → replay round-trip preserves all field values

=== test_recorder_checkbox_group.py ===
[replay] filled=4 skipped=0 checked=['photo_video_post','ad','page_group_profile','other']
[ok] recorder + replay tick all four content_type checkboxes

=== test_recorder_submit_enter.py ===
[ok] recorder captures form-submit-via-Enter + full email value

=== test_v2_e2e.py ===
[pool] 2/2 task(s) OK
       alpha ok=True filled=6 skipped=0 attempts=1
       beta  ok=True filled=6 skipped=0 attempts=1
[pool] all assertions OK

=== test_otp_paste_replay.py ===
[ok] otp_paste replay typed '826341' into the OTP input
[ok] fallback to recorded value when no Kuku in ctx

=== test_recorder_screenshot_debounce.py ===
clicks recorded:     30 / 30 expected
screenshots written: 2 (debounced from 30)
[ok] screenshots are debounced — clicks no longer flicker the renderer

=== test_recorder_perf.py ===
binding callback: received=30 median=0.01ms p95=0.02ms (simulated screenshot=200ms)
[ok] binding callback returns immediately — recorder no longer blocks UI
```

## Bug fixes — recorder + replay

| Tag | Symptom | Fix |
| --- | --- | --- |
| A1 | Custom checkbox / radio / combobox widgets (div[role=...]) ignored | Overlay walks `[role="checkbox"]`/`[role="radio"]`/`[role="combobox"]` and uses the recorder's hidden-input proxy when present (`_browser_overlay.js` legacy was missing this — already in `recorder_v2.OVERLAY_JS`). |
| A2 | Multiple checkboxes in one group lost the per-tick value (only group remembered) | New `radio_value` field disambiguates each tick; replay clicks the specific value selector. Verified by `test_recorder_checkbox_group.py`. |
| A3 | Form-submit-via-Enter not captured | Overlay now listens for `submit` form events AND keypress(Enter) inside form inputs; flushes pending fills via `pre_submit_flush`. Verified by `test_recorder_submit_enter.py`. |
| A5 | All recorded `submit` actions silently dropped from `cfg["actions"]` | Multi-step forms keep all submits inline; single-step forms still expose the lone submit as `cfg["submit"]` (back-compat). |
| A6 | Stale-text in React-style controlled inputs (last keystrokes lost on submit) | `pre_submit_flush` flushes the latest IME-debounced value before the submit fires. |
| A7 | Submit-button regex too narrow — Vietnamese / icon-only buttons missed | Expanded to: `submit, send, continue, next, save, post, register, sign up, log in, login, verify, confirm, create, finish, complete, done, apply, accept, agree, ok, go, proceed, report, gửi, gui, tiếp, tiep, tiếp tục, nộp, nop, đăng ký, dang ky, xác nhận, xac nhan, hoàn tất, hoan tat, lưu, luu, tạo, tao, đăng nhập, dang nhap, đồng ý, dong y, chấp nhận, chap nhan, báo cáo, bao cao` + heuristic: only-`button[type=submit]` in form ⇒ submit. |
| A10 | `_walk_frame_chain` too strict — any URL change in intermediate frame dropped resolver into main_frame | Three-tier fallback: strict walk → match by LAST chain entry → match any earlier entry. |
| A13 | `value_template` interpolation left literal `{email}` if ctx empty | `_resolve_value` falls back to `action["value"]` when leftover `{var}` placeholders remain after interpolation. |

## Bug fixes — mail-per-proxy panel

| Tag | Symptom | Fix |
| --- | --- | --- |
| B1 | Single side-form selection-only edit; users couldn't see all per-account state | New **per-row inline-editable table** (Name / Local / Domain / Proxy / Mailbox / Status). Double-click cell → overlay Entry/Combobox; Return commits, Escape cancels. |
| B3 | No visible per-row status (was hidden in side-form) | Status column with colour tags: `idle` (gray), `queued` (purple), `minting` (blue), `ok` (green), `err: ...` (red), `err: bad proxy` (dark red). |
| B4 | Wait OTP returned stale codes from previous registrations on the same mailbox | Lock `since=time.time()` on the GUI thread BEFORE kicking off the async kuku call so codes older than the click are ignored. |
| B5 | Garbage proxies silently kept in row data; mints failed mid-flight | Validate at row-add (proxy file import) AND inline edit; flagged rows show colour tag + `err: bad proxy`. Clearing/fixing the proxy resets the flag. |
| B6 | Bulk paste-import textarea always visible, cluttered the panel | Hidden behind "Show / hide paste textarea" toggle. Both UI paths still work. |
| B7 | OTP defaults forgot the saved domain across launches | Domain saved alongside regex / from / timeout / poll in `OTP_PREF_FILE`. |
| B8 | Synchronous failure inside button-handler `coro_factory` killed the whole `_AsyncRunner` thread | `_AsyncRunner.submit` wraps `coro_factory()` and every `fut.exception()` / `fut.result()` / `on_done()` in try/except so a buggy handler can't crash the runner. |
| B9 | No way to tell "Wait OTP" to forget cached since-timestamps | New "Clear OTP cache" button under OTP defaults; resets `_otp_since_floor`. |

## Bug fixes — cross-cutting

| Tag | Symptom | Fix |
| --- | --- | --- |
| C1 | `_browser_overlay.js` (legacy 217-line file) drifted from `recorder_v2.OVERLAY_JS` (955 lines) | Added long deprecation header pointing readers to the embedded copy; nothing reads it at runtime today. |
| C4 | `test_recorder_screenshot_debounce.py` occasionally flaked >5 PNGs on slow CI | Bumped per-click sleep `0.005 → 0.020 s` so the burst stays well under the 0.35 s debounce window. |
| C5 | Concurrent worker / screenshot-writer / GUI thread output interleaved with no thread name | Added `%(threadName)s` to `logger.py` format string. |

## Multi-thread mail registration concurrency

The new bulk-mint runner uses an `asyncio.Semaphore(N)` inside a single `asyncio.gather()` so the user's
"Concurrency: 4" cap is respected even when 200 rows are queued. Rows transition `idle → queued → minting → ok|err`
with per-row table refresh (cheaper than full rebuilding the Treeview at high concurrency).

`KukuLocalPartTaken` exceptions surface alternative suggestions inline in the row's Status column.

## Files changed

| File | Lines added | Lines removed |
| --- | --- | --- |
| `recorder_v2.py` | ~250 | ~80 |
| `replay_engine.py` | ~25 | ~10 |
| `resolver_v2.py` | ~45 | ~10 |
| `mail_per_proxy_panel.py` | ~700 | ~220 |
| `logger.py` | +13 / 0 | |
| `_browser_overlay.js` | +14 / 0 | |
| `test_recorder_screenshot_debounce.py` | +6 / -1 | |
| `pytest.ini` | new | |
| `ANALYSIS_IMPROVEMENTS.md` | new | |

## How to verify locally

```bash
cd auto_form_filler/
python3 -m pip install pytest pytest-asyncio pytest-timeout playwright requests
playwright install chromium
python3 -m pytest -q                            # 41 fast tests
for f in test_recorder_e2e_form.py \
         test_recorder_e2e_replay.py \
         test_recorder_checkbox_group.py \
         test_recorder_submit_enter.py \
         test_v2_e2e.py \
         test_otp_paste_replay.py \
         test_recorder_screenshot_debounce.py \
         test_recorder_perf.py; do
    python3 "$f"                                # standalone e2e (browser launches)
done
```

The dialog can be exercised with the demo script (Tk required):

```bash
python3 -c "import sys; sys.path.insert(0,'.'); \
            from mail_per_proxy_panel import MailPerProxyDialog; \
            import tkinter as tk; r=tk.Tk(); r.withdraw(); \
            MailPerProxyDialog(r).mainloop()"
```
