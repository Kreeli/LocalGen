"""pose-swap 的提交校验：至少一张参考图、尺寸白名单、未知参数拒绝。"""
import pytest

from localgen.jobs.models import Submission
from localgen.workflows import Registry


@pytest.fixture(scope="module")
def registry():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent.parent / "workflows"
    return Registry(root, "comfy")


@pytest.fixture(scope="module")
def workflow(registry):
    item = registry.current.get("pose-swap")
    assert item is not None, "pose-swap 工作流未注册"
    return item


def test_pose_swap_declares_library_inputs(workflow):
    assert "pose_id" in workflow.spec.inputs
    assert "garment_id" in workflow.spec.inputs


def test_default_size_is_768(workflow):
    assert workflow.spec.default_size == (768, 768)


def test_requires_at_least_one_reference(workflow):
    from localgen.jobs.models import AppError

    with pytest.raises(AppError) as caught:
        workflow.parameters(Submission(workflow_id="pose-swap", prompt="改姿势"))
    assert caught.value.detail.code == "REQUIRED_INPUT_MISSING"


def test_pose_only_is_enough(workflow):
    values = workflow.parameters(Submission(
        workflow_id="pose-swap", prompt="改姿势", pose_id="full_sexy_P001_front"))
    assert values["pose_id"] == "full_sexy_P001_front"
    assert values["image"] == ""
    assert values["width"] == 768 and values["height"] == 768


def test_garment_only_is_enough(workflow):
    values = workflow.parameters(Submission(
        workflow_id="pose-swap", prompt="换装", garment_id="SPR-0001"))
    assert values["garment_id"] == "SPR-0001"


def test_image_plus_pose(workflow):
    values = workflow.parameters(Submission(
        workflow_id="pose-swap", prompt="改姿势", image="a" * 32 + ".png",
        pose_id="full_sexy_P001_front", garment_id="SPR-0001"))
    assert values["image"] == "a" * 32 + ".png"
    assert values["pose_id"] and values["garment_id"]


def test_size_whitelist(workflow):
    from localgen.jobs.models import AppError

    with pytest.raises(AppError) as caught:
        workflow.parameters(Submission(workflow_id="pose-swap", prompt="改姿势",
                                       pose_id="P", width=1024, height=1024))
    assert caught.value.detail.code == "UNSUPPORTED_SIZE"


def test_library_rejected_by_workflow_without_it(registry):
    from localgen.jobs.models import AppError

    kontext = registry.current["kontext-dev"]
    with pytest.raises(AppError) as caught:
        kontext.parameters(Submission(workflow_id="kontext-dev", prompt="改",
                                      image="a" * 32 + ".png", pose_id="P"))
    assert caught.value.detail.code == "UNSUPPORTED_PARAMETER"


def test_template_steps_default(registry):
    """默认 15 步（用户实测 8 步出图偏模糊）；turbo 预设已随默认移除
    （turbo 是固定 8 档表，15 步会错拍，引擎侧 turbosecurity 同样会弃用）。"""
    graph = registry.current["pose-swap"].graph
    assert graph["1"]["inputs"]["steps"] == 15
    assert graph["1"]["inputs"]["pose_steps"] == 15
    assert graph["1"]["inputs"]["garment_steps"] == 15
    assert "sigmas" not in graph["1"]["inputs"]


def test_engine_fields_reach_the_node(registry):
    """分段步数走 [inputs] 映射落到节点；sigmas 已随默认 15 步移除。"""
    workflow = registry.current["pose-swap"]
    values = workflow.parameters(Submission(
        workflow_id="pose-swap", prompt="改姿势", pose_id="P001"))
    assert values["steps"] == 15
    assert values["pose_steps"] == 15
    assert values["garment_steps"] == 15
    assert "sigmas" not in values
    graph = workflow.render(values, "11111111-1111-1111-1111-111111111111")
    assert graph["1"]["inputs"]["steps"] == 15
    assert graph["1"]["inputs"]["pose_steps"] == 15
    assert "sigmas" not in graph["1"]["inputs"]

    values8 = workflow.parameters(Submission(
        workflow_id="pose-swap", prompt="改姿势", pose_id="P001",
        steps=8, pose_steps=8, garment_steps=8))
    graph8 = workflow.render(values8, "11111111-1111-1111-1111-111111111111")
    assert graph8["1"]["inputs"]["steps"] == 8
    assert graph8["1"]["inputs"]["pose_steps"] == 8
