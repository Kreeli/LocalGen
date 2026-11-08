import asyncio
import json
import logging
import time
from contextlib import suppress

import httpx

from localgen.engine import EngineStatus
from localgen.jobs.models import AppError, ErrorDetail, Progress

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, db, images, registry, engine, settings, recovery_confirmed=True):
        self.db = db
        self.images = images
        self.registry = registry
        self.engine = engine
        self.settings = settings
        self.recovery_confirmed = recovery_confirmed
        self.closing = False
        self.wake = asyncio.Event()
        self.active = None

    def notify(self):
        self.wake.set()

    async def pause(self, seconds=0.35):
        with suppress(TimeoutError):
            await asyncio.wait_for(self.wake.wait(), timeout=seconds)
        self.wake.clear()

    async def run(self):
        while not self.closing:
            if self.recovery_confirmed:
                await self.engine.tick()
                if self.engine.status.state == "ready" and not self.closing:
                    row = self.db.claim(self.eligible)
                    if row:
                        self.active = row["job_id"]
                        try:
                            await self.process(row)
                        finally:
                            self.active = None
                        continue
            await self.pause()

    def eligible(self, row):
        workflow = self.registry.versions.get((row["workflow_id"], row["workflow_revision"]))
        return workflow is None or workflow.spec.engine == self.registry.mode

    def stopping(self, job_id):
        return self.closing or self.db.row(job_id)["status"] == "cancelling"

    @staticmethod
    def error(code, message, retryable=True):
        return ErrorDetail(code=code, message=message, retryable=retryable)

    async def stop_task(self, job_id, prompt_id=None, error=None, uncertain=False):
        self.db.request_stop(job_id)
        confirmed = not uncertain and prompt_id is None
        if prompt_id is not None:
            try:
                async with asyncio.timeout(self.settings.cancel_timeout):
                    confirmed = await self.engine.cancel(prompt_id)
            except Exception:
                logger.warning("ENGINE_CANCEL_UNCONFIRMED job=%s", job_id)
        if not confirmed:
            confirmed = await self.engine.recycle()
        if not confirmed:
            self.recovery_confirmed = False
            self.engine.status = EngineStatus("error", "ENGINE_STOP_UNCONFIRMED")
            error = self.error("ENGINE_STOP_UNCONFIRMED", "无法确认引擎已停止，调度已暂停")
        else:
            self.engine.cleanup(job_id)
        self.db.finish(job_id, "failed" if error else "cancelled", error)

    async def process(self, row):
        job_id = row["job_id"]
        prompt_id = None
        attempted = False
        staged = None
        deadline = time.monotonic() + self.settings.job_timeout
        try:
            if self.stopping(job_id):
                await self.stop_task(job_id)
                return
            workflow = self.registry.versions.get((row["workflow_id"], row["workflow_revision"]))
            if workflow is None or workflow.digest != row["template_sha256"]:
                raise AppError(503, "WORKFLOW_REVISION_MISSING", "已入队的工作流版本缺失或已改变")
            if self.engine.missing(workflow):
                raise AppError(503, "WORKFLOW_DEPENDENCIES_MISSING", "任务依赖已不可用", True)
            parameters = json.loads(row["parameters"])
            graph = workflow.render(parameters, job_id)
            attempted = True
            try:
                async with asyncio.timeout(min(self.settings.request_timeout, self.settings.job_timeout)):
                    if workflow.spec.runner == "moss":
                        prompt_id = await self.engine.submit_moss(graph["1"]["inputs"], job_id)
                    else:
                        prompt_id = await self.engine.submit(graph, job_id)
            except (httpx.HTTPError, ValueError, KeyError, TypeError, TimeoutError):
                if self.stopping(job_id):
                    error = None
                elif time.monotonic() >= deadline:
                    error = self.error("JOB_TIMEOUT", "任务超时")
                else:
                    error = self.error("SUBMISSION_UNKNOWN", "未收到明确的提交结果，引擎已回收")
                await self.stop_task(job_id, error=error, uncertain=True)
                return
            self.db.running(job_id, prompt_id)
            width, height = parameters["width"], parameters["height"]
            while True:
                if self.stopping(job_id):
                    await self.stop_task(job_id, prompt_id)
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    await self.stop_task(job_id, prompt_id, self.error("JOB_TIMEOUT", "任务超时"))
                    return
                try:
                    async with asyncio.timeout(remaining):
                        result = await self.engine.observe(prompt_id, workflow, job_id)
                except (httpx.HTTPError, TimeoutError):
                    await self.pause()
                    continue
                if result.progress:
                    self.db.progress(job_id, Progress(stage="sampling", current=result.progress[0],
                                                       total=result.progress[1]))
                if result.image is not None:
                    if self.stopping(job_id):
                        await self.stop_task(job_id, prompt_id)
                        return
                    artifact_id, staged = await asyncio.to_thread(self.images.stage, result.image, width, height)
                    if self.stopping(job_id):
                        await self.stop_task(job_id, prompt_id)
                    elif time.monotonic() >= deadline:
                        await self.stop_task(job_id, prompt_id, self.error("JOB_TIMEOUT", "任务超时"))
                    elif self.images.publish(self.db, job_id, artifact_id, staged, width, height):
                        self.engine.cleanup(job_id)
                    return
                await self.pause()
        except AppError as exc:
            if exc.detail.code == "ENGINE_EXITED":
                await self.engine.tick()
                self.db.finish(job_id, "failed", exc.detail)
            elif self.stopping(job_id):
                await self.stop_task(job_id, prompt_id)
            elif exc.detail.code == "ENGINE_RESULT_LOST":
                await self.stop_task(job_id, prompt_id, exc.detail)
            else:
                if exc.detail.code == "GPU_OOM":
                    await self.engine.tick(verify=True)
                self.engine.cleanup(job_id)
                self.db.finish(job_id, "failed", exc.detail)
            logger.info("JOB_END job=%s code=%s", job_id, exc.detail.code)
        except Exception:
            logger.exception("JOB_INTERNAL_ERROR job=%s", job_id)
            await self.stop_task(job_id, prompt_id, self.error("JOB_INTERNAL_ERROR", "任务处理出错，请查看诊断日志"),
                                 uncertain=attempted)
        finally:
            if staged is not None:
                try:
                    staged.unlink(missing_ok=True)
                except OSError:
                    logger.warning("STAGING_CLEANUP_FAILED job=%s", job_id)
