"""配置文件与界面状态的 BOM 容错。

这两个 bug 都是"用户手改文件"才会触发，而且症状都很差：
- `config.toml` 带 BOM → 应用直接起不来（打包版没控制台，表现为双击没反应）
- `ui.json` 带 BOM → 布局静默丢失（异常被当成"文件不存在"吞掉）
记事本、VS Code、PowerShell 5.1 保存时都可能加 BOM。
"""
import json
import os

import pytest

from localgen.config import Settings, load_settings


def write(path, text, bom=False):
    payload = ("\ufeff" + text) if bom else text
    path.write_bytes(payload.encode("utf-8"))


# ---------- config.toml ----------

def test_config_without_bom_still_loads(tmp_path):
    write(tmp_path / "config.toml", 'api_port = 12138\nengine_mode = "comfy"\n')
    assert load_settings(tmp_path).api_port == 12138


def test_config_with_bom_loads(tmp_path):
    """修复前：tomllib 报 Invalid statement (at line 1, column 1)。"""
    write(tmp_path / "config.toml", 'api_port = 13138\nengine_mode = "comfy"\n', bom=True)
    assert load_settings(tmp_path).api_port == 13138


def test_config_syntax_error_names_the_file_and_the_cause(tmp_path):
    """原始报错只有 "Invalid statement (at line 1, column 1)"，用户看不懂。"""
    write(tmp_path / "config.toml", "api_port = \n")
    with pytest.raises(ValueError) as caught:
        load_settings(tmp_path)
    message = str(caught.value)
    assert "config.toml" in message          # 说清是哪个文件
    assert "BOM" in message                  # 说清最常见的原因
    assert "UTF-8（无 BOM）" in message       # 给一条能照做的办法


def test_config_invalid_value_names_the_file(tmp_path):
    write(tmp_path / "config.toml", 'engine_mode = "nonsense"\n')
    with pytest.raises(ValueError) as caught:
        load_settings(tmp_path)
    assert "config.toml" in str(caught.value)


def test_config_missing_is_created_from_defaults(tmp_path):
    settings = load_settings(tmp_path)
    assert (tmp_path / "config.toml").is_file()
    assert isinstance(settings, Settings)


# ---------- ui.json ----------

@pytest.fixture(scope="module")
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def make_window(qt_app, monkeypatch):
    from localgen.desktop import Window

    monkeypatch.setattr(Window, "refresh", lambda self: None)
    monkeypatch.setattr(Window, "refresh_workflows", lambda self: None)
    monkeypatch.setattr(Window, "refresh_history", lambda self: None)
    built = []

    def build(data):
        window = Window(data, Settings(engine_mode="mock", api_port=6199, min_free_mib=0), "t")
        window.timer.stop()
        built.append(window)
        return window

    yield build
    for window in built:
        window.timer.stop()
        window.close()


def test_ui_json_with_bom_is_read(make_window, tmp_path):
    """修复前：JSONDecodeError 被 except ValueError 吞掉 → 窗口回默认尺寸。"""
    payload = {"width": 1240, "height": 900, "x": 10, "y": 20,
               "columns": [360, 700], "split": [400, 260], "history": [380, 280],
               "workflow": "moss-image", "size": [768, 768]}
    (tmp_path / "ui.json").write_bytes(("\ufeff" + json.dumps(payload)).encode("utf-8"))
    window = make_window(tmp_path)
    assert (window.width(), window.height()) == (1240, 900)
    assert window.x() == 10 and window.y() == 20


def test_ui_json_without_bom_is_read(make_window, tmp_path):
    (tmp_path / "ui.json").write_bytes(
        json.dumps({"width": 1000, "height": 720}).encode("utf-8"))
    window = make_window(tmp_path)
    assert (window.width(), window.height()) == (1000, 720)


def test_broken_ui_json_falls_back_to_defaults(make_window, tmp_path):
    (tmp_path / "ui.json").write_bytes(b"{not json")
    window = make_window(tmp_path)
    assert (window.width(), window.height()) == (1180, 860)


def test_ui_json_round_trip(make_window, tmp_path):
    """写出去再读回来，尺寸与分栏都要对得上（打包态验收项）。

    分栏会被布局二次分配（离屏窗口没真正 show 时尤其明显），所以这里断言
    "读回来的比例与写出去的一致"，而不是死磕像素值。
    """
    window = make_window(tmp_path)
    window.resize(1240, 900)
    window.columns.setSizes([360, 700])
    window.save_ui()
    assert (tmp_path / "ui.json").is_file()
    written = json.loads((tmp_path / "ui.json").read_text("utf-8-sig"))["columns"]

    again = make_window(tmp_path)
    assert (again.width(), again.height()) == (1240, 900)
    assert again.columns.sizes() == written
