"""
config.py
=========
Hằng số cấu hình chung của ứng dụng + hàm đọc/ghi file config
(~/.douyin_video_manager.json) lưu cookie, Gemini API key, thư mục tải...
"""

from __future__ import annotations

import json
from pathlib import Path

APP_TITLE = "Douyin Video Manager"
CONFIG_FILE = Path.home() / ".douyin_video_manager.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

REQUEST_TIMEOUT = 15
PAGE_COUNT = 20          # số video mỗi lần gọi API
REQUEST_DELAY = 0.6      # giãn cách giữa các request để tránh bị chặn

# --- Cấu hình dịch tiêu đề bằng Gemini API ---
# Model chính và model dự phòng CHỌN ĐƯỢC trong mục Cài đặt (combobox có
# thể tự gõ tên model khác). Model dự phòng để trống ("") = tắt fallback,
# chỉ dùng đúng 1 model chính.
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
DEFAULT_GEMINI_FALLBACK_MODEL = "gemini-2.5-flash-lite"
# Danh sách gợi ý hiển thị trong combobox chọn model (người dùng vẫn gõ
# được tên model khác ngoài danh sách này).
GEMINI_MODEL_SUGGESTIONS = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-pro",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
]

# Số lô (mỗi lô tối đa 40 tiêu đề) được dịch SONG SONG cùng lúc. Tăng lên
# giúp dịch nhanh hơn khi danh sách dài, nhưng dễ chạm giới hạn tần suất
# (429 RPM) hơn nếu quá cao. Người dùng chỉnh được trong Cài đặt.
DEFAULT_GEMINI_BATCH_WORKERS = 3
MIN_GEMINI_BATCH_WORKERS = 1
MAX_GEMINI_BATCH_WORKERS = 8

# --- Kiểu dịch tiêu đề ---
# "literal": dịch nguyên văn sang tiếng Việt (bỏ hashtag, giữ emoji).
# "facebook": dịch rồi VIẾT LẠI thành 1 tiêu đề ngắn gọn (~10 từ) phù hợp
#   đăng Facebook — mỗi tiêu đề phải khác nhau về giọng điệu/cách mở đầu
#   khi dịch nhiều tiêu đề cùng lúc (tránh lặp công thức).
TRANSLATE_STYLE_LITERAL = "literal"
TRANSLATE_STYLE_FACEBOOK = "facebook"
# "custom": người dùng TỰ ĐIỀN prompt (hướng dẫn) để dịch/viết lại tiêu đề
#   theo ý mình (giọng điệu, độ dài, ngách nội dung...). App tự ghép thêm
#   phần định dạng bắt buộc (đánh số dòng) để vẫn tách kết quả chính xác.
TRANSLATE_STYLE_CUSTOM = "custom"
DEFAULT_TRANSLATE_STYLE = TRANSLATE_STYLE_LITERAL
TRANSLATE_STYLE_OPTIONS = {
    "Dịch nguyên văn (bỏ hashtag)": TRANSLATE_STYLE_LITERAL,
    "Dịch & viết lại tiêu đề cho Facebook (~10 từ)": TRANSLATE_STYLE_FACEBOOK,
    "Tự điền prompt riêng (tùy chỉnh)": TRANSLATE_STYLE_CUSTOM,
}

# Prompt mẫu hiển thị sẵn trong khung soạn prompt (người dùng sửa tùy ý).
# Có thể dùng {lang} -> tự thay bằng ngôn ngữ đích (mặc định "Tiếng Việt").
DEFAULT_CUSTOM_TITLE_PROMPT = (
    "Dịch tiêu đề video sang {lang}, rồi viết lại thành 1 tiêu đề tự nhiên, "
    "gợi tò mò, khoảng 12 từ, đúng giọng người Việt đăng Facebook. "
    "Bỏ hết hashtag, không dùng dấu ngoặc kép, không thêm emoji nếu bản gốc không có."
)

# --- Cấu hình tải video song song ---
# Số video được TẢI CÙNG LÚC khi bấm "Tải video đã chọn". Tăng lên giúp
# tải nhanh hơn khi có nhiều video, nhưng quá cao dễ bị Douyin/CDN giới
# hạn tốc độ hoặc chặn tạm thời. Người dùng chỉnh được trực tiếp trên
# thanh công cụ chính.
DEFAULT_DOWNLOAD_WORKERS = 3
MIN_DOWNLOAD_WORKERS = 1
MAX_DOWNLOAD_WORKERS = 8

# --- Cấu hình độ dài tên file khi tải video ---
# Số ký tự TỐI ĐA lấy từ tiêu đề để đặt tên file (phần "_{id_video}.mp4"
# luôn được nối thêm vào SAU, không tính vào giới hạn này, để đảm bảo
# tên file không bao giờ trùng nhau giữa 2 video khác nhau). Tăng lên để
# tên file hiển thị đầy đủ tiêu đề hơn; giảm xuống nếu muốn tên file gọn
# hơn khi xem trong Finder/Explorer. Người dùng chỉnh được trên thanh
# công cụ chính.
DEFAULT_FILENAME_MAX_LEN = 60
MIN_FILENAME_MAX_LEN = 10
# Chặn trần ở 120: hầu hết filesystem giới hạn tên file ~255 BYTE, mà mỗi
# ký tự tiếng Việt có dấu chiếm tới 3 byte khi mã hoá UTF-8, cộng thêm
# phần "_{id_video}.mp4" nối phía sau (~25 byte) -> 120 ký tự tiêu đề
# (~360 byte trường hợp xấu nhất) vẫn có rủi ro vượt giới hạn với tiêu đề
# toàn tiếng Việt có dấu, nhưng lỗi đó (nếu có) chỉ ảnh hưởng đúng 1 video
# và được báo trong cột Log, không làm hỏng các video khác.
MAX_FILENAME_MAX_LEN = 120

# --- Cấu hình tính năng "Ghép Audio vào Video" (dùng ffmpeg) ---
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".flv", ".webm", ".m4v", ".ts"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".wma"}

# Chất lượng (độ phân giải) xuất ra — tính theo CẠNH NGẮN của khung hình,
# đúng như CapCut/TikTok: video dọc 9:16 chọn 1080p -> 1080x1920, video
# ngang 16:9 chọn 1080p -> 1920x1080, video vuông chọn 2K -> 1440x1440.
# (Video Douyin đa phần là video DỌC nên KHÔNG ép cứng kiểu "1920x1080" để
# tránh méo hình.) Giá trị None = giữ nguyên độ phân giải gốc.
RESOLUTION_OPTIONS = {
    "Giữ nguyên độ phân giải gốc": None,
    "4K  (2160p)": 2160,
    "2K  (1440p)": 1440,
    "1080p  (Full HD)": 1080,
    "720p  (HD)": 720,
    "540p": 540,
    "480p  (SD)": 480,
    "360p": 360,
}

# Chất lượng video xuất ra, theo CRF (Constant Rate Factor) của libx264 —
# CRF càng THẤP thì chất lượng càng CAO và dung lượng file càng LỚN.
# "Giữ nguyên" = copy nguyên luồng video gốc (không encode lại) -> nhanh
# nhất, không mất chất lượng, nhưng chỉ dùng được khi đồng thời cũng chọn
# "Giữ nguyên kích thước gốc" (đã đổi kích thước thì bắt buộc phải encode
# lại nên không copy được nữa).
QUALITY_OPTIONS = {
    "Giữ nguyên chất lượng gốc (copy, nhanh nhất)": None,
    "Cao (CRF 18, file nặng hơn)": 18,
    "Trung bình (CRF 23, khuyến nghị)": 23,
    "Thấp (CRF 28, file nhẹ hơn)": 28,
}

OUTPUT_FORMAT_OPTIONS = ["mp4", "mkv", "mov"]
DEFAULT_OUTPUT_FORMAT = "mp4"

# Số cặp video/audio ghép SONG SONG cùng lúc. Ghép video tốn CPU nặng hơn
# nhiều so với tải/dịch (encode lại video), nên giới hạn thấp hơn hẳn để
# tránh treo máy khi để quá cao.
DEFAULT_MERGE_WORKERS = 2
MIN_MERGE_WORKERS = 1
MAX_MERGE_WORKERS = 4

# --- Chế độ ghép cặp ---
PAIRING_MODE_FILENAME = "filename"
PAIRING_MODE_RANDOM = "random"
PAIRING_MODE_OPTIONS = {
    "Khớp theo tên file": PAIRING_MODE_FILENAME,
    "Trộn ngẫu nhiên (Random Mix)": PAIRING_MODE_RANDOM,
}

# --- Tỉ lệ khung hình xuất ra (None = giữ nguyên tỉ lệ gốc, không crop/pad) ---
ASPECT_RATIO_OPTIONS = {
    "Giữ nguyên tỉ lệ gốc": None,
    "16:9 (Ngang)": (16, 9),
    "9:16 (Dọc - TikTok/Reels)": (9, 16),
    "1:1 (Vuông)": (1, 1),
    "4:5 (Instagram)": (4, 5),
    "Tùy chỉnh...": "custom",
}

# Cách xử lý phần dư khi tỉ lệ xuất KHÁC tỉ lệ gốc của video
FIT_MODE_OPTIONS = {
    "Cắt cho vừa khung (Crop)": "crop",
    "Thêm nền mờ cho vừa khung (Blur pad — đẹp hơn viền đen)": "pad_blur",
    "Thêm viền đen cho vừa khung (Pad đen)": "pad_black",
}

# --- Chèn chữ (drawtext) lên video: giá trị mặc định của 1 lớp chữ mới ---
# (vị trí là tỉ lệ 0..1 của khung hình, cỡ chữ là % chiều cao khung hình;
# toàn bộ chỉnh sửa được làm bằng trình chỉnh sửa trực quan text_editor.py)
DEFAULT_TEXT_SIZE_PERCENT = 6
MIN_TEXT_SIZE_PERCENT = 1
MAX_TEXT_SIZE_PERCENT = 30
DEFAULT_TEXT_COLOR = "#FFFFFF"
DEFAULT_TEXT_BOX_OPACITY = 60

DEFAULT_LOUDNORM_FILTER = "loudnorm=I=-16:TP=-1.5:LRA=11"
DEFAULT_FADE_SECONDS = 1.0

# --- Nhạc nền: trộn THÊM 1 file nhạc cố định vào audio chính (không thay thế) ---
DEFAULT_BG_MUSIC_VOLUME_PERCENT = 20

# Độ dài bản xem thử (giây). Bản xem thử chỉ được tạo trong thư mục TẠM của
# hệ điều hành và tự xóa khi thoát app — không lưu vào thư mục xuất.
DEFAULT_PREVIEW_SECONDS = 5
MIN_PREVIEW_SECONDS = 2
MAX_PREVIEW_SECONDS = 30
MERGE_HISTORY_MAX = 2000  # số cặp (video,audio) tối đa lưu lại để tránh lặp giữa các lần chạy


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_config(cfg: dict):
    try:
        CONFIG_FILE.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass