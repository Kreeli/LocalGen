"""参考图输入框：拖入 / 选择 / 自定义裁切 / 清除，并在模型不支持时锁定。"""
import logging
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFileDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
)

from localgen.imaging import SUFFIXES, open_rgb, preview, reference_png

from .cropdialog import CropDialog, qimage

logger = logging.getLogger(__name__)
UPLOAD_WATCHDOG = 100_000       # 冷启动/杀软扫描时的兜底，防止界面卡在"上传中"
RETARGET_DELAY = 250
THUMB = 74


class ReferencePicker(QFrame):
    """一个自足的参考图输入控件。

    `upload(content, filename, callback)` 由窗口注入（通常是 Client.upload_image），
    控件本身不关心 HTTP 细节，也方便测试注入替身。
    """

    changed = Signal()

    def __init__(self, upload, parent=None):
        super().__init__(parent)
        self.setObjectName("reference")
        self.setProperty("locked", "no")
        self.setAcceptDrops(True)
        self._upload = upload
        self.target = (1024, 1024)
        self.path = None
        self.box = None
        self.source_size = (0, 0)
        self.token = None
        self.locked = False
        self.busy = False
        self._thumb = None
        self._watchdog = None
        self._retarget = QTimer(self)
        self._retarget.setSingleShot(True)
        self._retarget.setInterval(RETARGET_DELAY)
        self._retarget.timeout.connect(self._reupload)
        self.build()
        self.set_locked(False)

    # ---------- 构建 ----------
    def build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 9)
        layout.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(6)
        title = QLabel("参考图")
        title.setObjectName("caption")
        head.addWidget(title)
        head.addStretch()
        self.hint = QLabel("")
        self.hint.setObjectName("referenceHint")
        head.addWidget(self.hint)
        layout.addLayout(head)

        body = QHBoxLayout()
        body.setSpacing(9)
        self.thumb = QLabel()
        self.thumb.setObjectName("canvas")
        self.thumb.setFixedSize(THUMB, THUMB)
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body.addWidget(self.thumb)
        column = QVBoxLayout()
        column.setSpacing(2)
        self.name = QLabel("未选择参考图")
        self.name.setObjectName("referenceName")
        self.name.setWordWrap(True)
        column.addWidget(self.name)
        self.meta = QLabel("支持点击选择或直接拖入图片")
        self.meta.setObjectName("caption")
        self.meta.setWordWrap(True)
        column.addWidget(self.meta)
        column.addStretch()
        row = QHBoxLayout()
        row.setSpacing(6)
        self.choose_button = QPushButton("选择图片")
        self.choose_button.clicked.connect(self.choose)
        self.crop_button = QPushButton("重新裁切")
        self.crop_button.clicked.connect(self.recrop)
        self.clear_button = QPushButton("清除")
        self.clear_button.clicked.connect(self.clear)
        for control in (self.choose_button, self.crop_button, self.clear_button):
            control.setFixedHeight(26)
            control.setObjectName("ghost")
            row.addWidget(control)
        row.addStretch()
        column.addLayout(row)
        body.addLayout(column, 1)
        layout.addLayout(body)
        self.setMinimumHeight(THUMB + 34)

    # ---------- 状态 ----------
    def set_target(self, width, height):
        target = (int(width), int(height))
        if target == self.target:
            return
        self.target = target
        if self.path and not self.locked and not self.busy:
            # 尺寸换了 → 旧裁切框比例失效，按新比例重新居中裁切并重传
            self.box = None
            self._retarget.start()

    def set_locked(self, locked, reason="此模型不支持参考图"):
        self.locked = bool(locked)
        self._enable_buttons()
        self.setProperty("locked", "yes" if self.locked else "no")
        self.style().unpolish(self)
        self.style().polish(self)
        if self.locked:
            self.hint.setText(reason)
            self.clear(silent=True)
            self.meta.setText("当前模型为纯文生图，参考图输入已锁定")
        else:
            self.hint.setText("")
            if not self.path:
                self.meta.setText("支持点击选择或直接拖入图片")

    def _enable_buttons(self):
        for control in (self.choose_button, self.crop_button, self.clear_button):
            control.setEnabled(not self.busy and not self.locked)
        self.crop_button.setEnabled(bool(self.path) and not self.busy and not self.locked)
        self.clear_button.setEnabled(bool(self.path or self.token) and not self.busy and not self.locked)

    def set_busy(self, busy, text=""):
        self.busy = bool(busy)
        self._enable_buttons()
        if self.busy:
            self.meta.setText(text or "正在按输出尺寸裁切并上传…")
            if self._watchdog is None:
                self._watchdog = QTimer(self)
                self._watchdog.setSingleShot(True)
                self._watchdog.timeout.connect(self._timed_out)
            self._watchdog.start(UPLOAD_WATCHDOG)
        elif self._watchdog is not None:
            self._watchdog.stop()

    def _timed_out(self):
        if self.busy:
            self.set_busy(False)
            self.meta.setText("上传超时：请确认本机服务正常后重试")

    def clear(self, silent=False):
        self._retarget.stop()
        self.path = None
        self.box = None
        self.token = None
        self.source_size = (0, 0)
        self._thumb = None
        self.set_busy(False)
        self.thumb.clear()
        self.name.setText("未选择参考图")
        if not self.locked:
            self.meta.setText("支持点击选择或直接拖入图片")
        self._enable_buttons()
        if not silent:
            self.changed.emit()

    def error(self, text):
        self.set_busy(False)
        self.meta.setText(text)
        self.changed.emit()

    def restore(self, token, source_size=(0, 0), thumb=None):
        """回填一张已经上传过的参考图（历史任务的"复用参数"用）。

        不走上传：令牌仍指向 `gateway-input/<token>`，所以复用后直接提交即可，
        省掉一次裁切 + 上传；`thumb` 是引擎实际吃到的那份裁剪图，用来显示缩略图。
        """
        self._retarget.stop()
        self.path = None
        self.box = None
        self.token = token
        self.source_size = (int(source_size[0]), int(source_size[1]))
        self._thumb = thumb
        self.set_busy(False)
        self.name.setText(f"已复用 {str(token).split('.')[0][:8]}")
        if self.source_size[0]:
            self._describe()
        else:
            self.meta.setText(f"已复用的参考图 · 按 {self.target[0]} × {self.target[1]} "
                              f"裁切后提交（不拉伸）")
        self._show_thumb()
        self._enable_buttons()
        if not self.locked:
            self.hint.setText("")
        self.changed.emit()

    # ---------- 选择 / 裁切 / 上传 ----------
    def choose(self):
        if self.locked or self.busy:
            return
        path, _ = QFileDialog.getOpenFileName(self, "选择参考图", "",
                                              "图片 (*.png *.jpg *.jpeg *.webp)")
        if path:
            self.load(path)

    def recrop(self):
        if self.path and not self.locked and not self.busy:
            self.load(self.path)

    def load(self, path):
        """读取本地图片 →（可选的）裁切对话框 → 上传裁切后的图。"""
        path = Path(path)
        if path.suffix.lower() not in SUFFIXES:
            self.error("仅支持 PNG、JPEG 或 WebP 参考图")
            return
        try:
            content = path.read_bytes()
            image = open_rgb(content)
        except Exception:
            logger.debug("REFERENCE_UNREADABLE %s", path)
            self.error("无法读取此图片，请换一张 PNG/JPEG/WebP")
            return
        display = qimage(preview(image))
        if display.isNull():
            self.error("无法读取此图片，请换一张 PNG/JPEG/WebP")
            return
        keep = self.box
        dialog = CropDialog(display, self.target, self.window())
        if keep is not None:
            dialog.canvas.set_fractional(keep)
        if dialog.exec() != CropDialog.DialogCode.Accepted:
            return
        self.path = path
        self.source_size = image.size
        self.box = dialog.box
        self._crop_and_upload(content)

    def _reupload(self):
        if not self.path or self.locked:
            return
        try:
            content = self.path.read_bytes()
        except OSError:
            self.error("原图已被移动或删除，请重新选择")
            self.clear(silent=True)
            return
        self._crop_and_upload(content)

    def _crop_and_upload(self, content):
        try:
            cropped = reference_png(content, self.target, self.box)
        except Exception:
            logger.debug("REFERENCE_CROP_FAILED %s", self.path)
            self.error("裁切失败：这张图片可能已损坏")
            return
        self._thumb = cropped
        self.name.setText(self.path.name if self.path else "参考图")
        self.set_busy(True)
        try:
            self._upload(cropped, "reference.png", self._uploaded)
        except Exception as exc:      # 客户端抛错也必须回收界面状态（历史上就卡在这里）
            logger.debug("REFERENCE_UPLOAD_RAISED", exc_info=exc)
            self.error(f"上传失败：{exc}")

    def _uploaded(self, value, error):
        self.set_busy(False)
        if error:
            self.token = None
            self.name.setText("上传失败")
            self.error(error)
            return
        token = (value or {}).get("name")
        if not token:
            self.token = None
            self.error("本机服务没有返回参考图引用")
            return
        self.token = token
        self._describe()
        self._show_thumb()
        self.changed.emit()

    def _describe(self):
        width, height = self.source_size
        if not width:
            return
        self.meta.setText(f"原图 {width} × {height} · 按 {self.target[0]} × {self.target[1]} "
                          f"裁切后提交（不拉伸）")

    def _show_thumb(self):
        pixmap = QPixmap()
        if self._thumb and pixmap.loadFromData(self._thumb, "PNG"):
            self.thumb.setPixmap(pixmap.scaled(QSize(THUMB - 8, THUMB - 8),
                                               Qt.AspectRatioMode.KeepAspectRatio,
                                               Qt.TransformationMode.SmoothTransformation))
        elif self.path:
            fallback = QPixmap(str(self.path))
            if not fallback.isNull():
                self.thumb.setPixmap(fallback.scaled(QSize(THUMB - 8, THUMB - 8),
                                                     Qt.AspectRatioMode.KeepAspectRatio,
                                                     Qt.TransformationMode.SmoothTransformation))

    # ---------- 拖放 ----------
    def _dragged(self, event):
        mime = event.mimeData()
        if not mime.hasUrls():
            return None
        for url in mime.urls():
            path = Path(url.toLocalFile())
            if path.suffix.lower() in SUFFIXES and path.is_file():
                return path
        return None

    def dragEnterEvent(self, event):
        if not self.locked and not self.busy and self._dragged(event):
            event.acceptProposedAction()

    def dropEvent(self, event):
        path = self._dragged(event)
        if path is not None and not self.locked and not self.busy:
            event.acceptProposedAction()
            self.load(path)
