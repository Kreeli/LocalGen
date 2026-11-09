"""统一编辑入口：只收语义参数，模型差异由这里翻译。

存在的理由
----------
`/api/v1/jobs` 要求调用方知道 `workflow_id` 以及那个工作流的参数怪癖
（qwen 要 pose_id、kontext 要 image、moss 走 strength）。外部程序（MCP、
插件、脚本）不该被这些细节绑住 —— 换模型就得改调用方。

`/api/v1/edit` 收的是意图：

    {"image": "<token>", "prompt": "把姿势改成站立", "pose": "full_sexy_P001_front"}

模型分派规则（实测得出，不是猜的）
----------------------------------
- 要换姿势 / 换服装（给了 pose / garment）→ **只能 Qwen**。
  FLUX Kontext 是单参考图指令编辑模型，实测 377 s 后姿势纹丝不动，只把
  一只手抬到头顶 —— 它根本没有"目标姿势长什么样"的输入通道。
- 纯指令编辑（只有 image + prompt）→ Qwen 或 Kontext 都可以：
  Kontext 局部精修更稳，Qwen 上下文更强、且 8 步就能出图（253 s vs 377 s），
  所以默认仍走 Qwen，除非调用方点名 kontext。
- 只要图生图、不要 fig 参考的强指令 → moss 兜底（用户评价"勉强可用"）。

尺寸：各工作流 verified_sizes 不同（pose-swap 只有 768²，kontext 只有 1024²）。
给了 size 就用，没给就用该工作流的 default_size —— 绝不把 768 塞进只认 1024 的工作流。
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from localgen.jobs.models import (
    AppError, ImageToken, LibraryItemId, Negative, Prompt, Seed, Size, Submission,
)

Engine = Literal["auto", "qwen", "kontext", "moss"]


class EditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    prompt: Prompt
    image: ImageToken | None = None
    pose: LibraryItemId | None = None
    garment: LibraryItemId | None = None
    engine: Engine = "auto"
    size: Annotated[list[Size], Field(min_length=2, max_length=2)] | None = None
    seed: Seed | None = None
    negative_prompt: Negative | None = None
    # 扩散步数 / CFG 覆盖：None = 工作流模板默认。上限见 jobs/models.py 注释。
    steps: Annotated[int, Field(strict=True, ge=1, le=25)] | None = None
    cfg_scale: Annotated[float, Field(strict=True, ge=1, le=15)] | None = None


def choose_engine(request: EditRequest) -> tuple[str, str]:
    """返回 (workflow_id, 理由)。理由进日志，便于回答"为什么走了这个模型"。"""
    wants_reference = bool(request.pose or request.garment)
    if request.engine in ("qwen", "kontext", "moss"):
        if request.engine == "kontext" and wants_reference:
            raise AppError(422, "ENGINE_CANNOT_FOLLOW_REFERENCE",
                           "Kontext 只接受单张参考图，无法按姿势/服装参考改图，请用 qwen")
        return {"qwen": "qwen-image-21", "kontext": "kontext-dev",
                "moss": "moss-image"}[request.engine], "调用方指定"
    if wants_reference:
        return "pose-swap", "需要按图库参考改姿势/换装"
    if request.image:
        return "qwen-image-21", "单图指令编辑，Qwen 8 步最快"
    raise AppError(422, "INVALID_EDIT_REQUEST",
                   "编辑任务需要 image、pose 或 garment 中的至少一项")


def to_submission(request: EditRequest, registry) -> Submission:
    """把意图翻译成具体工作流的 Submission，并挑一个该工作流支持的尺寸。"""
    workflow_id, _reason = choose_engine(request)
    workflow = registry.current.get(workflow_id)
    if workflow is None:
        raise AppError(503, "WORKFLOW_NOT_FOUND", "本机没有可用的编辑工作流", True)

    allowed = [tuple(size) for size in workflow.spec.verified_sizes]
    default = tuple(workflow.spec.default_size)
    if request.size is not None:
        wanted = (int(request.size[0]), int(request.size[1]))
        if wanted not in allowed:
            options = "、".join(f"{w}×{h}" for w, h in allowed)
            raise AppError(422, "UNSUPPORTED_SIZE",
                           f"{workflow_id} 支持的尺寸是 {options}")
        width, height = wanted
    else:
        width, height = default

    return Submission(
        workflow_id=workflow_id,
        prompt=request.prompt,
        width=width,
        height=height,
        seed=request.seed,
        negative_prompt=request.negative_prompt,
        image=request.image,
        pose_id=request.pose,
        garment_id=request.garment,
        steps=request.steps,
        cfg_scale=request.cfg_scale,
    )
