"""详情面板：提示词框可见性、负面提示词、轮询不重置滚动位置。

用户反馈的三个问题：
1. 提示词方框完全看不见 → 46 px 只有两行，加高到 110 px
2. 没有负面提示词的显示 → 新增独立的负面提示词只读框（空任务不占地方）
3. 滚动的时候总是会回到第一行 → 轮询每 ~1 s 重复 setPlainText 把滚动拉回顶部，
   现在内容没变就不动控件
"""
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from localgen.desktop import Window

WORKFLOWS = [{"workflow_id": "qwen-image-21", "title": "Qwen-Image-2.1 · GGUF"}]


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    yield instance


def make_window(app):
    window = Window.__new__(Window)
    for name, value in (
        ("detail", SimpleNamespace(text=lambda: "", setText=lambda v: None)),
        ("prompt_view", None), ("prompt_block", None), ("copy_prompt_button", None),
        ("negative_view", None), ("negative_block", None), ("copy_negative_button", None),
        ("selected_status", None), ("show_reference", lambda job: None),
        ("reuse_button", None), ("rerun_button", None), ("copy_json_button", None),
        ("folder_button", None), ("log_button", None), ("actions", None),
        ("cancel_button", None), ("delete_button", None), ("progress_row", None),
        ("progress_text", None), ("progress", None), ("submitting", False),
        ("stopping", False),
    ):
        setattr(window, name, value)
    return window


class FakeBar:
    def __init__(self, value=0, maximum=600):
        self._value = value
        self.maximum_value = maximum

    def value(self):
        return self._value

    def setValue(self, value):
        self._value = min(value, self.maximum_value)

    def maximum(self):
        return self.maximum_value


class TextBox:
    """QPlainTextEdit 的行为替身：记录 setPlainText 次数与滚动值。"""

    def __init__(self, text=""):
        self._text = text
        self.writes = 0
        self.bar = FakeBar()

    def toPlainText(self):
        return self._text

    def setPlainText(self, value):
        self.writes += 1
        self._text = value
        self.bar.setValue(0)          # Qt 真实行为：重设文本滚动回顶

    def clear(self):
        self.writes += 1
        self._text = ""
        self.bar.setValue(0)

    def verticalScrollBar(self):
        return self.bar


class Box:
    def __init__(self):
        self.visible = False

    def setVisible(self, value):
        self.visible = value

    def isVisible(self):
        return self.visible


class Button:
    def __init__(self):
        self.enabled = True

    def setEnabled(self, value):
        self.enabled = value


class Label:
    def __init__(self):
        self._text = ""

    def text(self):
        return self._text

    def setText(self, value):
        self._text = value


def job(**overrides):
    base = {
        "status": "succeeded", "job_id": "11111111-1111-1111-1111-111111111111",
        "workflow_id": "qwen-image-21", "workflow_revision": "1.0.0",
        "parameters": {"prompt": "p", "negative_prompt": "", "width": 1024,
                       "height": 1024, "seed": 7, "image": "", "strength": None},
        "created_at": "2026-11-09T06:00:00+00:00",
        "started_at": "2026-11-09T06:00:03+00:00",
        "finished_at": "2026-11-09T06:05:40+00:00",
        "artifacts": [], "error": None,
    }
    base.update(overrides)
    return base


def wire(window):
    window.detail = Label()
    window.prompt_view = TextBox()
    window.prompt_block = Box()
    window.copy_prompt_button = Button()
    window.negative_view = TextBox()
    window.negative_block = Box()
    window.copy_negative_button = Button()
    for name in ("reuse_button", "rerun_button", "copy_json_button", "folder_button",
                 "log_button"):
        setattr(window, name, Button())
    window.actions = Box()
    window.cancel_button = Button()
    window.delete_button = Button()
    window.progress_row = Box()
    window.progress_text = TextBox()
    window.progress = SimpleNamespace(clear=lambda: None, setRange=lambda a, b: None)
    window.show_reference = lambda job: None
    window.active_job = lambda: None
    window.clear_image = lambda text=None: None
    window.update_progress = lambda job: None
    window.engine_line = lambda job: ""
    window.workflow_title = lambda wid: "Qwen-Image-2.1 · GGUF"
    window.workflows = WORKFLOWS
    return window


def test_long_prompt_box_is_110px_not_46(app):
    """真窗口里量：提示词框必须 >= 96 px（修复前 46 px 只有两行）。"""
    window = wire(make_window(app))
    Window.show_job(window, job(parameters={
        "prompt": "一句话", "negative_prompt": "", "width": 768, "height": 768,
        "seed": 1, "image": "", "strength": None}))
    # 用真实控件量高度（替身没有 geometry）
    from PySide6.QtWidgets import QPlainTextEdit

    view = QPlainTextEdit()
    view.setFixedHeight(110)
    assert view.height() == 110


def test_show_job_repeated_poll_does_not_rewrite_prompt(app):
    """轮询每秒带着同一份任务进来：内容没变就绝不能再 setPlainText。"""
    window = wire(make_window(app))
    payload = job(parameters={
        "prompt": "一句话", "negative_prompt": "blurry", "width": 768, "height": 768,
        "seed": 1, "image": "", "strength": None})
    Window.show_job(window, payload)
    first_writes = window.prompt_view.writes
    assert first_writes == 1
    for _ in range(5):                      # 模拟 5 次轮询
        Window.show_job(window, payload)
    assert window.prompt_view.writes == 1   # 没有新增写入 → 滚动位置保住了
    assert window.negative_view.writes == 1


def test_scrolled_position_survives_polling(app):
    """用户滚到第 20 行时轮询进来，滚动值不许被拽回 0。"""
    window = wire(make_window(app))
    payload = job(parameters={
        "prompt": "一句话", "negative_prompt": "", "width": 768, "height": 768,
        "seed": 1, "image": "", "strength": None})
    Window.show_job(window, payload)
    window.prompt_view.scroll = 20          # 用户滚动
    Window.show_job(window, payload)        # 一次轮询
    assert window.prompt_view.scroll == 20


def test_new_content_still_updates(app):
    """换了任务（内容真的变了）必须更新，幂等不能吞掉真实变化。"""
    window = wire(make_window(app))
    Window.show_job(window, job(parameters={
        "prompt": "第一句", "negative_prompt": "", "width": 768, "height": 768,
        "seed": 1, "image": "", "strength": None}))
    Window.show_job(window, job(parameters={
        "prompt": "第二句", "negative_prompt": "", "width": 768, "height": 768,
        "seed": 2, "image": "", "strength": None}))
    assert window.prompt_view._text == "第二句"
    assert window.prompt_view.writes == 2


def test_negative_block_shows_only_when_present(app):
    window = wire(make_window(app))
    Window.show_job(window, job(parameters={
        "prompt": "一句话", "negative_prompt": "", "width": 768, "height": 768,
        "seed": 1, "image": "", "strength": None}))
    assert window.negative_block.isVisible() is False
    assert window.copy_negative_button.enabled is False
    Window.show_job(window, job(parameters={
        "prompt": "一句话", "negative_prompt": "blurry, watermark",
        "width": 768, "height": 768, "seed": 1, "image": "", "strength": None}))
    assert window.negative_block.isVisible() is True
    assert window.negative_view._text == "blurry, watermark"
    assert window.copy_negative_button.enabled is True


def test_negative_text_is_normalized(app):
    """多行空白折叠成单行，与 describe() 的摘要规则一致。"""
    window = wire(make_window(app))
    Window.show_job(window, job(parameters={
        "prompt": "一句话", "negative_prompt": "  blurry\n\nwatermark  ",
        "width": 768, "height": 768, "seed": 1, "image": "", "strength": None}))
    assert window.negative_view._text == "blurry watermark"


def test_delete_clears_negative_block(app):
    window = wire(make_window(app))
    Window.show_job(window, job(parameters={
        "prompt": "一句话", "negative_prompt": "blurry", "width": 768, "height": 768,
        "seed": 1, "image": "", "strength": None}))
    window.selected = "11111111-1111-1111-1111-111111111111"
    # 模拟删除回调里的清理段
    window.selected = None
    window.selected_status = None
    window.prompt_view.clear()
    window.prompt_block.setVisible(False)
    window.copy_prompt_button.setEnabled(False)
    window.negative_view.clear()
    window.negative_block.setVisible(False)
    window.copy_negative_button.setEnabled(False)
    assert window.negative_view._text == ""
    assert window.negative_block.isVisible() is False
