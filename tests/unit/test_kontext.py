import io
from pathlib import Path
from uuid import uuid4

import pytest
from PIL import Image

from localgen.engine.sdcpp import KontextJob
from localgen.jobs.models import AppError, Submission
from localgen.workflows import Registry


def _workflow():
    root = Path(__file__).resolve().parents[2] / "workflows"
    return Registry(root, "comfy").current["kontext-dev"]


def _png_bytes(color=(24, 48, 96), size=(64, 64)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "PNG")
    return buffer.getvalue()


def test_kontext_job_resizes_reference_to_output_size(tmp_path):
    workflow = _workflow()
    token = f"{uuid4().hex}.png"
    request = Submission(workflow_id="kontext-dev", prompt="改成黄昏色调", seed=11, image=token)
    job_id = str(uuid4())
    parameters = workflow.render(workflow.parameters(request), job_id)["1"]["inputs"]
    input_root = tmp_path / "input"
    input_root.mkdir()
    (input_root / token).write_bytes(_png_bytes(size=(3072, 4096)))
    job = KontextJob(job_id, tmp_path, tmp_path / "runtime", parameters, 0, input_root)
    assert job.command[job.command.index("--ref-image") + 1] == str(job.folder / "input.png")
    assert job.command[job.command.index("--t5xxl") + 1].endswith("t5-v1_1-xxl-encoder-Q4_K_S.gguf")
    assert job.command[job.command.index("--guidance") + 1] == "2.5"
    assert job.command[job.command.index("--cfg-scale") + 1] == "1"
    assert "--init-img" not in job.command
    with Image.open(job.folder / "input.png") as staged:
        assert staged.size == (1024, 1024)


def test_kontext_parameters_require_reference_image():
    """manifest 的 required_inputs 必须在提交阶段就拦住，不能等引擎跑空一次。"""
    workflow = _workflow()
    with pytest.raises(AppError) as exc:
        workflow.parameters(Submission(workflow_id="kontext-dev", prompt="x", seed=1))
    assert exc.value.detail.code == "REQUIRED_INPUT_MISSING"


def test_kontext_job_requires_reference_image(tmp_path):
    workflow = _workflow()
    token = f"{uuid4().hex}.png"
    parameters = workflow.render(workflow.parameters(
        Submission(workflow_id="kontext-dev", prompt="x", seed=1, image=token)),
        str(uuid4()))["1"]["inputs"]
    parameters["image"] = ""  # 引擎侧兜底：令牌被清空也不能静默退化成文生图
    with pytest.raises(AppError) as missing_token:
        KontextJob(str(uuid4()), tmp_path, tmp_path / "runtime", parameters, 0, tmp_path / "input")
    assert missing_token.value.detail.code == "INVALID_INPUT_IMAGE"
    token = f"{uuid4().hex}.jpg"
    parameters["image"] = token
    (tmp_path / "input").mkdir()
    (tmp_path / "input" / token).write_bytes(b"not an image")
    with pytest.raises(AppError) as broken:
        KontextJob(str(uuid4()), tmp_path, tmp_path / "runtime", parameters, 0, tmp_path / "input")
    assert broken.value.detail.code == "INVALID_INPUT_IMAGE"
    (tmp_path / "input" / token).write_bytes(_png_bytes())
    with pytest.raises(AppError) as missing_file:
        KontextJob(str(uuid4()), tmp_path, tmp_path / "runtime", parameters, 0, tmp_path / "empty")
    assert missing_file.value.detail.code == "INPUT_IMAGE_MISSING"


def test_kontext_rejects_strength(tmp_path):
    workflow = _workflow()
    with pytest.raises(AppError) as exc:
        workflow.parameters(Submission(workflow_id="kontext-dev", prompt="x", seed=1,
                                       image=f"{uuid4().hex}.png", strength=0.6))
    assert exc.value.detail.code == "UNSUPPORTED_PARAMETER"
