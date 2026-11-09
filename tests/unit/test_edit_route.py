"""统一编辑入口的路由规则与翻译。

分派依据全部来自真机实测（见 docs/姿势服装图库接入方案.md 6.5.4）：
Kontext 单参考图，实测换不动姿势，所以带 pose/garment 时不允许路由到它。
"""
from pathlib import Path

import pytest

from localgen.jobs.edit import EditRequest, choose_engine, to_submission
from localgen.jobs.models import AppError
from localgen.workflows import Registry

TOKEN = "a" * 32 + ".png"


@pytest.fixture(scope="module")
def registry():
    root = Path(__file__).resolve().parent.parent.parent / "workflows"
    return Registry(root, "comfy")


def test_pose_routes_to_pose_swap(registry):
    request = EditRequest(prompt="改成站姿", image=TOKEN, pose="P001")
    workflow_id, reason = choose_engine(request)
    assert workflow_id == "pose-swap"
    assert "参考" in reason


def test_garment_routes_to_pose_swap(registry):
    assert choose_engine(EditRequest(prompt="换装", garment="SPR-0001"))[0] == "pose-swap"


def test_plain_edit_routes_to_qwen(registry):
    assert choose_engine(EditRequest(prompt="把头发变红", image=TOKEN))[0] == "qwen-image-21"


def test_explicit_engine_override(registry):
    assert choose_engine(EditRequest(prompt="改", image=TOKEN,
                                     engine="kontext"))[0] == "kontext-dev"
    assert choose_engine(EditRequest(prompt="改", image=TOKEN, engine="moss"))[0] == "moss-image"


def test_kontext_refuses_reference_based_edit(registry):
    """实测结论：Kontext 带姿势参考只会浪费时间，直接拒绝。"""
    with pytest.raises(AppError) as caught:
        choose_engine(EditRequest(prompt="改姿势", image=TOKEN, pose="P001",
                                  engine="kontext"))
    assert caught.value.detail.code == "ENGINE_CANNOT_FOLLOW_REFERENCE"


def test_no_input_rejected(registry):
    with pytest.raises(AppError) as caught:
        choose_engine(EditRequest(prompt="改点什么"))
    assert caught.value.detail.code == "INVALID_EDIT_REQUEST"


def test_pose_only_without_person_image_allowed(registry):
    """只有姿势也能提交：reference 条件由「上传或图库」之一满足。"""
    submission = to_submission(EditRequest(prompt="换成这个姿势", pose="P001"), registry)
    assert submission.workflow_id == "pose-swap"
    assert submission.pose_id == "P001"
    assert submission.image is None


def test_size_defaults_to_workflow_default(registry):
    submission = to_submission(EditRequest(prompt="改姿势", pose="P001"), registry)
    assert (submission.width, submission.height) == (768, 768)


def test_size_rejected_when_not_verified(registry):
    with pytest.raises(AppError) as caught:
        to_submission(EditRequest(prompt="改姿势", pose="P001", size=[1024, 1024]), registry)
    assert caught.value.detail.code == "UNSUPPORTED_SIZE"
    assert "768" in caught.value.detail.message


def test_size_passthrough_when_verified(registry):
    submission = to_submission(EditRequest(prompt="把头发变红", image=TOKEN,
                                           size=[1024, 1024]), registry)
    assert (submission.width, submission.height) == (1024, 1024)
    assert submission.workflow_id == "qwen-image-21"


def test_seed_and_negative_pass_through(registry):
    submission = to_submission(EditRequest(prompt="改", pose="P001", seed=7,
                                           negative_prompt="blurry"), registry)
    assert submission.seed == 7
    assert submission.negative_prompt == "blurry"


def test_translated_submission_is_accepted_by_workflow(registry):
    """翻译出来的 Submission 必须能过目标工作流自己的校验。"""
    submission = to_submission(EditRequest(prompt="改成这个姿势", image=TOKEN,
                                           pose="P001", garment="SPR-0001"), registry)
    workflow = registry.current[submission.workflow_id]
    values = workflow.parameters(submission)
    assert values["pose_id"] == "P001"
    assert values["garment_id"] == "SPR-0001"
    assert values["image"] == TOKEN
    assert values["steps"] == 8
    assert values["sigmas"] == "qwen-image-2.1-turbo"


def test_extra_fields_forbidden():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        EditRequest(prompt="改", image=TOKEN, steps=50)
