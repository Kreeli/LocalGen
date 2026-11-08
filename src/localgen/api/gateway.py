"""Minimal ComfyUI-protocol gateway over the LocalGen job pipeline.

Lets tools that only speak the ComfyUI HTTP API (such as dsh-image-gen) drive
LocalGen workflows — including runner=moss ones — through the normal queue.
Mounted at /comfyui/* on the LocalGen port; exempt from the Bearer token
(loopback-only, Host/Origin checks still apply) because ComfyUI clients carry
no credentials.
"""
import io
import re
import uuid as uuidlib
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from PIL import Image

from localgen.config import atomic_write, guarded
from localgen.jobs.models import AppError, Submission, validation_error

router = APIRouter(prefix="/comfyui")
MARKER_PREFIX = "dsh-image-gen:"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
IMAGE_SUFFIXES = (".png", ".jpg", ".webp")


def _service(request: Request):
    return request.app.state.service


def _find_marker(graph: dict):
    for node in graph.values():
        if not isinstance(node, dict):
            continue
        meta = node.get("_meta")
        title = meta.get("title") if isinstance(meta, dict) else None
        if isinstance(title, str) and title.startswith(MARKER_PREFIX):
            parts = title[len(MARKER_PREFIX):].split(":")
            if parts and parts[0]:
                return parts[0], (parts[1] if len(parts) > 1 else "text2image")
    return None, None


def _node_field(graph, spec_inputs, name):
    node, field = spec_inputs[name]
    body = graph.get(node)
    if not isinstance(body, dict):
        raise AppError(400, "GATEWAY_REJECTED", f"工作流图缺少节点 {node}")
    inputs = body.get("inputs")
    if not isinstance(inputs, dict) or field not in inputs:
        raise AppError(400, "GATEWAY_REJECTED", f"工作流图缺少字段 {node}.{field}")
    return inputs[field]


def _extract_parameters(workflow, graph):
    spec = workflow.spec
    prompt = _node_field(graph, spec.inputs, "prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise AppError(400, "GATEWAY_REJECTED", "提示词为空或无效")
    seed = _node_field(graph, spec.inputs, "seed")
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 4294967295:
        raise AppError(400, "GATEWAY_REJECTED", "种子必须是 0–4294967295 的整数")
    width = _node_field(graph, spec.inputs, "width")
    height = _node_field(graph, spec.inputs, "height")
    for value in (width, height):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 8192:
            raise AppError(400, "GATEWAY_REJECTED", "尺寸必须是 1–8192 的整数")
    negative = None
    if "negative_prompt" in spec.inputs:
        value = _node_field(graph, spec.inputs, "negative_prompt")
        negative = value if isinstance(value, str) else None
    return prompt.strip(), seed, width, height, negative


@router.post("/prompt")
async def prompt(request: Request):
    service = _service(request)
    try:
        body = await request.json()
    except Exception:
        raise AppError(400, "GATEWAY_REJECTED", "请求体不是有效 JSON") from None
    graph = body.get("prompt") if isinstance(body, dict) else None
    if not isinstance(graph, dict) or not graph:
        raise AppError(400, "GATEWAY_REJECTED", "缺少 prompt 工作流图")
    workflow_id, variant = _find_marker(graph)
    if workflow_id is None:
        raise AppError(400, "GATEWAY_REJECTED",
                       "这不是由 LocalGen 导出的工作流（缺少 dsh-image-gen 标记）；"
                       "请在 LocalGen 中导出后重新导入到 dsh-image-gen")
    workflow = service.registry.current.get(workflow_id)
    if workflow is None:
        raise AppError(400, "GATEWAY_UNKNOWN_WORKFLOW",
                       f"本机没有工作流 {workflow_id}，请在 LocalGen 重新导出")
    if variant == "image2image" and "image" not in workflow.spec.inputs:
        raise AppError(400, "GATEWAY_REJECTED", "此工作流不支持图生图；请使用文生图工作流")
    prompt, seed, width, height, negative = _extract_parameters(workflow, graph)
    image = None
    strength = None
    if variant == "image2image":
        image = _node_field(graph, workflow.spec.inputs, "image")
        if not isinstance(image, str) or not re.match(r"^[0-9a-f]{32}\.(png|jpg|webp)$", image):
            raise AppError(400, "GATEWAY_REJECTED",
                           "输入图引用无效（应为本网关 /upload/image 返回的文件名）")
        if "strength" in workflow.spec.inputs:
            strength = _node_field(graph, workflow.spec.inputs, "strength")
            if isinstance(strength, bool) or not isinstance(strength, (int, float)) or not 0 < strength <= 1:
                raise AppError(400, "GATEWAY_REJECTED", "重绘强度必须是 0–1 之间的数值")
            strength = float(strength)
    try:
        submission = Submission(workflow_id=workflow_id, prompt=prompt, width=width,
                                height=height, seed=seed, negative_prompt=negative,
                                image=image, strength=strength)
    except Exception:
        raise validation_error() from None
    job = service.submit(submission)
    return {"prompt_id": str(job.job_id)}


@router.get("/history/{prompt_id}")
async def history(prompt_id: str, request: Request):
    service = _service(request)
    if not UUID_RE.match(prompt_id):
        return {}
    try:
        job = service.get_job(prompt_id)
    except AppError:
        return {}
    if job.status == "succeeded" and job.artifacts and job.artifacts[0].available:
        workflow = service.registry.versions.get((job.workflow_id, job.workflow_revision))
        output_node = workflow.spec.output_node if workflow else "output"
        image = {"filename": f"{job.artifacts[0].artifact_id}.png",
                 "subfolder": str(job.job_id), "type": "output"}
        return {prompt_id: {"status": {"status_str": "success", "completed": True, "messages": []},
                            "outputs": {output_node: {"images": [image]}}}}
    if job.status in ("failed", "cancelled", "interrupted"):
        detail = job.error
        message = detail.message if detail is not None else job.status
        code = detail.code if detail is not None else job.status
        return {prompt_id: {"status": {"status_str": "error", "completed": False,
                                       "messages": [["localgen_error", {"code": code, "message": message}]]},
                            "outputs": {}}}
    return {}


@router.get("/view")
async def view(request: Request, filename: str, subfolder: str = "", type: str = "output"):
    if type != "output" or not filename.endswith(".png"):
        raise AppError(404, "ARTIFACT_NOT_FOUND", "图片不存在或类型不受支持")
    artifact_id, job_id = filename[:-4], subfolder
    if not UUID_RE.match(artifact_id) or not UUID_RE.match(job_id):
        raise AppError(404, "ARTIFACT_NOT_FOUND", "图片标识无效")
    try:
        file = _service(request).artifact(uuidlib.UUID(job_id), uuidlib.UUID(artifact_id))
    except AppError:
        raise
    return FileResponse(file, media_type="image/png", headers={"Cache-Control": "no-store",
                                                               "X-Content-Type-Options": "nosniff"})


@router.post("/upload/image")
async def upload_image(request: Request):
    settings = request.app.state.settings
    form = await request.form()
    upload = form.get("image")
    if upload is None or not hasattr(upload, "read"):
        raise AppError(400, "GATEWAY_REJECTED", "缺少 image 文件字段")
    content = await upload.read()
    limit = settings.max_image_mib * 2**20
    if not content or len(content) > limit:
        raise AppError(413, "REQUEST_TOO_LARGE", "源图超过大小上限")
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix not in IMAGE_SUFFIXES:
        raise AppError(400, "GATEWAY_REJECTED", "源图仅支持 PNG、JPEG 或 WebP")
    try:
        with Image.open(io.BytesIO(content)) as image:
            if image.format not in ("PNG", "JPEG", "WEBP"):
                raise ValueError("unsupported format")
            if getattr(image, "is_animated", False):
                raise ValueError("animated image")
            image.verify()
    except Exception:
        raise AppError(400, "GATEWAY_REJECTED", "源图不是完整的 PNG/JPEG/WebP 图片") from None
    name = f"{uuidlib.uuid4().hex}{suffix}"
    folder = guarded(request.app.state.data / "gateway-input")
    folder.mkdir(exist_ok=True)
    atomic_write(folder / name, content)
    return {"name": name, "subfolder": "", "type": "input"}


@router.get("/system_stats")
async def system_stats(request: Request):
    engine = _service(request).engine_info()
    return {"system": {"localgen_gateway": True, "mode": engine.get("mode"),
                       "engine_state": engine.get("state")}, "devices": []}
