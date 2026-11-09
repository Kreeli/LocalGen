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


def test_template_steps_stay_low(registry):
    """用户明确要求步数不要改多：默认 8 步 + turbo 预设，且绝不超过 20。"""
    graph = registry.current["pose-swap"].graph
    assert graph["1"]["inputs"]["steps"] == 8
    assert graph["1"]["inputs"]["steps"] <= 20
    assert graph["1"]["inputs"]["sigmas"] == "qwen-image-2.1-turbo"


def test_engine_fields_reach_the_node(registry):
    """sigmas 不在 [inputs] 映射里，也必须通过 render() 落到节点上。"""
    workflow = registry.current["pose-swap"]
    values = workflow.parameters(Submission(
        workflow_id="pose-swap", prompt="改姿势", pose_id="P001"))
    assert values["sigmas"] == "qwen-image-2.1-turbo"
    assert values["steps"] == 8
    graph = workflow.render(values, "11111111-1111-1111-1111-111111111111")
    assert graph["1"]["inputs"]["sigmas"] == "qwen-image-2.1-turbo"
    assert graph["1"]["inputs"]["steps"] == 8
