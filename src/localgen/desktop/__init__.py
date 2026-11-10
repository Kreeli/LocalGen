import json
import logging
import re
import sys
import threading
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from uuid import UUID, uuid4

from PySide6.QtCore import QEvent, QIODevice, QLocale, QObject, QSaveFile, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import (QColor, QDesktopServices, QIcon, QImageReader, QKeySequence, QPainter,
                           QPixmap, QShortcut)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSlider, QSpinBox,
    QSplitter, QSystemTrayIcon, QVBoxLayout, QWidget,
)

from localgen.config import Settings, save_settings
from localgen.storage.images import GARMENT_NAME, POSE_NAME

from .client import Client
from .reference import ReferencePicker
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
    "SDCPP_ACTIVE": "sdcpp 引擎运行中。",
}
ROLE = Qt.ItemDataRole.UserRole
TERMINAL = ("succeeded", "failed", "cancelled", "interrupted")
# 状态色：一眼认出来，而不是读四遍中文（C9）。
STATUS_COLORS = {"succeeded": "#8ad6a8", "failed": "#ff9c9c", "cancelled": "#98a1b8",
                 "interrupted": "#e4c393", "running": "#c8b6ff", "submitting": "#c8b6ff",
                 "cancelling": "#e4c393", "queued": "#9ca7c0"}
NOTES = ("提示词最多 2000 字符。", "种子范围为 0–4294967295。",
         "此模型必须选择一张参考图（可拖入图片）。")
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
        self.setMinimumSize(900, 680)
        self.resize(1180, 860)
        self.selected = None
        self.selected_status = None
        self.real_exit = False
        self.tray_hint_shown = False
        self._history_items = {}
        self.reference_required = False
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
        self.ui_workflow = None
        self.ui_size = None
        self.status = {}
        self.build()
        self.shortcuts()
        self.load_ui()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1100)
        QTimer.singleShot(0, self.refresh_workflows)
        QTimer.singleShot(0, self.refresh_history)
        QTimer.singleShot(0, self.refresh)

    def build(self):
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        header = card("header")
        row = QHBoxLayout(header)
        row.setContentsMargins(14, 7, 10, 7)
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
        self.columns = QSplitter(Qt.Orientation.Horizontal)
        self.columns.setObjectName("columns")
        self.columns.setChildrenCollapsible(False)
        self.columns.setHandleWidth(9)
        self.columns.addWidget(self.editor())
        self.split = QSplitter(Qt.Orientation.Vertical)
        self.split.setObjectName("split")
        self.split.setChildrenCollapsible(False)
        self.split.setHandleWidth(9)
        self.split.addWidget(self.preview())
        self.history_panel = self.history()
        self.split.addWidget(self.history_panel)
        # 详情搬进历史卡右侧后，历史要多给一点高度（≥220 才放得下 7~8 行）。
        self.split.setStretchFactor(0, 3)
        self.split.setStretchFactor(1, 2)
        self.split.setSizes([470, 330])
        self.columns.addWidget(self.split)
        self.columns.setStretchFactor(0, 0)
        self.columns.setStretchFactor(1, 1)
        self.columns.setSizes([350, 830])
        columns.addWidget(self.columns, 1)

        self.footer = label("本机 REST 与 MCP · 127.0.0.1:" + str(self.settings.api_port), "caption")
        layout.addWidget(self.footer)

    def editor(self):
        panel = card()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(7)
        layout.addWidget(label("生成图片", "heading"))
        workflow_row = QHBoxLayout()
        workflow_row.setSpacing(6)
        self.workflow = QComboBox()
        self.workflow.currentIndexChanged.connect(self.workflow_changed)
        workflow_row.addWidget(self.workflow, 1)
        self.export_button = button("导出", self.export_plugin_json)
        self.export_button.setToolTip("导出 dsh-image-gen 插件工作流 JSON（文生图 / 图生图）")
        self.export_button.setFixedHeight(34)
        workflow_row.addWidget(self.export_button)
        layout.addLayout(workflow_row)
        self.requirements = label("正在检查引擎和工作流…", "notice", True)
        self.requirements.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.requirements)
        layout.addWidget(label("提示词", "caption"))
        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText("描述画面、光线和构图…")
        self.prompt.textChanged.connect(self.controls)
        layout.addWidget(self.prompt, 2)
        layout.addWidget(label("负面提示词 · 可选", "caption"))
        self.negative = QPlainTextEdit()
        self.negative.setPlaceholderText("例如 blurry, watermark")
        self.negative.setMaximumHeight(72)
        self.negative.textChanged.connect(self.controls)
        layout.addWidget(self.negative)
        parameters = QHBoxLayout()
        parameters.setSpacing(8)
        self.size = QComboBox()
        self.size.currentIndexChanged.connect(self.size_changed)
        self.seed = QLineEdit()
        self.seed.setMaxLength(10)
        self.seed.setPlaceholderText("留空随机")
        self.seed.textChanged.connect(self.controls)
        for title, widget in (("分辨率", self.size), ("种子", self.seed)):
            field = QVBoxLayout()
            field.setSpacing(3)
            field.addWidget(label(title, "caption"))
            field.addWidget(widget)
            parameters.addLayout(field, 1)
        layout.addLayout(parameters)
        self.reference = ReferencePicker(self.api.upload_image)
        self.reference.changed.connect(self.controls)
        layout.addWidget(self.reference)
        # 图库选择器：只在支持 pose_id/garment_id 的工作流上出现（见 library_rows）。
        self.pose_picker = None
        self.garment_picker = None
        self.library_slot = QVBoxLayout()   # 选择器的落点，「参考图」下方
        self.library_slot.setContentsMargins(0, 0, 0, 0)
        self.library_slot.setSpacing(4)
        layout.addLayout(self.library_slot)
        self.strength_box = QWidget()
        strength_layout = QVBoxLayout(self.strength_box)
        strength_layout.setContentsMargins(0, 2, 0, 0)
        strength_layout.setSpacing(2)
        strength_head = QHBoxLayout()
        strength_head.addWidget(label("重绘强度 · 图生图", "caption"))
        strength_head.addStretch()
        self.strength_value = label("0.50", "caption")
        strength_head.addWidget(self.strength_value)
        strength_layout.addLayout(strength_head)
        self.strength_slider = QSlider(Qt.Orientation.Horizontal)
        self.strength_slider.setRange(5, 100)
        self.strength_slider.setValue(50)
        self.strength_slider.setToolTip("越小越接近参考图；越大越放手重绘（0.45–0.55 通常最稳）")
        self.strength_slider.valueChanged.connect(
            lambda value: self.strength_value.setText(f"{value / 100:.2f}"))
        strength_layout.addWidget(self.strength_slider)
        layout.addWidget(self.strength_box)
        self.strength_box.setVisible(False)
        # 两段式（姿势+服装同时选中）的分段步数：0 = 跟随模板默认（8 步 turbo）。
        # 只在支持的工作流（pose-swap 1.1.0+）出现；调高后引擎自动弃用 turbo 预设。
        self.stage_steps_box = QWidget()
        steps_layout = QHBoxLayout(self.stage_steps_box)
        steps_layout.setContentsMargins(0, 2, 0, 0)
        steps_layout.setSpacing(6)
        steps_layout.addWidget(label("分段步数", "caption"))
        self.pose_steps_spin = QSpinBox()
        self.pose_steps_spin.setRange(0, 25)
        self.pose_steps_spin.setSpecialValueText("默认")
        self.pose_steps_spin.setPrefix("姿势 ")
        self.pose_steps_spin.setToolTip("姿势段步数：默认 15；8 步出图偏模糊，调低会更快但更糊")
        steps_layout.addWidget(self.pose_steps_spin)
        self.garment_steps_spin = QSpinBox()
        self.garment_steps_spin.setRange(0, 25)
        self.garment_steps_spin.setSpecialValueText("默认")
        self.garment_steps_spin.setPrefix("换装 ")
        self.garment_steps_spin.setToolTip("换装段步数：默认 15；服装是局部编辑，跟姿势段保持一致即可")
        steps_layout.addWidget(self.garment_steps_spin)
        steps_layout.addStretch()
        layout.addWidget(self.stage_steps_box)
        self.stage_steps_box.setVisible(False)
        layout.addStretch()
        scroll = QScrollArea()
        scroll.setObjectName("editor")
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(panel)
        # 生成按钮固定在左栏底部：窗口再小也不用来回滚动才能出图。
        column = QWidget()
        # 死宽 350 → 可拖的 330~430：宽屏能多给提示词，窄屏也不会挤掉预览。
        column.setMinimumWidth(330)
        column.setMaximumWidth(430)
        outer = QVBoxLayout(column)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)
        outer.addWidget(scroll, 1)
        self.generate = button("生成一张", self.submit, "primary")
        self.generate.setEnabled(False)
        outer.addWidget(self.generate)
        self.form_note = label("", "notice", True)
        self.form_note.setTextFormat(Qt.TextFormat.PlainText)
        outer.addWidget(self.form_note)
        return column

    def size_changed(self):
        size = self.size.currentData()
        if size:
            self.reference.set_target(*size)
        self.controls()

    # ---------- 快捷键与布局记忆（C8 / C10） ----------
    def shortcuts(self):
        QShortcut(QKeySequence("Ctrl+Return"), self, self.submit)
        QShortcut(QKeySequence("Ctrl+Enter"), self, self.submit)
        QShortcut(QKeySequence.StandardKey.Save, self, self.save_png)
        # Delete 只挂在历史列表上：挂在窗口上会把输入框里的删除键一起吃掉。
        QShortcut(QKeySequence(Qt.Key.Key_Delete), self.list, self.delete_job)

    def ui_path(self):
        return self.data / "ui.json"

    def load_ui(self):
        """上一次的窗口大小与分栏比例（只写 data/ui.json，不碰注册表）。

        用 `utf-8-sig`：手改过这个文件（记事本 / PowerShell 5.1）就会带 BOM，
        而 `json.loads` 遇到 BOM 抛 ValueError —— 原来被下面的 except 当"文件不存在"
        吞掉，表现是布局静默丢失、用户以为"又没记住"。utf-8-sig 对无 BOM 文件等价。
        """
        try:
            saved = json.loads(self.ui_path().read_text("utf-8-sig"))
        except (OSError, ValueError):
            logger.debug("UI_STATE_UNREADABLE", exc_info=True)
            return
        if not isinstance(saved, dict):
            return
        try:
            width = max(900, int(saved.get("width") or 1180))
            height = max(680, int(saved.get("height") or 860))
        except (TypeError, ValueError):
            width, height = 1180, 860
        self.resize(width, height)
        x, y = saved.get("x"), saved.get("y")
        if isinstance(x, int) and isinstance(y, int):
            self.move(x, y)
        columns = saved.get("columns")
        if isinstance(columns, list) and len(columns) == 2:
            self.columns.setSizes([max(330, int(columns[0])), max(420, int(columns[1]))])
        split = saved.get("split")
        if isinstance(split, list) and len(split) == 2:
            self.split.setSizes([max(220, int(split[0])), max(190, int(split[1]))])
        history = saved.get("history")
        if isinstance(history, list) and len(history) == 2:
            self.history_split.setSizes([max(280, int(history[0])), max(240, int(history[1]))])
        self.ui_workflow = saved.get("workflow")
        self.ui_size = saved.get("size")

    def save_ui(self):
        size = self.size.currentData()
        payload = {"width": self.width(), "height": self.height(),
                   "x": self.x(), "y": self.y(),
                   "columns": self.columns.sizes(), "split": self.split.sizes(),
                   "history": self.history_split.sizes(),
                   "workflow": self.workflow.currentData(),
                   "size": list(size) if size else None}
        try:
            self.ui_path().write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
        except OSError:
            logger.debug("UI_STATE_UNSAVED", exc_info=True)

    def preview(self):
        panel = card()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)
        title = QHBoxLayout()
        title.addWidget(label("预览", "heading"))
        title.addStretch()
        self.zoom_button = button("放大预览", self.toggle_zoom)
        title.addWidget(self.zoom_button)
        self.cancel_button = button("取消任务", self.cancel)
        self.copy_button = button("复制图片", self.copy_image)
        self.save_button = button("另存 PNG", self.save_png)
        for control in (self.cancel_button, self.copy_button, self.save_button):
            control.setEnabled(False)
            title.addWidget(control)
        layout.addLayout(title)
        # 当前任务条：与"选中的历史条目"解耦 —— 取消按钮跟它走，而不是跟选中项走。
        self.current_bar = label("", "caption")
        self.current_bar.setTextFormat(Qt.TextFormat.PlainText)
        self.current_bar.setObjectName("currentJob")
        layout.addWidget(self.current_bar)
        self.current_bar.setVisible(False)
        self.canvas = card("canvas")
        self.canvas.setMinimumHeight(220)
        self.canvas.installEventFilter(self)
        canvas_layout = QVBoxLayout(self.canvas)
        canvas_layout.setContentsMargins(0, 0, 0, 0)
        canvas_layout.setSpacing(0)
        self.image = label("", "placeholder", True)
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setTextFormat(Qt.TextFormat.PlainText)
        self.image.setContentsMargins(0, 0, 0, 0)
        self.image.setMinimumSize(1, 1)
        self.image.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.image.setVisible(False)
        canvas_layout.addWidget(self.image)
        # 空状态：两行文案 + 一个真能点的出口，而不是一行灰字晾在那儿。
        self.empty = QWidget()
        empty_layout = QVBoxLayout(self.empty)
        empty_layout.setContentsMargins(0, 0, 0, 0)
        empty_layout.setSpacing(10)
        empty_layout.addStretch()
        self.empty_text = label("还没有图片\n生成一张，或从下方历史里选一条", "placeholder", True)
        self.empty_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(self.empty_text)
        empty_row = QHBoxLayout()
        empty_row.addStretch()
        empty_row.addWidget(button("打开输出目录", self.open_output_dir))
        empty_row.addStretch()
        # 只有真正"闲着没图"时才给出口按钮；"正在加载/等待生成"这类临时文案不配按钮。
        self.empty_actions = QWidget()
        actions_layout = QVBoxLayout(self.empty_actions)
        actions_layout.setContentsMargins(0, 0, 0, 0)
        actions_layout.addLayout(empty_row)
        empty_layout.addWidget(self.empty_actions)
        empty_layout.addStretch()
        canvas_layout.addWidget(self.empty)
        self.canvas.setProperty("filled", "no")
        layout.addWidget(self.canvas, 1)
        # 进度条 + 右侧数字：纯细条看不出在动，必须配 "12 / 30 · 已用 3:12"。
        self.progress_row = QWidget()
        progress_layout = QHBoxLayout(self.progress_row)
        progress_layout.setContentsMargins(0, 0, 0, 0)
        progress_layout.setSpacing(10)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        progress_layout.addWidget(self.progress, 1)
        self.progress_text = label("", "caption")
        progress_layout.addWidget(self.progress_text)
        layout.addWidget(self.progress_row)
        self.progress_row.hide()
        return panel

    def details(self):
        """任务详情一栏：字段 + 参考图 + 动作 + 完整提示词。

        放在历史卡右侧（方案 §3.2 方案 A）：那里本来一直空着一条横向空间，
        而预览卡要留给图片 —— 详情挤在图片下面会把画布压成一条缝。
        """
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 2, 2, 2)
        layout.setSpacing(8)
        self.detail = label("从历史中选择任务查看详情", "caption", True)
        self.detail.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.detail)
        # 动作条紧跟字段：小窗口下详情栏要滚动，按钮必须在上半屏就够得着。
        # 2×2 网格而不是一行四个：详情栏最窄只有 280 px，一行放不下四个中文按钮。
        self.actions = QWidget()
        action_grid = QGridLayout(self.actions)
        action_grid.setContentsMargins(0, 0, 0, 0)
        action_grid.setSpacing(6)
        self.reuse_button = button("复用参数", lambda: self.reuse_parameters())
        self.reuse_button.setToolTip("把这条任务的提示词 / 尺寸 / 种子 / 强度 / 参考图整组回填左栏（不自动提交）")
        self.rerun_button = button("同参重跑", self.rerun_job)
        self.rerun_button.setToolTip("同样的参数，换一个新的随机种子再提交一张")
        self.copy_json_button = button("复制参数", self.copy_parameters)
        self.copy_json_button.setToolTip("把这条任务的参数复制成一行 JSON（含 workflow 与 revision）")
        self.folder_button = button("打开文件夹", self.open_results)
        self.folder_button.setToolTip("在资源管理器中打开这条任务的成品目录")
        self.log_button = button("引擎日志", self.open_log)
        self.log_button.setToolTip("打开这条任务自己的 sd-cli / ComfyUI 日志（三段耗时都在里面）")
        for position, control in enumerate((self.reuse_button, self.rerun_button,
                                            self.copy_json_button, self.folder_button,
                                            self.log_button)):
            control.setEnabled(False)
            action_grid.addWidget(control, position // 2, position % 2)
        layout.addWidget(self.actions)
        self.actions.setVisible(False)
        # 参考图实体（引擎实际吃到的那一份）+ 一行"原图多大 → 裁成多大"的说明。
        self.reference_block = QWidget()
        reference_detail = QHBoxLayout(self.reference_block)
        reference_detail.setContentsMargins(0, 0, 0, 0)
        reference_detail.setSpacing(10)
        self.reference_thumb = QLabel()
        self.reference_thumb.setObjectName("referenceThumb")
        self.reference_thumb.setFixedSize(72, 72)
        self.reference_thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.reference_thumb.setToolTip("点击用系统图片查看器打开这张参考图")
        self.reference_thumb.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reference_thumb.installEventFilter(self)
        reference_detail.addWidget(self.reference_thumb)
        self.reference_caption = label("", "caption", True)
        self.reference_caption.setTextFormat(Qt.TextFormat.PlainText)
        reference_detail.addWidget(self.reference_caption, 1)
        layout.addWidget(self.reference_block)
        self.reference_block.setVisible(False)
        self.reference_source = None
        # 姿势 / 服装：图库随时可能被重新导入或删掉，历史里要能一直看到
        # "当时选的是哪一张"（用户反馈：历史记录里看不到姿势和服装的图）。
        # 两张缩略图竖着排 —— 详情栏最窄只有 280 px，横排会把说明挤成一条缝。
        self.library_block = QWidget()
        library_layout = QVBoxLayout(self.library_block)
        library_layout.setContentsMargins(0, 0, 0, 0)
        library_layout.setSpacing(6)
        self.pose_thumb, self.pose_caption = self.library_row(library_layout, "姿势")
        self.garment_thumb, self.garment_caption = self.library_row(library_layout, "服装")
        layout.addWidget(self.library_block)
        self.library_block.setVisible(False)
        self.library_source = {}
        # 提示词整块只在有选中任务时出现：平时把纵向空间全留给列表。
        self.prompt_block = QWidget()
        prompt_layout = QVBoxLayout(self.prompt_block)
        prompt_layout.setContentsMargins(0, 0, 0, 0)
        prompt_layout.setSpacing(4)
        prompt_row = QHBoxLayout()
        prompt_row.addWidget(label("提示词", "caption"))
        prompt_row.addStretch()
        self.copy_prompt_button = button("复制提示词", self.copy_prompt)
        self.copy_prompt_button.setEnabled(False)
        prompt_row.addWidget(self.copy_prompt_button)
        prompt_layout.addLayout(prompt_row)
        self.prompt_view = QPlainTextEdit()
        self.prompt_view.setReadOnly(True)
        # 46 px 只有两行：长提示词滚不动也看不全，用户反馈"完全看不见"。
        # 110 px 约 4~5 行，配合详情区的滚动条看长文。
        self.prompt_view.setFixedHeight(110)
        self.prompt_view.setPlaceholderText("选中任务后显示完整提示词")
        self.prompt_view.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        prompt_layout.addWidget(self.prompt_view)
        layout.addWidget(self.prompt_block)
        self.prompt_block.setVisible(False)
        # 负面提示词整块：只有任务真的带负面提示词才出现，平时不占地方。
        self.negative_block = QWidget()
        negative_layout = QVBoxLayout(self.negative_block)
        negative_layout.setContentsMargins(0, 0, 0, 0)
        negative_layout.setSpacing(4)
        negative_row = QHBoxLayout()
        negative_row.addWidget(label("负面提示词", "caption"))
        negative_row.addStretch()
        self.copy_negative_button = button("复制负面", self.copy_negative)
        self.copy_negative_button.setEnabled(False)
        negative_row.addWidget(self.copy_negative_button)
        negative_layout.addLayout(negative_row)
        self.negative_view = QPlainTextEdit()
        self.negative_view.setReadOnly(True)
        self.negative_view.setFixedHeight(72)
        self.negative_view.setPlaceholderText("这条任务没有负面提示词")
        self.negative_view.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        negative_layout.addWidget(self.negative_view)
        layout.addWidget(self.negative_block)
        self.negative_block.setVisible(False)
        layout.addStretch()
        scroll = QScrollArea()
        scroll.setObjectName("detail")
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(panel)
        return scroll

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
        # 历史卡左右分栏：左 = 任务列表，右 = 选中任务详情（可拖，方案 §3.2 方案 A）。
        self.history_split = QSplitter(Qt.Orientation.Horizontal)
        self.history_split.setObjectName("history")
        self.history_split.setChildrenCollapsible(False)
        self.history_split.setHandleWidth(9)
        self.list = QListWidget()
        self.list.setAccessibleName("任务历史")
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.list.setUniformItemSizes(True)
        self.list.setMinimumHeight(120)
        self.list.setMinimumWidth(220)
        self.list.currentItemChanged.connect(self.select_item)
        self.history_split.addWidget(self.list)
        details = self.details()
        details.setMinimumWidth(280)
        self.history_split.addWidget(details)
        self.history_split.setStretchFactor(0, 3)
        self.history_split.setStretchFactor(1, 2)
        self.history_split.setSizes([430, 320])
        layout.addWidget(self.history_split)
        self.history_panel = panel
        panel.setMinimumHeight(190)
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
        # 稳态只打两路（原来是三路）：/status 说"谁在跑/排第几"，/engine 说"引擎能不能用"。
        # 模型清单与历史列表都不随秒变，改成事件驱动 —— 否则每 1.1 s 白拉两张表。
        self.request_once("status", "/api/v1/status", self.update_status)
        self.request_once("engine", "/api/v1/engine", self.update_engine)
        self.refresh_selected()

    def refresh_workflows(self):
        self.request_once("workflows", "/api/v1/workflows", self.update_workflows)

    def refresh_history(self):
        self.request_once("history", "/api/v1/jobs?limit=20", self.update_history)

    def update_status(self, value, error):
        """`/status` 驱动"当前任务条"与取消按钮，顺带当历史列表的刷新门槛。"""
        if error or not value:
            return
        previous = (self.status or {}).get("active_job")
        self.status = value
        active = value.get("active_job")
        stage = value.get("stage")
        if active:
            title = self.workflow_title(value.get("workflow_id"))
            stage_name = {"loading": "加载模型", "sampling": "采样",
                          "decoding": "解码"}.get(stage or "", stage or "准备中")
            text = f"正在跑 {title}  ·  {stage_name}"
            elapsed = value.get("elapsed_s")
            if elapsed is not None:
                text += f"  ·  已用 {self.span(elapsed)}"
            queued = value.get("queue_length") or 0
            if queued:
                text += f"  ·  队列 {queued}"
        elif value.get("queued_job_id"):
            text = f"队列 {value.get('queue_length') or 1} 条  ·  等待引擎"
        else:
            text = ""
        self.current_bar.setText(text)
        self.current_bar.setVisible(bool(text))
        self.refresh_cancel()
        if previous and not active:
            # 刚跑完一条 → 列表和选中项都该更新了（这是唯一需要拉历史列表的时刻）。
            self.refresh_history()
            self.refresh_selected()

    def update_engine(self, value, error):
        previous = (self.engine or {}).get("state")
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
        if state == "ready" and previous != "ready":
            # 冷启动窗口里拉到的工作流列表 available=False 会一直缓存（列表只在
            # 提交/删除/完成事件时重拉）——引擎就绪是唯一必须补拉的时机，否则
            # "生成一张"永远灰着（用户反馈：引擎就绪但按钮点不动）。
            self.refresh_workflows()
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
        remembered = self.ui_workflow
        self.ui_workflow = None
        if remembered and self.workflow.findData(remembered) >= 0:
            # 上次用的模型还在 → 打开就落在它上面（只认一次，之后跟着人走）
            self.workflow.setCurrentIndex(self.workflow.findData(remembered))
        self.workflow.blockSignals(False)
        self.workflow_changed()

    def current_workflow(self):
        return next((item for item in self.workflows or [] if item["workflow_id"] == self.workflow.currentData()), {})

    def workflow_changed(self):
        item = self.current_workflow()
        workflow_id = item.get("workflow_id")
        size = self.size.currentData() if workflow_id == self.size_workflow_id else item.get("default_size")
        self.size_workflow_id = workflow_id
        self.size.blockSignals(True)
        self.size.clear()
        for width, height in item.get("verified_sizes", []):
            self.size.addItem(f"{width} × {height}", [width, height])
        index = self.size.findData(size if size is not None else item.get("default_size"))
        if index >= 0:
            self.size.setCurrentIndex(index)
        remembered = self.ui_size
        self.ui_size = None
        if remembered and len(remembered) == 2:
            position = self.size.findData(list(remembered))
            if position >= 0:
                self.size.setCurrentIndex(position)
        self.size.blockSignals(False)
        supported = item.get("supported_parameters", [])
        self.negative.setEnabled("negative_prompt" in supported)
        supports_image = "image" in supported
        self.reference_required = supports_image and "image" in item.get("required_parameters", [])
        if supports_image:
            self.reference.set_locked(
                False, "此模型必须提供参考图" if self.reference_required else "")
        else:
            self.reference.set_locked(True, "此模型不支持参考图")
        self.strength_box.setVisible("strength" in supported)
        self.library_rows(item)
        default_strength = item.get("default_strength")
        if isinstance(default_strength, (int, float)) and 0 < float(default_strength) <= 1:
            self.strength_slider.setValue(int(round(float(default_strength) * 100)))
        if item.get("missing_dependencies"):
            text = "缺少依赖\n" + "\n".join(item["missing_dependencies"])
        elif item.get("available"):
            text = f"版本 {item['revision']} · 单张 PNG"
        else:
            text = DIAGNOSTICS.get(item.get("diagnostic_code"), "暂无可用工作流")
        self.requirements.setText(text)
        self.size_changed()

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
        suffix = "-edit" if variant == "image2image" else ""
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

    def library_rows(self, item):
        """按工作流能力显示/隐藏「姿势」「服装」选择器。

        懒创建：只有真正支持的工作流才付构建代价，也避免不支持时白占地方。
        """
        supported = item.get("supported_parameters", [])
        wanted = (("pose_id", "poses", "姿势 · 换姿势参考", "未选择姿势（仅做指令编辑）",
                   "pose_picker"),
                  ("garment_id", "garments", "服装 · 换装参考", "未选择服装",
                   "garment_picker"))
        for name, kind, title, hint, attribute in wanted:
            needs = name in supported
            picker = getattr(self, attribute, None)
            if needs and picker is None:
                from localgen.desktop.library import LibraryPicker

                picker = LibraryPicker(self.api, kind, title, hint)
                picker.setObjectName("libraryPicker")
                picker.changed.connect(self.controls)
                self.library_slot.addWidget(picker)
                setattr(self, attribute, picker)
            if picker is not None:
                picker.setVisible(needs)
        # 分段步数：两段式（姿势+服装）才有意义，能力声明驱动显隐。
        self.stage_steps_box.setVisible("pose_steps" in supported)

    def controls(self):
        if not hasattr(self, "generate"):
            return
        prompt = self.prompt.toPlainText().strip()
        seed = self.seed.text()
        valid_seed = not seed or (re.fullmatch(r"[0-9]+", seed) and int(seed) <= 4294967295)
        workflow = self.current_workflow()
        note = ""
        # 换姿势/换装：参考图可以由人像上传 + 图库选中组合提供，任一即可。
        library_chosen = any(
            getattr(self, attribute, None) is not None and getattr(self, attribute).selected_id()
            for attribute in ("pose_picker", "garment_picker"))
        has_reference = bool(self.reference.token) or library_chosen
        if len(prompt) > 2000 or len(self.negative.toPlainText().strip()) > 2000:
            note = NOTES[0]
        elif not valid_seed:
            note = NOTES[1]
        elif self.reference_required and not has_reference:
            note = NOTES[2]
        if note:
            self.form_note.setText(note)
        elif self.form_note.text() in NOTES:
            self.form_note.clear()
        enabled = (self.engine.get("available") and workflow.get("available")
                   and self.size.currentData() and prompt and not note
                   and not self.submitting and not self.stopping)
        self.generate.setEnabled(bool(enabled))

    def submit(self):
        if not self.generate.isEnabled():
            return
        width, height = self.size.currentData()
        supported = self.current_workflow().get("supported_parameters", [])
        payload = {"workflow_id": self.workflow.currentData(), "prompt": self.prompt.toPlainText().strip(),
                   "width": width, "height": height}
        if self.seed.text():
            payload["seed"] = int(self.seed.text())
        if self.negative.isEnabled() and self.negative.toPlainText().strip():
            payload["negative_prompt"] = self.negative.toPlainText().strip()
        if self.reference.token and "image" in supported:
            payload["image"] = self.reference.token
            if "strength" in supported:
                payload["strength"] = round(self.strength_slider.value() / 100, 2)
        # 图库选择：姿势 / 服装作为额外参考图，由服务端解析成素材路径。
        for name, attribute in (("pose_id", "pose_picker"), ("garment_id", "garment_picker")):
            picker = getattr(self, attribute, None)
            if picker is not None and name in supported:
                chosen = picker.selected_id()
                if chosen:
                    payload[name] = chosen
        # 分段步数：0 = 模板默认（8 步 turbo），不进 payload。
        for name, spin in (("pose_steps", self.pose_steps_spin),
                           ("garment_steps", self.garment_steps_spin)):
            if name in supported and spin.value() > 0:
                payload[name] = int(spin.value())
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
            self.refresh_workflows()
            self.refresh_history()

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
        # 增量同步：仅新增/更新/移除变化的条目，避免整表重建导致的滚动跳动。
        self.list.blockSignals(True)
        scroll = self.list.verticalScrollBar().value()
        jobs = self.latest + self.older
        seen = set()
        for row, job in enumerate(jobs):
            jid = job["job_id"]
            seen.add(jid)
            prompt = " ".join(job["parameters"]["prompt"].split())
            text = (f"{STATUS[job['status']]}  ·  "
                    f"{datetime.fromisoformat(job['created_at']).astimezone().strftime('%m-%d %H:%M')}"
                    f"  ·  {prompt[:90]}")
            item = self._history_items.get(jid)
            if item is None:
                item = QListWidgetItem(text)
                item.setData(ROLE, jid)
                self._history_items[jid] = item
                self.list.insertItem(row, item)
            else:
                if item.text() != text:
                    item.setText(text)
                if self.list.row(item) != row:
                    self.list.takeItem(self.list.row(item))
                    self.list.insertItem(row, item)
            item.setToolTip(f"<span style='white-space:pre-wrap'>{escape(prompt)}</span>")
            item.setForeground(QColor(STATUS_COLORS.get(job["status"], "#c3cbe0")))
        for jid in [key for key in self._history_items if key not in seen]:
            stale = self._history_items.pop(jid)
            row = self.list.row(stale)
            if row >= 0:
                self.list.takeItem(row)
        # 只在真的换了才重设：每 1.1 s 无脑 setCurrentItem 会 scrollToItem，列表就回弹。
        wanted = self._history_items.get(self.selected)
        if wanted is not None and self.list.currentItem() is not wanted:
            self.list.setCurrentItem(wanted)
        self.list.blockSignals(False)
        QTimer.singleShot(0, lambda: self.list.verticalScrollBar().setValue(scroll))
        self.more.setEnabled(bool(self.cursor))
        self.refresh_cancel()

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
        self.prompt_block.setVisible(False)
        self.copy_prompt_button.setEnabled(False)
        self.hide_actions()
        self.clear_image("正在读取任务…")
        self.cancel_button.setEnabled(False)
        self.refresh_selected()

    def refresh_selected(self):
        job_id = self.selected
        if job_id:
            self.request_once(f"job:{job_id}", f"/api/v1/jobs/{job_id}",
                              lambda value, error: self.show_job(value) if value and self.selected == job_id else None)

    def clear_image(self, text=None):
        """清空预览。`text=None` = 空闲空状态（带出口按钮），给了文案 = 临时状态。"""
        self.png = self.pixmap = self.artifact = None
        self.image.clear()
        self.image.setText("")
        idle = not text
        self.empty_text.setText(text or "还没有图片\n生成一张，或从下方历史里选一条")
        self.empty_actions.setVisible(idle)
        self.show_canvas(False)
        self.canvas.setCursor(Qt.CursorShape.ArrowCursor)
        self.canvas.setToolTip("")
        self.copy_button.setEnabled(False)
        self.save_button.setEnabled(False)

    def show_canvas(self, filled):
        """虚框 = 还没内容，实框 = 有图；空状态和有图两种叠放只露一个。

        `filled` 只是"该显示图"的意图；真正能画出东西还要求 `self.pixmap` 存在。
        刚由隐藏变可见时布局还没算完，同步重绘会用旧尺寸，所以补两拍。
        """
        drawable = bool(filled) and self.pixmap is not None
        self.image.setVisible(drawable)
        self.empty.setVisible(not drawable)
        self.canvas.setProperty("filled", "yes" if drawable else "no")
        self.canvas.style().unpolish(self.canvas)
        self.canvas.style().polish(self.canvas)
        if drawable:
            QTimer.singleShot(0, self.paint_image)
            QTimer.singleShot(30, self.paint_image)

    def open_output_dir(self):
        folder = self.data / "results"
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def active_job(self):
        """正在跑（或排队中）的那条任务 —— 取消按钮的服务对象。"""
        return next((job for job in self.latest + self.older
                     if job["status"] in {"submitting", "running", "cancelling", "queued"}), None)

    def refresh_cancel(self):
        self.cancel_button.setEnabled(self.active_job() is not None and not self.stopping)

    def workflow_title(self, workflow_id):
        return next((item.get("title") for item in self.workflows or []
                     if item["workflow_id"] == workflow_id), workflow_id)

    @staticmethod
    def moment(value):
        try:
            return datetime.fromisoformat(value) if value else None
        except ValueError:
            return None

    @staticmethod
    def span(seconds):
        seconds = max(0, int(seconds))
        return f"{seconds // 60}:{seconds % 60:02d}" if seconds >= 60 else f"{seconds} s"

    def engine_line(self, job):
        """引擎侧固定参数 + 诊断日志路径（`engine_parameters` 只读字段）。

        步数/CFG/采样器这些用户改不了，但恰恰是"这张图是怎么跑出来的"的关键，
        而且它只存在于模板里。日志路径给出来，想看三段耗时自己开文件 —— 比往
        DB 塞 metrics 更准，也不用迁移。
        """
        item = next((entry for entry in self.workflows or []
                     if entry["workflow_id"] == job.get("workflow_id")), {})
        params = item.get("engine_parameters") or {}
        parts = []
        if params.get("steps"):
            parts.append(f"{params['steps']} 步")
        if params.get("cfg_scale") is not None:
            parts.append(f"CFG {params['cfg_scale']}")
        if params.get("sampling_method"):
            parts.append(str(params["sampling_method"]))
        if params.get("scheduler"):
            parts.append(str(params["scheduler"]))
        return "  ·  ".join(parts)

    def log_path(self, job):
        try:
            job_id = str(UUID(job["job_id"]))
        except (ValueError, TypeError, KeyError):
            return None
        folder = self.data / "logs"
        for name in (f"sdcpp-{job_id}.log", f"comfy-{job_id}.log"):
            candidate = folder / name
            if candidate.is_file():
                return candidate
        return None

    def open_log(self):
        path = self.log_path(self.current_job() or {})
        if path is None:
            self.form_note.setText("这条任务没有单独的引擎日志")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def describe(self, job):
        """状态 / 模型 / 尺寸种子 / 时刻耗时 / 产物 / 引擎参数，一行一件事。

        详情栏只有 300 多像素宽，长句会被折成两行读不懂 —— 所以这里刻意断成短行。
        参数一栏读的是落库后的 parameters（负面词和重绘强度在用户留空时已被
        workflow 的默认值填好），所以显示的就是引擎真正收到的那一份。
        """
        parameters = job["parameters"]
        lines = [f"{STATUS[job['status']]}  ·  {self.workflow_title(job['workflow_id'])}"
                 f"  ·  {job['workflow_revision']}",
                 f"{parameters['width']} × {parameters['height']}  ·  种子 {parameters['seed']}"]
        created = self.moment(job["created_at"])
        started = self.moment(job["started_at"])
        finished = self.moment(job["finished_at"])
        if created:
            parts = [f"提交 {created.astimezone().strftime('%m-%d %H:%M')}"]
            if started:
                parts.append(f"排队 {self.span((started - created).total_seconds())}")
            if finished and started:
                parts.append(f"生成 {self.span((finished - started).total_seconds())}")
            elif started and job["status"] == "running":
                parts.append(f"已用 {self.span((datetime.now(UTC) - started).total_seconds())}")
            else:
                parts.append("等待开始")
            lines.append("  ·  ".join(parts))
        artifact = (job["artifacts"] or [None])[0]
        if artifact:
            lines.append(f"产物 {artifact['width']} × {artifact['height']}"
                         f"  ·  {artifact['size_bytes'] / 1048576:.1f} MB")
        extras = []
        if job["workflow_id"] and parameters.get("image"):
            extras.append("参考图 " + parameters["image"].split(".")[0][:8])
        if parameters.get("strength") is not None:
            extras.append(f"重绘强度 {float(parameters['strength']):.2f}")
        if extras:
            lines.append("  ·  ".join(extras))
        # 图库参考单独一行：两个编号都不短，挤进上一行会破 60 字的读行长度。
        picked = [f"{title} {parameters[key]}" for key, title in
                  (("pose_id", "姿势"), ("garment_id", "服装")) if parameters.get(key)]
        if picked:
            lines.append("  ·  ".join(picked))
        negative = " ".join((parameters.get("negative_prompt") or "").split())
        if negative:
            lines.append("负面 " + (negative[:60] + "…" if len(negative) > 60 else negative))
        engine = self.engine_line(job)
        if engine:
            lines.append("引擎 " + engine)
        if job["error"]:
            lines.append(f"{job['error']['code']}  ·  {job['error']['message']}")
        return "\n".join(lines)

    def update_progress(self, job):
        progress = job["progress"] or {}
        total = progress.get("total") or 0
        current = progress.get("current") or 0
        stage_key = progress.get("stage") or ""
        if stage_key == "decoding":
            # 解码是收尾的固定动作（1~2 s），停在满格会像"卡住"；改忙碌态还在动。
            self.progress.setRange(0, 0)
        elif total:
            self.progress.setRange(0, total)
            self.progress.setValue(current)
        else:
            # 还没有步数：忙碌态，同时用文字说明卡在哪一步。
            self.progress.setRange(0, 0)
        stage = {"loading": "加载模型", "sampling": "采样", "decoding": "解码"}.get(
            stage_key, stage_key or "准备中")
        if stage_key == "decoding":
            # 解码没有步数（50 / 50 是采样过的步），别印成"解码 50 / 50"。
            text = stage
        elif total:
            text = f"{stage} {current} / {total}"
        else:
            text = stage
        if progress.get("detail"):
            text += "  ·  " + progress["detail"]
        started = self.moment(job["started_at"])
        if started:
            text += f"  ·  已用 {self.span((datetime.now(UTC) - started).total_seconds())}"
        self.progress_text.setText(text)
        self.progress.setVisible(True)

    def show_job(self, job):
        status = job["status"]
        self.selected_status = status
        parameters = job["parameters"]
        # 轮询每 ~1 s 就会带着同一份任务再进来。setText/setPlainText 会把
        # QPlainTextEdit 的滚动位置拉回顶部 —— 用户正滚着看提示词就被拽回去，
        # 反馈为"滚动的时候总是会回到第一行"。内容没变就不动它。
        described = self.describe(job)
        if self.detail.text() != described:
            self.detail.setText(described)
        prompt_text = parameters["prompt"]
        # setPlainText 之后光标停在文档开头。轮询期间 describe 的"已用"时长
        # 每秒变化会让详情区重排，QPlainTextEdit 重排后 ensureCursorVisible
        # 把视图拉回光标处 = 第一行 —— 用户滚到最后一行时最刺眼（第二次反馈）。
        # 这里记录用户所在位置，本轮更新全部做完后把它恢复回去；内容真的变了
        # （切换到另一条任务）则回到顶部，符合"看新任务从头看"的预期。
        prompt_changed = self.prompt_view.toPlainText() != prompt_text
        prompt_scroll = 0 if prompt_changed else self.prompt_view.verticalScrollBar().value()
        negative_text = " ".join((parameters.get("negative_prompt") or "").split())
        negative_changed = self.negative_view.toPlainText() != negative_text
        negative_scroll = 0 if negative_changed else self.negative_view.verticalScrollBar().value()
        if prompt_changed:
            self.prompt_view.setPlainText(prompt_text)
        self.prompt_block.setVisible(bool(prompt_text))
        self.copy_prompt_button.setEnabled(bool(prompt_text))
        if negative_changed:
            self.negative_view.setPlainText(negative_text)
        self.negative_block.setVisible(bool(negative_text))
        self.copy_negative_button.setEnabled(bool(negative_text))
        if prompt_scroll or negative_scroll:
            QTimer.singleShot(0, lambda: self._restore_detail_scroll(prompt_scroll, negative_scroll))
        self.show_reference(job)
        self.show_library_refs(job)
        for control in (self.reuse_button, self.rerun_button, self.copy_json_button,
                        self.folder_button, self.log_button):
            control.setEnabled(bool(job.get("job_id")) and not self.submitting)
        self.actions.setVisible(True)
        self.cancel_button.setEnabled(self.active_job() is not None and not self.stopping)
        self.delete_button.setEnabled(status in TERMINAL and not self.stopping)
        active = status in {"submitting", "running", "cancelling"}
        self.progress_row.setVisible(active)
        if active:
            self.update_progress(job)
        elif not self.active_job():
            # 没有在跑的任务时清掉残字：行虽然隐藏，留着旧文案迟早被改成常显而翻车。
            self.progress_text.clear()
            self.progress.setRange(0, 0)
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
            self.show_canvas(True)          # 内部会在布局落地后补两次 paint_image
            self.canvas.setCursor(Qt.CursorShape.PointingHandCursor)
            self.canvas.setToolTip("点击用系统图片查看器打开原图")
            self.copy_button.setEnabled(True)
            self.save_button.setEnabled(True)

        self.api.request(result["download_path"], completed, png=True)

    def _restore_detail_scroll(self, prompt_scroll: int, negative_scroll: int):
        """轮询更新后的下一拍把用户所在位置恢复回去。

        布局事件在本轮事件循环里落地，singleShot(0) 恰好排在其后；这时
        maximum 已经稳定，直接恢复即可。任何中途的重置（幂等写入、重排）
        都被这一拍兜住。
        """
        bar = self.prompt_view.verticalScrollBar()
        if prompt_scroll and bar.value() != prompt_scroll:
            bar.setValue(min(prompt_scroll, bar.maximum()))
        bar = self.negative_view.verticalScrollBar()
        if negative_scroll and bar.value() != negative_scroll:
            bar.setValue(min(negative_scroll, bar.maximum()))

    def toggle_zoom(self):
        """一键把预览放大到整列（隐藏任务历史），再点还原。"""
        expanded = not self.history_panel.isVisible()
        self.history_panel.setVisible(expanded)
        self.zoom_button.setText("放大预览" if expanded else "还原布局")
        if not expanded:
            QTimer.singleShot(0, self.paint_image)

    def paint_image(self):
        if not self.pixmap:
            return
        # 用**画布**的尺寸算缩放框，不用 image 标签自己的：选中历史时 image 刚从
        # 隐藏切到可见，布局还没重新分配，`self.image.size()` 拿到的是隐藏前的旧值
        # （甚至 1×1），图就被缩成一个小点 —— 这正是"点了生成记录不显示、放大或
        # 缩放窗口才出来"的原因。画布始终可见，尺寸永远是对的。
        area = self.canvas.size()
        box = QSize(max(1, area.width()), max(1, area.height()))
        self.image.setPixmap(self.pixmap.scaled(box, Qt.AspectRatioMode.KeepAspectRatio,
                                                Qt.TransformationMode.SmoothTransformation))

    def eventFilter(self, obj, event):
        if obj is getattr(self, "canvas", None):
            if event.type() == QEvent.Type.Resize:
                QTimer.singleShot(0, self.paint_image)
            elif event.type() == QEvent.Type.MouseButtonRelease and self.png is not None:
                self.open_original()
        elif obj is getattr(self, "reference_thumb", None) \
                and event.type() == QEvent.Type.MouseButtonRelease:
            self.open_reference()
        elif event.type() == QEvent.Type.MouseButtonRelease:
            # 姿势 / 服装缩略图共用这一个分支：谁被点开谁那一张。
            for title, thumb_name in (("姿势", "pose_thumb"), ("服装", "garment_thumb")):
                if obj is getattr(self, thumb_name, None):
                    self.open_library(title)
                    break
        return super().eventFilter(obj, event)

    def open_reference(self):
        if self.reference_source is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.reference_source)))

    def open_library(self, title):
        source = (getattr(self, "library_source", None) or {}).get(title)
        if source is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(source)))

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

    def pick_reference(self):
        self.reference.choose()

    def clear_reference(self):
        self.reference.clear()

    def copy_image(self):
        if self.pixmap is not None:
            QApplication.clipboard().setPixmap(self.pixmap)

    def copy_prompt(self):
        text = self.prompt_view.toPlainText()
        if text:
            QApplication.clipboard().setText(text)

    def copy_negative(self):
        text = self.negative_view.toPlainText()
        if text:
            QApplication.clipboard().setText(text)

    # ---------- 参考图实体与裁剪说明（方案 §3.1 A2） ----------
    @staticmethod
    def image_size(path):
        """只读文件头拿尺寸，不解码整张图（4 MB 的手机照片也不拖慢界面）。"""
        if path is None:
            return None
        size = QImageReader(str(path)).size()
        return (size.width(), size.height()) if size.isValid() else None

    def reference_paths(self, job):
        """(引擎实际吃到的参考图, 原始上传图)，文件不在就返回 None。

        优先读任务附属物 `results/<job_id>/reference.png`：那是发布成功时从引擎目录
        转存出来的，永久有效。老任务没有这一份，就回退到引擎目录 `comfy-output/
        <job_id>/input.png`（只在任务刚跑完、还没清理时存在）。原始上传图在
        `gateway-input/<token>`，用来把"原图多大 → 裁成多大"这句话说完整。
        """
        cropped = original = None
        try:
            job_id = str(UUID(job["job_id"]))
        except (ValueError, TypeError, KeyError):
            job_id = None
        if job_id:
            for candidate in (self.data / "results" / job_id / "reference.png",
                              self.data / "comfy-output" / job_id / "input.png"):
                if candidate.is_file():
                    cropped = candidate
                    break
        token = (job.get("parameters") or {}).get("image")
        if token:
            candidate = self.data / "gateway-input" / str(token)
            if candidate.is_file():
                original = candidate
        return cropped, original

    def reference_note(self, job, cropped=None, original=None):
        parameters = job.get("parameters") or {}
        target = f"{parameters.get('width', '?')} × {parameters.get('height', '?')}"
        source = self.image_size(original)
        if source:
            line = (f"参考图  原图 {source[0]} × {source[1]} → 按 {target} 裁切（不拉伸）")
        else:
            line = f"参考图  按 {target} 裁切（不拉伸）"
        if cropped is None and original is None:
            return ""       # 两份文件都没了 → 整块不出现，别显示一个空框
        if cropped is None:
            line += "  ·  引擎副本已清理"
        return line

    def show_reference(self, job):
        self.reference_block.setVisible(False)
        self.reference_thumb.clear()
        self.reference_source = None
        if not (job.get("parameters") or {}).get("image"):
            return
        cropped, original = self.reference_paths(job)
        caption = self.reference_note(job, cropped, original)
        if not caption:
            return
        source = cropped or original
        pixmap = QPixmap(str(source))
        if pixmap.isNull():
            self.reference_thumb.setText("无法读取")
        else:
            self.reference_thumb.setPixmap(pixmap.scaled(
                QSize(68, 68), Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
        self.reference_source = source
        self.reference_caption.setText(caption)
        self.reference_block.setVisible(True)

    def library_row(self, layout, title):
        """详情栏里的一行「图库参考」：72×72 缩略图 + 一行说明。"""
        row = QWidget()
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(10)
        thumb = QLabel()
        thumb.setObjectName("referenceThumb")
        thumb.setFixedSize(72, 72)
        thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        thumb.setToolTip(f"点击用系统图片查看器打开这张{title}图")
        thumb.setCursor(Qt.CursorShape.PointingHandCursor)
        thumb.installEventFilter(self)
        line.addWidget(thumb)
        caption = label("", "caption", True)
        caption.setTextFormat(Qt.TextFormat.PlainText)
        line.addWidget(caption, 1)
        layout.addWidget(row)
        return thumb, caption

    def show_library_refs(self, job):
        """历史里的姿势 / 服装：优先读任务附属物，读不到才说"没留下"。"""
        block = getattr(self, "library_block", None)
        if block is None:
            return
        block.setVisible(False)
        self.library_source = {}
        parameters = job.get("parameters") or {}
        # 跑着的时候附属物还没落盘（发布成功才转存），别写成"没有留存"吓人。
        settled = job.get("status") in TERMINAL
        shown = False
        # 每一对：参数名 / 附属物文件名 / 中文名 / 控件
        for key, name, title, thumb_name, caption_name in (
                ("pose_id", POSE_NAME, "姿势", "pose_thumb", "pose_caption"),
                ("garment_id", GARMENT_NAME, "服装", "garment_thumb", "garment_caption")):
            thumb = getattr(self, thumb_name, None)
            caption = getattr(self, caption_name, None)
            if thumb is None or caption is None:
                continue
            thumb.clear()
            caption.setText("")
            row = thumb.parentWidget()
            if row is not None:
                row.setVisible(False)      # 上一条任务的残留行不许漏出来
            item_id = parameters.get(key)
            if not item_id:
                continue
            if row is not None:
                row.setVisible(True)
            source = self.library_paths(job, name)
            if source is None:
                # 早期任务没存附属物：留一句"当时选的是哪一张"也算有交代。
                caption.setText(f"{title} · {item_id}" + ("（没有留存图片）" if settled else ""))
                shown = True
                continue
            pixmap = QPixmap(str(source))
            if pixmap.isNull():
                thumb.setText("无法读取")
            else:
                thumb.setPixmap(pixmap.scaled(
                    QSize(68, 68), Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation))
            self.library_source[title] = source
            caption.setText(f"{title} · {item_id}")
            shown = True
        if shown:
            block.setVisible(True)

    def library_paths(self, job, name):
        try:
            folder = self.data / "results" / str(UUID(str(job["job_id"])))
        except (ValueError, TypeError):
            return None
        candidate = folder / name
        return candidate if candidate.is_file() else None

    # ---------- 动作（方案 §3.3） ----------
    def hide_actions(self):
        self.actions.setVisible(False)
        for control in (self.reuse_button, self.rerun_button, self.copy_json_button,
                        self.folder_button, self.log_button):
            control.setEnabled(False)

    def current_job(self):
        return next((job for job in self.latest + self.older if job["job_id"] == self.selected), None)

    def action_job(self):
        job = self.current_job()
        if job is None:
            self.form_note.setText("请先在历史中选中一条任务")
        return job

    def reuse_parameters(self, rerun=False):
        """把选中任务的参数整组回填左栏；`rerun=True` 时清空种子（同参重跑）。

        回填本身不提交 —— 人可能只想改一个词再跑，这一点和"重跑"按钮是分开的。
        """
        job = self.action_job()
        if job is None:
            return None
        parameters = job.get("parameters") or {}
        index = self.workflow.findData(job["workflow_id"])
        if index < 0:
            self.form_note.setText("这条任务用的模型已不在本机，无法回填")
            return None
        self.workflow.setCurrentIndex(index)      # 会重置尺寸列表与参考图锁定状态
        self.size_changed()
        position = self.size.findData([parameters.get("width"), parameters.get("height")])
        if position >= 0:
            self.size.setCurrentIndex(position)
        self.prompt.setPlainText(parameters.get("prompt") or "")
        self.negative.setPlainText(parameters.get("negative_prompt") or "")
        self.seed.setText("" if rerun else str(parameters.get("seed", "")))
        strength = parameters.get("strength")
        if isinstance(strength, (int, float)):
            self.strength_slider.setValue(int(round(float(strength) * 100)))
        token = parameters.get("image")
        if token and not self.reference.locked:
            cropped, original = self.reference_paths(job)
            thumb = cropped.read_bytes() if cropped is not None else None
            self.reference.restore(token, self.image_size(original) or (0, 0), thumb)
        elif token and self.reference.locked:
            self.form_note.setText("这条任务用的是图生图模型，参考图没有回填")
            return job
        # 姿势 / 服装：选择器可能在上面 setCurrentIndex 之后才建出来，也可能
        # 因为当前工作流不支持而为 None —— 只回填真的存在的那个。
        for key, attribute in (("pose_id", "pose_picker"), ("garment_id", "garment_picker")):
            picker = getattr(self, attribute, None)
            if picker is None:
                continue
            chosen = parameters.get(key) or ""
            if chosen:
                picker.select_id(chosen)     # 条目被删也能靠编号摆上，不阻断回填
            else:
                picker.clear()
        self.form_note.setText("已按这一条重新排好参数，换了一个新种子" if rerun
                               else "已回填这一条的参数（未提交）")
        return job

    def rerun_job(self):
        if self.reuse_parameters(rerun=True) is not None:
            self.submit()

    def copy_parameters(self):
        job = self.action_job()
        if job is None:
            return
        parameters = job.get("parameters") or {}
        payload = {"workflow_id": job["workflow_id"],
                   "workflow_revision": job["workflow_revision"],
                   "seed": parameters.get("seed"),
                   "prompt": parameters.get("prompt"),
                   "negative_prompt": parameters.get("negative_prompt"),
                   "width": parameters.get("width"),
                   "height": parameters.get("height")}
        for key in ("strength", "image", "pose_id", "garment_id"):
            if parameters.get(key) is not None:
                payload[key] = parameters[key]
        QApplication.clipboard().setText(json.dumps(payload, ensure_ascii=False))
        self.form_note.setText("已复制这一条的参数 JSON")

    def open_results(self):
        job = self.action_job()
        if job is None:
            return
        try:
            folder = self.data / "results" / str(UUID(job["job_id"]))
        except (ValueError, TypeError):
            return
        if not folder.is_dir():
            self.form_note.setText("这条任务还没有成品文件夹")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

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
                self.prompt_block.setVisible(False)
                self.copy_prompt_button.setEnabled(False)
                self.negative_view.clear()
                self.negative_block.setVisible(False)
                self.copy_negative_button.setEnabled(False)
                self.hide_actions()
                self.reference_block.setVisible(False)
                if getattr(self, "library_block", None) is not None:
                    self.library_block.setVisible(False)
                self.clear_image("任务已删除")
                self.detail.setText("从历史中选择任务查看详情")
            self.render_history()
            self.refresh_workflows()
            self.refresh_history()

        self.request_once(f"delete:{job_id}", f"/api/v1/jobs/{job_id}", completed, delete=True)

    def cancel(self):
        # 取消按钮跟"正在跑的那条"走，而不是跟"选中的那条"（C4）。
        job = self.active_job()
        if job is None:
            return
        job_id = job["job_id"]
        self.cancel_button.setEnabled(False)

        def completed(value, error):
            if self.selected == job_id and value:
                self.show_job(value)
            if error:
                self.form_note.setText(error)
            self.refresh_history()

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
            self.save_ui()
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


def build_tray_menu(window, open_window, quit_app):
    """托盘右键菜单。

    必须挂在窗口上（parent=window）并由 window 持有引用 —— 之前 `QMenu()` 无父对象、
    只有 tray 自己引用它，PySide 端 menu 随局部作用域被回收，右键就变成空弹窗：
    用户只能回到软件里点「保存后退出」，右键根本找不到「退出」。
    """
    menu = QMenu(window)
    window.tray_menu = menu
    menu.addAction("打开 LocalGen").triggered.connect(open_window)
    menu.addSeparator()
    exit_action = menu.addAction("退出 LocalGen")
    exit_action.setToolTip("保存界面状态后完全退出（含本机 API 服务）")
    exit_action.triggered.connect(quit_app)
    return menu


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
        window.save_ui()
        window.stopping = True
        window.timer.stop()
        server.should_exit = True

    # 菜单必须挂在窗口上并由 window 持有引用（见 build_tray_menu 的注释）。
    tray = QSystemTrayIcon(dsh_tray_icon(), window)

    def open_window():
        window.show()
        window.raise_()
        window.activateWindow()

    def quit_app():
        window.save_ui()
        window.real_exit = True
        window.close()

    tray_menu = build_tray_menu(window, open_window, quit_app)
    tray.setContextMenu(tray_menu)
    tray.setToolTip("LocalGen · 本地生图工作台（右键可退出）")
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
