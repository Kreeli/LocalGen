"""OpenAI Images-compatible API over the LocalGen job pipeline.

Lets OpenAI-ecosystem tools (including the dsh-image-gen openai-compat
channel) use every LocalGen workflow — moss included — with standard
Bearer auth and model-name routing instead of workflow JSON files.
Requests are synchronous: the route waits for the queued job to finish
and answers with base64 image data.
"""
import asyncio
import base64
import io
import time
import uuid as uuidlib
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from PIL import Image

from localgen.config import atomic_write, guarded
from localgen.jobs.models import TERMINAL, AppError, Submission, validation_error

router = APIRouter(prefix="/v1")
IMAGE_SUFFIXES = (".png", ".jpg", ".webp")


def _oai_error(status: int, message: str, code: str | None = None,
               error_type: str = "invalid_request_error"):
    return JSONResponse({"error": {"message": message, "type": error_type,
                                   "param": None, "code": code}}, status_code=status)


def _service(request: Request):
    return request.app.state.service


def _model_or_error(service, model):
    workflow = service.registry.current.get(model) if isinstance(model, str) else None
    if workflow is None:
        available = ", ".join(sorted(w.spec.workflow_id for w in service.registry.current.values()
                                     if w.spec.enabled))
        raise AppError(400, "MODEL_NOT_FOUND", f"未知模型 {model!r}；可用模型：{available}")
    return workflow


def _parse_size(workflow, size):
    width, height = workflow.spec.default_size
    if size:
        if not isinstance(size, str):
            raise AppError(400, "VALIDATION_ERROR", "size 必须是 '宽x高' 字符串")
        parts = size.lower().split("x")
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            raise AppError(400, "VALIDATION_ERROR", f"size 必须是 '宽x高' 格式，收到 {size!r}")
        width, height = int(parts[0]), int(parts[1])
    return width, height


def _stage_image(content: bytes, filename: str, settings, data: Path):
    limit = settings.max_image_mib * 2**20
    if not content or len(content) > limit:
        raise AppError(400, "VALIDATION_ERROR", f"输入图必须存在且不超过 {settings.max_image_mib} MiB")
    suffix = Path(filename or "").suffix.lower()
    if suffix not in IMAGE_SUFFIXES:
        raise AppError(400, "VALIDATION_ERROR", "输入图仅支持 PNG、JPEG 或 WebP")
    try:
        with Image.open(io.BytesIO(content)) as image:
            if image.format not in ("PNG", "JPEG", "WEBP"):
                raise ValueError("unsupported format")
            if getattr(image, "is_animated", False):
                raise ValueError("animated image")
            image.verify()
    except Exception:
        raise AppError(400, "VALIDATION_ERROR", "输入图不是完整的 PNG/JPEG/WebP 图片") from None
    token = f"{uuidlib.uuid4().hex}{suffix}"
    folder = guarded(data / "gateway-input")
    folder.mkdir(exist_ok=True)
    atomic_write(folder / token, content)
    return token


async def _wait_for_job(service, job_id: str, timeout: int):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = service.get_job(job_id)
        if job.status in TERMINAL:
            return job
        await asyncio.sleep(0.5)
    raise AppError(504, "GATEWAY_TIMEOUT",
                   f"任务 {job_id} 在 {timeout} 秒内未完成；它仍在 LocalGen 队列中，可稍后在历史里取图")


def _finished_b64(service, job) -> dict:
    if job.status != "succeeded" or not job.artifacts or not job.artifacts[0].available:
        detail = job.error.message if job.error is not None else job.status
        raise AppError(500, "GENERATION_FAILED", f"生成未成功（{job.status}）：{detail}")
    file = service.artifact(job.job_id, job.artifacts[0].artifact_id)
    encoded = base64.b64encode(file.read_bytes()).decode("ascii")
    return {"created": int(time.time()), "data": [{"b64_json": encoded}]}


@router.get("/models")
async def models(request: Request):
    service = _service(request)
    return {"object": "list", "data": [
        {"id": workflow.spec.workflow_id, "object": "model", "owned_by": "localgen"}
        for workflow in service.registry.current.values() if workflow.spec.enabled
    ]}


@router.post("/images/generations")
async def generations(request: Request):
    service = _service(request)
    try:
        try:
            body = await request.json()
        except Exception:
            raise AppError(400, "VALIDATION_ERROR", "请求体不是有效 JSON") from None
        if not isinstance(body, dict):
            raise AppError(400, "VALIDATION_ERROR", "请求体必须是 JSON 对象")
        workflow = _model_or_error(service, body.get("model"))
        if body.get("n") not in (None, 1):
            raise AppError(400, "VALIDATION_ERROR", "本机一次只支持 1 张图（n=1）")
        prompt = body.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise AppError(400, "VALIDATION_ERROR", "prompt 缺失或为空")
        negative = body.get("negative_prompt")
        width, height = _parse_size(workflow, body.get("size"))
        submission = Submission(workflow_id=workflow.spec.workflow_id, prompt=prompt,
                                width=width, height=height,
                                seed=body.get("seed") if isinstance(body.get("seed"), int) else None,
                                negative_prompt=negative if isinstance(negative, str) else None)
        job = service.submit(submission)
        finished = await _wait_for_job(service, str(job.job_id), service.settings.job_timeout)
        return _finished_b64(service, finished)
    except AppError as exc:
        return _oai_error(exc.status, exc.detail.message, exc.detail.code)
    except Exception:
        return _oai_error(500, "本机服务内部错误，请查看 LocalGen 诊断日志",
                          "internal_error", "server_error")


@router.post("/images/edits")
async def edits(request: Request):
    service = _service(request)
    settings = request.app.state.settings
    data = request.app.state.data
    try:
        form = await request.form()
        workflow = _model_or_error(service, form.get("model"))
        if "image" not in workflow.spec.inputs:
            raise AppError(400, "MODEL_NOT_SUPPORTED", f"模型 {workflow.spec.workflow_id} 不支持图生图编辑")
        prompt = form.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise AppError(400, "VALIDATION_ERROR", "prompt 缺失或为空")
        upload = form.get("image")
        if upload is None or not hasattr(upload, "read"):
            raise AppError(400, "VALIDATION_ERROR", "缺少 image 文件字段")
        content = await upload.read()
        token = _stage_image(content, upload.filename, settings, data)
        strength = form.get("strength")
        if strength is not None:
            try:
                strength = float(strength)
                if not 0 < strength <= 1:
                    raise ValueError
            except (TypeError, ValueError):
                raise AppError(400, "VALIDATION_ERROR", "strength 必须是 0–1 之间的数值") from None
        else:
            strength = None
        width, height = _parse_size(workflow, form.get("size"))
        submission = Submission(workflow_id=workflow.spec.workflow_id, prompt=prompt,
                                width=width, height=height, image=token, strength=strength)
        job = service.submit(submission)
        finished = await _wait_for_job(service, str(job.job_id), service.settings.job_timeout)
        return _finished_b64(service, finished)
    except AppError as exc:
        return _oai_error(exc.status, exc.detail.message, exc.detail.code)
    except Exception:
        return _oai_error(500, "本机服务内部错误，请查看 LocalGen 诊断日志",
                          "internal_error", "server_error")
