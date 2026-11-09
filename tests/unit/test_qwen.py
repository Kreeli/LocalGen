"""Qwen-Image-2.1 job wiring: sd-cli flags, reference staging and the job registry."""
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pytest
from PIL import Image, ImageDraw

from localgen.engine.comfy import SDCPP_JOBS
from localgen.engine.sdcpp import QwenImageJob
from localgen.jobs.models import AppError, Submission
from localgen.workflows import Registry


def _stripes(size=(600, 300)):
    """左红 / 中绿 / 右蓝的三段竖条：用来验证"裁切而非拉伸"。"""
    image = Image.new("RGB", size, (200, 20, 20))
    draw = ImageDraw.Draw(image)
    third = size[0] // 3
    draw.rectangle((third, 0, 2 * third - 1, size[1] - 1), fill=(20, 200, 20))
    draw.rectangle((2 * third, 0, size[0] - 1, size[1] - 1), fill=(20, 20, 200))
    buffer = BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def _workflow():
    root = Path(__file__).resolve().parents[2] / "workflows"
    return Registry(root, "comfy").current["qwen-image-21"]


def _job(workflow, request, tmp_path):
    job_id = str(uuid4())
    parameters = workflow.render(workflow.parameters(request), job_id)["1"]["inputs"]
    return QwenImageJob(job_id, tmp_path, tmp_path / "runtime", parameters, 0, tmp_path / "input")


def test_qwen_job_is_registered_for_the_sdcpp_dispatch():
    assert SDCPP_JOBS["LocalGenQwenImage21"] is QwenImageJob
    assert SDCPP_JOBS["LocalGenMossImage"] is not QwenImageJob


def test_qwen_job_uses_qwen3vl_encoder_and_private_prompt(tmp_path):
    workflow = _workflow()
    request = Submission(workflow_id="qwen-image-21", prompt="深夜咖啡", seed=42)
    job = _job(workflow, request, tmp_path)
    # 提示词只落任务私有文件，不进命令行
    assert "深夜咖啡" not in job.command
    assert (job.folder / "prompt.txt").read_text("utf-8") == "深夜咖啡"
    assert job.command[job.command.index("--llm") + 1].endswith("Qwen3VL-8B-Instruct-Q4_K_M.gguf")
    assert job.command[job.command.index("--vae") + 1].endswith("qwen_image_2.1_vae_bf16.safetensors")
    assert job.command[job.command.index("--steps") + 1] == "30"
    assert job.command[job.command.index("--cfg-scale") + 1] == "6"
    assert job.command[job.command.index("--backend") + 1] == "diffusion=cuda0,te=cpu,vae=cuda0"
    # 纯文生图不带参考图 → 不需要视觉投影器
    assert "--ref-image" not in job.command
    assert "--llm_vision" not in job.command
    assert "--sigmas" not in job.command

    job.on_line("|============> | 12/30 - 2.0s/it")
    assert job.progress == (12, 30)
    job.on_line("|============> | 12/426 - 1.4GB/s")
    assert job.progress == (12, 30)

    job.output.write_bytes(b"1234")
    assert job.read_result(0, 4) == b"1234"
    with pytest.raises(AppError) as exc:
        job.read_result(0, 3)
    assert exc.value.detail.code == "INVALID_IMAGE"


def test_qwen_job_stages_reference_and_vision_weights(tmp_path):
    workflow = _workflow()
    token = f"{uuid4().hex}.png"
    request = Submission(workflow_id="qwen-image-21", prompt="把背景换成海边日落", seed=7, image=token)
    input_root = tmp_path / "input"
    input_root.mkdir()
    (input_root / token).write_bytes(_stripes())
    job_id = str(uuid4())
    parameters = workflow.render(workflow.parameters(request), job_id)["1"]["inputs"]
    job = QwenImageJob(job_id, tmp_path, tmp_path / "runtime", parameters, 0, input_root)
    assert job.command[job.command.index("--ref-image") + 1] == str(job.folder / "input.png")
    assert job.command[job.command.index("--llm_vision") + 1].endswith("mmproj-Qwen3VL-8B-Instruct-F16.gguf")
    # 参考图按输出比例裁切（600×300 → 1024²），不是拉伸
    with Image.open(job.folder / "input.png") as staged:
        assert staged.size == (1024, 1024)
        assert staged.getpixel((10, 512))[0] > 120          # 左缘仍是红
        assert staged.getpixel((342, 512))[1] > 120         # 原 x=342 处是绿（拉伸会变红）
    with pytest.raises(AppError) as exc:
        QwenImageJob(str(uuid4()), tmp_path, tmp_path / "runtime", parameters, 0, tmp_path / "empty")
    assert exc.value.detail.code == "INPUT_IMAGE_MISSING"


def test_qwen_strength_is_not_a_supported_parameter():
    """编辑是提示词驱动的（无 --strength），传 strength 必须被拒绝。"""
    workflow = _workflow()
    with pytest.raises(AppError) as exc:
        workflow.parameters(Submission(workflow_id="qwen-image-21", prompt="x", strength=0.6))
    assert exc.value.detail.code == "UNSUPPORTED_PARAMETER"


def test_qwen_template_rejects_unknown_model_files(tmp_path):
    workflow = _workflow()
    parameters = workflow.render(workflow.parameters(
        Submission(workflow_id="qwen-image-21", prompt="x", seed=1)), str(uuid4()))["1"]["inputs"]
    parameters["diffusion_model"] = "something-else.gguf"
    with pytest.raises(AppError) as exc:
        QwenImageJob(str(uuid4()), tmp_path, tmp_path / "runtime", parameters, 0, tmp_path / "input")
    assert exc.value.detail.code == "WORKFLOW_DEPENDENCIES_MISSING"
