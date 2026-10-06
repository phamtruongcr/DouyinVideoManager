#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Douyin Video Manager
=====================
Công cụ desktop (chạy được trên Windows & macOS) để:
  1. Nhận link kênh Douyin (hoặc đoạn text lộn xộn chứa link) -> tự động
     trích xuất link Douyin sạch.
  2. Lấy danh sách video của kênh, hiển thị dạng bảng có checkbox.
  3. Tick chọn để TẢI hàng loạt, hoặc XÓA khỏi danh sách (bỏ những video
     không muốn tải, không phải xóa video trên Douyin).

Yêu cầu: Python 3.9+ (Tkinter đi kèm sẵn trong Python chuẩn trên cả
Windows và macOS), thư viện `requests`.

LƯU Ý QUAN TRỌNG VỀ KỸ THUẬT:
Douyin không cung cấp API công khai chính thức để lấy danh sách video
của một kênh. App này gọi trực tiếp endpoint mà chính trang web
douyin.com dùng khi bạn duyệt trang cá nhân của 1 tác giả
(aweme/v1/web/aweme/post/). Để endpoint này trả dữ liệu ổn định, Douyin
yêu cầu Cookie của một phiên trình duyệt đã từng truy cập
douyin.com (không cần đăng nhập tài khoản, chỉ cần cookie "khách" hợp
lệ). Xem hướng dẫn lấy Cookie trong README.md đi kèm.

Nếu Douyin thay đổi cơ chế chống bot (a_bogus/msToken) khiến endpoint
này ngừng hoạt động, hãy xem phần "Phương án dự phòng" trong README.md
(tự host Douyin_TikTok_Download_API của Evil0ctal) và chỉ cần đổi
API_BASE / hàm fetch_user_posts trong douyin_manager/douyin_client.py
để trỏ sang backend đó.

--------------------------------------------------------------------
Cấu trúc source code (đã tách nhỏ để dễ đọc / dễ sửa):
  main.py                          - file này, chỉ để khởi chạy app
  douyin_manager/config.py         - hằng số cấu hình + đọc/ghi config
  douyin_manager/utils.py          - trích xuất link, format ngày, tên file
  douyin_manager/gemini_translator.py - dịch tiêu đề qua Gemini API
  douyin_manager/douyin_client.py  - gọi API Douyin, tải video
  douyin_manager/theme.py          - giao diện tối dùng chung (bảng màu + ttk.Style)
  douyin_manager/widgets.py        - widget Tkinter tùy chỉnh (WrapFrame, thẻ, tab viên thuốc...)
  douyin_manager/gui.py            - cửa sổ chính (class DouyinApp) + tab "Tải video Douyin"
  douyin_manager/audio_merge_gui.py - tab "Ghép Audio vào Video" (class AudioMergeTab)
  douyin_manager/audio_merger.py   - logic ffmpeg ghép audio (không phụ thuộc GUI)
--------------------------------------------------------------------
"""

from __future__ import annotations

try:
    import requests  # noqa: F401  (kiểm tra sớm, báo lỗi rõ ràng nếu thiếu)
except ImportError:
    raise SystemExit(
        "Thiếu thư viện 'requests'. Hãy chạy: pip install requests"
    )

from douyin_manager.gui import DouyinApp


def main():
    app = DouyinApp()
    app.mainloop()


if __name__ == "__main__":
    main()
