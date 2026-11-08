from pathlib import Path
from uuid import uuid4

import pytest

from localgen.engine.moss import MossJob
from localgen.jobs.models import AppError, Submission
from localgen.workflows import Registry


def test_moss_job_uses_private_utf8_prompt_and_validates_output(tmp_path):
    root = Path(__file__).resolve().parents[2] / "workflows"
    workflow = Registry(root, "comfy").current["moss-image"]
    request = Submission(workflow_id="moss-image", prompt="深夜咖啡", seed=42)
    job_id = str(uuid4())
    parameters = workflow.render(workflow.parameters(request), job_id)["1"]["inputs"]
    job = MossJob(job_id, tmp_path, tmp_path / "runtime", parameters, 0, tmp_path / "input")
    assert "深夜咖啡" not in job.command
    assert (job.folder / "prompt.txt").read_text("utf-8") == "深夜咖啡"
    assert job.command[job.command.index("--steps") + 1] == "50"
    assert job.command[job.command.index("--backend") + 1] == "diffusion=cuda0,te=cpu,vae=cuda0"
    assert "--init-img" not in job.command

    job.on_line("|============> | 12/50 - 2.0s/it")
    assert job.progress == (12, 50)
    job.on_line("|============> | 12/426 - 1.4GB/s")
    assert job.progress == (12, 50)

    job.output.write_bytes(b"1234")
    assert job.read_result(0, 4) == b"1234"
    with pytest.raises(AppError) as exc:
        job.read_result(0, 3)
    assert exc.value.detail.code == "INVALID_IMAGE"


def test_moss_job_stages_private_init_image_for_img2img(tmp_path):
    root = Path(__file__).resolve().parents[2] / "workflows"
    workflow = Registry(root, "comfy").current["moss-image"]
    token = f"{uuid4().hex}.png"
    request = Submission(workflow_id="moss-image", prompt="保持构图改成夜景", seed=7, image=token)
    job_id = str(uuid4())
    parameters = workflow.render(workflow.parameters(request), job_id)["1"]["inputs"]
    assert parameters["image"] == token
    input_root = tmp_path / "input"
    input_root.mkdir()
    (input_root / token).write_bytes(b"staged-png")
    job = MossJob(job_id, tmp_path, tmp_path / "runtime", parameters, 0, input_root)
    assert job.command[job.command.index("--init-img") + 1] == str(job.folder / "input.png")
    assert job.command[job.command.index("--strength") + 1] == "0.5"
    assert (job.folder / "input.png").read_bytes() == b"staged-png"
    with pytest.raises(AppError) as exc:
        MossJob(str(uuid4()), tmp_path, tmp_path / "runtime", parameters, 0, tmp_path / "empty")
    assert exc.value.detail.code == "INPUT_IMAGE_MISSING"
