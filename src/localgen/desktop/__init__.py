import json
import logging
import re
import sys
import threading
from datetime import datetime
from html import escape
from pathlib import Path
from uuid import UUID, uuid4

from PySide6.QtCore import QEvent, QIODevice, QLocale, QObject, QSaveFile, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy, QSpinBox, QSystemTrayIcon,
    QVBoxLayout, QWidget,
)

from localgen.config import Settings, save_settings

from .client import Client
from .style import STYLE

logger = logging.getLogger(__name__)
STATUS = {"queued": "排队中", "submitting": "提交中", "running": "生成中", "cancelling": "正在取消",
          "cancelled": "已取消", "failed": "失败", "succeeded": "已完成", "interrupted": "已中断"}
DIAGNOSTICS = {
    "ENGINE_NOT_CONFIGURED": "请在设置中选择 ComfyUI Portable 文件夹。",
    "ENGINE_PATH_INVALID": "无法找到引擎或独立 Python，请检查设置。",
    "ENGINE_VERSION_UNSUPPORTED": "此应用使用 ComfyUI 0.3.76。",
    "ENGINE_PORT_IN_USE": "引擎端口已被占用，请检查端口设置。",
    "ENGINE_LISTENER_INVALID": "引擎的监听地址或归属检查失败。",
    "ENGINE_START_TIMEOUT": "引擎启动超时。可在诊断日志中查看详情。",
    "CUDA_UNAVAILABLE": "未检测到 NVIDIA CUDA 设备。",
    "ENGINE_STOP_UNCONFIRMED": "无法确认引擎已停止，已暂停调度。",
    "WORKFLOW_NOT_VERIFIED": "此工作流等待实机验证。",
    "ENGINE_UNAVAILABLE": "工作流将在引擎就绪后进行检查。",
}
ROLE = Qt.ItemDataRole.UserRole
TERMINAL = ("succeeded", "failed", "cancelled", "interrupted")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "LocalGen"


def autostart_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if pythonw.is_file():
        return f'"{pythonw}" -m localgen'
    return f'"{sys.executable}" -m localgen'


def autostart_enabled():
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_NAME)
        return True
    except OSError:
        return False


def set_autostart(enabled):
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, autostart_command())
            return
        try:
            winreg.DeleteValue(key, RUN_NAME)
        except FileNotFoundError:
            pass


def label(value, role="", wrap=False):
    control = QLabel(value)
    control.setObjectName(role)
    control.setWordWrap(wrap)
    return control


def card(name="card"):
    control = QFrame()
    control.setObjectName(name)
    return control


def button(title, action, name=""):
    control = QPushButton(title)
    control.setObjectName(name)
    control.clicked.connect(action)
    return control


class Window(QMainWindow):
    def __init__(self, data, settings, token):
        super().__init__()
        self.data, self.settings, self.token = data, settings, token
        self.api = Client(settings.api_port, token, self)
        self.setWindowTitle("LocalGen · 本地生图工作台")
        self.setMinimumSize(830, 650)
        self.resize(1100, 780)
        self.selected = None
        self.selected_status = None
        self.real_exit = False
        self.tray_hint_shown = False
        self.artifact = None
        self.png = None
        self.pixmap = None
        self.workflows = None
        self.size_workflow_id = None
        self.engine = {}
        self.latest = []
        self.older = []
        self.cursor = None
        self.pending = set()
        self.submitting = False
        self.stopping = False
        self.idempotency = None
        self.build()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1100)
        QTimer.singleShot(0, self.refresh)

    def build(self):
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        header = card("header")
        row = QHBoxLayout(header)
        row.setContentsMargins(16, 9, 12, 9)
        row.addWidget(label("LocalGen", "brand"))
        row.addWidget(label("本地生图工作台", "caption"))
        row.addStretch()
        self.engine_badge = label("●  正在连接", "engine")
        row.addWidget(self.engine_badge)
        row.addWidget(button("设置", self.preferences))
        layout.addWidget(header)

        columns = QHBoxLayout()
        columns.setSpacing(12)
        layout.addLayout(columns, 1)
        columns.addWidget(self.editor())
        workspace = QVBoxLayout()
        workspace.setSpacing(12)
        columns.addLayout(workspace, 1)
        workspace.addWidget(self.preview(), 3)
        workspace.addWidget(self.history(), 2)

        self.footer = label("本机 REST 与 MCP · 127.0.0.1:" + str(self.settings.api_port), "caption")
        layout.addWidget(self.footer)

    def editor(self):
        panel = card()
        panel.setFixedWidth(328)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(8)
        layout.addWidget(label("生成图片", "heading"))
        layout.addWidget(label("工作流", "caption"))
        self.workflow = QComboBox()
        self.workflow.currentIndexChanged.connect(self.workflow_changed)
        layout.addWidget(self.workflow)
        self.requirements = label("正在检查引擎和工作流…", "notice", True)
        self.requirements.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.requirements)
        self.export_button = button("导出插件 JSON", self.export_plugin_json)
        layout.addWidget(self.export_button)
        layout.addWidget(label("提示词", "caption"))
        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText("描述画面、光线和构图…")
        self.prompt.textChanged.connect(self.controls)
        layout.addWidget(self.prompt, 2)
        layout.addWidget(label("负面提示词 · 可选", "caption"))
        self.negative = QPlainTextEdit()
        self.negative.setPlaceholderText("例如 blurry, watermark")
        self.negative.setMaximumHeight(78)
        self.negative.textChanged.connect(self.controls)
        layout.addWidget(self.negative, 1)
        parameters = QHBoxLayout()
        self.size = QComboBox()
        self.seed = QLineEdit()
        self.seed.setMaxLength(10)
        self.seed.setPlaceholderText("留空随机")
        self.seed.textChanged.connect(self.controls)
        for title, widget in (("分辨率", self.size), ("种子", self.seed)):
            field = QVBoxLayout()
            field.addWidget(label(title, "caption"))
            field.addWidget(widget)
            parameters.addLayout(field, 1)
        layout.addLayout(parameters)
        self.generate = button("生成一张", self.submit, "primary")
        self.generate.setEnabled(False)
        layout.addWidget(self.generate)
        self.form_note = label("", "notice", True)
        self.form_note.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.form_note)
        return panel

    def preview(self):
        panel = card()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 12, 14, 12)
        title = QHBoxLayout()
        title.addWidget(label("预览", "heading"))
        title.addStretch()
        self.cancel_button = button("取消任务", self.cancel)
        self.copy_button = button("复制图片", self.copy_image)
        self.save_button = button("另存 PNG", self.save_png)
        for control in (self.cancel_button, self.copy_button, self.save_button):
            control.setEnabled(False)
            title.addWidget(control)
        layout.addLayout(title)
        self.canvas = card("canvas")
        self.canvas.installEventFilter(self)
        canvas_layout = QVBoxLayout(self.canvas)
        self.image = label("生成完成后，图片会显示在这里", "placeholder", True)
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setTextFormat(Qt.TextFormat.PlainText)
        self.image.setMinimumSize(1, 1)
        self.image.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        canvas_layout.addWidget(self.image)
        layout.addWidget(self.canvas, 1)
        prompt_row = QHBoxLayout()
        prompt_row.addWidget(label("提示词", "caption"))
        prompt_row.addStretch()
        self.copy_prompt_button = button("复制提示词", self.copy_prompt)
        self.copy_prompt_button.setEnabled(False)
        prompt_row.addWidget(self.copy_prompt_button)
        layout.addLayout(prompt_row)
        self.prompt_view = QPlainTextEdit()
        self.prompt_view.setReadOnly(True)
        self.prompt_view.setMaximumHeight(64)
        self.prompt_view.setPlaceholderText("选中任务后显示完整提示词")
        self.prompt_view.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                                 | Qt.TextInteractionFlag.TextEditable)
        layout.addWidget(self.prompt_view)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.detail = label("从历史中选择任务查看详情", "caption", True)
        self.detail.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.detail)
        return panel

    def history(self):
        panel = card()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 10, 14, 12)
        heading = QHBoxLayout()
        heading.addWidget(label("任务历史", "heading"))
        heading.addStretch()
        self.delete_button = button("删除任务", self.delete_job)
        self.delete_button.setEnabled(False)
        heading.addWidget(self.delete_button)
        self.more = button("更早任务", self.load_more)
        self.more.setEnabled(False)
        heading.addWidget(self.more)
        layout.addLayout(heading)
        self.list = QListWidget()
        self.list.setAccessibleName("任务历史")
        self.list.currentItemChanged.connect(self.select_item)
        layout.addWidget(self.list)
        return panel

    def request_once(self, slot, path, callback, **kwargs):
        if slot in self.pending:
            return
        self.pending.add(slot)

        def completed(value, error):
            self.pending.discard(slot)
            callback(value, error)

        self.api.request(path, completed, **kwargs)

    def refresh(self):
        if self.stopping:
            return
        self.request_once("engine", "/api/v1/engine", self.update_engine)
        self.request_once("workflows", "/api/v1/workflows", self.update_workflows)
        self.request_once("history", "/api/v1/jobs?limit=20", self.update_history)
        self.refresh_selected()

    def update_engine(self, value, error):
        self.engine = value or {}
        state = self.engine.get("state")
        name = {"ready": "引擎就绪", "starting": "引擎启动中", "stopping": "引擎停止中",
                "stopped": "引擎未启动", "error": "引擎不可用"}.get(state, "连接本机服务")
        if self.engine.get("mode") == "mock":
            name = "演示模式 · 程序绘图"
        self.engine_badge.setText(f"●  {name}")
        code = self.engine.get("diagnostic_code", "")
        if state == "ready":
            self.footer.setText(f"{self.engine.get('device') or ''}  ·  本机服务 127.0.0.1:{self.settings.api_port}")
        elif code:
            self.footer.setText(DIAGNOSTICS.get(code, code) + f"  ·  引擎端口 {self.settings.engine_port}")
        self.controls()

    def update_workflows(self, value, error):
        items = (value or {}).get("items", [])
        if items == self.workflows:
            return
        current = self.workflow.currentData()
        self.workflows = items
        self.workflow.blockSignals(True)
        self.workflow.clear()
        for item in items:
            self.workflow.addItem(item["title"], item["workflow_id"])
        if current is not None and self.workflow.findData(current) >= 0:
            self.workflow.setCurrentIndex(self.workflow.findData(current))
        self.workflow.blockSignals(False)
        self.workflow_changed()

    def current_workflow(self):
        return next((item for item in self.workflows or [] if item["workflow_id"] == self.workflow.currentData()), {})

    def workflow_changed(self):
        item = self.current_workflow()
        workflow_id = item.get("workflow_id")
        size = self.size.currentData() if workflow_id == self.size_workflow_id else item.get("default_size")
        self.size_workflow_id = workflow_id
        self.size.clear()
        for width, height in item.get("verified_sizes", []):
            self.size.addItem(f"{width} × {height}", [width, height])
        index = self.size.findData(size if size is not None else item.get("default_size"))
        if index >= 0:
            self.size.setCurrentIndex(index)
        self.negative.setEnabled("negative_prompt" in item.get("supported_parameters", []))
        if item.get("missing_dependencies"):
            text = "缺少依赖\n" + "\n".join(item["missing_dependencies"])
        elif item.get("available"):
            text = f"版本 {item['revision']} · 单张 PNG"
        else:
            text = DIAGNOSTICS.get(item.get("diagnostic_code"), "暂无可用工作流")
        self.requirements.setText(text)
        self.controls()

    def export_plugin_json(self):
        workflow_id = self.workflow.currentData()
        if not workflow_id:
            return
        menu = QMenu(self)
        text_action = menu.addAction("文生图工作流")
        edit_action = menu.addAction("图生图工作流")
        chosen = menu.exec(self.export_button.mapToGlobal(self.export_button.rect().bottomLeft()))
        if chosen is None:
            return
        variant = "image2image" if chosen is edit_action else "text2image"
        suffix = "-edit" if chosen is edit_action else ""
        path, _ = QFileDialog.getSaveFileName(self, "导出 dsh-image-gen 工作流",
                                              f"dsh-image-gen-{workflow_id}{suffix}.json", "JSON (*.json)")
        if not path:
            return

        def completed(value, error):
            if error:
                QMessageBox.warning(self, "导出失败", error)
                return
            try:
                Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf-8")
            except OSError as exc:
                QMessageBox.warning(self, "导出失败", str(exc))
                return
            self.form_note.setText(f"已导出 {Path(path).name}，可在 dsh-image-gen 设置中导入")

        self.api.request(f"/api/v1/workflows/{workflow_id}/dsh-plugin?variant={variant}", completed)

    def controls(self):
        if not hasattr(self, "generate"):
            return
        prompt = self.prompt.toPlainText().strip()
        seed = self.seed.text()
        valid_seed = not seed or (re.fullmatch(r"[0-9]+", seed) and int(seed) <= 4294967295)
        note = ""
        if len(prompt) > 2000 or len(self.negative.toPlainText().strip()) > 2000:
            note = "提示词最多 2000 字符。"
        elif not valid_seed:
            note = "种子范围为 0–4294967295。"
        if note:
            self.form_note.setText(note)
        elif self.form_note.text() in ("提示词最多 2000 字符。", "种子范围为 0–4294967295。"):
            self.form_note.clear()
        enabled = (self.engine.get("available") and self.current_workflow().get("available")
                   and self.size.currentData() and prompt and not note and not self.submitting and not self.stopping)
        self.generate.setEnabled(bool(enabled))

    def submit(self):
        if not self.generate.isEnabled():
            return
        width, height = self.size.currentData()
        payload = {"workflow_id": self.workflow.currentData(), "prompt": self.prompt.toPlainText().strip(),
                   "width": width, "height": height}
        if self.seed.text():
            payload["seed"] = int(self.seed.text())
        if self.negative.isEnabled() and self.negative.toPlainText().strip():
            payload["negative_prompt"] = self.negative.toPlainText().strip()
        if self.idempotency is None or self.idempotency[0] != payload:
            self.idempotency = payload, uuid4().hex
        self.submitting = True
        self.form_note.clear()
        self.controls()

        def completed(value, error):
            self.submitting = False
            self.controls()
            if error:
                self.form_note.setText(error)
                return
            self.idempotency = None
            self.latest = [value, *[item for item in self.latest if item["job_id"] != value["job_id"]]]
            self.selected = value["job_id"]
            self.render_history()
            self.show_job(value)
            self.refresh()

        self.api.request("/api/v1/jobs", completed, payload=payload, post=True, key=self.idempotency[1])

    def update_history(self, value, error):
        if error:
            return
        previous = {job["job_id"] for job in self.latest}
        self.latest = value["items"]
        if not self.older or previous != {job["job_id"] for job in self.latest}:
            self.older = []
            self.cursor = value["next_cursor"]
        self.render_history()

    def render_history(self):
        self.list.blockSignals(True)
        previous_scroll = self.list.verticalScrollBar().value()
        self.list.clear()
        for job in self.latest + self.older:
            prompt = " ".join(job["parameters"]["prompt"].split())
            timestamp = datetime.fromisoformat(job["created_at"]).astimezone().strftime("%m-%d %H:%M")
            item = QListWidgetItem(f"{STATUS[job['status']]}  ·  {timestamp}  ·  {prompt[:90]}")
            item.setData(ROLE, job["job_id"])
            item.setToolTip(f"<span style='white-space:pre-wrap'>{escape(prompt)}</span>")
            self.list.addItem(item)
            if job["job_id"] == self.selected:
                self.list.setCurrentItem(item)
        self.list.verticalScrollBar().setValue(previous_scroll)
        self.list.blockSignals(False)
        self.more.setEnabled(bool(self.cursor))

    def load_more(self):
        cursor = self.cursor
        if not cursor:
            return

        def completed(value, error):
            if not error and cursor == self.cursor:
                self.older.extend(value["items"])
                self.cursor = value["next_cursor"]
                self.render_history()

        self.request_once("older", f"/api/v1/jobs?limit=20&cursor={cursor}", completed)

    def select_item(self, item, _):
        self.selected = item.data(ROLE) if item else None
        self.selected_status = None
        self.delete_button.setEnabled(False)
        self.prompt_view.clear()
        self.copy_prompt_button.setEnabled(False)
        self.clear_image("正在读取任务…")
        self.cancel_button.setEnabled(False)
        self.refresh_selected()

    def refresh_selected(self):
        job_id = self.selected
        if job_id:
            self.request_once(f"job:{job_id}", f"/api/v1/jobs/{job_id}",
                              lambda value, error: self.show_job(value) if value and self.selected == job_id else None)

    def clear_image(self, text):
        self.png = self.pixmap = self.artifact = None
        self.image.clear()
        self.image.setText(text)
        self.canvas.setCursor(Qt.CursorShape.ArrowCursor)
        self.canvas.setToolTip("")
        self.copy_button.setEnabled(False)
        self.save_button.setEnabled(False)

    def show_job(self, job):
        status = job["status"]
        self.selected_status = status
        parameters = job["parameters"]
        text = (f"{STATUS[status]}  ·  {parameters['width']} × {parameters['height']}"
                f"  ·  种子 {parameters['seed']}  ·  {job['job_id'][:8]}")
        if job["error"]:
            text += "\n" + job["error"]["message"] + "  ·  " + job["error"]["code"]
        self.detail.setText(text)
        self.prompt_view.setPlainText(parameters["prompt"])
        self.copy_prompt_button.setEnabled(bool(parameters["prompt"]))
        self.cancel_button.setEnabled(status in {"queued", "submitting", "running"} and not self.stopping)
        self.delete_button.setEnabled(status in TERMINAL and not self.stopping)
        self.progress.setVisible(status in {"submitting", "running", "cancelling"})
        progress = job["progress"] or {}
        self.progress.setRange(0, progress.get("total") or 0)
        self.progress.setValue(progress.get("current") or 0)
        if not job["artifacts"]:
            self.clear_image({"queued": "正在等待生成", "running": "正在生成图片",
                              "submitting": "正在提交任务", "cancelling": "正在停止任务"}.get(status, STATUS[status]))
            return
        result = job["artifacts"][0]
        if not result["available"]:
            self.clear_image("原始 PNG 文件已丢失")
            return
        artifact = result["artifact_id"]
        if self.artifact == artifact:
            return
        self.clear_image("正在加载 PNG…")
        self.artifact = artifact

        def completed(content, error):
            if job["job_id"] != self.selected or self.artifact != artifact:
                return
            pixmap = QPixmap()
            if error or not pixmap.loadFromData(content, "PNG"):
                self.clear_image(error or "无法显示此 PNG")
                return
            self.png, self.pixmap = content, pixmap
            self.paint_image()
            self.canvas.setCursor(Qt.CursorShape.PointingHandCursor)
            self.canvas.setToolTip("点击用系统图片查看器打开原图")
            self.copy_button.setEnabled(True)
            self.save_button.setEnabled(True)

        self.api.request(result["download_path"], completed, png=True)

    def paint_image(self):
        if self.pixmap:
            self.image.setPixmap(self.pixmap.scaled(self.canvas.size() / 1.04, Qt.AspectRatioMode.KeepAspectRatio,
                                                   Qt.TransformationMode.SmoothTransformation))

    def eventFilter(self, obj, event):
        if obj is getattr(self, "canvas", None):
            if event.type() == QEvent.Type.Resize:
                QTimer.singleShot(0, self.paint_image)
            elif event.type() == QEvent.Type.MouseButtonRelease and self.png is not None:
                self.open_original()
        return super().eventFilter(obj, event)

    def open_original(self):
        job_id, artifact_id = self.selected, self.artifact
        try:
            file = self.data / "results" / str(UUID(job_id)) / f"{UUID(artifact_id)}.png"
        except (ValueError, TypeError, OSError):
            file = None
        if file is not None and file.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(file)))
        else:
            QMessageBox.warning(self, "无法打开", "原始 PNG 文件已丢失或被移动。")

    def copy_image(self):
        if self.pixmap is not None:
            QApplication.clipboard().setPixmap(self.pixmap)

    def copy_prompt(self):
        text = self.prompt_view.toPlainText()
        if text:
            QApplication.clipboard().setText(text)

    def delete_job(self):
        job_id = self.selected
        if not job_id or self.selected_status not in TERMINAL:
            return
        answer = QMessageBox.question(
            self, "删除任务",
            f"删除任务 {job_id[:8]} 及其已保存的图片？此操作不可恢复。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.delete_button.setEnabled(False)

        def completed(value, error):
            if error:
                QMessageBox.warning(self, "删除失败", error)
                self.refresh_selected()
                return
            self.latest = [job for job in self.latest if job["job_id"] != job_id]
            self.older = [job for job in self.older if job["job_id"] != job_id]
            if self.selected == job_id:
                self.selected = None
                self.selected_status = None
                self.prompt_view.clear()
                self.copy_prompt_button.setEnabled(False)
                self.clear_image("任务已删除")
                self.detail.setText("从历史中选择任务查看详情")
            self.render_history()
            self.refresh()

        self.request_once(f"delete:{job_id}", f"/api/v1/jobs/{job_id}", completed, delete=True)

    def cancel(self):
        if not self.selected:
            return
        job_id = self.selected
        self.cancel_button.setEnabled(False)

        def completed(value, error):
            if self.selected == job_id and value:
                self.show_job(value)
            if error:
                self.form_note.setText(error)
            self.refresh()

        self.request_once(f"cancel:{job_id}", f"/api/v1/jobs/{job_id}/cancel", completed, post=True)

    def save_png(self):
        png = self.png
        if png is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "另存图片", "LocalGen.png", "PNG 图片 (*.png)")
        if path:
            if not path.lower().endswith(".png"):
                path += ".png"
            file = QSaveFile(path)
            if not file.open(QIODevice.OpenModeFlag.WriteOnly) or file.write(png) != len(png) or not file.commit():
                QMessageBox.warning(self, "保存失败", "无法写入所选位置。")

    def preferences(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("LocalGen 设置")
        dialog.setMinimumWidth(550)
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        mode = QComboBox()
        mode.addItem("ComfyUI · NVIDIA GPU", "comfy")
        mode.addItem("演示 · 程序绘图", "mock")
        mode.setCurrentIndex(mode.findData(self.settings.engine_mode))
        form.addRow("引擎模式", mode)
        root = QLineEdit(self.settings.engine_root)
        python = QLineEdit(self.settings.engine_python)
        python.setPlaceholderText("留空使用 Portable 自带的 Python")

        def browse(control, directory):
            selected = (QFileDialog.getExistingDirectory(dialog, "选择 ComfyUI Portable 目录", control.text())
                        if directory else QFileDialog.getOpenFileName(
                            dialog, "选择独立 Python", control.text(), "Python (python.exe)"
                        )[0])
            if selected:
                control.setText(selected)

        for name, control, directory in (("引擎目录", root, True), ("Python 路径", python, False)):
            row = QHBoxLayout()
            row.addWidget(control, 1)
            row.addWidget(button("浏览", lambda _, field=control, folder=directory: browse(field, folder)))
            form.addRow(name, row)
        api_port, engine_port = QSpinBox(), QSpinBox()
        for title, field, value in (("LocalGen 端口", api_port, self.settings.api_port),
                                    ("ComfyUI 端口", engine_port, self.settings.engine_port)):
            field.setRange(1024, 65535)
            # 某些区域格式（如中文·澳门）向 Qt 报告非 ASCII 本地数字，端口显示必须固定为 ASCII 数字。
            field.setLocale(QLocale.c())
            field.setValue(value)
            form.addRow(title, field)
        autostart = QCheckBox("开机自动启动（登录后常驻系统托盘，供本机插件随时调用）")
        autostart.setChecked(autostart_enabled())
        form.addRow("", autostart)
        layout.addLayout(form)
        layout.addWidget(label("保存后 LocalGen 会退出，重新打开即可使用新设置。", "caption", True))
        row = QHBoxLayout()
        row.addWidget(button("打开诊断日志", lambda: QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(self.data / "logs")))))
        row.addWidget(button("复制 API 令牌", lambda: QApplication.clipboard().setText(self.token)))
        row.addStretch()
        row.addWidget(button("关闭", dialog.reject))

        def save():
            try:
                value = Settings.model_validate(self.settings.model_dump() | {
                    "engine_mode": mode.currentData(), "engine_root": root.text().strip(),
                    "engine_python": python.text().strip(), "api_port": api_port.value(),
                    "engine_port": engine_port.value(),
                })
                save_settings(self.data, value)
                set_autostart(autostart.isChecked())
            except (ValueError, OSError):
                QMessageBox.warning(dialog, "设置未保存", "请检查本机绝对路径、端口和数据目录。")
                return
            dialog.accept()
            self.real_exit = True
            self.close()

        row.addWidget(button("保存并退出", save))
        layout.addLayout(row)
        dialog.exec()


def dsh_tray_icon():
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#3d5bd9"))
    painter.drawRoundedRect(2, 2, 28, 28, 8, 8)
    painter.setPen(QColor("white"))
    font = painter.font()
    font.setBold(True)
    font.setPixelSize(13)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "LG")
    painter.end()
    return QIcon(pixmap)


def run_desktop(server, sock, data, settings, token, serve):
    app = QApplication([])
    app.setApplicationName("LocalGen")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    app.setQuitOnLastWindowClosed(False)
    thread = threading.Thread(target=serve, args=(server, sock), name="LocalGen API")
    thread.start()
    window = Window(data, settings, token)
    window.show()

    def shutdown():
        window.stopping = True
        window.timer.stop()
        server.should_exit = True

    tray = QSystemTrayIcon(dsh_tray_icon())
    tray_menu = QMenu()
    open_action = tray_menu.addAction("打开 LocalGen")
    exit_action = tray_menu.addAction("退出")

    def open_window():
        window.show()
        window.raise_()
        window.activateWindow()

    open_action.triggered.connect(open_window)

    def quit_app():
        window.real_exit = True
        window.close()

    exit_action.triggered.connect(quit_app)
    tray.setContextMenu(tray_menu)
    tray.setToolTip("LocalGen · 本地生图工作台")
    tray.activated.connect(lambda reason: open_window()
                           if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                                         QSystemTrayIcon.ActivationReason.DoubleClick) else None)
    tray.show()
    window.tray = tray
    window.installEventFilter(close_filter := CloseFilter(window, shutdown))
    monitor = QTimer()

    def check():
        if not thread.is_alive():
            if not window.stopping:
                window.stopping = True
                QMessageBox.critical(window, "本机服务已停止", "请在诊断日志中查看启动或运行错误。")
            app.quit()

    monitor.timeout.connect(check)
    monitor.start(150)
    code = app.exec()
    server.should_exit = True
    thread.join()
    return code


class CloseFilter(QObject):
    def __init__(self, window, shutdown):
        super().__init__(window)
        self.window = window
        self.shutdown = shutdown

    def eventFilter(self, obj, event):
        if obj is self.window and event.type() == QEvent.Type.Close:
            if getattr(self.window, "real_exit", False):
                self.shutdown()
            else:
                self.window.hide()
                tray = getattr(self.window, "tray", None)
                if tray is not None and not self.window.tray_hint_shown:
                    self.window.tray_hint_shown = True
                    tray.showMessage("LocalGen", "已缩到系统托盘：点击图标打开，右键菜单退出",
                                     QSystemTrayIcon.MessageIcon.Information, 3000)
            return True
        return super().eventFilter(obj, event)
