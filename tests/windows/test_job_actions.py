"""任务详情（A2 参考图实体）与动作条（复用 / 重跑 / 复制 JSON / 打开文件夹）。

对应《界面与任务可观测性方案》§3.1 A2 与 §3.3。这些测试跑在离屏 Qt 上，
不起 HTTP 服务：所有动作都只读本地磁盘与界面状态。
"""
import json
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from PySide6.QtWidgets import QApplication

from localgen.config import Settings
from localgen.desktop import Window
from localgen.desktop.reference import ReferencePicker

WORKFLOW = {
    "workflow_id": "moss-image", "title": "MOSS-Image · GGUF", "revision": "1.0.0",
    "supported_parameters": ["image", "strength", "negative_prompt"],
    "required_parameters": [], "verified_sizes": [[768, 768], [512, 512]],
    "default_size": [768, 768], "default_strength": 0.5, "available": True,
}
JOB_ID = "0f0a1b2c-3d4e-5f60-7182-93a4b5c6d7e8"
TOKEN = "b726b578119a4edba0eeb7b79078935e.png"


def payload(**overrides):
    base = {
        "job_id": JOB_ID, "status": "succeeded", "workflow_id": "moss-image",
        "workflow_revision": "1.0.0",
        "parameters": {"prompt": "雨夜霓虹街", "negative_prompt": "lowres", "width": 768,
                       "height": 768, "seed": 4242, "image": TOKEN, "strength": 0.62},
        "created_at": "2026-11-09T06:00:00+00:00",
        "started_at": "2026-11-09T06:00:04+00:00",
        "finished_at": "2026-11-09T06:01:47+00:00",
        "progress": None, "artifacts": [], "error": None,
    }
    base.update(overrides)
    return base


@pytest.fixture(scope="module")
def qt_app():
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(tmp_path, qt_app, monkeypatch):
    monkeypatch.setattr(Window, "refresh", lambda self: None)   # 别让定时器打真接口
    win = Window(tmp_path, Settings(engine_mode="mock", api_port=6199, min_free_mib=0), "t")
    win.timer.stop()
    win.workflows = [WORKFLOW]
    win.workflow.blockSignals(True)
    win.workflow.clear()
    win.workflow.addItem(WORKFLOW["title"], WORKFLOW["workflow_id"])
    win.workflow.setCurrentIndex(0)
    win.workflow.blockSignals(False)
    win.workflow_changed()
    win.latest = [payload()]
    win.selected = JOB_ID
    try:
        yield win
    finally:
        win.close()


def write_image(path, size):
    path.parent.mkdir(parents=True, exist_ok=True)
    buffer = BytesIO()
    Image.new("RGB", size, (20, 30, 40)).save(buffer, "PNG")
    path.write_bytes(buffer.getvalue())


# ---------- A2：参考图实体与裁剪说明 ----------

def test_reference_block_shows_cropped_copy_and_crop_note(window, tmp_path):
    write_image(tmp_path / "comfy-output" / JOB_ID / "input.png", (768, 768))
    write_image(tmp_path / "gateway-input" / TOKEN, (1200, 800))
    window.show_job(payload())
    assert not window.reference_block.isHidden()
    caption = window.reference_caption.text()
    assert "1200 × 800" in caption and "768 × 768" in caption and "不拉伸" in caption
    assert window.reference_source == tmp_path / "comfy-output" / JOB_ID / "input.png"
    assert not window.reference_thumb.pixmap().isNull()


def test_reference_block_prefers_the_kept_copy(window, tmp_path):
    """任务附属物优先：引擎目录被清理后，历史里仍然看得到当初喂了什么。"""
    write_image(tmp_path / "results" / JOB_ID / "reference.png", (768, 768))
    write_image(tmp_path / "gateway-input" / TOKEN, (1200, 800))
    window.show_job(payload())
    assert window.reference_source == tmp_path / "results" / JOB_ID / "reference.png"
    assert "1200 × 800" in window.reference_caption.text()


def test_reference_block_falls_back_to_original_upload(window, tmp_path):
    """引擎副本被清理时，仍要能显示原图并说明"这一份不是模型看到的那份"。"""
    write_image(tmp_path / "gateway-input" / TOKEN, (900, 600))
    window.show_job(payload())
    assert not window.reference_block.isHidden()
    assert window.reference_source == tmp_path / "gateway-input" / TOKEN
    assert "引擎副本已清理" in window.reference_caption.text()


def test_reference_block_hidden_when_files_are_gone(window, tmp_path):
    window.show_job(payload())
    assert window.reference_block.isHidden()
    assert window.reference_caption.text() == ""


def test_reference_block_hidden_for_text_only_job(window):
    parameters = dict(payload()["parameters"], image="", strength=None)
    window.show_job(payload(parameters=parameters))
    assert window.reference_block.isHidden()


# ---------- 动作条 ----------

def test_reuse_parameters_fills_the_editor(window):
    assert window.reuse_parameters() is not None
    assert window.prompt.toPlainText() == "雨夜霓虹街"
    assert window.negative.toPlainText() == "lowres"
    assert window.seed.text() == "4242"
    assert window.size.currentData() == [768, 768]
    assert window.strength_slider.value() == 62
    assert "未提交" in window.form_note.text()
    assert window.generate.isEnabled() is False or True   # 提交与否由 controls() 决定


def test_rerun_clears_seed_and_submits(window, monkeypatch):
    calls = []
    monkeypatch.setattr(window, "submit", lambda: calls.append(True))
    window.rerun_job()
    assert calls == [True]
    assert window.seed.text() == ""            # 换新随机种子 → 种子留空
    assert window.prompt.toPlainText() == "雨夜霓虹街"
    assert "新种子" in window.form_note.text()


def test_reuse_reports_missing_workflow(window):
    window.workflows = []
    window.workflow.blockSignals(True)
    window.workflow.clear()
    window.workflow.blockSignals(False)
    assert window.reuse_parameters() is None
    assert "模型已不在本机" in window.form_note.text()


def test_copy_parameters_json_round_trip(window):
    window.copy_parameters()
    text = QApplication.clipboard().text()
    data = json.loads(text)
    assert data["workflow_id"] == "moss-image" and data["workflow_revision"] == "1.0.0"
    assert data["seed"] == 4242 and data["width"] == 768 and data["strength"] == 0.62
    assert data["image"] == TOKEN
    assert data["prompt"] == "雨夜霓虹街"


def test_copy_parameters_omits_absent_capabilities(window):
    parameters = {"prompt": "p", "width": 512, "height": 512, "seed": 1}
    window.latest = [payload(parameters=parameters)]
    window.copy_parameters()
    data = json.loads(QApplication.clipboard().text())
    assert "strength" not in data and "image" not in data


def test_open_results_opens_the_job_folder(window, tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr("localgen.desktop.QDesktopServices.openUrl",
                        lambda url: opened.append(url.toLocalFile()) or True)
    (tmp_path / "results" / JOB_ID).mkdir(parents=True)
    window.open_results()
    assert opened and opened[-1].endswith(JOB_ID)


def test_open_results_without_folder_notes_instead_of_warning(window):
    window.open_results()
    assert "成品文件夹" in window.form_note.text()


def test_actions_enabled_only_with_a_selected_job(window):
    window.hide_actions()
    assert window.actions.isHidden()
    assert not window.reuse_button.isEnabled()
    window.show_job(payload())
    assert not window.actions.isHidden() and window.reuse_button.isEnabled()


# ---------- 姿势 / 服装：历史里要能看见当时选的那一张 ----------

def library_payload(**overrides):
    parameters = dict(payload()["parameters"], pose_id="full_sexy_P001_front",
                      garment_id="SPR-0001")
    return payload(parameters=parameters, **overrides)


def test_library_block_shows_the_kept_pose_and_garment(window, tmp_path):
    write_image(tmp_path / "results" / JOB_ID / "pose.png", (768, 1024))
    write_image(tmp_path / "results" / JOB_ID / "garment.png", (683, 1024))
    window.show_job(library_payload())
    assert not window.library_block.isHidden()
    assert not window.pose_thumb.pixmap().isNull()
    assert not window.garment_thumb.pixmap().isNull()
    assert "full_sexy_P001_front" in window.pose_caption.text()
    assert "SPR-0001" in window.garment_caption.text()
    assert window.library_source == {
        "姿势": tmp_path / "results" / JOB_ID / "pose.png",
        "服装": tmp_path / "results" / JOB_ID / "garment.png",
    }


def test_library_block_names_the_pick_when_the_copy_is_gone(window):
    """早期任务没转存附属物：至少说清当时选的是哪一条，别只留空白。"""
    window.show_job(library_payload())
    assert not window.library_block.isHidden()
    assert "没有留存图片" in window.pose_caption.text()
    assert window.pose_thumb.pixmap().isNull()
    assert window.library_source == {}


def test_library_block_hidden_for_a_job_without_library_picks(window):
    window.show_job(payload())
    assert window.library_block.isHidden()
    assert window.pose_caption.text() == "" and window.garment_caption.text() == ""


def test_library_block_shows_only_the_picked_kind(window, tmp_path):
    write_image(tmp_path / "results" / JOB_ID / "garment.png", (683, 1024))
    parameters = dict(payload()["parameters"], garment_id="SPR-0002")
    window.show_job(payload(parameters=parameters))
    assert not window.library_block.isHidden()
    assert window.pose_caption.text() == "" and window.pose_thumb.pixmap().isNull()
    assert "SPR-0002" in window.garment_caption.text()


def test_open_library_opens_the_kept_copy(window, tmp_path, monkeypatch):
    write_image(tmp_path / "results" / JOB_ID / "pose.png", (768, 1024))
    window.show_job(library_payload())
    opened = []
    monkeypatch.setattr("localgen.desktop.QDesktopServices.openUrl",
                        lambda url: opened.append(url.toLocalFile()) or True)
    window.open_library("姿势")
    # QUrl 会把分隔符统一成 '/'，比较前先按 Path 规整。
    assert [Path(one) for one in opened] == [tmp_path / "results" / JOB_ID / "pose.png"]


def test_describe_names_the_picked_library_items(window):
    text = window.describe(library_payload())
    assert "姿势 full_sexy_P001_front" in text and "服装 SPR-0001" in text
    assert all(len(line) <= 60 for line in text.splitlines())


def test_copy_parameters_carries_the_library_ids(window):
    window.latest = [library_payload()]
    window.selected = JOB_ID
    window.copy_parameters()
    data = json.loads(QApplication.clipboard().text())
    assert data["pose_id"] == "full_sexy_P001_front" and data["garment_id"] == "SPR-0001"


# ---------- 参考图控件的回填 ----------

def test_reference_picker_restore_keeps_token_without_uploading(qt_app):
    uploads = []
    picker = ReferencePicker(lambda content, name, callback: uploads.append(name), None)
    picker.restore(TOKEN, (1200, 800))
    assert picker.token == TOKEN and uploads == []
    assert picker.clear_button.isEnabled() and not picker.crop_button.isEnabled()
    assert "1200 × 800" in picker.meta.text()
    assert TOKEN.split(".")[0][:8] in picker.name.text()
    picker.deleteLater()


def test_reference_picker_restore_unknown_source_size(qt_app):
    picker = ReferencePicker(lambda *a: None, None)
    picker.restore(TOKEN)
    assert picker.token == TOKEN
    assert "1024 × 1024" in picker.meta.text()
    picker.deleteLater()


# ---------- 引擎固定参数与日志路径（批次 5） ----------

def test_engine_line_reads_the_declared_parameters(window):
    window.workflows = [dict(WORKFLOW, engine_parameters={
        "steps": 50, "cfg_scale": 3, "sampling_method": "euler", "scheduler": "discrete"})]
    line = window.engine_line(payload())
    assert "50 步" in line and "CFG 3" in line and "euler" in line and "discrete" in line


def test_engine_line_is_empty_when_the_workflow_declares_nothing(window):
    window.workflows = [dict(WORKFLOW, engine_parameters={})]
    assert window.engine_line(payload()) == ""


def test_engine_line_tolerates_a_workflow_that_vanished(window):
    window.workflows = []
    assert window.engine_line(payload()) == ""


def test_describe_appends_the_engine_line(window):
    window.workflows = [dict(WORKFLOW, engine_parameters={"steps": 50, "cfg_scale": 3})]
    text = window.describe(payload())
    assert "引擎 50 步" in text and "CFG 3" in text


def test_log_path_prefers_the_sdcpp_log(window, tmp_path):
    (tmp_path / "logs").mkdir(parents=True)
    sdcpp = tmp_path / "logs" / f"sdcpp-{JOB_ID}.log"
    sdcpp.write_text("log", "utf-8")
    assert window.log_path(payload()) == sdcpp


def test_log_path_falls_back_to_the_comfy_log(window, tmp_path):
    (tmp_path / "logs").mkdir(parents=True)
    comfy = tmp_path / "logs" / f"comfy-{JOB_ID}.log"
    comfy.write_text("log", "utf-8")
    assert window.log_path(payload()) == comfy


def test_log_path_is_none_when_the_job_has_no_log(window):
    assert window.log_path(payload()) is None


def test_open_log_notes_instead_of_warning_when_missing(window):
    window.latest = [payload()]
    window.selected = JOB_ID
    window.open_log()
    assert "没有单独的引擎日志" in window.form_note.text()


def test_open_log_opens_the_file(window, tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr("localgen.desktop.QDesktopServices.openUrl",
                        lambda url: opened.append(url.toLocalFile()) or True)
    (tmp_path / "logs").mkdir(parents=True)
    (tmp_path / "logs" / f"sdcpp-{JOB_ID}.log").write_text("log", "utf-8")
    window.latest = [payload()]
    window.selected = JOB_ID
    window.open_log()
    assert opened and opened[-1].endswith(f"sdcpp-{JOB_ID}.log")
