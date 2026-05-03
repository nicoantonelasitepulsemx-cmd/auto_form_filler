# Kế hoạch chi tiết — Recorder accuracy + Mail-per-proxy multi-thread UI

> **Mục tiêu chung**
> 1. Cho recorder bắt **chính xác từng tick / radio / submit** (kể cả group cùng `name`, hidden-input proxy, Enter-submit, JS-submit).
> 2. Refactor panel **Mail-per-proxy** sang dạng **đa luồng**: mỗi worker có ô nhập email/proxy riêng (như spreadsheet), giữ nguyên proxy + filter case mail, dễ dùng hơn.
> 3. Sửa các bug nhỏ tìm thấy trong toàn bộ pipeline.

> **⚠️ Em chưa code** — file này là **kế hoạch + danh sách bug**. Chờ anh duyệt rồi em mới mở PR.

---

## A. RECORDER (`recorder_v2.py` + `replay_engine.py` + `resolver_v2.py`)

### A1. [BUG] `__af_proxy_handled` flag không được dọn khi `change` event không fire
- **File**: `recorder_v2.py:445, 651`
- **Hiện trạng**:
  ```js
  // proxy click branch ship('check', assocInput, ...)
  assocInput.__af_proxy_handled = true;
  // change handler:
  if (el.__af_proxy_handled) { delete el.__af_proxy_handled; return; }
  ```
  Nếu `change` không fire (JS preventDefault, custom React handler, hoặc input bị unmount trước khi event bubble xong) thì cờ `__af_proxy_handled` ở lại trên element **vĩnh viễn**. Lần kế tiếp user click vào CÙNG checkbox đó qua proxy → branch proxy ship action OK, nhưng nếu user thao tác qua đường khác (ví dụ kbd shortcut → real `change` event) thì action bị nuốt.
- **Hậu quả**: Mất tick / lệch state khi user click đi click lại cùng 1 ô.
- **Fix**: Dùng timestamp expiry thay cho boolean: `assocInput.__af_proxy_ts = ts(); ` rồi trong change handler check `if (ts() - el.__af_proxy_ts < 800) { delete el.__af_proxy_ts; return; }`. Tự động "hết hạn" sau 800ms — không bao giờ kẹt cờ.

### A2. [BUG] Hidden input có cùng vị trí offscreen ⇒ Euclidean distance vô nghĩa
- **File**: `recorder_v2.py:550-590` (`_findAssociatedInput`)
- **Hiện trạng**: Khi tất cả 4 hidden checkbox `content_type[]` đều có style `position:absolute;left:-9999px`, bbox của chúng trùng nhau ⇒ `Math.hypot()` ranking về cơ bản chọn ngẫu nhiên (Chrome stable order may vary).
- **Hậu quả**: Trên Facebook trademark form, có thể vẫn lệch 1 trong 4 option mặc dù hit-test theo label đã cover ~75% trường hợp.
- **Fix**:
  1. Khi `inputs.length > 1` và mọi input đều **off-screen** (`r.left < -1000 || r.top < -1000`) → bỏ qua distance, **chỉ dựa vào label hit-test** (lines 558-569).
  2. Nếu không có input nào pass label hit-test, **không return ngẫu nhiên** — trả về `null` để click handler ship một `click` action thường thay vì gắn nhầm vào hidden input.

### A3. [BUG] `pre_submit_flush` không cover trường hợp `el.value` rỗng do React rerender
- **File**: `recorder_v2.py:672-681, 698-705`
- **Hiện trạng**: `pendingFills.forEach(pf => ship("fill", pf, { value: pf.value }))` — đọc `pf.value` tại thời điểm submit. Nhưng khi React rerender form sau submit, ref bị thay thế ⇒ `pf` còn là DOM cũ với `value === ""`.
- **Hậu quả**: Email/password bị ghi đè `""` lên action đã có (do dedup logic). User sẽ thấy "đã type alice@example.com" nhưng config bị reset thành `""`.
- **Fix**: Khi flush, đối chiếu `lastValue.get(pf)` (đã lưu từ `input` event) và **chỉ ship khi value mới khác rỗng**. Nếu `pf.value === ""` thì dùng giá trị cuối cùng từ `lastValue`.
  ```js
  const v = pf.value || lastValue.get(pf) || "";
  if (v) ship("fill", pf, { value: v, ... });
  ```

### A4. [BUG] Submit-via-JS (form.submit() / fetch + redirect) không bị bắt
- **File**: `recorder_v2.py:693-719`
- **Hiện trạng**: Listener `submit` chỉ fire cho **form submission native**. Nếu site dùng `fetch('/api')` rồi `window.location = '/next'` thì cả `submit` event lẫn `looksLikeSubmit(click)` đều miss (button text là `"Verify"` — không match regex).
- **Hậu quả**: Replay không có submit action ⇒ form không được nộp.
- **Fix**:
  1. Thêm listener `popstate` + `framenavigated` (Python side đã có rồi nhưng synthetic wait, không phải submit).
  2. Mở rộng `looksLikeSubmit` regex: thêm `verify|confirm|create|register|sign\s*up|log\s*in|save|post|báo\s*cáo|xác\s*minh|hoàn\s*tất|complete|finish|done|apply`.
  3. Khi action gần nhất là `click` mà ngay sau đó có `framenavigated` → upgrade `kind: "click"` thành `kind: "submit"` và lưu `_via: "post_click_nav"`.

### A5. [BUG] Multi-step form: chỉ giữ 1 submit cuối cùng
- **File**: `recorder_v2.py:1087-1097` (`build_config`)
- **Hiện trạng**:
  ```python
  for a in session.actions:
      if a.get("kind") == "submit":
          submit = { ... }   # ← overwrite
          continue
  ```
  Multi-step form (Next → Next → Submit) sinh ra 2-3 submit nhưng chỉ submit **cuối** được lưu vào `cfg["submit"]`.
- **Hậu quả**: Replay step 2 không click "Next", form kẹt ở step 1.
- **Fix**: Giữ submit **inline** trong `actions` list (không tách ra `cfg["submit"]`), hoặc `cfg["submits"] = [list]`. Replay engine đã handle `kind: "submit"` qua `_action_may_navigate` nên chỉ cần để submit ở trong actions là chạy được.

### A6. [BUG] Submit detection regex không cover button có icon-only / chữ trong span con
- **File**: `recorder_v2.py:771-786`
- **Hiện trạng**: Đọc `el.innerText` — với button cấu trúc `<button><svg/><span>Send</span></button>`, `innerText` ok; nhưng với `<button aria-label="Send"></button>` (icon-only), `innerText === ""` ⇒ regex fail nếu aria-label cũng không match.
- **Fix**: Thêm path check cho `el.querySelector("[aria-label*='submit' i]")` + `el.querySelector("[data-action*='submit' i]")`. Ngoài ra, fallback: `if (form.matches(":valid") && this is the only button[type=submit] in form) → looksLikeSubmit = true`.

### A7. [IMPROVE] Screenshot debounce có thể "ăn" screenshot cuối khi user click rồi đóng tab nhanh
- **File**: `recorder_v2.py:1604-1613`
- **Hiện trạng**: Khi user nhấn `Done`, code đợi `_shot_task[0]` trong 2s. Nhưng debounce time là 350ms ⇒ task **mới chạy** sẽ bị huỷ nếu chưa kịp shot. Edge case nhỏ.
- **Fix**: Trước khi `await fut`, force-flush: `if _shot_pending[0] is not None: await _do_screenshot(*_shot_pending[0]); _shot_pending[0] = None`.

### A8. [BUG] `cssEscape` chưa được polyfill nếu Chromium thiếu — nhưng đó là Chrome ≥ 64 nên OK; **THỰC SỰ** cần check là `cssEscape(el.id)` khi `el.id` chứa ký tự đặc biệt (số đầu, dấu chấm…) — code đã có dùng nhưng **không** check edge case `id` chứa `:` (Tailwind responsive class).
- **File**: `recorder_v2.py:64-78` (cssEscape function)
- **Fix**: Đã có `try { return CSS.escape(s); } catch { ... }` — OK. Để xác nhận, thêm test với id `foo:bar`.

### A9. [IMPROVE] `accessibleName` ưu tiên `aria-label` trước cả `label[for]` ⇒ với input có label đẹp + aria-label generic → chọn aria-label
- **File**: `recorder_v2.py:126-137`
- **Hiện trạng**: order = aria-label > labelText > placeholder > title > button text.
- **Vấn đề**: Một số form gắn `aria-label="text input"` chung chung, trong khi `label` text lại là "Email address" chính xác. Recorder sẽ dùng `"text input"` làm field_id → kém readable.
- **Fix**: Đổi thứ tự thành `labelText > aria-label > placeholder > title`. Hoặc giữ thứ tự nhưng nếu aria-label quá generic (`/^(input|text|textbox|field)$/i`) thì skip xuống labelText.

### A10. [BUG] `_walk_frame_chain` không match nested same-origin iframe có cùng URL pattern
- **File**: `resolver_v2.py:82-113`
- **Hiện trạng**: `if entry in url or entry == name` — match đầu tiên thắng. Hai iframe nested cùng base URL ⇒ chọn nhầm.
- **Fix**: Track cả chain depth: nếu `frame_chain` length là 3 thì chỉ accept frames ở depth tương đương. Ngoài ra, fallback DFS hiện tại có thể chọn frame quá xa — thêm guard `if depth_diff > 1: continue`.

### A11. [IMPROVE] Replay `_do_check` không verify lại `aria-checked` cho custom widget
- **File**: `replay_engine.py:618-712`
- **Hiện trạng**: Sau khi `loc.click()`, đọc `_is_checked()` dùng `await loc.is_checked()` — chỉ work cho `<input>`. Với `div[role=checkbox]` thì `is_checked()` raise hoặc return False sai.
- **Fix**: Trong `_is_checked`, nếu element là `[role=checkbox/radio]` thì đọc `aria-checked` thay cho `.is_checked()`:
  ```python
  async def _is_checked():
      try:
          return await loc.is_checked()
      except Exception:
          return await loc.evaluate("e => e.getAttribute('aria-checked')==='true'")
  ```

### A12. [IMPROVE] Replay không retry action trên `Element is not stable` / `not visible`
- **File**: `replay_engine.py:920-943`
- **Hiện trạng**: Resolve có retry 3 lần, nhưng `_do_check` / `_do_fill` raise → return False ngay.
- **Fix**: Bọc dispatcher trong loop `for attempt in range(2)` với 200ms sleep giữa các attempt. Đặc biệt cho `not stable` / `not actionable` errors.

### A13. [IMPROVE] `looksLikeSubmit` không tìm submit button trong form **bên ngoài** click target
- **File**: `recorder_v2.py:711-718`
- **Hiện trạng**: Khi submit event fire, code tìm `form.querySelector("button[type=submit], input[type=submit]")`. Đúng. Nhưng nếu không có submit button trong form (button nằm ngoài form, dùng `form="..."` attribute), code rơi vào ship `target=form` → selectors sẽ là form, không phải button.
- **Fix**: Thêm fallback: `document.querySelector(\`[form="${form.id}"]\`)`.

### A14. [BUG] Test `test_recorder_e2e_form.py` line 75 — `await page.fill("#email", ...)` không trigger `input` event như user thật
- **File**: `test_recorder_e2e_form.py:45-49`
- **Hiện trạng**: `page.fill(selector, value)` set `.value` qua DOM, KHÔNG fire `input`/`change` event nếu element không nhận focus. Test có thể pass nhờ `change` được fire qua `Tab`.
- **Vấn đề**: Test không cover production path "user gõ ký tự". Cần thêm test dùng `page.type()` hoặc `page.keyboard.type()` để verify recorder bắt được input events từng ký tự.
- **Fix**: Thêm test mới `test_recorder_keystroke_typing.py` dùng `page.keyboard.type("hello", delay=20)`.

---

## B. MAIL-PER-PROXY PANEL (`mail_per_proxy_panel.py`)

### B1. [UX] Bulk-register textarea quá ngắn, khó nhìn từng worker
- **File**: `mail_per_proxy_panel.py:597-614`
- **Hiện trạng**: 1 textarea `height=6` cho ALL bulk lines — user phải nhớ format `name | local@domain | proxy`, khó scan, dễ nhập sai cột.
- **Fix theo yêu cầu user**: Refactor thành **bảng đa luồng** (Treeview-as-form):
  - Mỗi dòng = 1 worker với 4 ô nhập: **[Name] [Email/Local] [Domain] [Proxy]**
  - Có column `[Status]` (idle / minting / done / error) cập nhật real-time
  - Nút `+ Add row` để thêm worker mới
  - Nút `× Remove row` cho từng dòng
  - Nút `▶ Register selected` / `▶ Register all`
  - Concurrency spinbox vẫn giữ
  - Ô **Email/Local** trống ⇒ kuku.lu sinh random (đúng yêu cầu "ô trống để tùy chọn")
  - Drag-drop reorder (nice-to-have)
- **Compatibility**: Vẫn parse được old textarea format (cho backward compat — paste-import button), giữ proxy + filter case mail logic.

### B2. [BUG] `_cmd_mint_mailbox` tạo HTTP backend mới mỗi lần ⇒ leak proxy connection
- **File**: `mail_per_proxy_panel.py:992-1003`
- **Hiện trạng**: Mỗi click "Mint" tạo `Kuku.from_requests(proxy=...)` → mở session mới, không reuse.
- **Hậu quả**: Bulk 50 mint → 50 sessions; nếu Cloudflare trigger, mỗi session phải solve riêng.
- **Fix**: Cache `Kuku` instance per `(proxy_dict, creds)` trong `_AsyncRunner` lifetime. Sau khi finish, `aclose()` đồng loạt.

### B3. [BUG] Bulk mint không hiển thị "row đang process"
- **File**: `mail_per_proxy_panel.py:1356-1448`
- **Hiện trạng**: User chỉ thấy `"X/Y done · A ok · B err"` — không biết worker nào đang chạy.
- **Fix**: Trong table mới (B1), set status column = `"⏳ minting"` khi semaphore acquire, đổi sang `"✓ done"` / `"✗ err"` khi xong.

### B4. [BUG] `wait_for_code` mặc định `since=None` ⇒ có thể nhặt OTP CŨ trong inbox
- **File**: `kuku_lu.py:557-602`
- **Hiện trạng**: `since` parameter có nhưng không được set trong `_cmd_wait_code` (`mail_per_proxy_panel.py:1180-1218`). Hàm `wait_for_code` track `seen` set để tránh đọc lại nhưng KHÔNG check `entry.timestamp` ⇒ mail cũ trong inbox có thể trả về.
- **Fix**: Trong `_cmd_wait_code`, pass `since=time.time()` để chỉ chấp nhận mail mới sau khi click button.

### B5. [BUG] `parse_proxy_string` raise → row có proxy invalid bị tạo nhưng silently no-proxy
- **File**: `mail_per_proxy_panel.py:202-210, 1304-1308`
- **Hiện trạng**: `_proxy_to_playwright_dict` return None on failure → `kuku_proxy = None` → mint qua direct IP. User không biết proxy đã fail.
- **Fix**: Validate proxy at row-add time; nếu invalid, mark status `"⚠ proxy invalid"` và **không** mint (require user fix proxy hoặc explicit "no proxy" toggle).

### B6. [IMPROVE] Local part picker hiện chỉ ảnh hưởng tới SELECTED row, không bulk
- **File**: `mail_per_proxy_panel.py:1338-1349`
- **Hiện trạng**: Bulk mint cố tình bỏ `local_part` (comment lines 1338-1339).
- **Fix theo yêu cầu user**: Trong bảng mới (B1), mỗi row có ô **[Local]** riêng. Empty = random. User có thể set per-row trước khi click "Register all".

### B7. [BUG] OTP defaults dùng `domain` nhưng UI không nhất quán giữa save/load
- **File**: `mail_per_proxy_panel.py:1247-1256`
- **Hiện trạng**: `_cmd_save_otp_defaults` lưu `domain` từ `self.domain_var.get()`, nhưng `load_otp_defaults` (line 417) chỉ đọc `regex / from_filter / timeout / poll / domain` — OK, nhưng test `test_mail_per_proxy_panel.py` không cover field `domain` ⇒ regression risk.
- **Fix**: Thêm `domain` vào `DEFAULT_OTP_PREFS` constant + test coverage.

### B8. [BUG] `_AsyncRunner` không expose error nếu coroutine factory raise đồng bộ
- **File**: `mail_per_proxy_panel.py:377-385`
- **Hiện trạng**: `coro_factory()` raise sync → `asyncio.ensure_future` re-raises trên loop thread → silent.
- **Fix**: Wrap `coro_factory()` trong try/except inside `schedule()`, post error qua `on_done(None, exc)`.

### B9. [UX] "Clear inbox" button không có
- **Yêu cầu user**: "chức năng lọc case mail vẫn giữ nguyên, và chuẩn xác".
- **Fix**: Thêm button `"🗑 Clear OTP cache"` để reset `seen` set của wait_for_code (nếu cần re-trigger). Plus dropdown filter theo subject/from quick-select.

### B10. [BUG] `From proxy file…` không lưu raw proxy string (mất info nếu format đặc biệt)
- **File**: cần đọc thêm `mail_per_proxy_panel.py:870-923`
- **Fix**: Sẽ check thêm khi implement.

---

## C. CROSS-CUTTING & INFRASTRUCTURE

### C1. [BUG] `_browser_overlay.js` (file đính kèm rời) có vẻ là phiên bản cũ — không sync với inline OVERLAY_JS trong `recorder_v2.py`
- **Action**: Xoá `_browser_overlay.js` hoặc gắn comment "@deprecated — do not edit, see recorder_v2.OVERLAY_JS".

### C2. [INFRA] Không có `pytest.ini` / `pyproject.toml` ⇒ test files chạy bằng `python test_*.py` chứ không phải `pytest`
- **Fix**: Thêm `pyproject.toml` minimal với `[tool.pytest.ini_options]` để CI có thể `pytest -v`. Hoặc viết `Makefile` với `test:` target tương đương.

### C3. [INFRA] Không có `.github/workflows/ci.yml`
- **Fix**: Thêm CI chạy unit test (skip những test cần Playwright browser nếu không cần) — sẽ tích hợp khi tạo PR.

### C4. [BUG] `test_recorder_screenshot_debounce.py:142` assert `len(actions) == 30` — flaky trên CI chậm
- **Fix**: Tăng `asyncio.sleep(0.005)` → `0.02`, hoặc retry assertion với poll.

### C5. [IMPROVE] Logger formatting không có TID ⇒ khó debug multi-thread
- **File**: `logger.py`
- **Fix**: Thêm `%(threadName)s` vào format string.

---

## D. DESIGN — Mail-per-proxy "đa luồng dạng bảng" (đáp ứng yêu cầu user)

### Mockup layout (dạng table-as-form):

```
┌─────────────────────── Mail-per-proxy Manager ─────────────────────────┐
│ accounts.json: ./accounts.json  [Open] [Save] [Save As]                 │
│                                                                          │
│ ┌──────────── 📋 Workers (multi-thread mint) ────────────────────┐      │
│ │ #  Name      Local          Domain        Proxy           Status│     │
│ │ 1  acct1     alice          kpay.be       [host:port:u:p] idle  │     │
│ │ 2  acct2     (auto)         (auto)        (none)          idle  │     │
│ │ 3  acct3     bob            kpay.be       [host:port:u:p] ⏳    │     │
│ │ 4  acct4                                                  ✗ err │     │
│ │ ...                                                              │     │
│ │ [+ Add row]  [× Remove row]  [Import file…] [From proxy list…]  │     │
│ │ Concurrency: [4 ▾]   [▶ Register all]  [▶ Register selected]    │     │
│ │ Status: 2/10 done · 1 ok · 1 err                                │     │
│ └──────────────────────────────────────────────────────────────────┘     │
│                                                                          │
│ ┌─── 📥 Inbox actions ───────┐  ┌─── 🔧 OTP defaults ─────────────┐    │
│ │ [📥 Test inbox] [⏳ Wait OTP]│  │ Regex:   [(?<!\d)(\d{5,8})...]  │    │
│ │ [🔄 New address]            │  │ From:    [facebook]              │    │
│ │ [📋 Copy address]           │  │ Timeout: [180]  Poll: [4]        │    │
│ └─────────────────────────────┘  │ [Save defaults]                  │    │
│                                   └──────────────────────────────────┘    │
│                                                                          │
│ Log: ───────────────────────────────────────────────────────────────────│
│ [bulk] acct1 → alice@kpay.be                                            │
│ [bulk] acct3: minting…                                                   │
│ [bulk] acct4: FAIL — proxy timeout                                       │
└──────────────────────────────────────────────────────────────────────────┘
```

### Behaviour
- **Each row = 1 worker** với độc lập về proxy + email + status.
- **Empty fields = "tùy chọn"** (đáp ứng yêu cầu "ô trống để tùy chọn"):
  - Local trống → kuku.lu pick random
  - Domain trống → dùng `Domain default` (top of panel) hoặc kuku.lu auto
  - Proxy trống → mint qua direct IP
- **Multi-thread mint**: `asyncio.Semaphore(concurrency)` (giữ logic cũ), chỉ chạy được trên row nào status = idle / err.
- **Per-row actions**: right-click context menu = `Mint | New address | Test inbox | Wait OTP | Copy address | Remove`.
- **Backwards compat**:
  - File `accounts.json` schema không đổi.
  - Vẫn parse được old textarea format qua nút `Import paste…`.
  - Proxy + case-mail filter logic giữ nguyên (không đổi `kuku_lu.py` API).

### Tests cần thêm
- `test_mail_per_proxy_table_panel.py`:
  - Add/remove row đồng bộ với `_rows` list.
  - Edit cell in-place persist sau Save.
  - Concurrent mint không race trên `_rows[]`.
  - Empty cells = "auto" semantic.
  - Per-row status updates real-time.

---

## E. THỨ TỰ IMPLEMENT (phasing)

> Em sẽ commit theo phase, mỗi phase 1 commit để dễ review.

| Phase | Nội dung                                       | Files                                       | Tests           |
|-------|------------------------------------------------|---------------------------------------------|-----------------|
| P0    | Setup repo + CI scaffolding                    | `pyproject.toml`, `.github/workflows/`      | smoke           |
| P1    | Recorder fixes A1, A2, A3, A6, A8, A11, A12    | `recorder_v2.py`, `replay_engine.py`        | A1-A6 tests     |
| P2    | Recorder submit upgrades A4, A5, A13, A14      | `recorder_v2.py`                            | submit-* tests  |
| P3    | Resolver / replay improvements A10             | `resolver_v2.py`                            | resolver test   |
| P4    | Mail panel: per-worker table UI (B1, B3, B6)   | `mail_per_proxy_panel.py`                   | new table test  |
| P5    | Mail panel bug fixes B2, B4, B5, B7, B8, B9    | `mail_per_proxy_panel.py`, `kuku_lu.py`     | regression      |
| P6    | Cross-cutting C1, C4, C5                       | misc                                        | -               |
| P7    | Final smoke + manual e2e                       | -                                           | full pytest     |

---

## F. CÂU HỎI CHO ANH

1. **Repo Git**: Hiện em chỉ có file rời ở `/home/ubuntu/work/`, **chưa có repo**. Anh muốn:
   - **(a)** Em tạo repo mới trên GitHub (nếu anh share được token / repo URL có sẵn).
   - **(b)** Em làm thành 1 patch / archive `.tar.gz` để anh apply local.
   - **(c)** Anh có repo private sẵn — gửi em URL hoặc Add Devin GitHub access.

2. **Backward compat**: Mail panel UI mới có cần giữ **textarea cũ** không, hay thay hẳn bằng table? Em đề xuất **giữ cả hai** (table mặc định + nút `Paste import…` để dùng textarea ad-hoc) — anh OK chứ?

3. **Submit detection regex**: Anh có site cụ thể đang fail submit detection không? Nếu có em thêm text patterns chính xác hơn (đỡ false-positive).

4. **Test scope**: Em chạy được `pytest` headless cho recorder/replay (Chromium). OK chứ ạ? Hay anh muốn em record video làm bằng chứng?

5. **`_browser_overlay.js`** (file rời): Em được phép xoá/deprecate file này không? (Có vẻ là code cũ, đã được merge vào `recorder_v2.OVERLAY_JS`.)

---

## G. SCOPE — Cái em **KHÔNG** đụng vào (tránh blast radius)

- `auto_fill.py`, `auto_fill_gui.py` (main GUI): chỉ thay đổi nếu cần cho Mail panel hook lại. Không refactor toàn bộ.
- `proxy_utils.py`, `accounts.py`: API giữ nguyên.
- `kuku_lu.py`: chỉ thêm `since` default + minor fix B4. Không đổi public API.
- `value_templates.py`, `auto_template.py`: out of scope.
- `worker_pool.py`, `picker.py`, `captcha.py`: out of scope.

---

**Tổng kết**: 14 bug recorder + 10 bug/UX mail panel + 5 cross-cutting = 29 items. Em propose chia làm 7 phases như trên. Anh duyệt phần nào, em ưu tiên phần đó. Đợi anh confirm rồi em mới code & PR. 🙏
