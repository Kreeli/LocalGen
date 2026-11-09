"""姿势 / 服装图库选择器。

一个按钮 + 一张选中预览，点按钮开对话框：左侧筛选（体系/分类），上方搜索，
右侧缩略图网格。选中即返回条目 dict，调用方拿 id 去提交换姿势/换装。

图片走本机 API（Client.request png=True），不走文件系统：
- 数据目录可能被移动，URL 稳定；
- 素材是第三方只读资源，走服务端能统一做路径越权防护。
"""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt, QUrl, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from localgen.desktop import label

PAGE = 60          # 每次取多少条
COLUMNS = 5        # 网格列数
THUMB = QSize(104, 138)


class LibraryPicker(QWidget):
    """一行：标题 + 当前选中 + 「选择 / 清除」按钮。"""

    changed = Signal()

    def __init__(self, api, kind: str, title: str, placeholder: str, parent=None):
        super().__init__(parent)
        self.api = api
        self.kind = kind
        self.title = title
        self.item: dict | None = None
        self.hint = placeholder

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 2, 0, 0)
        row.setSpacing(8)
        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(label(title, "caption"))
        self.summary = label(placeholder, "caption", True)
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        text.addWidget(self.summary)
        row.addLayout(text, 1)
        self.choose = QPushButton("选择")
        self.choose.setObjectName("ghost")
        self.choose.clicked.connect(self.open)
        row.addWidget(self.choose)
        self.clear_button = QPushButton("清除")
        self.clear_button.setObjectName("ghost")
        self.clear_button.clicked.connect(self.clear)
        self.clear_button.setVisible(False)
        row.addWidget(self.clear_button)

    # ---------- 选中状态 ----------

    def selected_id(self) -> str:
        return (self.item or {}).get("id", "")

    def selection(self) -> dict | None:
        return self.item

    def clear(self) -> None:
        if self.item is None:
            return
        self.item = None
        self.summary.setText(self.hint)
        self.clear_button.setVisible(False)
        self.changed.emit()

    def set_item(self, item: dict | None) -> None:
        self.item = item or None
        if item:
            angle = f" · {item.get('angle')}" if item.get("angle") else ""
            name = item.get("title") or item.get("description") or item.get("id", "")
            self.summary.setText(f"{name}{angle}")
            self.clear_button.setVisible(True)
        else:
            self.summary.setText(self.hint)
            self.clear_button.setVisible(False)
        self.changed.emit()

    def open(self) -> None:
        dialog = LibraryDialog(self.api, self.kind, self.title, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.chosen:
            self.set_item(dialog.chosen)


class LibraryDialog(QDialog):
    """缩略图网格 + 筛选 + 搜索。双击或点「使用」即选中。"""

    def __init__(self, api, kind: str, title: str, parent=None):
        super().__init__(parent)
        self.api = api
        self.kind = kind
        self.chosen: dict | None = None
        self.items: list[dict] = []
        self.offset = 0
        self.total = 0
        self.cards: list[QPushButton] = []
        self.active_id: str | None = None
        self._thumb_cache: dict[str, QPixmap] = {}
        self.pending = set()

        self.setWindowTitle(f"选择{title}")
        self.resize(1040, 720)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(10)

        # 顶栏：搜索 + 体系 + 分类
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索姿势名称、编号或分类…")
        self.search.returnPressed.connect(self.reload)
        bar.addWidget(self.search, 2)
        self.collection = QComboBox()
        self.collection.addItem("全部体系", "")
        self.collection.setVisible(kind == "poses")
        self.collection.currentIndexChanged.connect(self.reload)
        bar.addWidget(self.collection, 1)
        self.category = QComboBox()
        self.category.addItem("全部分类", "")
        self.category.currentIndexChanged.connect(self.reload)
        bar.addWidget(self.category, 1)
        find = QPushButton("搜索")
        find.clicked.connect(self.reload)
        bar.addWidget(find)
        outer.addLayout(bar)

        self.count = label("正在读取图库…", "caption")
        outer.addWidget(self.count)

        # 网格
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.verticalScrollBar().valueChanged.connect(self.maybe_more)
        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(8)
        self.scroll.setWidget(self.grid_host)
        outer.addWidget(self.scroll, 1)

        # 底栏
        foot = QHBoxLayout()
        self.detail = label("选中一张后点「使用」", "caption", True)
        self.detail.setTextFormat(Qt.TextFormat.PlainText)
        foot.addWidget(self.detail, 1)
        self.more = QPushButton("加载更多")
        self.more.clicked.connect(self.load_more)
        self.more.setVisible(False)
        foot.addWidget(self.more)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        foot.addWidget(cancel)
        self.use = QPushButton("使用")
        self.use.setObjectName("primary")
        self.use.setEnabled(False)
        self.use.clicked.connect(self.accept_current)
        foot.addWidget(self.use)
        outer.addLayout(foot)

        self.load_facets()
        self.reload()

    # ---------- 数据 ----------

    def load_facets(self) -> None:
        def done(value, error):
            if error or not value:
                return
            for name in value.get("collections") or []:
                self.collection.addItem(name, name)
            for name in value.get("categories") or []:
                self.category.addItem(name, name)
        self.api.request(f"/api/v1/libraries/{self.kind}?limit=1", done)

    def reload(self) -> None:
        self.offset = 0
        self.items = []
        self.active_id = None
        self.use.setEnabled(False)
        self.detail.setText("选中一张后点「使用」")
        self.fetch()

    def load_more(self) -> None:
        self.offset = len(self.items)
        self.fetch()

    def fetch(self) -> None:
        query = self.search.text().strip()
        parts = [f"limit={PAGE}", f"offset={self.offset}"]
        if query:
            parts.append("query=" + QUrl.toPercentEncoding(query).data().decode())
        category = self.category.currentData()
        if category:
            parts.append("category=" + QUrl.toPercentEncoding(category).data().decode())
        collection = self.collection.currentData()
        if collection:
            parts.append("collection=" + QUrl.toPercentEncoding(collection).data().decode())
        path = f"/api/v1/libraries/{self.kind}?" + "&".join(parts)

        def done(value, error):
            if error:
                self.count.setText(error)
                return
            self.total = value.get("total", 0)
            fresh = value.get("items") or []
            self.items.extend(fresh)
            self.render(fresh)
            self.count.setText(
                f"共 {self.total} 条 · 已显示 {len(self.items)} 条" if self.total else "没有匹配的条目")
            self.more.setVisible(len(self.items) < self.total)
        self.api.request(path, done)

    def maybe_more(self) -> None:
        if not self.more.isVisible():
            return
        bar = self.scroll.verticalScrollBar()
        if bar.maximum() - bar.value() < 240:
            if f"{self.kind}:{len(self.items)}" not in self.pending:
                self.pending.add(f"{self.kind}:{len(self.items)}")
                self.load_more()

    # ---------- 渲染 ----------

    def render(self, fresh: list[dict]) -> None:
        for index, item in enumerate(fresh):
            position = len(self.cards)
            row, column = divmod(position, COLUMNS)
            card = QPushButton()
            card.setObjectName("libraryCard")
            card.setFixedSize(THUMB.width() + 12, THUMB.height() + 34)
            card.setIconSize(THUMB)
            card.setFlat(True)
            name = item.get("title") or item.get("description") or item.get("id", "")
            angle = item.get("angle") or item.get("category_cn") or item.get("category") or ""
            card.setText(f"{name}\n{angle}" if angle else name)
            card.setToolTip(f"{item.get('id')}\n{name} {angle}")
            card.clicked.connect(lambda _=False, it=item: self.select(it))
            card.mouseDoubleClickEvent = (          # 双击直接使用
                lambda event, it=item: self.accept_item(it))
            self.grid.addWidget(card, row, column)
            self.cards.append(card)
            self.load_thumb(item, card)

    def load_thumb(self, item: dict, card: QPushButton) -> None:
        item_id = item.get("id", "")
        cached = self._thumb_cache.get(item_id)
        if cached is not None:
            card.setIcon(QIcon(cached))
            return
        path = f"/api/v1/libraries/{self.kind}/{QUrl.toPercentEncoding(item_id).data().decode()}/thumb"

        def done(content, error):
            if error or not content:
                return
            pixmap = QPixmap()
            if not pixmap.loadFromData(content):
                return
            scaled = pixmap.scaled(THUMB, Qt.AspectRatioMode.KeepAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation)
            self._thumb_cache[item_id] = scaled
            card.setIcon(QIcon(scaled))
        self.api.request(path, done, png=True)

    def select(self, item: dict) -> None:
        self.chosen = item
        self.active_id = item.get("id")
        for card, candidate in zip(self.cards, self.items):
            selected = candidate.get("id") == self.active_id
            card.setProperty("selected", "true" if selected else "false")
            card.style().unpolish(card)
            card.style().polish(card)
        angle = f" · {item['angle']}" if item.get("angle") else ""
        name = item.get("title") or item.get("description") or item.get("id", "")
        description = item.get("description") or ""
        text = f"{name}{angle}\n{description}".strip() if description else f"{name}{angle}"
        self.detail.setText(text)
        self.use.setEnabled(True)

    def accept_current(self) -> None:
        if self.chosen:
            self.accept()

    def accept_item(self, item: dict) -> None:
        self.chosen = item
        self.accept()
