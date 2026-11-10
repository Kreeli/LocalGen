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
    for kind, item_id, relative, extra in (
        ("poses", "P001_front", "img/P001_front.webp",
         {"title": "S形站姿・胯部侧顶", "category": "站姿", "angle": "正面"}),
        ("garments", "SPR-0001", "img/SPR-0001.webp",
         {"description": "blue swimsuit jacket sneakers mesh bag",
          "category": "春装", "category_cn": "春装"}),
    ):
        folder = libraries / kind
        (folder / "img").mkdir(parents=True, exist_ok=True)
        (folder / relative).write_bytes(png_bytes)
        payload = {"version": 1, "count": 1,
                   "items": [dict({"id": item_id, "image": relative, "thumb": relative}, **extra)]}
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
    assert refs_after(job.command) == ["ref0-person.png", "ref1-pose.png"]


def test_garment_then_person_order(tmp_path, png_bytes):
    job = run(tmp_path, png_bytes, garment_id="SPR-0001")
    assert refs_after(job.command) == ["ref0-person.png", "ref1-garment.png"]


def test_pose_and_garment_and_person(tmp_path, png_bytes):
    """姿势+服装同时选中 = 两段式：第一段人物+姿势，第二段接第一段产物换装。"""
    job = run(tmp_path, png_bytes, pose_id="P001_front", garment_id="SPR-0001")
    assert len(job.stages) == 2
    assert refs_after(job.command) == ["ref0-person.png", "ref1-pose.png"]
    assert Path(job.command[job.command.index("--output") + 1]).name == "stage-pose.png"
    assert job.advance_ready(0) and not job.advance_ready(1)
    job.output.write_bytes(png_bytes)   # 伪造姿势段产物（真实运行由 sd-cli 写出）
    job.output.write_bytes(png_bytes)
    job.advance()
    assert refs_after(job.command) == ["s2-ref0-person.png", "s2-ref1-garment.png"]
    assert Path(job.command[job.command.index("--output") + 1]).name == "image.png"
    # 第二段的人物图就是第一段产物
    person = job.command[job.command.index("--ref-image") + 1]
    assert Path(person).name == "s2-ref0-person.png"
    assert (job.folder / "s2-ref0-person.png").is_file()
    # 分段日志：姿势段 -s1，换装段用正名（桌面「引擎日志」按钮直开最终段）
    assert job.stages[0]["log_name"].endswith("-s1.log")
    assert job.stages[1]["log_name"] == f"sdcpp-{job.job_id}.log"


def test_two_stage_steps_are_independent(tmp_path, png_bytes):
    job = run(tmp_path, png_bytes, pose_id="P001_front", garment_id="SPR-0001",
              pose_steps=16, garment_steps=6)
    assert job.command[job.command.index("--steps") + 1] == "16"
    # 步数不是 8 → turbo 预设自动弃用（错拍保护）
    assert "--sigmas" not in job.command
    job.output.write_bytes(png_bytes)
    job.advance()
    assert job.command[job.command.index("--steps") + 1] == "6"
    assert "--sigmas" not in job.command


def test_two_stage_default_steps_are_15(tmp_path, png_bytes):
    """默认 15 步（用户实测 8 步出图偏模糊），模板不再带 turbo 预设。"""
    job = run(tmp_path, png_bytes, pose_id="P001_front", garment_id="SPR-0001", steps=None)
    assert job.command[job.command.index("--steps") + 1] == "15"
    assert "--sigmas" not in job.command
    job.output.write_bytes(png_bytes)
    job.advance()
    assert job.command[job.command.index("--steps") + 1] == "15"


def test_explicit_8_steps_keeps_turbo(tmp_path, png_bytes):
    """显式 8 步仍可配 turbo 预设（8 档 sigma 表正好对齐）。"""
    job = run(tmp_path, png_bytes, pose_id="P001_front", garment_id="SPR-0001",
              steps=8, sigmas="qwen-image-2.1-turbo")
    assert job.command[job.command.index("--steps") + 1] == "8"
    assert job.command[job.command.index("--sigmas") + 1] == "qwen-image-2.1-turbo"


def test_two_stage_lora_only_on_pose_stage(tmp_path, png_bytes):
    """姿势 LoRA 只挂姿势段；换装段挂它会把服装往训练集样子上拽。"""
    job = run(tmp_path, png_bytes, pose_id="P001_front", garment_id="SPR-0001",
              lora_model_dir="loras", lora_apply_mode="auto")
    assert "--lora-model-dir" in job.command
    job.output.write_bytes(png_bytes)
    job.advance()
    assert "--lora-model-dir" not in job.command


def test_two_stage_seed_is_shared(tmp_path, png_bytes):
    job = run(tmp_path, png_bytes, pose_id="P001_front", garment_id="SPR-0001", seed=42)
    first = job.command[job.command.index("--seed") + 1]
    job.output.write_bytes(png_bytes)
    job.advance()
    assert job.command[job.command.index("--seed") + 1] == first == "42"


def test_two_stage_progress_carries_stage_label(tmp_path, png_bytes):
    job = run(tmp_path, png_bytes, pose_id="P001_front", garment_id="SPR-0001")
    job.on_line("|===>  | 3/8 - 2.0s/it")
    assert job.note.startswith("姿势段 · ")
    assert job.stage == "sampling"
    # 不匹配进度模式的行不许重复叠加段落前缀（真机第一次两段跑踩到）
    job.on_line("[I] some unparsed sd-cli output")
    assert job.note.count("姿势段") == 1
    job.output.write_bytes(png_bytes)
    job.advance()
    job.on_line("|===>  | 5/8 - 2.0s/it")
    assert job.note.startswith("换装段 · ")


def test_stage_log_name_follows_current_stage(tmp_path, png_bytes):
    """桌面「引擎日志」按钮固定开 sdcpp-<id>.log → 最终段必须用正名。"""
    job = run(tmp_path, png_bytes, pose_id="P001_front", garment_id="SPR-0001")
    assert job.log_name.endswith("-s1.log")
    job.output.write_bytes(png_bytes)
    job.advance()
    assert job.log_name == f"sdcpp-{job.job_id}.log"


def test_single_stage_has_no_stage_label(tmp_path, png_bytes):
    job = run(tmp_path, png_bytes, pose_id="P001_front")
    job.on_line("|===>  | 3/8 - 2.0s/it")
    assert not job.note.startswith("姿势段")


def test_increase_ref_index_only_with_multiple(tmp_path, png_bytes):
    single = run(tmp_path / "a", png_bytes, pose_id="P001_front", want_token=False)
    assert refs_after(single.command) == ["ref0-pose.png"]
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


def test_garment_description_reaches_prompt(tmp_path, png_bytes):
    """图库服装描述必须进 prompt：鞋袜包这类小件不点名，模型就不替换（实测）。

    姿势+服装是两段式：服装描述在换装段的 prompt-s2.txt 里。
    """
    job = run(tmp_path, png_bytes, pose_id="P001_front", garment_id="SPR-0001")
    job.output.write_bytes(png_bytes)
    job.advance()
    text = (job.folder / "prompt-s2.txt").read_text("utf-8")
    assert "blue swimsuit jacket sneakers mesh bag" in text
    assert "Outfit to dress the person in" in text
    assert "their pose is already final" in text
    # 姿势段的提示词里不该出现服装指令
    assert "Outfit to dress" not in (job.folder / "prompt.txt").read_text("utf-8")


def test_pose_title_reaches_prompt(tmp_path, png_bytes):
    """姿势名进 prompt：骨骼图只有线条，语义靠这条文字补。"""
    job = run(tmp_path, png_bytes, pose_id="P001_front")
    text = (job.folder / "prompt.txt").read_text("utf-8")
    assert "Pose to reproduce: S形站姿・胯部侧顶" in text


def test_user_prompt_stays_verbatim_at_the_end(tmp_path, png_bytes):
    """注入只允许追加在前缀里，用户原话必须一字不动留在末尾。"""
    job = run(tmp_path, png_bytes, pose_id="P001_front")
    text = (job.folder / "prompt.txt").read_text("utf-8")
    assert text.endswith("改成站姿")


def test_person_only_submission_stays_clean(tmp_path, png_bytes):
    """不上图库时不得注入姿势/服装句子 —— 保持纯指令编辑语义。"""
    job = run(tmp_path, png_bytes)
    text = (job.folder / "prompt.txt").read_text("utf-8")
    assert "Pose to reproduce" not in text
    assert "Outfit to dress" not in text
    assert "Reference images, in order:" in text     # 角色说明仍在
