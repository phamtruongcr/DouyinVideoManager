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
# Lịch sử video đã tải (SQLite) — xem download_history.py
HISTORY_DB_FILE = Path.home() / ".douyin_video_manager_history.db"
# Thư mục + tên file nhật ký (log) — xem app_logger.py
LOG_DIR = Path.home() / ".douyin_video_manager_logs"
LOG_FILE_NAME = "app.log"
LOG_LEVEL_OPTIONS = ["INFO", "DEBUG"]   # INFO = gọn; DEBUG = chi tiết (khi cần tìm lỗi)
DEFAULT_LOG_LEVEL = "INFO"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# UA di động: dùng khi đọc trang chia sẻ video đơn lẻ của Douyin (iesdouyin.com)
MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
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

# --- Điều kiện khi LẤY DANH SÁCH video của kênh (xem fetch_filters.py) ---
# Thứ tự kết quả: nhãn hiển thị trong ô chọn -> giá trị lưu vào config.
FETCH_ORDER_NEWEST = "newest"
FETCH_ORDER_OLDEST = "oldest"
FETCH_ORDER_OPTIONS = {
    "Mới nhất -> Cũ nhất": FETCH_ORDER_NEWEST,
    "Cũ nhất -> Mới nhất": FETCH_ORDER_OLDEST,
}
DEFAULT_FETCH_ORDER = FETCH_ORDER_NEWEST
DEFAULT_FETCH_MAX_ITEMS = 10   # số video tối đa mỗi lần lấy (để trống/0 = không giới hạn)

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


# --- Review + giọng đọc (kịch bản Gemini -> TTS -> ghép audio) ---
# Các khóa lưu trong file cấu hình (cfg) — đọc qua `read_review_settings(cfg)`:
#   review_script_model     model Gemini viết kịch bản (trống = dùng model mặc định bên dưới;
#                           model dự phòng dùng chung khóa "gemini_fallback_model")
#   review_voice_sample     ĐƯỜNG DẪN file giọng mẫu trên máy (chỉ lưu đường dẫn, không sao
#                           chép/commit file; để trống = chưa chọn)
#   review_words_per_second tốc độ đọc (từ/giây) dùng để ước lượng số từ theo độ dài video
#                           (tự hiệu chỉnh sau mỗi lần Tạo giọng nói; review_wps_samples = số lần đã đo)
#   review_style            phong cách lời đọc (xem REVIEW_STYLE_OPTIONS)
#   review_style_prompt     hướng dẫn tự điền, chỉ dùng khi review_style = "custom"
DEFAULT_REVIEW_SCRIPT_MODEL = DEFAULT_GEMINI_MODEL
DEFAULT_REVIEW_WORDS_PER_SECOND = 4.1   # đo thực tế với giọng VieNeu: 86 từ đọc hết 18,2 giây ở tốc độ 1,15×
MIN_REVIEW_WORDS_PER_SECOND = 1.0
MAX_REVIEW_WORDS_PER_SECOND = 6.0

REVIEW_STYLE_KOC = "koc"
REVIEW_STYLE_NATURAL = "natural"
REVIEW_STYLE_LIVELY = "lively"
REVIEW_STYLE_EXPERT = "expert"
REVIEW_STYLE_HUMOR = "humor"
REVIEW_STYLE_CUSTOM = "custom"
DEFAULT_REVIEW_STYLE = REVIEW_STYLE_KOC
REVIEW_STYLE_OPTIONS = {
    "KOC review đời thường (TikTok/Reels)": REVIEW_STYLE_KOC,
    "Tự nhiên, kể chuyện": REVIEW_STYLE_NATURAL,
    "Hào hứng, thu hút": REVIEW_STYLE_LIVELY,
    "Chuyên gia, đáng tin": REVIEW_STYLE_EXPERT,
    "Hài hước, gần gũi": REVIEW_STYLE_HUMOR,
    "Tự điền hướng dẫn": REVIEW_STYLE_CUSTOM,
}
# Gợi ý phong cách đưa vào prompt (phần "yêu cầu bổ sung" của người dùng).
REVIEW_STYLE_HINTS = {
    # KOC: phần hướng dẫn chi tiết nằm trong system instruction (review_script.py),
    # ở đây chỉ để dòng mô tả ngắn cho đủ bảng.
    REVIEW_STYLE_KOC: "Giọng KOC/Reviewer đời thường: gần gũi, nhiệt tình, ngôn ngữ nói tự nhiên của người Việt.",
    REVIEW_STYLE_NATURAL: "Giọng kể tự nhiên, như người thật đang kể lại cho bạn bè nghe.",
    REVIEW_STYLE_LIVELY: "Giọng hào hứng, nhịp nhanh, tạo tò mò và muốn xem tiếp.",
    REVIEW_STYLE_EXPERT: "Giọng điềm đạm, như chuyên gia nhận xét khách quan, có căn cứ từ video.",
    REVIEW_STYLE_HUMOR: "Giọng hài hước, gần gũi, dí dỏm vừa phải, không lố.",
}


# --- Thông tin đầu vào của prompt KOC (xem review_script.build_system_instruction) ---
# Khóa trong cfg: review_product, review_duration, review_use_video_len,
# review_address_terms, review_slang
DEFAULT_REVIEW_PRODUCT = ""
DEFAULT_REVIEW_DURATION = 15          # giây — thời lượng mục tiêu của lời đọc
MIN_REVIEW_DURATION = 5
MAX_REVIEW_DURATION = 120
DEFAULT_REVIEW_ADDRESS_TERMS = "Bác nào, Bà con, Các chị em"
DEFAULT_REVIEW_SLANG = (
    "chân ái, nhàn tênh, nhàn cái thân, trong một nốt nhạc, ngon ơ, chốt ngay, rinh ngay"
)


# --- Đọc giọng (TTS) — xem tts_local.py ---
# Khóa trong cfg: tts_backend, tts_gemini_model, tts_gemini_voice, tts_vieneu_voice
# (giọng mẫu để nhân bản giọng dùng chung khóa "review_voice_sample").
TTS_BACKEND_VIENEU = "vieneu"
TTS_BACKEND_GEMINI = "gemini"
DEFAULT_TTS_BACKEND = TTS_BACKEND_VIENEU
TTS_BACKEND_OPTIONS = {
    "VieNeu-TTS (chạy trên máy, hỗ trợ giọng mẫu)": TTS_BACKEND_VIENEU,
    "Gemini TTS (online, cần API Key)": TTS_BACKEND_GEMINI,
}
DEFAULT_TTS_GEMINI_MODEL = "gemini-2.5-flash-preview-tts"
DEFAULT_TTS_GEMINI_VOICE = "Kore"
GEMINI_TTS_VOICES = [
    "Kore", "Puck", "Zephyr", "Charon", "Fenrir", "Leda", "Orus", "Aoede",
    "Callirrhoe", "Autonoe", "Enceladus", "Iapetus", "Umbriel", "Algieba",
    "Despina", "Erinome", "Algenib", "Rasalgethi", "Laomedeia", "Achernar",
    "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird", "Zubenelgenubi",
    "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat",
]
TTS_PREVIEW_MAX_WORDS = 25   # nghe thử: chỉ đọc câu đầu, tối đa chừng này từ


def _to_float(value, default: float) -> float:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return default
    # NaN / vô cực -> dùng mặc định
    return x if x == x and x not in (float("inf"), float("-inf")) else default


def read_review_settings(cfg: dict) -> dict:
    """Đọc + chuẩn hoá cấu hình Review từ `cfg` (giá trị lạ/hỏng quay về mặc định,
    không bao giờ ném lỗi). Trả về dict:
      script_model, voice_sample, words_per_second, style, style_prompt, style_hint
    `style_hint` là câu hướng dẫn phong cách sẵn sàng đưa vào prompt (rỗng nếu
    style = custom mà chưa điền prompt)."""
    cfg = cfg if isinstance(cfg, dict) else {}

    model = str(cfg.get("review_script_model") or "").strip() or DEFAULT_REVIEW_SCRIPT_MODEL
    voice = str(cfg.get("review_voice_sample") or "").strip()

    wps = _to_float(cfg.get("review_words_per_second"), DEFAULT_REVIEW_WORDS_PER_SECOND)
    wps = max(MIN_REVIEW_WORDS_PER_SECOND, min(MAX_REVIEW_WORDS_PER_SECOND, wps))

    style = cfg.get("review_style")
    if style not in REVIEW_STYLE_OPTIONS.values():
        style = DEFAULT_REVIEW_STYLE
    style_prompt = str(cfg.get("review_style_prompt") or "").strip()
    hint = style_prompt if style == REVIEW_STYLE_CUSTOM else REVIEW_STYLE_HINTS.get(style, "")

    return {
        "script_model": model,
        "voice_sample": voice,
        "words_per_second": wps,
        "style": style,
        "style_prompt": style_prompt,
        "style_hint": hint,
    }


# --- Cài đặt giọng đọc (thanh trượt ở cột phải của tab Kịch bản & Giọng đọc) ---
# Khóa trong cfg: tts_stability, tts_speed, tts_pause_enabled, tts_pause_sentence,
# tts_pause_comma, tts_autofit
#   stability : 0 (biểu cảm) .. 5 (ổn định). Đổi thành "temperature" của model:
#               ổn định cao -> temperature thấp -> đọc đều, ít biến thiên.
#   speed     : hệ số tốc độ (atempo của ffmpeg), 1.0 = bình thường.
#   pause_*   : giây im lặng chèn sau dấu kết câu (. ! ? …) / sau dấu phẩy (, ; :).
#   autofit   : tự chỉnh tốc độ để audio khớp đúng thời lượng mục tiêu.
MIN_TTS_STABILITY, MAX_TTS_STABILITY, DEFAULT_TTS_STABILITY = 0.0, 5.0, 2.8
MIN_TTS_SPEED, MAX_TTS_SPEED, DEFAULT_TTS_SPEED = 0.5, 1.5, 1.0
MIN_TTS_PAUSE, MAX_TTS_PAUSE_SENTENCE, MAX_TTS_PAUSE_COMMA = 0.0, 1.5, 0.6
DEFAULT_TTS_PAUSE_SENTENCE = 0.35
DEFAULT_TTS_PAUSE_COMMA = 0.15
AUTOFIT_MIN_SPEED, AUTOFIT_MAX_SPEED = 0.8, 1.5   # giới hạn khi tự chỉnh để khớp thời lượng
TTS_HISTORY_MAX = 100


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def read_tts_settings(cfg: dict) -> dict:
    """Đọc + chuẩn hoá cài đặt giọng đọc từ `cfg` (giá trị hỏng -> mặc định, không ném lỗi)."""
    cfg = cfg if isinstance(cfg, dict) else {}

    def flag(key: str, default: bool) -> bool:
        v = cfg.get(key, default)
        return v if isinstance(v, bool) else default

    return {
        "stability": _clamp(_to_float(cfg.get("tts_stability"), DEFAULT_TTS_STABILITY),
                            MIN_TTS_STABILITY, MAX_TTS_STABILITY),
        "speed": _clamp(_to_float(cfg.get("tts_speed"), DEFAULT_TTS_SPEED),
                        MIN_TTS_SPEED, MAX_TTS_SPEED),
        "pause_enabled": flag("tts_pause_enabled", True),
        "pause_sentence": _clamp(_to_float(cfg.get("tts_pause_sentence"), DEFAULT_TTS_PAUSE_SENTENCE),
                                 MIN_TTS_PAUSE, MAX_TTS_PAUSE_SENTENCE),
        "pause_comma": _clamp(_to_float(cfg.get("tts_pause_comma"), DEFAULT_TTS_PAUSE_COMMA),
                              MIN_TTS_PAUSE, MAX_TTS_PAUSE_COMMA),
        "autofit": flag("tts_autofit", False),
    }


def read_koc_brief(cfg: dict) -> dict:
    """Đọc thông tin đầu vào của prompt KOC (sản phẩm, thời lượng, xưng hô, từ khóa)."""
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        duration = int(float(cfg.get("review_duration", DEFAULT_REVIEW_DURATION)))
    except (TypeError, ValueError, OverflowError):
        duration = DEFAULT_REVIEW_DURATION
    duration = max(MIN_REVIEW_DURATION, min(MAX_REVIEW_DURATION, duration))
    use_len = cfg.get("review_use_video_len", False)
    return {
        "product": str(cfg.get("review_product") or DEFAULT_REVIEW_PRODUCT).strip(),
        "duration": duration,
        "use_video_len": use_len if isinstance(use_len, bool) else False,
        "address_terms": str(cfg.get("review_address_terms") or DEFAULT_REVIEW_ADDRESS_TERMS).strip(),
        "slang": str(cfg.get("review_slang") or DEFAULT_REVIEW_SLANG).strip(),
    }


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