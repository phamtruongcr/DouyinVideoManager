"""Tao assets/app_icon.ico (da kich thuoc) cho Douyin Video Manager.
Chay: python make_icon.py   (can Pillow). Muon dung icon rieng: chep file
.ico cua ban de de len assets/app_icon.ico, build_windows.bat se dung no."""
from pathlib import Path
from PIL import Image, ImageDraw

S = 1024
out = Path(__file__).parent / "assets"
out.mkdir(exist_ok=True)

img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# Nen bo goc, mau trung voi theme toi cua app
d.rounded_rectangle((32, 32, S - 32, S - 32), radius=230, fill="#14161b", outline="#2f6fdc", width=28)

# Hinh tron xanh chua nut play
cx, cy, r = S // 2, 430, 270
d.ellipse((cx - r, cy - r, cx + r, cy + r), fill="#2563eb")
d.polygon([(cx - 85, cy - 140), (cx - 85, cy + 140), (cx + 150, cy)], fill="#ffffff")

# Mui ten tai xuong + khay
ax = S // 2
d.polygon([(ax - 110, 790), (ax + 110, 790), (ax, 890)], fill="#22c55e")
d.rounded_rectangle((ax - 230, 905, ax + 230, 940), radius=17, fill="#22c55e")

img.resize((256, 256), Image.LANCZOS).save(out / "app_icon.png")
img.save(out / "app_icon.icns")  # icon cho macOS
img.save(out / "app_icon.ico", sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16)])
print("Da tao", out / "app_icon.ico")
