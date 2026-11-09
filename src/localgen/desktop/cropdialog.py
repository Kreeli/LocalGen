"""交互式参考图裁切对话框：比例锁定输出尺寸，只裁不拉。"""
import io

from PIL import Image
from PySide6.QtCore import QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from localgen.imaging import clamp_fractional, cover_box

HANDLE = 5          # 把手半径（像素）
MIN_WIDGET_SIDE = 28


def qimage(image: Image.Image) -> QImage:
    """PIL 图 → QImage（经 PNG 中转，避免通道顺序与缓冲区生命周期问题）。"""
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return QImage.fromData(buffer.getvalue(), "PNG")


def caption(text, wrap=False):
    control = QLabel(text)
    control.setObjectName("caption")
    control.setWordWrap(wrap)
    return control


class CropCanvas(QWidget):
    """显示图片 + 可拖动/缩放的裁切框（宽高比恒等于输出比例）。"""

    box_changed = Signal()

    def __init__(self, image: QImage, ratio: float, parent=None):
        super().__init__(parent)
        self._image = image
        self._ratio = max(0.01, float(ratio))
        self._box = QRectF()
        self._press_image = QPointF()
        self._press_box = QRectF()
        self._mode = None
        self.setMinimumSize(460, 340)
        self.setMouseTracking(True)
        self.reset()

    # ---------- 状态 ----------
    @property
    def image_size(self):
        return self._image.width(), self._image.height()

    def fractional(self):
        width, height = self.image_size
        box = self._box
        return (box.left() / width, box.top() / height,
                box.right() / width, box.bottom() / height)

    def reset(self):
        width, height = self.image_size
        left, top, right, bottom = cover_box((width, height),
                                             (max(1, round(self._ratio * 1000)), 1000))
        self._box = QRectF(left, top, right - left, bottom - top)
        self.update()
        self.box_changed.emit()

    def set_fractional(self, box):
        """回填上一次的裁切框（归一化坐标），保留用户的构图习惯。"""
        width, height = self.image_size
        left, top, right, bottom = clamp_fractional(box)
        self._box = QRectF(left * width, top * height,
                           max(1.0, (right - left) * width), max(1.0, (bottom - top) * height))
        self.update()
        self.box_changed.emit()

    # ---------- 坐标换算 ----------
    def _viewport(self):
        area = self.rect().adjusted(10, 10, -10, -10)
        if area.width() < 10 or area.height() < 10:
            area = QRect(0, 0, max(1, self.width()), max(1, self.height()))
        width, height = self.image_size
        scale = min(area.width() / width, area.height() / height)
        return scale, area.left() + (area.width() - width * scale) / 2, \
            area.top() + (area.height() - height * scale) / 2

    def _shown(self):
        scale, left, top = self._viewport()
        return QRectF(left + self._box.left() * scale, top + self._box.top() * scale,
                      self._box.width() * scale, self._box.height() * scale)

    def _to_image(self, point):
        scale, left, top = self._viewport()
        return QPointF((point.x() - left) / scale, (point.y() - top) / scale)

    # ---------- 命中测试 ----------
    def _hit(self, point):
        shown = self._shown()
        near = HANDLE + 4
        for name, corner in (("nw", shown.topLeft()), ("ne", shown.topRight()),
                             ("sw", shown.bottomLeft()), ("se", shown.bottomRight())):
            if abs(point.x() - corner.x()) <= near and abs(point.y() - corner.y()) <= near:
                return name
        return "move" if shown.contains(point) else None

    @staticmethod
    def _cursor(mode):
        return {"nw": Qt.CursorShape.SizeFDiagCursor, "se": Qt.CursorShape.SizeFDiagCursor,
                "ne": Qt.CursorShape.SizeBDiagCursor, "sw": Qt.CursorShape.SizeBDiagCursor,
                "move": Qt.CursorShape.SizeAllCursor}.get(mode, Qt.CursorShape.ArrowCursor)

    # ---------- 鼠标 ----------
    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        point = QPointF(event.position())
        self._mode = self._hit(point)
        if self._mode is None:
            return
        self._press_image = self._to_image(point)
        self._press_box = QRectF(self._box)

    def mouseMoveEvent(self, event):
        point = QPointF(event.position())
        if self._mode is None:
            self.setCursor(self._cursor(self._hit(point)))
            return
        if self._mode == "move":
            self._move_to(self._to_image(point))
        else:
            self._resize_to(self._mode, self._to_image(point))
        self.update()
        self.box_changed.emit()

    def mouseReleaseEvent(self, event):
        self._mode = None
        self.setCursor(self._cursor(self._hit(QPointF(event.position()))))

    def _move_to(self, point):
        width, height = self.image_size
        dx = point.x() - self._press_image.x()
        dy = point.y() - self._press_image.y()
        box = QRectF(self._press_box)
        box.moveTo(min(max(0.0, box.x() + dx), width - box.width()),
                   min(max(0.0, box.y() + dy), height - box.height()))
        self._box = box

    def _resize_to(self, handle, point):
        width, height = self.image_size
        scale, _, _ = self._viewport()
        anchor = {"nw": self._box.bottomRight(), "ne": self._box.bottomLeft(),
                  "sw": self._box.topRight(), "se": self._box.topLeft()}[handle]
        dx = point.x() - anchor.x()
        dy = point.y() - anchor.y()
        limit_w = anchor.x() if dx < 0 else width - anchor.x()
        limit_h = anchor.y() if dy < 0 else height - anchor.y()
        if limit_w < 4 or limit_h < 4:
            return
        minimum = max(4.0, MIN_WIDGET_SIDE / max(scale, 1e-6))
        wanted = max(abs(dx), abs(dy) * self._ratio, minimum)
        size_w = min(wanted, limit_w, limit_h * self._ratio)
        size_h = size_w / self._ratio
        if size_h > limit_h:
            size_h, size_w = limit_h, limit_h * self._ratio
        left = anchor.x() - size_w if dx < 0 else anchor.x()
        top = anchor.y() - size_h if dy < 0 else anchor.y()
        self._box = QRectF(left, top, size_w, size_h)

    # ---------- 绘制 ----------
    def paintEvent(self, _):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#0d101a"))
        scale, left, top = self._viewport()
        width, height = self.image_size
        painter.drawImage(QRectF(left, top, width * scale, height * scale), self._image)
        shown = self._shown()
        outside = QPainterPath()
        outside.addRect(QRectF(self.rect()))
        inside = QPainterPath()
        inside.addRect(shown)
        painter.fillPath(outside.subtracted(inside), QColor(8, 10, 16, 168))
        painter.setPen(QPen(QColor("#a591ff"), 1.5))
        painter.drawRect(shown)
        painter.setPen(QPen(QColor(255, 255, 255, 70), 1, Qt.PenStyle.DashLine))
        for step in (1, 2):
            x = shown.left() + shown.width() * step / 3
            y = shown.top() + shown.height() * step / 3
            painter.drawLine(QPointF(x, shown.top()), QPointF(x, shown.bottom()))
            painter.drawLine(QPointF(shown.left(), y), QPointF(shown.right(), y))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#a591ff"))
        for corner in (shown.topLeft(), shown.topRight(), shown.bottomLeft(), shown.bottomRight()):
            painter.drawRect(QRectF(corner.x() - HANDLE, corner.y() - HANDLE, HANDLE * 2, HANDLE * 2))


class CropDialog(QDialog):
    """返回归一化裁切框（0–1），交给 imaging 用原始像素裁切。"""

    def __init__(self, image: QImage, target, parent=None):
        super().__init__(parent)
        self.target = (int(target[0]), int(target[1]))
        self.setWindowTitle("裁切参考图")
        self.setModal(True)
        self.setMinimumSize(700, 580)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        layout.addWidget(caption(
            "拖动方框选择要保留的部分，四角可缩放。比例锁定为输出尺寸，"
            "图片只会被裁切和等比缩放，不会被拉伸。", True))
        self.canvas = CropCanvas(image, self.target[0] / self.target[1], self)
        layout.addWidget(self.canvas, 1)
        self.info = caption("")
        layout.addWidget(self.info)
        row = QHBoxLayout()
        reset = QPushButton("重置为居中裁切")
        reset.clicked.connect(self.canvas.reset)
        row.addWidget(reset)
        row.addStretch()
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        use = QPushButton("使用此裁切")
        use.setObjectName("primary")
        use.clicked.connect(self.accept)
        row.addWidget(use)
        layout.addLayout(row)
        self.canvas.box_changed.connect(self.refresh_info)
        self.refresh_info()

    def refresh_info(self):
        width, height = self.canvas.image_size
        left, top, right, bottom = self.canvas.fractional()
        crop_w, crop_h = max(1, round((right - left) * width)), max(1, round((bottom - top) * height))
        self.info.setText(f"原图 {width} × {height}    →    裁切 {crop_w} × {crop_h}"
                          f"    →    输出 {self.target[0]} × {self.target[1]}")

    @property
    def box(self):
        return self.canvas.fractional()
