# Fix giật/nháy khi click trong lúc record

## Nguyên nhân (xác nhận)

Mỗi action (click / fill / change) đều gọi `await page.screenshot()`
qua CDP `Page.captureScreenshot`. Lệnh đó **làm Chromium tạm dừng
renderer thread** ~100–500 ms để compose surface + encode PNG. User
nhìn thấy đó là **cú nháy đúng kiểu screenshot** trên mỗi click.

`kuku_lu.py` (mail server) **không phải nguyên nhân** — nó chỉ chạy
khi user bấm thẳng nút "✎ Get OTP → paste" trên panel, không gắn vào
event listener của các click thường.

## Fix (2 lớp)

### 1. Tắt screenshot mặc định

`record_to_config(..., capture_screenshots=False)` — default mới là
**OFF**. Replay chỉ cần selectors + fingerprint, không cần ảnh PNG,
nên mặc định bỏ luôn để click không bao giờ bị nháy.

GUI (`auto_fill_gui.py → cmd_record_session`) gọi recorder không truyền
`capture_screenshots`, nên tự động kế thừa default mới → hết nháy.

CLI:

```bash
# Mặc định: KHÔNG screenshot (không nháy)
python recorder_v2.py --url ... --out config.json

# Opt-in nếu thật sự cần PNG cho mỗi step
python recorder_v2.py --url ... --out config.json --screenshots
```

`--no-screenshots` cũ vẫn còn (no-op) để script cũ không vỡ.

### 2. Khi user vẫn opt-in `--screenshots`, debounce 350 ms

Thay vì 1 screenshot / click (cú nháy mỗi click), dồn lại — chỉ chụp
**1 lần sau khi user dừng tay 350 ms**. Click liên tục 30 cái → chỉ
2 screenshot, click rồi dừng → 1 screenshot. Vẫn còn 1 cú nháy lúc
chụp nhưng chỉ xuất hiện khi user nghỉ tay, không xen giữa thao tác.

Test `test_recorder_screenshot_debounce.py` chứng minh:

```
clicks recorded:     30 / 30 expected
screenshots written: 2 (debounced from 30)
binding callback:    median=0.04ms p95=0.12ms
```

## Files thay đổi

- `recorder_v2.py`
  - `record_to_config`: `capture_screenshots: bool = False` (mặc định)
  - `record_to_config_sync`: same
  - `_capture_screenshot`: thay coroutine + single-flight bằng sync
    schedule + debounced background flusher
  - CLI: `--no-screenshots` → `--screenshots` (đảo dấu)
  - Drain pending screenshot trước khi đóng browser
- `test_recorder_screenshot_debounce.py` (mới) — xác nhận coalescing

## Tests pass

- `test_recorder_perf.py`
- `test_smoke.py`
- `test_recorder_checkbox_group.py`
- `test_recorder_screenshot_debounce.py`
