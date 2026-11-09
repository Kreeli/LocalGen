"""PoseSwapJob：双参考图的命令行拼接、顺序、步数、缺参报错。

这些都是纯字符串/路径逻辑，不需要 GPU：构造 job 就会拼好 command。
"""
import json
from pathlib import Path

import pytest
from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QImage

from localgen.engine.sdcpp import PoseSwapJob
from localgen.jobs.models import AppError

MODELS = {
    "diffusion_model": "qwen_image_2.1-Q4_K.gguf",
    "text_encoder": "Qwen3VL-8B-Instruct-Q4_K_M.gguf",
    "vision_encoder": "mmproj-Qwen3VL-8B-Instruct-F16.gguf",
    "vae": "qwen_image_2.1_vae_bf16.safetensors",
}
JOB_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def png_bytes():
    image = QImage(64, 64, QImage.Format.Format_RGB32)
    image.fill(0xFF3366)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


def build_env(tmp_path, png_bytes):
    """造一份最小 runtime：sd-cli.exe + 4 个模型文件 + 图库 + 上传区。"""
    runtime = tmp_path / "runtime"
    (runtime / "bin").mkdir(parents=True)
    (runtime / "bin" / "sd-cli.exe").write_bytes(b"MZ")
    (runtime / "models").mkdir()
    for name in MODELS.values():
        (runtime / "models" / name).write_bytes(b"w")

    libraries = tmp_path / "libraries"
    for kind, item_id, relative in (
        ("poses", "P001_front", "img/P001_front.webp"),
        ("garments", "SPR-0001", "img/SPR-0001.webp"),
    ):
        folder = libraries / kind
        (folder / "img").mkdir(parents=True)
        (folder / relative).write_bytes(png_bytes)
        payload = {"version": 1, "count": 1,
                   "items": [{"id": item_id, "image": relative, "thumb": relative}]}
        (folder / "index.json").write_text(json.dumps(payload), "utf-8")

    incoming = tmp_path / "gateway-input"
    incoming.mkdir()
    token = "a" * 32 + ".png"
    (incoming / token).write_bytes(png_bytes)
    return runtime, libraries, incoming, token


def params(**overrides):
    base = dict(MODELS, prompt="改成站姿", negative_prompt="", image="", pose_id="",
                garment_id="", width=768, height=768, seed=42, steps=20,
                cfg_scale=4, sampling_method="euler")
    base.update(overrides)
    return base


def run(tmp_path, png_bytes, *, want_token=True, job_id=JOB_ID, library=None,
        no_library=False, **overrides):
    """建 env → 按需把上传 token 填进 image → 构造 job。"""
    runtime, libraries, incoming, token = build_env(tmp_path, png_bytes)
    given = dict(overrides)
    if want_token or "image" in overrides:
        given.setdefault("image", token)
    root = None if no_library else (libraries if library is None else library)
    return PoseSwapJob(job_id, tmp_path / "out", runtime, params(**given), 0, incoming,
                       library_root=root)


def refs_after(command):
    out = []
    for index, part in enumerate(command):
        if part == "--ref-image":
            out.append(Path(command[index + 1]).name)
    return out


def test_pose_then_person_order(tmp_path, png_bytes):
    """姿势在前、人物在后 —— 实测这个顺序快 27%。"""
    job = run(tmp_path, png_bytes, pose_id="P001_front")
    assert refs_after(job.command) == ["ref0-library.png", "ref1-person.png"]


def test_garment_then_person_order(tmp_path, png_bytes):
    job = run(tmp_path, png_bytes, garment_id="SPR-0001")
    assert refs_after(job.command) == ["ref0-library.png", "ref1-person.png"]


def test_pose_and_garment_and_person(tmp_path, png_bytes):
    job = run(tmp_path, png_bytes, pose_id="P001_front", garment_id="SPR-0001")
    assert refs_after(job.command) == [
        "ref0-library.png", "ref1-library.png", "ref2-person.png"]


def test_increase_ref_index_only_with_multiple(tmp_path, png_bytes):
    single = run(tmp_path / "a", png_bytes, pose_id="P001_front", want_token=False)
    assert refs_after(single.command) == ["ref0-library.png"]
    assert "--increase-ref-index" not in single.command
    multi = run(tmp_path / "b", png_bytes, pose_id="P001_front")
    assert "--increase-ref-index" in multi.command


def test_steps_are_not_inflated(tmp_path, png_bytes):
    """用户明确要求：扩散步数不要改多。默认 20，传多少就是多少。"""
    job = run(tmp_path, png_bytes, pose_id="P001_front")
    assert job.command[job.command.index("--steps") + 1] == "20"
    fewer = run(tmp_path / "b", png_bytes, pose_id="P001_front", steps=15)
    assert fewer.command[fewer.command.index("--steps") + 1] == "15"


def test_no_reference_at_all_rejected(tmp_path, png_bytes):
    with pytest.raises(AppError) as caught:
        run(tmp_path, png_bytes, want_token=False)
    assert caught.value.detail.code == "INVALID_INPUT_IMAGE"


def test_unknown_pose_id_rejected(tmp_path, png_bytes):
    with pytest.raises(AppError) as caught:
        run(tmp_path, png_bytes, pose_id="NOPE")
    assert caught.value.detail.code == "LIBRARY_ITEM_NOT_FOUND"


def test_library_missing(tmp_path, png_bytes):
    with pytest.raises(AppError) as caught:
        run(tmp_path, png_bytes, pose_id="P001_front", no_library=True)
    assert caught.value.detail.code == "LIBRARY_NOT_INSTALLED"


def test_wrong_models_rejected(tmp_path, png_bytes):
    with pytest.raises(AppError) as caught:
        run(tmp_path, png_bytes, pose_id="P001_front", diffusion_model="other.gguf")
    assert caught.value.detail.code == "WORKFLOW_DEPENDENCIES_MISSING"


def test_references_are_cropped_to_output_size(tmp_path, png_bytes):
    """两张参考图都要被 stage 成输出尺寸，否则 sd.cpp 会按原始尺寸吃显存。"""
    job = run(tmp_path, png_bytes, pose_id="P001_front")
    for name in refs_after(job.command):
        path = job.folder / name
        assert path.is_file(), path
        image = QImage(str(path))
        assert (image.width(), image.height()) == (768, 768)


def test_vision_encoder_is_passed(tmp_path, png_bytes):
    job = run(tmp_path, png_bytes, pose_id="P001_front")
    value = job.command[job.command.index("--llm_vision") + 1]
    assert value.endswith("mmproj-Qwen3VL-8B-Instruct-F16.gguf")
