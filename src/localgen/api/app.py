import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse, JSONResponse

from localgen.config import guarded, resources
from localgen.engine import EngineStatus
from localgen.engine.comfy import ComfyEngine, previous_engine_exited
from localgen.engine.mock import MockEngine
from localgen.jobs.edit import EditRequest, choose_engine, to_submission
from localgen.jobs.models import AppError, Job, JobPage, JobQuery, Submission, validation_error
from localgen.jobs.scheduler import Scheduler
from localgen.jobs.service import JobService
from localgen.mcp import build_mcp
from localgen.storage.database import Database
from localgen.storage.images import ImageStore
from localgen.storage.libraries import KINDS, LibraryStore

from localgen.api.gateway import router as gateway_router
from localgen.api.openai_compat import router as openai_router, stage_image
from localgen.api.security import LocalSecurity

logger = logging.getLogger(__name__)


def make_app(data, settings, token, *, engine=None, workflow_root=None):
    from localgen.workflows import Registry

    hosts = {f"{host}:{settings.api_port}" for host in settings.allowed_hosts}
    registry = Registry(workflow_root or resources() / "workflows", settings.engine_mode)

    @asynccontextmanager
    async def lifespan(app):
        db = Database(guarded(data / "jobs.sqlite3"), token)
        backend = engine
        task = None
        scheduler = None
        try:
            images = ImageStore(data, settings)
            images.reconcile(db)
            app.state.images = images
            if backend is None:
                backend = MockEngine() if settings.engine_mode == "mock" else ComfyEngine(data, settings)
            confirmed = await previous_engine_exited(data)
            if confirmed:
                db.recover()
                await backend.start()
            else:
                backend.status = EngineStatus("error", "ENGINE_PREVIOUS_INSTANCE_UNCONFIRMED")
            scheduler = Scheduler(db, images, registry, backend, settings, confirmed)
            app.state.service = JobService(data, settings, registry, backend, db, images, scheduler)
            task = asyncio.create_task(scheduler.run(), name="LocalGen scheduler")
            task.add_done_callback(log_scheduler_failure)
            async with manager.run():
                yield
        finally:
            if task:
                scheduler.closing = True
                scheduler.notify()
                try:
                    await asyncio.wait_for(task, settings.request_timeout + settings.cancel_timeout + 9)
                except Exception:
                    logger.exception("SCHEDULER_STOP_FAILED")
            try:
                if backend is not None:
                    await backend.shutdown()
            finally:
                db.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
    app.state.data = data
    app.state.settings = settings
    app.add_middleware(LocalSecurity, token=token, hosts=hosts, limit=settings.max_request_bytes,
                       upload_limit=settings.max_image_mib * 2**20)

    def service():
        return app.state.service

    def failure(error):
        return JSONResponse(error.body(), status_code=error.status)

    @app.exception_handler(AppError)
    async def app_error(_, exc):
        return failure(exc)

    @app.exception_handler(RequestValidationError)
    async def request_error(_, exc):
        return failure(validation_error())

    @app.exception_handler(HTTPException)
    async def http_error(_, exc):
        return failure(AppError(exc.status_code, "HTTP_ERROR", "请求的本机接口不存在或不支持该方法"))

    @app.exception_handler(Exception)
    async def internal_error(_, exc):
        logger.error("API_INTERNAL_ERROR", exc_info=exc)
        return failure(AppError(500, "INTERNAL_ERROR", "本机服务出错，请查看诊断日志", True))

    router = APIRouter(prefix="/api/v1")

    @app.get("/health")
    @router.get("/health")
    async def health():
        return {"status": "ok"}

    @router.get("/engine")
    async def engine_status():
        return service().engine_info()

    @router.get("/workflows")
    async def workflows():
        return service().list_workflows()

    @router.get("/workflows/{workflow_id}/dsh-plugin")
    async def dsh_plugin(workflow_id: str, variant: Literal["text2image", "image2image"] = "text2image"):
        payload = json.dumps(service().dsh_plugin_json(workflow_id, variant),
                             ensure_ascii=False, indent=2)
        return Response(content=payload, media_type="application/json",
                        headers={"Content-Disposition":
                                 f'attachment; filename="dsh-image-gen-{workflow_id}.json"'})

    @router.post("/jobs", status_code=202, response_model=Job)
    async def submit(request: Submission, response: Response,
                     idempotency_key: Annotated[str | None, Header()] = None):
        job = service().submit(request, idempotency_key)
        response.headers["Location"] = f"/api/v1/jobs/{job.job_id}"
        return job

    @router.post("/edit", status_code=202)
    async def edit(request: EditRequest, response: Response,
                   idempotency_key: Annotated[str | None, Header()] = None):
        """语义化编辑入口：只给意图，模型分派由后端负责（见 jobs/edit.py）。"""
        try:
            workflow_id, reason = choose_engine(request)
        except AppError as exc:
            return failure(exc)
        try:
            submission = to_submission(request, service().registry)
        except AppError as exc:
            return failure(exc)
        logger.info("EDIT_ROUTED workflow=%s engine=%s reason=%s",
                    workflow_id, request.engine, reason)
        try:
            job = service().submit(submission, idempotency_key)
        except AppError as exc:
            # 路由成功但引擎没准备好时，把路由结果一并带回：
            # 调用方能看出"选对了模型，只是引擎不可用"，而不是以为参数被拒。
            detail = exc.detail.model_dump()
            detail["routed_to"] = workflow_id
            detail["route_reason"] = reason
            return JSONResponse({"error": detail}, status_code=exc.status)
        response.headers["Location"] = f"/api/v1/jobs/{job.job_id}"
        body = job.model_dump()
        body["routed_to"] = workflow_id
        body["route_reason"] = reason
        return body

    @router.get("/jobs", response_model=JobPage)
    async def jobs(query: Annotated[JobQuery, Query()]):
        return service().list_jobs(query)

    @router.get("/progress")
    async def progress():
        return service().progress()

    @router.get("/status")
    async def status():
        return service().status()

    @router.delete("/jobs/{job_id}", status_code=204)
    async def delete_job(job_id: UUID):
        service().delete(job_id)

    @router.post("/images")
    async def upload_reference(request: Request):
        from fastapi import Form

        try:
            form = await request.form()
            upload = form.get("image")
            if upload is None or not hasattr(upload, "read"):
                raise AppError(400, "VALIDATION_ERROR", "缺少 image 文件字段")
            content = await upload.read()
            token = stage_image(content, upload.filename,
                                request.app.state.settings, request.app.state.data)
            return {"name": token}
        except AppError as exc:
            return failure(exc)

    @router.get("/jobs/{job_id}", response_model=Job)
    async def get_job(job_id: UUID):
        return service().get_job(job_id)

    @router.post("/jobs/{job_id}/cancel", response_model=Job)
    async def cancel(job_id: UUID, response: Response):
        job = service().cancel(job_id)
        response.status_code = 202 if job.status == "cancelling" else 200
        return job

    @router.get("/jobs/{job_id}/artifacts/{artifact_id}")
    async def image(job_id: UUID, artifact_id: UUID):
        file = service().artifact(job_id, artifact_id)
        return FileResponse(file, media_type="image/png", filename=f"LocalGen-{job_id}.png",
                            content_disposition_type="inline", headers={"Cache-Control": "no-store",
                                                                        "X-Content-Type-Options": "nosniff"})

    # ---------- 姿势 / 服装图库 ----------
    # 图库是只读素材，选中后作为参考图参与生成；这里只提供查询与出图。

    def libraries():
        store = getattr(app.state, "libraries", None)
        if store is None:
            store = LibraryStore(app.state.data)
            app.state.libraries = store
        return store

    @router.get("/libraries")
    async def library_list():
        store = libraries()
        return {kind: {"installed": store.installed(kind),
                       "count": store.index(kind)["count"] if store.installed(kind) else 0}
                for kind in KINDS}

    @router.get("/libraries/{kind}")
    async def library_catalog(kind: str, query: str = "", category: str = "",
                              collection: str = "", limit: int = 60, offset: int = 0):
        try:
            return libraries().catalog(kind, query=query, category=category,
                                       collection=collection,
                                       limit=max(1, min(limit, 200)), offset=max(0, offset))
        except AppError as exc:
            return failure(exc)

    @router.get("/libraries/{kind}/{item_id}")
    async def library_entry(kind: str, item_id: str):
        try:
            return libraries().entry(kind, item_id)
        except AppError as exc:
            return failure(exc)

    def asset_headers():
        # 图库是静态素材，可以放心缓存；still no sniff。
        return {"Cache-Control": "private, max-age=86400",
                "X-Content-Type-Options": "nosniff"}

    @router.get("/libraries/{kind}/{item_id}/image")
    async def library_image(kind: str, item_id: str):
        try:
            path, media = libraries().image(kind, item_id)
            return FileResponse(path, media_type=media, headers=asset_headers())
        except AppError as exc:
            return failure(exc)

    @router.get("/libraries/{kind}/{item_id}/thumb")
    async def library_thumb(kind: str, item_id: str):
        try:
            path, media = libraries().thumb(kind, item_id)
            return FileResponse(path, media_type=media, headers=asset_headers())
        except AppError as exc:
            return failure(exc)

    app.include_router(router)
    app.include_router(gateway_router)
    app.include_router(openai_router)
    manager, route = build_mcp(service, hosts)
    app.router.routes.append(route)
    return app


def log_scheduler_failure(task):
    if not task.cancelled() and task.exception():
        logger.error("SCHEDULER_EXITED", exc_info=task.exception())
