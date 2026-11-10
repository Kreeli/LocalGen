"""图库选择器：选中状态、清除、提交载荷、按工作流能力显隐、卡片排版。

不打真网络：Client 换成记录调用的假对象，缩略图请求直接忽略。
"""
import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication

from localgen.desktop.library import LibraryPicker

POSES = [
    {"id": "P001_front", "title": "S形站姿", "angle": "正面", "kind": "pose"},
    {"id": "P001_high", "title": "S形站姿", "angle": "俯视", "kind": "pose"},
]


@pytest.fixture(scope="module")
def app():
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # show() 不许闪真实窗口
    instance = QApplication.instance() or QApplication([])
    yield instance


class FakeApi:
    def __init__(self, payload=None):
        self.calls = []
        self.payload = payload or {
            "total": 2, "items": POSES, "collections": ["全身单人"],
            "categories": ["站姿"]}

    def request(self, path, callback, *, payload=None, post=False, png=False, key=None,
                delete=False):
        self.calls.append((path, png))
        if png:
            return                     # 缩略图留空，图标保持占位
        callback(self.payload, None)


def test_picker_starts_empty(app):
    picker = LibraryPicker(FakeApi(), "poses", "姿势", "未选择姿势")
    assert picker.selected_id() == ""
    assert picker.selection() is None
    assert picker.clear_button.isVisible() is False


def test_set_item_shows_name_and_angle(app):
    picker = LibraryPicker(FakeApi(), "poses", "姿势", "未选择姿势")
    picker.set_item(POSES[0])
    assert picker.selected_id() == "P001_front"
    assert "S形站姿" in picker.summary.text()
    assert "正面" in picker.summary.text()


def test_changed_fires_on_select_and_clear(app):
    picker = LibraryPicker(FakeApi(), "poses", "姿势", "未选择姿势")
    seen = []
    picker.changed.connect(lambda: seen.append(picker.selected_id()))
    picker.set_item(POSES[0])
    picker.clear()
    assert seen == ["P001_front", ""]


def test_clear_restores_hint(app):
    picker = LibraryPicker(FakeApi(), "poses", "姿势", "未选择姿势")
    picker.set_item(POSES[1])
    picker.clear()
    assert picker.summary.text() == "未选择姿势"
    assert picker.selected_id() == ""


def test_clear_is_noop_when_empty(app):
    picker = LibraryPicker(FakeApi(), "poses", "姿势", "未选择姿势")
    seen = []
    picker.changed.connect(lambda: seen.append(1))
    picker.clear()
    assert seen == []


def test_dialog_lists_and_selects(app):
    from localgen.desktop.library import LibraryDialog

    dialog = LibraryDialog(FakeApi(), "poses", "姿势")
    assert dialog.total == 2
    assert len(dialog.cards) == 2
    assert dialog.use.isEnabled() is False
    dialog.select(POSES[0])
    assert dialog.use.isEnabled() is True
    assert dialog.chosen["id"] == "P001_front"
    dialog.accept_current()
    assert dialog.result() == LibraryDialog.DialogCode.Accepted


def test_dialog_double_click_accepts(app):
    from localgen.desktop.library import LibraryDialog

    dialog = LibraryDialog(FakeApi(), "garments", "服装")
    dialog.accept_item(POSES[1])
    assert dialog.chosen["id"] == "P001_high"


def test_dialog_search_filters_via_query(app):
    from localgen.desktop.library import LibraryDialog

    api = FakeApi()
    dialog = LibraryDialog(api, "poses", "姿势")
    dialog.search.setText("站姿")
    dialog.reload()
    assert any("query=" in path for path, _ in api.calls)


def test_reload_replaces_cards_instead_of_appending(app):
    """筛选/搜索后新结果必须替换旧卡片：追加在下面 = 看起来"筛选没生效"（用户反馈）。"""
    from localgen.desktop.library import LibraryDialog

    api = FakeApi()                      # 2 条姿势
    dialog = LibraryDialog(api, "poses", "姿势")
    assert len(dialog.cards) == 2

    # 换一批条目（模拟筛选结果），reload 后网格里只剩新结果
    api.payload = {"total": 1,
                   "items": [{"id": "K001", "title": "跪姿侧影", "angle": "侧面", "kind": "pose"}],
                   "collections": [], "categories": []}
    dialog.reload()
    assert len(dialog.cards) == 1
    assert len(dialog.items) == 1
    assert dialog.cards[0].text().splitlines()[0].startswith("跪姿侧影")
    # cards 与 items 保持对位（select/relayout 靠 zip）
    dialog.select(dialog.items[0])
    assert dialog.chosen["id"] == "K001"


def test_dialog_requests_thumbs(app):
    from localgen.desktop.library import LibraryDialog

    api = FakeApi()
    LibraryDialog(api, "poses", "姿势")
    thumb_calls = [path for path, png in api.calls if png and path.endswith("/thumb")]
    assert len(thumb_calls) == 2


def test_dialog_empty_library_message(app):
    from localgen.desktop.library import LibraryDialog

    api = FakeApi({"total": 0, "items": [], "collections": [], "categories": []})
    dialog = LibraryDialog(api, "poses", "姿势")
    assert "没有匹配" in dialog.count.text()


def test_select_id_paints_the_id_then_enriches_from_the_catalog(app):
    """「复用参数」按编号恢复：编号先摆上，完整条目后到再补标题。"""
    entry = dict(POSES[0], category="站姿")

    class OneItem(FakeApi):
        def request(self, path, callback, **kwargs):
            self.calls.append((path, entry))
            if path.endswith("/P001_front"):
                callback(entry, None)

    api = OneItem()
    picker = LibraryPicker(api, "poses", "姿势", "未选择姿势")
    assert picker.select_id("P001_front") is True
    assert picker.selected_id() == "P001_front"
    assert "S形站姿" in picker.summary.text()      # 条目回来后补全
    assert any(path.endswith("/libraries/poses/P001_front") for path, _ in api.calls)


def test_select_id_keeps_the_id_when_the_entry_vanished(app):
    class Missing(FakeApi):
        def request(self, path, callback, **kwargs):
            self.calls.append((path, None))
            callback(None, "图库里找不到该条目")

    picker = LibraryPicker(Missing(), "poses", "姿势", "未选择姿势")
    assert picker.select_id("P001_gone") is True
    assert picker.selected_id() == "P001_gone"
    assert picker.summary.text() == "P001_gone"


def test_select_id_empty_is_a_noop(app):
    picker = LibraryPicker(FakeApi(), "poses", "姿势", "未选择姿势")
    seen = []
    picker.changed.connect(lambda: seen.append(1))
    assert picker.select_id("") is False
    assert seen == []


def test_picker_preview_box_is_fixed_size(app):
    """选中不同比例的图不许把左栏撑开（用户反馈"预览乱套了"）。"""
    from localgen.desktop.library import PICKER_THUMB

    picker = LibraryPicker(FakeApi(), "poses", "姿势", "未选择姿势")
    assert picker.thumb.width() == PICKER_THUMB.width()
    assert picker.thumb.height() == PICKER_THUMB.height()
    assert picker.thumb.minimumHeight() == picker.thumb.maximumHeight()


def test_garment_without_title_falls_back_to_the_id(app):
    """服装条目没有 title，只有一长串英文描述 —— 别把描述当标题念两遍。"""
    garment = {"id": "SPR-0001", "kind": "garment", "category_cn": "春装",
               "description": "ivory frill knit denim mini skirt coat socks white sneakers"}

    picker = LibraryPicker(FakeApi(), "garments", "服装", "未选择服装")
    picker.set_item(garment)
    assert picker.summary.text() == "SPR-0001"
    assert picker.meta.text().startswith("ivory frill knit")
    assert "春装" == picker.state.text()
    assert picker.summary.text() != picker.meta.text()


def test_dialog_card_fills_the_frame_with_the_name_below(app):
    """卡片不能是按钮图标+文字左右排：图占左半、名字占右半（用户反馈）。"""
    from localgen.desktop.library import CARD, GRID_SPACING, LibraryDialog

    dialog = LibraryDialog(FakeApi(), "poses", "姿势")
    dialog.show()                    # 隐藏时布局不激活、视口宽度是假的
    app.processEvents()
    card = dialog.cards[0]
    assert card.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonTextUnderIcon
    assert card.iconSize() == dialog.icon_size
    assert card.size() == dialog.card_size
    # 默认 1040 宽弹窗：卡片要比 1x 基准大，随窗口放大而放大
    assert dialog.card_size.width() > CARD.width()
    # 行宽边到边填满视口：剩下的空白不足再放一张卡
    used = dialog.columns * dialog.card_size.width() + (dialog.columns - 1) * GRID_SPACING
    viewport = dialog.scroll.viewport().width()
    assert used <= viewport < used + dialog.card_size.width()
    lines = card.text().splitlines()
    assert lines[0].startswith("S形站姿")          # 名称在图下方
    assert lines[1] == "正面"                     # 角度第二行
    dialog.close()


def test_cards_refill_the_row_when_the_dialog_resizes(app):
    """全屏后卡片不能只占一点点位置：随窗口拉大、行宽填满、不重叠（用户反馈）。"""
    from localgen.desktop.library import GRID_SPACING, LibraryDialog

    dialog = LibraryDialog(FakeApi(), "poses", "姿势")
    dialog.show()
    app.processEvents()

    def row_fill() -> tuple[int, int]:
        used = dialog.columns * dialog.card_size.width() + (dialog.columns - 1) * GRID_SPACING
        return used, dialog.scroll.viewport().width()

    dialog.resize(520, 420)
    app.processEvents()
    dialog.relayout()
    narrow_columns, narrow_width = dialog.columns, dialog.card_size.width()
    used, viewport = row_fill()
    assert used <= viewport < used + narrow_width
    assert dialog.cards[0].width() == narrow_width   # 卡片真的跟着变了

    dialog.resize(1600, 900)
    app.processEvents()
    dialog.relayout()
    assert dialog.columns > narrow_columns           # 变宽 → 列更多
    assert dialog.card_size.width() > narrow_width   # 卡片更大
    used, viewport = row_fill()
    assert used <= viewport < used + dialog.card_size.width()
    assert dialog.cards[0].size() == dialog.card_size
    dialog.close()


def test_garment_card_shows_id_instead_of_the_long_description(app):
    """服装没有 title：卡片上绝不能摆那串英文描述，否则图标被挤没（用户反馈）。"""
    from localgen.desktop.library import LibraryDialog

    payload = {"total": 1,
               "items": [{"id": "SPR-0001", "kind": "garment", "category_cn": "春装",
                          "description": "ivory frill knit denim mini skirt coat socks"}],
               "collections": [], "categories": ["春"]}
    dialog = LibraryDialog(FakeApi(payload), "garments", "服装")
    card = dialog.cards[0]
    assert card.text().splitlines()[0] == "SPR-0001"
    assert "ivory" not in card.text()
    assert "ivory" in card.toolTip()              # 完整描述留在 tooltip


def test_dialog_detail_does_not_repeat_the_description(app):
    from localgen.desktop.library import LibraryDialog

    payload = {"total": 1,
               "items": [{"id": "SPR-0001", "kind": "garment", "category_cn": "春装",
                          "description": "ivory frill knit denim mini skirt coat socks"}],
               "collections": [], "categories": ["春"]}
    dialog = LibraryDialog(FakeApi(payload), "garments", "服装")
    dialog.select(dialog.items[0])
    assert dialog.detail.text().count("ivory") == 1


def test_fill_thumb_fills_the_frame(app):
    """图片必须铺满画框（必要时居中裁切），不留左右白边。"""
    from localgen.desktop.library import THUMB, fill_thumb

    wide = QPixmap(200, 100)
    wide.fill(Qt.GlobalColor.red)
    out = fill_thumb(wide, THUMB)
    assert out.width() == THUMB.width()
    assert out.height() == THUMB.height()
