# Douyin Video Manager

Ứng dụng desktop (Windows & macOS, giao diện Tkinter tối màu) để **lấy danh sách video của một kênh, chọn và tải hàng loạt**, dịch tiêu đề sang tiếng Việt bằng Gemini, rồi **chèn chữ / ghép audio** vào video bằng ffmpeg.

Hỗ trợ ba nền tảng: **Douyin**, **TikTok** và **Facebook** (video thường, Reel, fb.watch, trang/kênh).

> ⚠️ Chỉ tải và sử dụng lại nội dung khi bạn có quyền (nội dung của chính bạn hoặc đã được chủ sở hữu cho phép). Bạn tự chịu trách nhiệm tuân thủ điều khoản của từng nền tảng và quy định bản quyền.

## Tính năng

### Tab "Tải video"
- **Dán link kênh hoặc link video** (một hay nhiều link, mỗi link một dòng). Có thể dán cả đoạn text lộn xộn — app tự trích link sạch và nhận diện nền tảng.
- **Bảng video có checkbox**: chọn/bỏ chọn, sửa tiêu đề, xóa khỏi danh sách (chỉ xóa trong app, không xóa video trên nền tảng), xem log từng video.
- **Điều kiện lọc khi lấy danh sách**: khoảng ngày đăng, thứ tự (mới → cũ hoặc cũ → mới), lượt xem tối thiểu, lượt tym tối thiểu, số video tối đa.
- **Tải hàng loạt song song** (1–8 luồng), có nút dừng tải ngay cả khi video đang tải dở, tải từng video riêng lẻ.
- **Dịch tiêu đề bằng Gemini**: dịch nguyên văn, viết lại thành tiêu đề ngắn kiểu Facebook, hoặc dùng prompt riêng do bạn soạn. Dịch theo lô, chạy song song, có model dự phòng.
- **Lịch sử tải**: ghi nhớ video đã tải (xem bên dưới).
- **Xuất danh sách** ra TXT hoặc Excel.
- **Lấy danh sách Douyin/TikTok/Facebook bằng trình duyệt ẩn (Playwright)**: app mở Chrome/Edge có sẵn trên máy (hoặc tự tải Chromium lần đầu), cuộn trang và bắt các response JSON mà chính trang web gọi, nên không bị lỗi HTTP 403 do thiếu chữ ký như khi gọi API trực tiếp. Nếu Playwright không dùng được hoặc bị chặn, Douyin tự lùi về gọi API trực tiếp, TikTok/Facebook lùi về yt-dlp. Việc tải video của TikTok/Facebook vẫn dùng yt-dlp.
- **Nhật ký (log) ra file**: ghi lại mọi bước lấy danh sách, tải video và lỗi để dễ kiểm tra (xem mục *Nhật ký* bên dưới).
- **Tự cập nhật yt-dlp** ở nền khi mở app (giới hạn tần suất 12 giờ/lần). Bản đóng gói PyInstaller không tự cập nhật được, cần build lại.
- **Tự lấy Cookie từ trình duyệt** (Firefox, Chrome, Edge, Brave, Opera, Vivaldi, Chromium, Safari) hoặc dán thủ công.

### Tab "Kịch bản & Giọng đọc"
Luồng một video: **chọn video** (từ *Lịch sử tải* hoặc một thư mục, hoặc chọn 1 file) → **✨ Tạo kịch bản** (Gemini xem video, viết lời đọc thuần văn bản — không tiêu đề/markdown/emoji/mốc thời gian, số viết thành chữ, giới hạn số từ tự theo độ dài video) → kịch bản hiện trong ô soạn thảo, **sửa tự do** → **🎙 Đọc & lưu mp3** → lưu `<tên video>.mp3` vào **Thư mục Audio của tab Ghép Audio**, để tab đó tự ghép cặp theo tên file.
- **Prompt KOC/Reviewer** (phong cách mặc định): nhập **Sản phẩm**, **Thời lượng mục tiêu** (mặc định 15 giây, hoặc tick *Theo độ dài video*), tuỳ chỉnh **Xưng hô** ("Bác nào", "Bà con", "Các chị em"...) và **Từ khóa review** ("chân ái", "nhàn tênh", "ngon ơ", "chốt ngay", "rinh ngay"...). Kịch bản theo cấu trúc Hook → 2-3 tính năng → chốt đơn. App tự tính số từ cần viết từ thời lượng, tốc độ đọc và ngắt nghỉ; nếu Gemini viết lệch khoảng cho phép thì tự nhờ viết lại 1 lần (thay vì cắt cụt đoạn kết). Ô **Ghi chú / tính năng** để dán thêm tính năng sản phẩm.
- **Cài đặt giọng đọc** (cột phải, tab *Cài đặt*): **Độ ổn định giọng** (biểu cảm ↔ ổn định, đổi thành temperature của model), **Tốc độ đọc** (0.5×–1.5×, ffmpeg `atempo`, giữ cao độ), **Tự chỉnh tốc độ để khớp thời lượng mục tiêu**, **Ngắt nghỉ** theo dấu câu (sau câu / sau dấu phẩy, chỉnh bằng giây; Gemini TTS chỉ ngắt theo câu để đỡ tốn quota). Tab *Lịch sử* liệt kê các file audio đã lưu (double-click để phát).
- Backend giọng đọc: **VieNeu-TTS** (chạy trên máy, nhân bản giọng từ *file giọng mẫu*) hoặc **Gemini TTS** (online; chọn giọng dựng sẵn, không dùng giọng mẫu).
- **Giọng đã lưu**: chọn file giọng mẫu rồi bấm **💾 Lưu giọng này** để đặt tên; file được sao chép vào `~/.douyin_video_manager_voices/` nên không mất khi xóa file gốc. Chọn lại giọng trong ô *Giọng đã lưu*; **🗑 Xóa giọng** chỉ xóa bản sao của app.
- **Thư mục lưu audio** chọn ngay trên tab (dùng chung với tab Ghép Audio), có nút *Mở thư mục*.
- **🔊 Nghe thử 1 câu** (câu đầu của kịch bản), thanh trạng thái, nút **■ Dừng**.
- Cần Gemini API Key (Cài đặt) để tạo kịch bản. Có ffmpeg thì lưu mp3; không có thì lưu `.wav`.
- VieNeu là tùy chọn: `pip install vieneu` (Python 3.10+). Lần đầu chạy sẽ tải model.

### Tab "Ghép Audio vào Video" (cần ffmpeg)
- Ghép audio vào hàng loạt video: khớp theo tên file hoặc trộn ngẫu nhiên.
- Chọn độ phân giải, chất lượng (CRF), tỉ lệ khung hình (16:9, 9:16, 1:1, 4:5, tùy chỉnh) và cách xử lý phần dư (crop / nền mờ / viền đen).
- Chuẩn hóa âm lượng, fade, trộn thêm nhạc nền, giữ một phần audio gốc.
- **Trình chỉnh sửa chữ kiểu CapCut**: nhiều lớp chữ, kéo thả trên khung xem trước, timeline thời gian hiện từng lớp, mẫu kiểu chữ dựng sẵn, và lớp **blur** để che phụ đề/logo cũ.
- Xem thử một đoạn ngắn trước khi xuất.

### Lịch sử tải (chống tải trùng)
Mỗi video tải thành công được ghi vào một file SQLite theo cặp *(nền tảng, ID video)*. Nhờ đó:

- Khi lấy lại danh sách của cùng một kênh, video đã tải hiện trạng thái **"Đã tải trước đó"** (xem cột Log để biết thời điểm và đường dẫn file; app cũng báo nếu file không còn ở vị trí cũ).
- Khi bấm **"Tải video đã chọn"** mà trong số đó có video đã tải, app hỏi: **Có** = bỏ qua video đã tải · **Không** = tải lại tất cả · **Hủy** = không làm gì.
- Bấm tải riêng một video luôn tải, không hỏi.
- Quản lý tại **Cài đặt → 📜 Lịch sử tải** (xem số lượng, xóa toàn bộ lịch sử). Xóa lịch sử **không xóa** file video trên máy.

Vị trí file lịch sử: `~/.douyin_video_manager_history.db`.

## Yêu cầu

- **Python 3.9+** (khuyến nghị bản có sẵn Tkinter: cài từ python.org, hoặc `brew install python-tk` trên macOS; trên Ubuntu/Debian: `sudo apt install python3-tk`).
- **ffmpeg + ffprobe** nếu dùng tab ghép audio / chèn chữ, hoặc tải Facebook chất lượng cao. Tải tại [ffmpeg.org](https://ffmpeg.org) (Windows: [gyan.dev](https://www.gyan.dev/ffmpeg/builds/); macOS: `brew install ffmpeg`). Có thể chọn đường dẫn ffmpeg trong app.
- **Gemini API key** (miễn phí) nếu muốn dịch tiêu đề: lấy tại [aistudio.google.com/apikey](https://aistudio.google.com/apikey).

## Cài đặt và chạy từ mã nguồn

```bash
git clone https://github.com/phamtruongcr/DouyinVideoManager.git
cd DouyinVideoManager

python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt
python main.py
```

Thư viện chính: `requests`, `openpyxl`, `Pillow` (tính ngắt dòng chữ chính xác hơn), `yt-dlp[default,curl-cffi]` (TikTok & Facebook), `playwright` (lấy danh sách kênh bằng trình duyệt ẩn; không bắt buộc), `pyinstaller` (đóng gói).

## Đóng gói thành ứng dụng

| Hệ điều hành | Lệnh | Kết quả |
|---|---|---|
| Windows | chạy `build_windows.bat` | `dist\DouyinVideoManager.exe` (1 file) |
| macOS | `chmod +x build_mac.sh && ./build_mac.sh` | `dist/DouyinVideoManager.app` và file `.zip` |

Lần đầu mở app trên macOS: chuột phải → *Open*, hoặc chạy `xattr -cr dist/DouyinVideoManager.app`.

## Hướng dẫn sử dụng nhanh

1. **Cài đặt → Quản lý Cookie**: với Douyin, cần Cookie của một phiên trình duyệt đã vào douyin.com (không cần đăng nhập tài khoản). Cách dễ nhất: chọn trình duyệt/profile rồi bấm **Lấy Cookie ngay**, hoặc bật **Tự lấy lại mỗi lần Lấy danh sách**. Firefox đọc ổn định nhất; Chrome/Edge bản mới trên Windows có thể không đọc được — khi đó hãy dán Cookie thủ công. TikTok và Facebook thường không bắt buộc Cookie, trừ kênh bị chặn hoặc video riêng tư.
2. (Tùy chọn) **Cài đặt → Cấu hình Gemini API**: dán API key, bấm tải danh sách model và chọn model chính + dự phòng.
3. Chọn **thư mục lưu** trên thanh công cụ chính.
4. Dán link kênh hoặc link video → **Lấy danh sách video** (đặt điều kiện lọc nếu cần).
5. Tick chọn video → (tùy chọn) **Dịch tiêu đề** → **Tải video đã chọn**.
6. Chuyển sang tab **Ghép Audio vào Video** nếu cần chèn chữ/ghép nhạc.

## Nhật ký (log)

App ghi log vào `~/.douyin_video_manager_logs/app.log` (tự xoay vòng: 1 MB × 5 file cũ). Log gồm thông tin môi trường lúc mở app (phiên bản Python, yt-dlp, Playwright; **tên** các mục Cookie, không có giá trị), từng bước lấy danh sách (Playwright bắt được response nào, HTTP bao nhiêu, bao nhiêu video; API trực tiếp trả gì), kết quả từng lần tải và toàn bộ lỗi kèm traceback.

- **Cookie, msToken, a_bogus, API key... được tự động che** (`<đã che>`) trước khi ghi, nên có thể gửi file log cho người khác xem lỗi.
- Vào **Cài đặt → 🧾 Nhật ký** để: đổi mức chi tiết (INFO gọn / DEBUG chi tiết hơn), mở thư mục/file log, **sao chép 200 dòng cuối** vào clipboard (tiện dán gửi đi), hoặc xóa log.
- Khi gặp lỗi: chuyển sang DEBUG → tái hiện lỗi → gửi 200 dòng cuối của log.

## Cấu hình và dữ liệu cá nhân

| File | Nội dung |
|---|---|
| `~/.douyin_video_manager.json` | Cài đặt app, **Cookie** và **Gemini API key** (lưu dạng văn bản thường) |
| `~/.douyin_video_manager_history.db` | Lịch sử video đã tải (SQLite) |
| `~/.douyin_video_manager_logs/app.log` | Nhật ký hoạt động (đã che Cookie/token) |

Không commit, không chia sẻ các file này. `.gitignore` của dự án đã loại trừ chúng cùng các file video/audio.

## Xử lý sự cố

- **Douyin báo HTTP 403 / không lấy được danh sách**: gọi API trực tiếp của Douyin cần chữ ký động (`a_bogus`, `msToken`) nên thường bị 403. App đã ưu tiên trình duyệt ẩn (Playwright) để trình duyệt tự tạo chữ ký. Hãy: (1) cài Playwright (`pip install playwright`) và có Chrome/Edge trên máy; (2) cập nhật Cookie trong Cài đặt (Firefox đọc ổn định nhất); (3) nếu log ghi *"yêu cầu xác minh (captcha)"*, mở douyin.com bằng trình duyệt thường, vượt xác minh rồi lấy lại Cookie. Xem file log để biết chính xác bước nào lỗi. Nếu cả hai cách đều hỏng, tham khảo phương án dự phòng là tự host [Douyin_TikTok_Download_API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API) và đổi hàm `fetch_user_posts_page` trong `douyin_manager/douyin_client.py` để trỏ sang backend đó.
- **TikTok / Facebook lỗi hoặc không liệt kê được video**: cập nhật yt-dlp bằng `pip install -U yt-dlp` (khi chạy từ mã nguồn app đã tự làm việc này ở nền). Nếu dùng Playwright mà lỗi, thử cài Chrome/Edge hoặc chạy `playwright install chromium`. Với Facebook, nếu không liệt kê được cả trang, hãy dán từng link video (mỗi link một dòng).
- **Dịch lỗi 429**: Gemini giới hạn tần suất. Giảm số lô dịch song song hoặc chọn model dự phòng.
- **Không ghép được audio**: kiểm tra đã cài cả `ffmpeg` lẫn `ffprobe` và nằm trong PATH (hoặc chọn đường dẫn trong app).
- **Lịch sử tải không hoạt động**: nếu file lịch sử bị hỏng hoặc không ghi được, app vẫn tải bình thường; thông báo lỗi hiện trong Cài đặt → Lịch sử tải. Có thể xóa file `.douyin_video_manager_history.db` để tạo mới.

## Cấu trúc mã nguồn

```
main.py                         Điểm khởi chạy
douyin_manager/
  config.py                     Hằng số + đọc/ghi cấu hình
  utils.py                      Trích link, nhận diện nền tảng, tên file an toàn
  fetch_filters.py              Bộ lọc khi lấy danh sách (ngày, lượt xem, tym...)
  douyin_client.py              Gọi API Douyin, tải video
  tiktok_client.py              TikTok (danh sách: Playwright/yt-dlp; tải: yt-dlp)
  facebook_client.py            Facebook (danh sách: Playwright/yt-dlp; tải: yt-dlp)
  browser_sniffer.py            Trình duyệt ẩn Playwright, bắt JSON API khi cuộn trang
  ytdlp_updater.py              Tự cập nhật yt-dlp bằng pip
  browser_cookies.py            Đọc Cookie từ trình duyệt
  gemini_translator.py          Dịch tiêu đề bằng Gemini
  gemini_client.py              Gọi Gemini REST dùng chung (retry, model dự phòng)
  review_script.py              Gemini viết kịch bản review + lọc kết quả (số -> chữ, đếm từ)
  tts_local.py                  Đọc giọng: VieNeu + Gemini TTS, PCM -> WAV -> mp3
  voice_library.py              Thư viện giọng mẫu đã lưu (sao chép file vào thư mục riêng)
  script_voice_gui.py           Tab "Kịch bản & Giọng đọc"
  download_history.py           Lịch sử video đã tải (SQLite)
  app_logger.py                 Ghi log ra file (xoay vòng, tự che Cookie/token)
  audio_merger.py               Logic ffmpeg ghép audio / chèn chữ / blur
  text_editor.py                Trình chỉnh sửa chữ kiểu CapCut
  gui.py                        Cửa sổ chính + tab "Tải video"
  audio_merge_gui.py            Tab "Ghép Audio vào Video"
  theme.py, widgets.py, ui_icons.py   Giao diện dùng chung
tests/                          Unit test
assets/                         Icon ứng dụng
```

## Chạy test

```bash
python -m unittest discover -s tests -v
```

Các module `fetch_filters.py`, `utils.py`, `download_history.py`, `audio_merger.py` không phụ thuộc giao diện nên dễ viết thêm test.
