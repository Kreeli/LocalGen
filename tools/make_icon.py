"""按给定尺寸重绘 LocalGen 图标并写成多尺寸 .ico（快捷方式 / exe 用）。

托盘那个 32px 图标放大到 256 会糊，所以这里照同一套视觉（圆角蓝底 + 白色 LG）
按目标尺寸重新绘制，每个尺寸都清晰。
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

from PIL import Image  # noqa: E402
from PySide6.QtCore import QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

SIZES = [16, 24, 32, 48, 64, 128, 256]
BLUE = "#3d5bd9"


def draw(size):
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(BLUE))
    inset = size * 0.06
    radius = size * 0.25
    painter.drawRoundedRect(QRectF(inset, inset, size - 2 * inset, size - 2 * inset),
                            radius, radius)
    painter.setPen(QColor("white"))
    font = QFont("Segoe UI")
    font.setBold(True)
    font.setPixelSize(max(7, int(size * 0.42)))
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "LG")
    painter.end()
    return pixmap


def main():
    QApplication([])
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "assets/localgen.ico")
    out.parent.mkdir(parents=True, exist_ok=True)
    images = []
    for size in SIZES:
        image = draw(size).toImage().convertToFormat(QImage.Format.Format_RGBA8888)
        images.append(Image.frombytes("RGBA", (image.width(), image.height()),
                                      bytes(image.constBits()), "raw", "RGBA"))
    # 以最大的那张为底，附带所有尺寸，Windows 会按需挑
    images[-1].save(out, format="ICO", sizes=[(i.width, i.height) for i in images])
    print("saved", out, out.stat().st_size, "bytes", SIZES)


if __name__ == "__main__":
    main()
