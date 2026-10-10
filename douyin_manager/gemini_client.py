"""
gemini_client.py
================
Lớp gọi Gemini REST API DÙNG CHUNG (tách từ gemini_translator.py ở Giai đoạn 0,
KHÔNG đổi hành vi). Dùng cho dịch tiêu đề (gemini_translator) và viết kịch bản
review (review_script).

Xử lý lỗi & retry (áp dụng cho MỖI lời gọi `generate_content`):
  - 503 (server quá tải)         -> retry với backoff; hết retry thì chuyển
                                     sang model dự phòng (nếu có)
  - 429 "RPM" (giới hạn/phút)    -> retry với backoff (ưu tiên Retry-After)
  - 429 "RPD" (giới hạn/ngày)    -> KHÔNG retry ở model này; chuyển sang model
                                     dự phòng; nếu model cuối cũng hết quota
                                     ngày -> GeminiQuotaExceededError (FATAL)
  - 400                          -> GeminiBadRequestError (FATAL, không retry)
  - 401 / 403                    -> GeminiAuthError (FATAL, không retry)
  - Lỗi kết nối / timeout        -> retry với backoff, KHÔNG fatal
  - Mã lỗi khác                  -> RuntimeError nguyên văn, KHÔNG fatal

Không bao giờ log payload hoặc API key.
"""

from __future__ import annotations

import json
import random
import time
from typing import Optional

import requests

from .config import (
    REQUEST_TIMEOUT,
    DEFAULT_GEMINI_MODEL,
    DEFAULT_GEMINI_FALLBACK_MODEL,
)

GEMINI_MAX_RETRIES = 4
GEMINI_RETRY_BASE_DELAY = 2.0  # 2s, 4s, 8s, 16s


class GeminiAuthError(RuntimeError):
    """API key sai / không có quyền truy cập model (401 / 403).
    Không retry, không đổi model (vì cùng 1 key cho mọi model). FATAL."""
    pass


class GeminiBadRequestError(RuntimeError):
    """Request gửi lên sai định dạng (400). Không retry, không đổi model
    (vì lỗi nằm ở payload, không phải ở model hay server). FATAL."""
    pass


class GeminiQuotaExceededError(RuntimeError):
    """Đã hết quota theo NGÀY (429 RPD) ở TẤT CẢ model đã thử (hoặc không
    có model dự phòng để thử thêm). FATAL — không retry nữa vì quota ngày
    chỉ reset sau khi qua ngày mới."""
    pass


def _call_gemini(model: str, payload: dict, api_key: str, timeout: float = REQUEST_TIMEOUT):
    url = (
        f"https://generativelanguage.googleapis.com/"
        f"v1beta/models/{model}:generateContent"
    )

    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
    }

    return requests.post(
        url,
        headers=headers,
        json=payload,
        timeout=timeout,
    )


def _parse_error_body(resp) -> tuple[str, dict]:
    """Đọc message + dict 'error' từ body lỗi JSON của Gemini.
    Fallback về resp.text nếu body không phải JSON hợp lệ."""
    try:
        data = resp.json()
        error = data.get("error", {}) or {}
        message = error.get("message") or resp.text[:500]
        return message, error
    except ValueError:
        return resp.text[:500], {}


def _classify_429(error: dict, message: str) -> str:
    """Phân loại lỗi 429 dựa vào 'details' (QuotaFailure.violations) hoặc
    nội dung message trả về từ Gemini:
      - "RPD": vượt quota theo NGÀY (RequestsPerDay)    -> KHÔNG nên retry
      - "RPM": vượt quota theo PHÚT (RequestsPerMinute) -> nên retry/backoff
      - "UNKNOWN": không xác định được -> xử lý như RPM cho an toàn
        (vẫn retry vài lần trước khi chuyển model, tránh bỏ sót lỗi tạm thời)
    """
    blob = (json.dumps(error.get("details", [])) + " " + message).lower()
    blob = blob.replace(" ", "").replace("_", "").replace("-", "")

    if "perday" in blob or "daily" in blob:
        return "RPD"
    if "perminute" in blob:
        return "RPM"
    return "UNKNOWN"


def _resolve_models(
    model: Optional[str], fallback_model: Optional[str], purpose: str = "dịch"
) -> list[str]:
    """Chuẩn hóa model chính + model dự phòng (có thể để trống) thành 1
    danh sách model sẽ thử lần lượt. Loại bỏ trùng lặp, loại bỏ chuỗi rỗng."""
    primary = (model or DEFAULT_GEMINI_MODEL or "").strip()
    fallback = (fallback_model if fallback_model is not None else DEFAULT_GEMINI_FALLBACK_MODEL) or ""
    fallback = fallback.strip()

    models = [m for m in (primary, fallback) if m]
    # loại trùng, giữ thứ tự
    seen = set()
    unique_models = []
    for m in models:
        if m not in seen:
            seen.add(m)
            unique_models.append(m)
    if not unique_models:
        raise GeminiBadRequestError(f"Chưa chọn Model Gemini nào để {purpose} (mục Cài đặt).")
    return unique_models


def _extract_text(data: dict) -> str:
    """Lấy text từ JSON trả về của Gemini; ném ValueError (kèm lý do chặn nếu có)."""
    candidates = data.get("candidates", [])
    if not candidates:
        reason = (data.get("promptFeedback") or {}).get("blockReason")
        if reason:
            raise ValueError(f"Gemini từ chối xử lý nội dung (blockReason={reason})")
        raise ValueError("Gemini không trả về candidates")
    parts = candidates[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts).strip()


def generate_content(
    payload: dict,
    api_key: str,
    model: Optional[str] = None,
    fallback_model: Optional[str] = None,
    timeout: float = REQUEST_TIMEOUT,
    purpose: str = "dịch",
    extract=None,
):
    """Gửi `payload` (body generateContent đầy đủ: contents, systemInstruction...)
    lên Gemini và trả về text thô, tự retry/backoff và tự chuyển sang model dự
    phòng khi cần (xem docstring module). Dùng chung cho dịch tiêu đề, viết
    kịch bản review và đọc giọng (TTS). KHÔNG log payload/API key (payload có
    thể chứa video). `purpose` chỉ dùng để viết thông báo lỗi (VD "dịch",
    "viết kịch bản"). `extract(data: dict)` (tùy chọn) thay cho cách lấy text
    mặc định — TTS dùng để lấy audio base64; ném ValueError nếu không đọc được."""
    if extract is None:
        extract = _extract_text
    models = _resolve_models(model, fallback_model, purpose)
    last_error: RuntimeError | None = None

    for model_index, cur_model in enumerate(models):
        is_last_model = model_index == len(models) - 1

        for attempt in range(1, GEMINI_MAX_RETRIES + 1):
            try:
                resp = _call_gemini(model=cur_model, payload=payload, api_key=api_key, timeout=timeout)
            except requests.RequestException as exc:
                # Lỗi kết nối / timeout -> coi như lỗi tạm thời, retry backoff
                last_error = RuntimeError(f"Lỗi kết nối tới Gemini ({cur_model}): {exc}")
                if attempt < GEMINI_MAX_RETRIES:
                    delay = GEMINI_RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 1)
                    time.sleep(delay)
                    continue
                break  # hết retry cho model này -> vòng for ngoài sang model kế tiếp

            status_code = resp.status_code

            # ===================== THÀNH CÔNG =====================
            if status_code == 200:
                try:
                    translated = extract(resp.json())
                except (ValueError, KeyError, IndexError, TypeError) as exc:
                    raise RuntimeError(
                        f"Không đọc được kết quả {purpose} từ Gemini: {exc}"
                    ) from exc
                return translated

            message, error = _parse_error_body(resp)

            # ================ 400: request sai định dạng ================
            if status_code == 400:
                raise GeminiBadRequestError(
                    f"Gemini API lỗi 400 (yêu cầu không hợp lệ, model {cur_model}): {message}"
                )

            # ============ 401 / 403: API key sai / không có quyền ============
            if status_code in (401, 403):
                raise GeminiAuthError(
                    f"Gemini API lỗi {status_code} (API Key sai hoặc không có "
                    f"quyền truy cập model '{cur_model}'): {message}. "
                    "Kiểm tra lại API Key trong mục Cài đặt."
                )

            # ==================== 503: server quá tải ====================
            if status_code == 503:
                last_error = RuntimeError(
                    f"Gemini API lỗi 503 (model {cur_model} quá tải): {message}"
                )
                if attempt < GEMINI_MAX_RETRIES:
                    delay = GEMINI_RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 1)
                    time.sleep(delay)
                    continue
                break

            # ========== 429: phân biệt RPM (phút) và RPD (ngày) ==========
            if status_code == 429:
                kind = _classify_429(error, message)

                if kind == "RPD":
                    # Hết quota NGÀY -> retry ở model này vô ích
                    last_error = RuntimeError(
                        f"Gemini API lỗi 429 (hết quota theo NGÀY cho model "
                        f"'{cur_model}'): {message}"
                    )
                    if is_last_model:
                        # Model cuối cùng cũng hết quota ngày -> dừng hẳn ngay,
                        # không retry thêm lần nào nữa
                        raise GeminiQuotaExceededError(
                            "Đã hết quota Gemini API hôm nay ở tất cả model đã "
                            f"thử ({', '.join(models)}). Vui lòng thử lại vào "
                            "ngày mai, đổi Model khác trong Cài đặt, hoặc nâng "
                            f"cấp gói trả phí. Chi tiết lỗi cuối: {last_error}"
                        )
                    break  # sang model dự phòng ngay, không retry thêm

                # kind == "RPM" hoặc "UNKNOWN" -> lỗi tạm thời, retry backoff
                last_error = RuntimeError(
                    f"Gemini API lỗi 429 (vượt giới hạn tần suất/phút, "
                    f"model {cur_model}): {message}"
                )
                retry_after = resp.headers.get("Retry-After")
                if retry_after:
                    try:
                        delay = float(retry_after)
                    except ValueError:
                        delay = GEMINI_RETRY_BASE_DELAY * (2 ** (attempt - 1))
                else:
                    delay = GEMINI_RETRY_BASE_DELAY * (2 ** (attempt - 1))
                delay += random.uniform(0, 1)

                if attempt < GEMINI_MAX_RETRIES:
                    time.sleep(delay)
                    continue
                break

            # ================ Các mã lỗi khác -> dừng ngay ================
            raise RuntimeError(
                f"Gemini API lỗi HTTP {status_code} (model {cur_model}): {message}"
            )

        # hết vòng retry cho model hiện tại -> vòng for ngoài tự chuyển model kế

    raise last_error or RuntimeError("Gemini API lỗi không xác định.")


def list_available_models(api_key: str) -> list[str]:
    """Lấy danh sách TẤT CẢ model Gemini hiện khả dụng cho chính API Key
    này (qua endpoint ListModels chính thức của Gemini API), CHỈ giữ lại
    những model hỗ trợ `generateContent` (dùng để dịch text — 1 số model
    Gemini chỉ hỗ trợ embedding/audio/image, không dùng được cho việc
    dịch). Trả về list tên model đã bỏ tiền tố "models/" (VD
    "gemini-2.5-flash"), sắp xếp theo alphabet, không trùng lặp.

    Dùng để đổ vào combobox chọn Model trong mục Cài đặt, thay vì phải tự
    gõ tay/đoán tên model — vì danh sách model Gemini có thể thay đổi
    (thêm model mới, ngừng hỗ trợ model cũ) mà không cần cập nhật app."""
    api_key = (api_key or "").strip()
    if not api_key:
        raise RuntimeError("Chưa có Gemini API Key.")

    url = "https://generativelanguage.googleapis.com/v1beta/models"
    headers = {"x-goog-api-key": api_key}
    models: list[str] = []
    page_token: Optional[str] = None

    for _ in range(20):  # an toàn: tránh lặp vô hạn nếu API trả pageToken bất thường
        params = {"pageSize": 200}
        if page_token:
            params["pageToken"] = page_token
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            raise RuntimeError(f"Lỗi kết nối tới Gemini: {exc}") from exc

        if resp.status_code in (401, 403):
            message, _ = _parse_error_body(resp)
            raise RuntimeError(
                f"Gemini API Key sai hoặc không có quyền (HTTP {resp.status_code}): {message}"
            )
        if resp.status_code != 200:
            message, _ = _parse_error_body(resp)
            raise RuntimeError(f"Gemini API lỗi HTTP {resp.status_code}: {message}")

        try:
            data = resp.json()
        except ValueError as exc:
            raise RuntimeError(f"Không đọc được danh sách model: {exc}") from exc

        for m in data.get("models", []):
            name = (m.get("name") or "")
            if name.startswith("models/"):
                name = name[len("models/"):]
            methods = m.get("supportedGenerationMethods") or []
            if name and "generateContent" in methods:
                models.append(name)

        page_token = data.get("nextPageToken")
        if not page_token:
            break

    return sorted(set(models))
