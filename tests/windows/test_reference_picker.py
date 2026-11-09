"""参考图控件：锁定态、裁切上传、异常兜底（不卡在"上传中"）。"""
import os
import time
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from localgen.desktop import reference as reference_module  # noqa: E402
from localgen.desktop.reference import ReferencePicker  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    return app


class Cropper:
    """替身裁切对话框：永远接受，返回固定的归一化裁切框。"""

    box = (0.0, 0.0, 1.0, 1.0)

    class DialogCode:
        Accepted = 1
        Rejected = 0

    def __init__(self, image, target, parent=None):
        self.image = image
        self.target = target
        self.canvas = SimpleNamespace(set_fractional=lambda box: None)

    def exec(self):
        return Cropper.DialogCode.Accepted


class Uploader:
    def __init__(self, token="a" * 32 + ".png", error=None, raises=None):
        self.calls = []
        self.result = ({"name": token}, None) if error is None else (None, error)
        self.raises = raises

    def __call__(self, content, filename, callback):
        self.calls.append((content, filename))
        if self.raises is not None:
            raise self.raises
        callback(*self.result)


def spin(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        QApplication.processEvents()
        time.sleep(0.02)
    assert predicate()


def sample_image(tmp_path, size=(600, 300)):
    image = Image.new("RGB", size, (200, 20, 20))
    ImageDraw.Draw(image).rectangle((size[0] // 3, 0, size[0] // 3 * 2, size[1]),
                                    fill=(20, 200, 20))
    path = tmp_path / "参考图.png"
    image.save(path, "PNG")
    return path


def test_picker_locks_and_unlocks(qt_app):
    picker = ReferencePicker(Uploader())
    picker.set_locked(True, "此模型不支持参考图")
    assert not picker.choose_button.isEnabled()
    assert picker.hint.text() == "此模型不支持参考图"
    picker.set_locked(False)
    assert picker.choose_button.isEnabled()
    assert picker.hint.text() == ""


def test_picker_crops_then_uploads(qt_app, tmp_path, monkeypatch):
    monkeypatch.setattr(reference_module, "CropDialog", Cropper)
    uploader = Uploader()
    picker = ReferencePicker(uploader)
    picker.set_target(256, 256)
    changes = []
    picker.changed.connect(lambda: changes.append(True))
    picker.load(sample_image(tmp_path))
    assert uploader.calls, "裁切后应该发起上传"
    content, filename = uploader.calls[-1]
    assert filename.endswith(".png")
    with Image.open(BytesIO(content)) as sent:
        assert sent.size == (256, 256)          # 上传的是按输出尺寸裁切后的图
    assert picker.token == "a" * 32 + ".png"
    assert not picker.busy
    assert "600 × 300" in picker.meta.text()
    assert changes


def test_picker_reports_upload_failure(qt_app, tmp_path, monkeypatch):
    monkeypatch.setattr(reference_module, "CropDialog", Cropper)
    picker = ReferencePicker(Uploader(error="本机服务暂不可用"))
    picker.load(sample_image(tmp_path))
    spin(lambda: not picker.busy)
    assert picker.token is None
    assert "本机服务暂不可用" in picker.meta.text()


def test_picker_recovers_when_client_raises(qt_app, tmp_path, monkeypatch):
    """回归：客户端抛错时按钮曾经永久停在"上传中"。"""
    monkeypatch.setattr(reference_module, "CropDialog", Cropper)
    picker = ReferencePicker(Uploader(raises=RuntimeError("boom")))
    picker.load(sample_image(tmp_path))
    assert not picker.busy
    assert picker.choose_button.isEnabled()
    assert "上传失败" in picker.meta.text()


def test_target_change_recrops_and_reuploads(qt_app, tmp_path, monkeypatch):
    monkeypatch.setattr(reference_module, "CropDialog", Cropper)
    uploader = Uploader()
    picker = ReferencePicker(uploader)
    picker.set_target(256, 256)
    picker.load(sample_image(tmp_path))
    assert len(uploader.calls) == 1
    picker.set_target(128, 192)
    spin(lambda: len(uploader.calls) == 2)
    with Image.open(BytesIO(uploader.calls[-1][0])) as sent:
        assert sent.size == (128, 192)
