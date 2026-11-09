import threading
import time
from pathlib import Path

import pytest
import uvicorn
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from localgen.__main__ import bind_port, serve
from localgen.api.app import make_app
from localgen.config import Settings
from localgen.desktop import Window
from localgen.desktop.style import STYLE
from localgen.engine.mock import MockEngine


@pytest.fixture(scope="module")
def qt_app():
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    return app


def spin(predicate, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        QApplication.processEvents()
        time.sleep(0.03)
    assert predicate()


def test_desktop_submit_cancel_preview(tmp_path, qt_app, monkeypatch):
    opened = []

    def fake_open_url(url):
        opened.append(url)
        return True

    monkeypatch.setattr("localgen.desktop.QDesktopServices.openUrl", fake_open_url)
    with bind_port(0) as sock:
        settings = Settings(engine_mode="mock", api_port=sock.getsockname()[1], min_free_mib=0)
        token = "desktop-test-token"
        server = uvicorn.Server(uvicorn.Config(
            make_app(tmp_path, settings, token, engine=MockEngine(1.6)), log_config=None,
            access_log=False, ws="none",
        ))
        thread = threading.Thread(target=serve, args=(server, sock), daemon=True)
        thread.start()
        window = Window(tmp_path, settings, token)
        window.show()
        try:
            spin(lambda: window.workflow.count() == 1 and window.engine.get("available"))
            window.prompt.setPlainText("测试桌面预览")
            QTest.mouseClick(window.generate, Qt.MouseButton.LeftButton)
            spin(lambda: window.cancel_button.isEnabled())
            QTest.mouseClick(window.cancel_button, Qt.MouseButton.LeftButton)
            spin(lambda: "已取消" in window.detail.text())
            window.prompt.setPlainText("测试 PNG")
            QTest.mouseClick(window.generate, Qt.MouseButton.LeftButton)
            spin(lambda: window.save_button.isEnabled())
            assert window.pixmap.width() == 512
            assert window.list.count() == 2
            assert window.png.startswith(b"\x89PNG")
            # 点击预览画布 → 系统图片查看器打开磁盘上的原始 PNG
            QTest.mouseClick(window.canvas, Qt.MouseButton.LeftButton)
            assert opened, "click on canvas should open the original image"
            opened_file = Path(opened[-1].toLocalFile())
            assert opened_file.is_file() and opened_file.suffix == ".png"
            assert opened_file.parent.parent.parent == tmp_path
            # 完整提示词展示与一键复制
            assert window.prompt_view.toPlainText() == "测试 PNG"
            QTest.mouseClick(window.copy_prompt_button, Qt.MouseButton.LeftButton)
            assert QApplication.clipboard().text() == "测试 PNG"
            # 删除任务：确认弹窗 → 任务、成品目录与界面条目一并清除
            monkeypatch.setattr("localgen.desktop.QMessageBox.question",
                                lambda *a, **k: QMessageBox.StandardButton.Yes)
            job_id = window.selected
            assert job_id
            QTest.mouseClick(window.delete_button, Qt.MouseButton.LeftButton)
            spin(lambda: window.list.count() == 1)
            spin(lambda: not (tmp_path / "results" / job_id).exists())
            assert window.prompt_view.toPlainText() == ""
            assert not window.delete_button.isEnabled()
        finally:
            window.timer.stop()
            window.close()
            server.should_exit = True
            thread.join(10)
            assert not thread.is_alive()


def test_desktop_reference_upload_and_lock(tmp_path, qt_app):
    """回归：Client 漏 import 曾让"选择图片"永久卡在加载中。"""
    from io import BytesIO

    from PIL import Image
    from localgen.desktop.reference import ReferencePicker

    with bind_port(0) as sock:
        settings = Settings(engine_mode="mock", api_port=sock.getsockname()[1], min_free_mib=0)
        token = "desktop-upload-token"
        server = uvicorn.Server(uvicorn.Config(
            make_app(tmp_path, settings, token, engine=MockEngine(0.1)), log_config=None,
            access_log=False, ws="none",
        ))
        thread = threading.Thread(target=serve, args=(server, sock), daemon=True)
        thread.start()
        window = Window(tmp_path, settings, token)
        try:
            buffer = BytesIO()
            Image.new("RGB", (320, 200), (12, 34, 56)).save(buffer, "PNG")
            answers = []
            window.api.upload_image(buffer.getvalue(), "reference.png",
                                    lambda value, error: answers.append((value, error)))
            spin(lambda: answers)
            value, error = answers[0]
            assert error is None, error
            assert value["name"].endswith(".png")

            # 默认的演示工作流没有 image 输入 → 参考图控件必须锁定
            spin(lambda: window.workflow.count() == 1)
            assert window.reference.locked
            assert not window.reference.choose_button.isEnabled()
            # 换成支持参考图的模型（直接喂工作流能力，不依赖真实引擎）→ 解锁
            picker = ReferencePicker(window.api.upload_image, window)
            picker.set_locked(False)
            assert picker.choose_button.isEnabled()
            picker.deleteLater()
        finally:
            window.timer.stop()
            window.close()
            server.should_exit = True
            thread.join(10)
