# Facebook Trademark Report — ví dụ tự động hóa

Bộ ví dụ này tự động:
1. Điền **toàn bộ form Trademark Report** (theo đúng các trường trong ảnh anh gửi).
2. **Submit** form (CAPTCHA tự pause để anh giải bằng tay — xem mục CAPTCHA bên dưới).
3. Sau khi submit, **đọc inbox qua IMAP** để lấy mã xác nhận Facebook gửi về email.
4. **Tự fill mã đó** vào trang xác nhận tiếp theo, rồi submit luôn.

## Files

| File                                        | Vai trò                                                                                |
|---------------------------------------------|----------------------------------------------------------------------------------------|
| `example_facebook_trademark.json`           | Config phase 1 — điền form chính. Sửa các giá trị `value` thành thông tin của anh.    |
| `example_facebook_trademark_otp.json`       | Config phase 2 — trang nhập mã. Để nguyên, script sẽ ghi đè `value` lúc chạy.         |
| `run_with_email_otp.py`                     | Orchestrator: chạy phase 1 → IMAP polling → phase 2.                                  |

## Setup (1 lần)

```bash
cd auto_form_filler
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

## Setup IMAP (đọc email tự động)

Đặt **biến môi trường** — không bao giờ ghi mật khẩu vào file JSON.

```bash
export OTP_IMAP_HOST=imap.gmail.com
export OTP_IMAP_USER=ban@gmail.com
export OTP_IMAP_PASS=xxxx_xxxx_xxxx_xxxx       # App Password — KHÔNG dùng password Gmail thường
export OTP_FROM_FILTER=facebookmail.com         # tùy chọn — chỉ lọc email từ Facebook
export OTP_SUBJECT_FILTER='confirmation'        # tùy chọn — lọc theo subject
```

**Bắt buộc với Gmail**: phải bật 2-Step Verification, sau đó vào
<https://myaccount.google.com/apppasswords> tạo App Password riêng cho script này.
Dùng Outlook/Yahoo: thay `OTP_IMAP_HOST` thành `outlook.office365.com` /
`imap.mail.yahoo.com` và làm tương tự với app password của họ.

Mặc định script tìm chuỗi 5–8 chữ số liền nhau trong nội dung email. Nếu Facebook
đổi format thì truyền regex riêng:

```bash
python run_with_email_otp.py \
    --form-config example_facebook_trademark.json \
    --otp-config example_facebook_trademark_otp.json \
    --code-regex 'FB code[:\s]*([0-9]{6})'
```

## Sửa config phase 1

Mở `example_facebook_trademark.json` và sửa các giá trị:

```json
"full_name":              "Tên đầy đủ của anh",
"email":                  "địa chỉ trùng với OTP_IMAP_USER",
"email_confirm":          "địa chỉ trùng với OTP_IMAP_USER",
"rights_owner_name":      "Tên chủ sở hữu nhãn hiệu",
"rights_owner_url":       "https://website-cua-anh.com/",
"trademark_name":         "TÊN NHÃN HIỆU",
"trademark_country":      "Vietnam",
"trademark_registration_number": "số đăng ký",
"trademark_registration_url":    "URL đăng ký trên cơ sở dữ liệu trademark",
"infringing_links":       ["URL post vi phạm 1", "URL post vi phạm 2", ...],
"description":            "mô tả chi tiết...",
"electronic_signature":   "phải trùng full_name"
```

> Lưu ý: trường `email` và `email_confirm` **phải trùng** với `OTP_IMAP_USER`,
> nếu không sẽ không lấy được mã xác nhận.

## Chạy

```bash
# 1. Dry-run — kiểm tra mọi field resolve được, không submit, không IMAP
python run_with_email_otp.py \
    --form-config example_facebook_trademark.json \
    --otp-config  example_facebook_trademark_otp.json \
    --dry-run

# 2. Chạy thật, có trình duyệt hiện ra (khuyến nghị lần đầu)
python run_with_email_otp.py \
    --form-config example_facebook_trademark.json \
    --otp-config  example_facebook_trademark_otp.json \
    --debug \
    --screenshot result.png \
    --imap-timeout 300

# 3. Headless (chỉ chạy được nếu form không có CAPTCHA, hoặc anh đã tích hợp service auto-solve)
python run_with_email_otp.py \
    --form-config example_facebook_trademark.json \
    --otp-config  example_facebook_trademark_otp.json \
    --headless --screenshot result.png
```

Trình tự khi chạy thật:

1. Trình duyệt mở form, điền mọi trường.
2. **Nếu phát hiện CAPTCHA** (reCAPTCHA v2 / hCaptcha / Turnstile / image-captcha),
   một banner cam **"⚠ CAPTCHA detected — Solve it in this window, then click Continue"**
   sẽ hiện ở đầu trang. Anh giải CAPTCHA xong, bấm nút **✓ Continue** trên banner →
   script tiếp tục.
3. Script bấm Submit.
4. Phase IMAP: script log `[OTP] waiting for confirmation email since unix-ts ...`
   rồi poll inbox 5 giây/lần (mặc định 180 giây timeout).
5. Khi mã về → script ghi đè `value` của field `confirmation_code` và điền vào
   trang xác nhận → Submit lần 2.
6. Lưu screenshot trang cuối cùng (nếu `--screenshot` được set).

## CAPTCHA

Tool **đã có sẵn** module `captcha.py` phát hiện 5 loại CAPTCHA phổ biến và
inject một banner để anh giải bằng tay. Đây là cách **đáng tin nhất và rẻ nhất**
cho reCAPTCHA v2 / hCaptcha mà Facebook hay dùng — không có service nào auto-solve
được 100% reCAPTCHA v2 mà không bị flag.

Cấu hình CAPTCHA trong `example_facebook_trademark.json`:

```json
"pause_on_captcha": true,        // bật banner
"captcha_timeout":  600          // chờ tối đa 600s anh giải xong
```

Tắt pause bằng `--no-captcha-pause`. **Không khuyên** trừ khi anh chắc chắn form
không có CAPTCHA — nếu auto-submit vào CAPTCHA wall, request sẽ fail.

Nếu anh **thực sự muốn auto-solve** (ví dụ chạy hàng loạt), em có thể thêm
integration với 2Captcha hoặc Anti-Captcha (trả phí, ~$1–3 / 1000 captcha).
Anh xác nhận thì em làm thêm.

## Troubleshooting

| Triệu chứng                                              | Cách xử lý                                                                              |
|----------------------------------------------------------|-----------------------------------------------------------------------------------------|
| `[SKIP] '<field>' — no strategy matched`                 | Mở DevTools, xem field đó tên gì, thêm 1 target nữa (vd `id`, `name`, `aria_label`).   |
| `[IMAP] timed out after 180s waiting for code`           | Tăng `--imap-timeout 600`, kiểm tra `OTP_FROM_FILTER` đúng không, kiểm tra Spam folder.|
| `[IMAP] login/search error`                              | App Password sai hoặc IMAP chưa bật trong cài đặt mail. Gmail: Settings → Forwarding and POP/IMAP → Enable IMAP. |
| `[OTP] message matched filters but no code found`        | Format mã trong email lạ, dùng `--code-regex 'pattern_riêng_(\d+)'`.                   |
| Banner CAPTCHA không hiện                                | CAPTCHA loại lạ. Tắt headless, chạy `--debug` để xem screenshot, mở issue cho em.       |
