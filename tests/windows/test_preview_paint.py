"""预览图的显示时机：选中历史后必须立刻按画布尺寸铺满，不能等窗口缩放。

回归的是真实反馈："点击生成记录之后，预览图不会直接显示，点击放大预览才能看到，
或者最大化最小化之后才能看到"。
根因：`paint_image()` 原来用 `self.image.size()` 算缩放框，而 image 标签刚从隐藏
切到可见时布局还没重新分配，取到的是隐藏前的旧尺寸（甚至 1×1）。
"""
from io import BytesIO

import pytest
from PIL import Image
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication

from localgen.config import Settings
from localgen.desktop import Window


def png(size):
    buffer = BytesIO()
    Image.new("RGB", size, (30, 60, 90)).save(buffer, "PNG")
    return buffer.getvalue()


def load(size=(512, 512)):
    pixmap = QPixmap()
    assert pixmap.loadFromData(png(size), "PNG")
    return pixmap


@pytest.fixture(scope="module")
def qt_app():
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(tmp_path, qt_app, monkeypatch):
    monkeypatch.setattr(Window, "refresh", lambda self: None)
    monkeypatch.setattr(Window, "refresh_workflows", lambda self: None)
    monkeypatch.setattr(Window, "refresh_history", lambda self: None)
    win = Window(tmp_path, Settings(engine_mode="mock", api_port=6199, min_free_mib=0), "t")
    win.timer.stop()
    win.resize(1220, 840)
    win.show()
    try:
        yield win
    finally:
        win.timer.stop()
        win.close()


def settle(app, rounds=12):
    for _ in range(rounds):
        app.processEvents()


def test_pixmap_fills_the_canvas_without_any_resize(window, qt_app):
    """核心断言：显示后立刻接近画布尺寸，不需要 resize 触发重绘。"""
    window.pixmap = load((512, 512))
    window.png = b"x"
    settle(qt_app)

    window.show_canvas(True)
    settle(qt_app)

    shown = window.image.pixmap()
    assert not shown.isNull()
    canvas = window.canvas.size()
    # 512² 方形图：短边应铺满画布（留 1px 容差），而不是缩成一个小点
    assert shown.width() >= min(canvas.width(), canvas.height()) - 2, (shown.size(), canvas)


def test_paint_image_uses_canvas_size_not_the_hidden_label(window, qt_app):
    """image 标签被隐藏过（尺寸不可信）时，仍要按画布算框。"""
    window.image.setVisible(False)
    window.pixmap = load((768, 768))
    window.paint_image()
    assert window.image.pixmap().width() == window.canvas.size().width() or \
        window.image.pixmap().height() == window.canvas.size().height()


def test_clear_then_show_again_still_paints(window, qt_app):
    """切到别的任务（clear_image）再切回来，仍要能立刻显示。

    真实流程里 `completed()` 总是先设好 `self.pixmap` 再调 `show_canvas(True)`，
    这里照抄那个顺序；顺带确认 `clear_image` 之后单独 show_canvas 不会画出空白框。
    """
    window.pixmap = load((512, 512))
    window.png = b"x"
    window.show_canvas(True)
    settle(qt_app)
    first = window.image.pixmap().size()
    assert first.width() > 100

    window.clear_image("正在加载 PNG…")
    settle(qt_app)
    assert window.image.isHidden()          # 没有图 → 露出空状态，不留白框
    window.show_canvas(True)                # pixmap 还是 None：不该假装有图
    assert window.image.isHidden()

    window.pixmap = load((512, 512))        # completed() 的那一步
    window.show_canvas(True)
    settle(qt_app)
    assert window.image.pixmap().size() == first


def test_zoomed_layout_repaints_at_the_new_size(window, qt_app):
    """放大预览（隐藏历史卡）后要按新画布重绘，不能留着旧尺寸的模糊图。"""
    window.pixmap = load((512, 512))
    window.png = b"x"
    window.show_canvas(True)
    settle(qt_app)
    small = window.image.pixmap().height()

    window.toggle_zoom()
    settle(qt_app)
    assert window.image.pixmap().height() > small
