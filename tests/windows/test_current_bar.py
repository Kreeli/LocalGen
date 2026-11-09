"""当前任务条（/api/v1/status 驱动）的文案与可见性。"""
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication, QLabel


@pytest.fixture(scope="module")
def qt_app():
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def stub():
    from localgen.desktop import Window

    return SimpleNamespace(current_bar=QLabel(), status={}, stopping=False,
                           span=Window.span, refresh_cancel=lambda: None,
                           workflow_title=lambda wid: {"moss-image": "MOSS-Image · GGUF"}.get(wid, wid),
                           refresh_history=lambda: None, refresh_selected=lambda: None)


def test_bar_hidden_when_idle(qt_app):
    from localgen.desktop import Window

    s = stub()
    Window.update_status(s, {"active_job": None, "queue_length": 0, "queued_job_id": None}, None)
    assert s.current_bar.text() == ""
    assert s.current_bar.isHidden()


def test_bar_shows_model_name_not_a_bare_job_id(qt_app):
    """状态条存在的意义是"在跑哪个模型"，只印 job_id 前 8 位等于没说。"""
    from localgen.desktop import Window

    s = stub()
    Window.update_status(s, {"active_job": "0f0a1b2c-3d4e-5f60-7182-93a4b5c6d7e8",
                             "workflow_id": "moss-image", "stage": "sampling",
                             "elapsed_s": 192, "queue_length": 2}, None)
    text = s.current_bar.text()
    assert "MOSS-Image · GGUF" in text and "采样" in text
    assert "已用 3:12" in text and "队列 2" in text
    assert "0f0a1b2c" not in text
    assert not s.current_bar.isHidden()


def test_bar_refreshes_history_only_when_a_job_finishes(qt_app):
    """列表只在"有→无"翻转时拉一次，空闲时不许打接口。"""
    from localgen.desktop import Window

    s = stub()
    calls = []
    s.refresh_history = lambda: calls.append("history")
    Window.update_status(s, {"active_job": "aaa", "workflow_id": "moss-image",
                             "stage": "sampling", "elapsed_s": 5, "queue_length": 0}, None)
    assert calls == []                       # 正在跑：不拉
    Window.update_status(s, {"active_job": None, "queue_length": 0,
                             "queued_job_id": None}, None)
    assert calls == ["history"]              # 刚跑完：拉一次
    Window.update_status(s, {"active_job": None, "queue_length": 0,
                             "queued_job_id": None}, None)
    assert calls == ["history"]              # 之后继续空闲：不再拉


def test_bar_reports_queue_when_nothing_is_running(qt_app):
    from localgen.desktop import Window

    s = stub()
    Window.update_status(s, {"active_job": None, "queue_length": 3,
                             "queued_job_id": "abc", "position": 1}, None)
    assert "队列 3 条" in s.current_bar.text() and "等待引擎" in s.current_bar.text()


def test_bar_ignores_errors_and_keeps_last_text(qt_app):
    from localgen.desktop import Window

    s = stub()
    s.current_bar.setText("旧内容")
    Window.update_status(s, None, "boom")
    assert s.current_bar.text() == "旧内容"


def test_bar_maps_each_stage_name(qt_app):
    from localgen.desktop import Window

    for stage, expected in (("loading", "加载模型"), ("sampling", "采样"),
                            ("decoding", "解码"), ("weird", "weird"), (None, "准备中")):
        s = stub()
        Window.update_status(s, {"active_job": "abcdef1234567890", "workflow_id": "moss-image",
                                 "stage": stage, "elapsed_s": 5, "queue_length": 0}, None)
        assert expected in s.current_bar.text(), (stage, s.current_bar.text())
