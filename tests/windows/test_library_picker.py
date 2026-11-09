"""图库选择器：选中状态、清除、提交载荷、按工作流能力显隐。

不打真网络：Client 换成记录调用的假对象，缩略图请求直接忽略。
"""
import pytest
from PySide6.QtWidgets import QApplication

from localgen.desktop.library import LibraryPicker

POSES = [
    {"id": "P001_front", "title": "S形站姿", "angle": "正面", "kind": "pose"},
    {"id": "P001_high", "title": "S形站姿", "angle": "俯视", "kind": "pose"},
]


@pytest.fixture(scope="module")
def app():
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
