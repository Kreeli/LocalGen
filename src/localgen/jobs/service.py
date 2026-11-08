import hashlib
import logging
import shutil

from pydantic import TypeAdapter, ValidationError

from localgen.config import guarded
from localgen.jobs.models import AppError, JobQuery, Key, Submission, validation_error

logger = logging.getLogger(__name__)
key_adapter = TypeAdapter(Key)


class JobService:
    def __init__(self, data, settings, registry, engine, db, images, scheduler):
        self.data = data
        self.settings = settings
        self.registry = registry
        self.engine = engine
        self.db = db
        self.images = images
        self.scheduler = scheduler

    def engine_info(self):
        status = self.engine.status
        return {"state": status.state, "available": status.state == "ready",
                "diagnostic_code": status.code, "device": status.device,
                "mode": self.settings.engine_mode, "port": self.settings.engine_port}

    def workflow_info(self, workflow):
        spec = workflow.spec
        ready = self.engine.status.state == "ready"
        missing = self.engine.missing(workflow)
        reason = ("WORKFLOW_DEPENDENCIES_MISSING" if missing else
                  "WORKFLOW_NOT_VERIFIED" if not spec.enabled else
                  "ENGINE_UNAVAILABLE" if not ready else None)
        return {
            "workflow_id": spec.workflow_id, "revision": spec.revision, "title": spec.title,
            "available": reason is None, "diagnostic_code": reason, "missing_dependencies": missing,
            "verified_sizes": spec.verified_sizes, "default_size": spec.default_size,
            "supported_parameters": sorted(set(spec.inputs) - {"output_prefix"}), "resource_tier": spec.tier,
        }

    def list_workflows(self):
        workflows = sorted(self.registry.current.values(), key=lambda workflow: workflow.spec.runner != "comfy")
        return {"items": [self.workflow_info(workflow) for workflow in workflows]}

    def dsh_plugin_json(self, workflow_id: str, variant: str):
        workflow = self.registry.current.get(workflow_id)
        if workflow is None:
            raise AppError(404, "WORKFLOW_NOT_FOUND", "找不到此工作流")
        return workflow.dsh_plugin_graph(variant)

    def submit(self, request: Submission, key=None):
        if key is not None:
            try:
                key = key_adapter.validate_python(key)
            except ValidationError:
                raise validation_error() from None
        request_hash = hashlib.sha256(request.model_dump_json(exclude_none=True).encode()).hexdigest()

        def prepare():
            if self.scheduler.closing:
                raise AppError(503, "SERVICE_STOPPING", "LocalGen 正在退出", True)
            workflow = self.registry.current.get(request.workflow_id)
            if workflow is None:
                raise AppError(404, "WORKFLOW_NOT_FOUND", "找不到此工作流")
            parameters = workflow.parameters(request)
            if request.image:
                staged = guarded(self.data / "gateway-input" / request.image)
                if not staged.is_file():
                    raise AppError(422, "INPUT_IMAGE_MISSING", "输入图不存在或已过期，请重新上传")
            if parameters["width"] * parameters["height"] > self.settings.max_pixels:
                raise AppError(422, "UNSUPPORTED_SIZE", "图片像素数超过本机配置上限")
            if self.engine.status.state != "ready":
                raise AppError(503, "ENGINE_UNAVAILABLE", "引擎暂不可用", True)
            info = self.workflow_info(workflow)
            if not info["available"]:
                raise AppError(503, info["diagnostic_code"], "工作流缺少依赖或尚未通过实机验证", True)
            if shutil.disk_usage(self.data).free < self.settings.min_free_mib * 2**20:
                raise AppError(503, "DISK_SPACE_LOW", "可用磁盘空间不足", True)
            return workflow.spec, parameters

        job = self.db.enqueue(request_hash, key, self.settings.queue_limit, prepare)
        self.scheduler.notify()
        return job

    def get_job(self, job_id):
        return self.db.job(self.db.row(job_id))

    def list_jobs(self, query: JobQuery):
        return self.db.page(query)

    def cancel(self, job_id):
        job = self.db.cancel(job_id)
        self.scheduler.notify()
        return job

    def delete(self, job_id):
        self.db.delete(job_id)
        self.images.purge(job_id)
        self.engine.cleanup(str(job_id))

    def progress(self):
        row = self.db.active_row()
        return {"job": self.db.job(row) if row is not None else None, "queued": self.db.queued_count()}

    def artifact(self, job_id, artifact_id):
        return self.images.download(self.db, job_id, artifact_id)
