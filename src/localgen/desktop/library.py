"""姿势 / 服装图库选择器。

选择器是一个带固定预览框的卡片（`QFrame#libraryPicker`）：选中的姿势/服装
会立刻显示成缩略图，点「选择」开对话框：上方搜索、体系/分类筛选，下方缩略图
网格。选中即返回条目 dict，调用方拿 id 去提交换姿势/换装。

图片走本机 API（Client.request png=True），不走文件系统：
- 数据目录可能被移动，URL 稳定；
- 素材是第三方只读资源，走服务端能统一做路径越权防护。
"""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QFontMetrics, QIcon, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QScrollArea, QToolButton, QVBoxLayout, QWidget,
)

from localgen.desktop import label

PAGE = 60          # 每次取多少条
THUMB = QSize(104, 138)
# 选择器里那张定宽定高的预览；姿势图纵向偏长，用 72×96 一眼能认出来又不吃掉行高。
PICKER_THUMB = QSize(72, 96)
# 卡片 = 图片画框 + 下面两行小字（名称 / 角度·分类）。CARD 是 1x 基准比例，
# 实际大小随弹窗宽度伸缩（见 LibraryDialog._grid_metrics），行宽边到边填满。
CARD = QSize(THUMB.width() + 12, THUMB.height() + 44)
# 缩略图缓存的母版尺寸：2 倍画框，卡片放大到最大号也不发糊，60 条 ≈ 12MB。
MASTER = QSize(THUMB.width() * 2, THUMB.height() * 2)
GRID_SPACING = 8        # 卡片间距：要留缝，不许拥挤重叠


def fill_thumb(pixmap: QPixmap, size: QSize) -> QPixmap:
    """等比放大到铺满 size 再居中裁切 —— 图片要占领整个画框，不留左右白边。"""
    scaled = pixmap.scaled(size, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                           Qt.TransformationMode.SmoothTransformation)
    x = max(0, (scaled.width() - size.width()) // 2)
    y = max(0, (scaled.height() - size.height()) // 2)
    return scaled.copy(x, y, size.width(), size.height())


class LibraryPicker(QFrame):
    """姿势 / 服装输入框：固定尺寸预览 + 名称 + 「选择 / 清除」。

    预览框是**定宽定高**的：选中不同比例的姿势图时，左边那一栏不许跟着图片
    高度上下跳（用户反馈"预览非常烂，乱套了"就是这么来的）。缩略图走本机 API
    的 `/thumb`，和对话框里那张是同一份，不再单独解码原图。
    """

    changed = Signal()

    def __init__(self, api, kind: str, title: str, placeholder: str, parent=None):
        super().__init__(parent)
        self.setObjectName("libraryPicker")
        self.api = api
        self.kind = kind
        self.title = title
        self.hint = placeholder
        self.item: dict | None = None
        self._cache: dict[str, QPixmap] = {}
        self.build()
        self._show_placeholder()

    # ---------- 构建 ----------

    def build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 9)
        layout.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(6)
        head.addWidget(label(self.title, "caption"))
        head.addStretch()
        self.state = label("", "libraryHint")
        head.addWidget(self.state)
        layout.addLayout(head)

        body = QHBoxLayout()
        body.setSpacing(9)
        self.thumb = QLabel()
        self.thumb.setObjectName("libraryThumb")
        self.thumb.setFixedSize(PICKER_THUMB)
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.setToolTip("当前选中的参考图")
        body.addWidget(self.thumb)
        column = QVBoxLayout()
        column.setSpacing(2)
        self.summary = label(self.hint, "libraryName", True)
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        column.addWidget(self.summary)
        self.meta = label("点击「选择」从图库挑一张", "caption", True)
        self.meta.setTextFormat(Qt.TextFormat.PlainText)
        column.addWidget(self.meta)
        column.addStretch()
        row = QHBoxLayout()
        row.setSpacing(6)
        self.choose = QPushButton("选择")
        self.choose.setObjectName("ghost")
        self.choose.setFixedHeight(26)
        self.choose.clicked.connect(self.open)
        row.addWidget(self.choose)
        self.clear_button = QPushButton("清除")
        self.clear_button.setObjectName("ghost")
        self.clear_button.setFixedHeight(26)
        self.clear_button.clicked.connect(self.clear)
        self.clear_button.setVisible(False)
        row.addWidget(self.clear_button)
        row.addStretch()
        column.addLayout(row)
        body.addLayout(column, 1)
        layout.addLayout(body)
        # 固定最小高度 + 固定预览框：切换选中项时这一块的高度不参与协商。
        self.setMinimumHeight(PICKER_THUMB.height() + 42)

    # ---------- 预览 ----------

    def _repolish(self) -> None:
        self.thumb.style().unpolish(self.thumb)
        self.thumb.style().polish(self.thumb)

    def _show_placeholder(self, text="未选") -> None:
        self.thumb.setPixmap(QPixmap())
        self.thumb.setText(text)
        self.thumb.setProperty("filled", "no")
        self._repolish()

    def _paint(self, pixmap: QPixmap) -> None:
        self.thumb.setText("")
        self.thumb.setPixmap(pixmap)
        self.thumb.setProperty("filled", "yes")
        self._repolish()

    def _load_thumb(self, item_id: str) -> None:
        cached = self._cache.get(item_id)
        if cached is not None:
            self._paint(cached)
            return
        self._show_placeholder("…")
        encoded = QUrl.toPercentEncoding(item_id).data().decode()
        path = f"/api/v1/libraries/{self.kind}/{encoded}/thumb"

        def done(content, error):
            # 迟到的响应不许盖掉后来选中的那一张。
            if self.selected_id() != item_id:
                return
            if error or not content:
                self._show_placeholder("读不到")
                return
            pixmap = QPixmap()
            if not pixmap.loadFromData(content):
                self._show_placeholder("读不到")
                return
            scaled = pixmap.scaled(PICKER_THUMB, Qt.AspectRatioMode.KeepAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation)
            self._cache[item_id] = scaled
            self._paint(scaled)
        self.api.request(path, done, png=True)

    # ---------- 选中状态 ----------

    def selected_id(self) -> str:
        return (self.item or {}).get("id", "")

    def selection(self) -> dict | None:
        return self.item

    def clear(self) -> None:
        if self.item is None:
            return
        self.item = None
        self._show_placeholder()
        self.summary.setText(self.hint)
        self.state.setText("")
        self.meta.setText("点击「选择」从图库挑一张")
        self.clear_button.setVisible(False)
        self.changed.emit()

    def set_item(self, item: dict | None) -> None:
        self.item = item or None
        if item:
            angle = f" · {item.get('angle')}" if item.get("angle") else ""
            # 服装没有 title，只有一长串英文描述 —— 拿它当标题会和下面那行
            # 一模一样地重复两遍（实测看着像坏了），所以标题退到编号。
            name = item.get("title") or item.get("id", "")
            self.summary.setText(f"{name}{angle}")
            self.state.setText(item.get("category_cn") or item.get("category") or "已选择")
            description = " ".join((item.get("description") or "").split())
            self.meta.setText(description or f"编号 {item.get('id', '')}")
            self.clear_button.setVisible(True)
            self._load_thumb(item.get("id", ""))
        else:
            self._show_placeholder()
            self.summary.setText(self.hint)
            self.state.setText("")
            self.meta.setText("点击「选择」从图库挑一张")
            self.clear_button.setVisible(False)
        self.changed.emit()

    def open(self) -> None:
        dialog = LibraryDialog(self.api, self.kind, self.title, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.chosen:
            self.set_item(dialog.chosen)

    def select_id(self, item_id: str) -> bool:
        """按编号恢复选中（「复用参数」用）。

        先只拿编号把人选状态与缩略图摆上（图库重导入过也不会卡住回填），
        再向服务端要一份完整条目把标题/角度补全 —— 条目没了就保持编号。
        """
        item_id = (item_id or "").strip()
        if not item_id:
            return False
        self.set_item({"id": item_id})

        def done(value, error):
            if error or not isinstance(value, dict) or self.selected_id() != item_id:
                return
            entry = value.get("item") if isinstance(value.get("item"), dict) else value
            if isinstance(entry, dict) and entry.get("id") == item_id:
                self.set_item(entry)

        self.api.request(f"/api/v1/libraries/{self.kind}/{QUrl.toPercentEncoding(item_id).data().decode()}",
                         done)
        return True


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
        self.cards: list[QToolButton] = []
        self.active_id: str | None = None
        self._thumb_cache: dict[str, QPixmap] = {}
        self.pending = set()
        # 卡片大小/列数随弹窗宽度伸缩：全屏后不能只占一点点位置（用户反馈）。
        self.columns = 3
        self.card_size = CARD
        self.icon_size = THUMB
        self._relayout_timer = QTimer(self)
        self._relayout_timer.setSingleShot(True)
        self._relayout_timer.setInterval(120)
        self._relayout_timer.timeout.connect(self.relayout)

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
        self.grid.setSpacing(GRID_SPACING)
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

        self._grid_metrics()
        self.load_facets()
        self.reload()

    # ---------- 自适应网格 ----------

    def _grid_metrics(self) -> None:
        """按滚动区视口宽度定列数与卡片大小：行宽边到边填满，间距恒定。

        基准一行约 200px 一张（3~14 列），卡片宽上限 300 —— 全屏后卡片
        适量放大填满空白，又不至于变成海报（用户反馈：太小、只占一点点）。
        """
        width = max(self.scroll.viewport().width(), 3 * CARD.width())
        self.columns = max(3, min(14, width // 200))
        card_w = (width - (self.columns - 1) * GRID_SPACING) // self.columns
        card_w = max(CARD.width(), min(300, card_w))
        self.card_size = QSize(card_w, round(card_w * CARD.height() / CARD.width()))
        self.icon_size = QSize(round(card_w * THUMB.width() / CARD.width()),
                               round(card_w * THUMB.height() / CARD.width()))

    def card_caption(self, item: dict) -> str:
        name = item.get("title") or item.get("id", "")
        angle = item.get("angle") or item.get("category_cn") or item.get("category") or ""
        metrics = QFontMetrics(self.font())
        lines = [metrics.elidedText(name, Qt.TextElideMode.ElideRight, self.icon_size.width())]
        if angle:
            lines.append(metrics.elidedText(angle, Qt.TextElideMode.ElideRight,
                                            self.icon_size.width()))
        return "\n".join(lines)

    def relayout(self) -> None:
        """窗口大小变了：重算列数/卡片大小，把现有卡片按新网格摆回去。"""
        self._grid_metrics()
        for card in self.cards:
            self.grid.removeWidget(card)
        for index, card in enumerate(self.cards):
            row, column = divmod(index, self.columns)
            self.grid.addWidget(card, row, column)
        for card, item in zip(self.cards, self.items):
            card.setFixedSize(self.card_size)
            card.setIconSize(self.icon_size)
            card.setText(self.card_caption(item))
        self.refresh_icons()

    def refresh_icons(self) -> None:
        """按当前卡片大小从母版重出图（放大后不糊）。"""
        for card, item in zip(self.cards, self.items):
            master = self._thumb_cache.get(item.get("id", ""))
            if master is not None:
                card.setIcon(QIcon(fill_thumb(master, self.icon_size)))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._relayout_timer.start()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # 显示后视口才有真实宽度（隐藏时布局不激活），立刻按它排一次，
        # 免得首帧闪一下初始列数。
        self.relayout()

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
        # 清掉旧卡片再重查：不然后面 render() 只往网格里追加，筛选出来的结果
        # 被压在原列表下面，看起来就是"点了筛选没反应"（用户反馈）。
        # cards 与 items 必须保持一一对应（select/relayout 都靠 zip 对位）。
        for card in self.cards:
            self.grid.removeWidget(card)
            card.deleteLater()
        self.cards = []
        self.pending.clear()      # 防重复加载守卫按条数记账，换筛选后必须放行
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
            row, column = divmod(position, self.columns)
            # 图片铺满整个画框、名字退到图下方。QPushButton 的图标+文字是左右排的：
            # 姿势卡被名字挤掉半张图，服装条目没有 title、描述又是一长串英文，
            # 文字直接把按钮占满、图标被挤没（用户反馈"全是文字没有图片"）。
            card = QToolButton()
            card.setObjectName("libraryCard")
            card.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            card.setIconSize(self.icon_size)
            card.setFixedSize(self.card_size)
            card.setAutoRaise(True)     # QToolButton 没有 setFlat；外观由样式表接管
            card.setText(self.card_caption(item))
            # 完整信息（含服装那串英文描述）只进 tooltip 和底部详情条，不上卡片。
            name = item.get("title") or item.get("id", "")
            angle = item.get("angle") or item.get("category_cn") or item.get("category") or ""
            description = " ".join((item.get("description") or "").split())
            card.setToolTip("\n".join(filter(None, [
                str(item.get("id", "")), f"{name} {angle}".strip(), description])))
            card.clicked.connect(lambda _=False, it=item: self.select(it))
            card.mouseDoubleClickEvent = (          # 双击直接使用
                lambda event, it=item: self.accept_item(it))
            self.grid.addWidget(card, row, column)
            self.cards.append(card)
            self.load_thumb(item, card)

    def load_thumb(self, item: dict, card: QToolButton) -> None:
        item_id = item.get("id", "")
        cached = self._thumb_cache.get(item_id)
        if cached is not None:
            card.setIcon(QIcon(fill_thumb(cached, self.icon_size)))
            return
        path = f"/api/v1/libraries/{self.kind}/{QUrl.toPercentEncoding(item_id).data().decode()}/thumb"

        def done(content, error):
            if error or not content:
                return
            pixmap = QPixmap()
            if not pixmap.loadFromData(content):
                return
            # 缓存 2 倍母版：卡片随窗口放大时从母版重出图，不用再打接口。
            master = pixmap.scaled(MASTER, Qt.AspectRatioMode.KeepAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation)
            self._thumb_cache[item_id] = master
            card.setIcon(QIcon(fill_thumb(master, self.icon_size)))
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
        # 和卡片一致：服装没有 title，标题退到编号，别把英文描述念两遍。
        name = item.get("title") or item.get("id", "")
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
