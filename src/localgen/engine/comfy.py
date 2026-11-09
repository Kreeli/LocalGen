import asyncio
import json
import logging
import os
import re
import shutil
import sys
import time
from contextlib import suppress
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import psutil
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from localgen.config import atomic_write, guarded
from localgen.engine import EngineStatus, Observation
from localgen.engine.gguf import GGUF_FOLDER, verified_loader
from localgen.engine.moss import MossJob, missing_assets
from localgen.engine.sdcpp import KontextJob
from localgen.engine.windows import WindowsProcess, listeners
from localgen.jobs.models import AppError

logger = logging.getLogger(__name__)
COMFY_VERSION = "0.3.76"


async def previous_engine_exited(data: Path) -> bool:
    journal = guarded(data / "engine-identity.json")
    if not journal.exists():
        return True
    try:
        identity = json.loads(journal.read_text("utf-8"))
        deadline = time.monotonic() + 6
        while True:
            try:
                process = psutil.Process(identity["pid"])
                if process.create_time() != identity["created_at"] or not process.is_running():
                    break
            except psutil.NoSuchProcess:
                break
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.15)
        journal.unlink()
        return True
    except (OSError, ValueError, KeyError, TypeError, psutil.Error):
        logger.warning("ENGINE_IDENTITY_UNVERIFIED")
        return False


class ComfyEngine:
    def __init__(self, data, settings):
        self.data = data
        self.settings = settings
        self.status = EngineStatus()
        self.process = None
        self.moss_root = None
        self.moss_job = None
        self.launcher = None
        self.ws_task = None
        self.latest = {}
        self.absent_since = None
        self.object_info = {}
        self.next_probe = 0
        self.next_start = 0
        self.failures = 0
        self.blocked = False
        self.closing = False
        self.journal = guarded(data / "engine-identity.json")
        self.folders = {name: guarded(data / f"comfy-{name}") for name in ("output", "temp", "input", "user")}
        self.client_id = str(uuid4())
        self.base = f"127.0.0.1:{settings.engine_port}"
        self.http = httpx.AsyncClient(base_url=f"http://{self.base}", trust_env=False,
                                      timeout=settings.request_timeout)

    async def start(self):
        await self.tick()

    def paths(self):
        if not self.settings.engine_root:
            raise AppError(503, "ENGINE_NOT_CONFIGURED", "请在设置中选择 ComfyUI Portable 文件夹")
        root = guarded(Path(self.settings.engine_root))
        if not (root / "main.py").is_file():
            root = guarded(root / "ComfyUI")
        python = guarded(Path(self.settings.engine_python) if self.settings.engine_python
                         else root.parent / "python_embeded" / "python.exe")
        main = guarded(root / "main.py")
        if not python.is_file() or python.suffix.lower() != ".exe" or not main.is_file():
            raise AppError(503, "ENGINE_PATH_INVALID", "找不到 ComfyUI 或独立 Python 解释器")
        version_file = guarded(root / "comfyui_version.py")
        if not version_file.is_file() or not re.search(
            rf"""^__version__\s*=\s*["']{re.escape(COMFY_VERSION)}["']\s*$""",
            version_file.read_text("utf-8"), re.M,
        ):
            raise AppError(503, "ENGINE_VERSION_UNSUPPORTED", f"请使用 ComfyUI {COMFY_VERSION}")
        return root, python, main

    async def boot(self):
        try:
            if os.name != "nt":
                raise AppError(503, "ENGINE_WINDOWS_REQUIRED", "真实引擎需要 Windows")
            root, python, main = self.paths()
            self.moss_root = root.parent / "localgen-moss"
            if await asyncio.to_thread(listeners, self.settings.engine_port):
                raise AppError(503, "ENGINE_PORT_IN_USE", "ComfyUI 端口已被占用")
            for folder in self.folders.values():
                folder.mkdir(exist_ok=True)
            command = [
                str(python), "-s", "-u", str(main), "--listen", "127.0.0.1", "--port",
                str(self.settings.engine_port), "--cuda-device", str(self.settings.engine_device),
                "--disable-all-custom-nodes", "--disable-api-nodes", "--disable-metadata",
            ]
            if verified_loader(root):
                command.extend(["--whitelist-custom-nodes", GGUF_FOLDER])
            for key, folder in self.folders.items():
                command.extend([f"--{key}-directory", str(folder)])
            database = guarded(self.folders["user"] / "comfyui.db")
            command.extend(["--database-url", "sqlite:///" + database.as_posix()])
            self.launch_owned(command, root)
            logger.info("ENGINE_LAUNCHED pid=%s version=%s", self.process.pid, COMFY_VERSION)
            deadline = time.monotonic() + self.settings.startup_timeout
            while time.monotonic() < deadline:
                if not self.process.alive():
                    raise AppError(503, "ENGINE_EXITED", "ComfyUI 在启动时退出")
                entries = await asyncio.to_thread(listeners, self.settings.engine_port)
                if entries:
                    if any(host != "127.0.0.1" or pid not in self.process.pids() for host, pid in entries):
                        raise AppError(503, "ENGINE_LISTENER_INVALID", "ComfyUI 监听归属或地址无效")
                    try:
                        stats = await self.get("/system_stats")
                        if stats.get("system", {}).get("comfyui_version") != COMFY_VERSION:
                            raise AppError(503, "ENGINE_VERSION_UNSUPPORTED", "ComfyUI 服务版本不匹配")
                        devices = [item["name"] for item in stats.get("devices", [])
                                   if item.get("type") == "cuda" and "NVIDIA" in item.get("name", "").upper()]
                        if not devices:
                            raise AppError(503, "CUDA_UNAVAILABLE", "未检测到 NVIDIA CUDA 设备")
                        self.object_info = await self.get("/object_info")
                        if not isinstance(self.object_info, dict) or not self.object_info:
                            raise AppError(503, "ENGINE_API_INVALID", "ComfyUI 节点接口响应无效")
                        self.status = EngineStatus("ready", "ENGINE_READY", devices[0])
                        self.next_probe = time.monotonic() + 5
                        self.ws_task = asyncio.create_task(self.websocket())
                        return
                    except (httpx.HTTPError, ValueError):
                        pass
                await asyncio.sleep(0.4)
            raise AppError(503, "ENGINE_START_TIMEOUT", "ComfyUI 启动探测超时")
        except asyncio.CancelledError:
            raise
        except AppError as exc:
            code = exc.detail.code
            logger.warning("%s port=%s", code, self.settings.engine_port)
        except Exception:
            code = "ENGINE_LAUNCH_FAILED"
            logger.exception("ENGINE_LAUNCH_FAILED")
        await self.recover_process(code)

    def launch_owned(self, command, cwd, **kwargs):
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith(
            ("PYTHON", "VIRTUAL_ENV", "CONDA", "LOCALGEN_", "CUDA_VISIBLE_DEVICES", "_PYI_")
        )}
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        windows = os.environ.get("SystemRoot", "C:\\Windows")
        env["PATH"] = os.pathsep.join([str(Path(command[0]).parent), windows + "\\System32", windows])
        # PyInstaller's DLL lookup directory is inherited by Windows children.
        if getattr(sys, "frozen", False):
            import ctypes

            kernel = ctypes.windll.kernel32
            kernel.SetDllDirectoryW(None)
            try:
                self.process = WindowsProcess(command, cwd, env, **kwargs)
            finally:
                kernel.SetDllDirectoryW(str(sys._MEIPASS))
        else:
            self.process = WindowsProcess(command, cwd, env, **kwargs)
        atomic_write(self.journal, json.dumps({"pid": self.process.pid,
                     "created_at": self.process.created_at}).encode())

    async def recover_process(self, code):
        if await self.stop_process():
            self.failures += 1
            self.next_start = time.monotonic() + min(2 ** self.failures, 15)
            self.status = EngineStatus("error", code)

    async def tick(self, verify=False):
        if self.closing or self.blocked or self.moss_job is not None:
            return
        if self.status.state == "ready":
            if not self.process.alive():
                await self.recover_process("ENGINE_EXITED")
            elif verify or time.monotonic() >= self.next_probe:
                self.next_probe = time.monotonic() + 5
                try:
                    entries = await asyncio.to_thread(listeners, self.settings.engine_port)
                    if entries != [("127.0.0.1", self.process.pid)] and (
                        not entries or any(host != "127.0.0.1" or pid not in self.process.pids()
                                           for host, pid in entries)
                    ):
                        raise AppError(503, "ENGINE_LISTENER_INVALID", "ComfyUI 监听归属或地址无效")
                    self.object_info = await self.get("/object_info")
                except (AppError, httpx.HTTPError, OSError, ValueError, psutil.Error):
                    await self.recover_process("ENGINE_HEALTH_FAILED")
            return
        permanent = {"ENGINE_NOT_CONFIGURED", "ENGINE_PATH_INVALID", "ENGINE_VERSION_UNSUPPORTED",
                     "CUDA_UNAVAILABLE", "ENGINE_WINDOWS_REQUIRED", "ENGINE_LISTENER_INVALID"}
        if (self.status.code not in permanent and self.failures < 4
                and (self.launcher is None or self.launcher.done()) and time.monotonic() >= self.next_start):
            self.status = EngineStatus("starting", "ENGINE_STARTING")
            self.launcher = asyncio.create_task(self.boot())

    async def stop_process(self):
        if self.ws_task:
            self.ws_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.ws_task
            self.ws_task = None
        self.object_info = {}
        if self.process:
            self.status = EngineStatus("stopping", "ENGINE_STOPPING")
            try:
                if self.process.pids():
                    self.process.terminate()
                deadline = time.monotonic() + 6
                while self.process.alive() or self.process.pids():
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Job Object did not become empty")
                    await asyncio.sleep(0.1)
                self.process.close()
                self.process = None
                self.journal.unlink(missing_ok=True)
            except Exception:
                self.blocked = True
                self.status = EngineStatus("error", "ENGINE_STOP_UNCONFIRMED")
                logger.exception("ENGINE_STOP_UNCONFIRMED")
                return False
        self.moss_job = None
        return True

    async def recycle(self):
        if not await self.stop_process():
            return False
        self.next_start = time.monotonic() + 1
        self.status = EngineStatus("stopped", "ENGINE_RESTART_PENDING")
        return True

    async def shutdown(self):
        self.closing = True
        if self.launcher and not self.launcher.done():
            self.launcher.cancel()
            with suppress(asyncio.CancelledError):
                await self.launcher
        stopped = await self.stop_process()
        await self.http.aclose()
        if stopped:
            self.status = EngineStatus()
        return stopped

    async def get(self, path):
        response = await self.http.get(path)
        response.raise_for_status()
        return response.json()

    async def post(self, path, body):
        response = await self.http.post(path, json=body)
        response.raise_for_status()
        return response

    def missing(self, workflow):
        if workflow.spec.runner in ("moss", "sdcpp") and self.moss_root is not None:
            return missing_assets(self.moss_root, [name for _, _, name in workflow.spec.models])
        if self.status.state != "ready":
            return []
        return workflow.dependencies(self.object_info)

    def require_alive(self):
        if self.process is None or not self.process.alive():
            raise AppError(503, "ENGINE_EXITED", "ComfyUI 在任务期间退出", True)

    async def websocket(self):
        while self.status.state == "ready":
            try:
                async with connect(f"ws://{self.base}/ws?clientId={self.client_id}", proxy=None,
                                   open_timeout=3, close_timeout=1, max_size=1024 * 1024) as ws:
                    async for message in ws:
                        if not isinstance(message, str):
                            continue
                        try:
                            event = json.loads(message)
                            data = event.get("data", {})
                            if event.get("type") == "progress" and data.get("prompt_id") in self.latest:
                                self.latest[data["prompt_id"]] = (int(data["value"]), int(data["max"]))
                        except (ValueError, TypeError, KeyError, AttributeError):
                            continue
            except (OSError, WebSocketException, TimeoutError):
                await asyncio.sleep(0.8)

    async def submit(self, graph, job_id):
        self.require_alive()
        try:
            data = (await self.post("/prompt", {"prompt": graph, "client_id": self.client_id})).json()
            prompt_id = str(UUID(data["prompt_id"]))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 400:
                raise AppError(422, "ENGINE_REJECTED_PROMPT", "ComfyUI 拒绝了工作流，请检查依赖") from exc
            raise
        self.latest = {prompt_id: None}
        self.absent_since = None
        return prompt_id

    async def submit_sdcpp(self, workflow, graph, job_id):
        if self.moss_root is None or missing_assets(self.moss_root, [name for _, _, name in workflow.spec.models]):
            raise AppError(503, "WORKFLOW_DEPENDENCIES_MISSING", "本机模型文件缺失", True)
        device = self.settings.engine_device
        node_info = self.object_info
        if not await self.stop_process():
            raise AppError(503, "ENGINE_STOP_UNCONFIRMED", "无法释放 ComfyUI 显卡资源", True)
        job_class = MossJob if graph["1"]["class_type"] == "LocalGenMossImage" else KontextJob
        job = job_class(job_id, self.folders["output"], self.moss_root, graph["1"]["inputs"],
                        device, input_root=self.data / "gateway-input")
        self.moss_job = job
        try:
            self.launch_owned(job.command, job.cwd, on_line=job.on_line,
                              log_file=guarded(self.data / "logs" / f"sdcpp-{job_id}.log"))
        except OSError as exc:
            await self.recycle()
            raise AppError(503, "ENGINE_LAUNCH_FAILED", "引擎启动失败", True) from exc
        self.object_info = node_info
        self.status = EngineStatus("ready", "SDCPP_ACTIVE", device)
        logger.info("SDCPP_LAUNCHED job=%s class=%s pid=%s", job.job_id,
                    graph["1"]["class_type"], self.process.pid)
        return job.job_id

    async def observe_sdcpp(self, prompt_id):
        job = self.moss_job
        if job is None or job.job_id != prompt_id or self.process is None:
            raise AppError(503, "ENGINE_RESULT_LOST", "无法找到任务", True)
        if self.process.alive():
            return Observation(progress=job.progress)
        code = self.process.exit_code()
        if not await self.recycle():
            raise AppError(503, "ENGINE_STOP_UNCONFIRMED", "无法确认引擎已经退出", True)
        logger.info("SDCPP_EXIT job=%s code=%s", job.job_id, code)
        return Observation(image=job.read_result(code, self.settings.max_image_mib * 2**20),
                           progress=job.progress)

    async def queue(self):
        data = await self.get("/queue")
        return ({item[1] for item in data["queue_running"]}, {item[1] for item in data["queue_pending"]})

    async def observe(self, prompt_id, workflow, job_id):
        if workflow.spec.runner in ("moss", "sdcpp"):
            return await self.observe_sdcpp(prompt_id)
        self.require_alive()
        item = (await self.get(f"/history/{prompt_id}")).get(prompt_id)
        if item is None:
            running, waiting = await self.queue()
            if prompt_id in running | waiting:
                self.absent_since = None
            elif self.absent_since is None:
                self.absent_since = time.monotonic()
            elif time.monotonic() - self.absent_since > 2:
                raise AppError(503, "ENGINE_RESULT_LOST", "ComfyUI 队列和历史中均找不到任务", True)
            return Observation(progress=self.latest.get(prompt_id))
        status = item.get("status", {})
        if status.get("status_str") != "success" or not status.get("completed"):
            details = json.dumps(status.get("messages", []), ensure_ascii=False).lower()
            oom = any(value in details for value in ("out of memory", "outofmemory", "allocation on device"))
            if oom:
                raise AppError(503, "GPU_OOM", "显存不足，可尝试较小的已验证尺寸", True)
            raise AppError(503, "ENGINE_EXECUTION_FAILED", "ComfyUI 执行失败，请查看本机诊断日志", True)
        images = item.get("outputs", {}).get(workflow.spec.output_node, {}).get("images", [])
        if len(images) != 1:
            raise AppError(422, "INVALID_IMAGE", "预期输出节点必须返回单张 PNG")
        image = images[0]
        filename = image.get("filename", "")
        if (not re.fullmatch(r"image_\d{5,}_.png", filename) or image.get("subfolder") != job_id
                or image.get("type") != "output"):
            raise AppError(422, "INVALID_IMAGE_PATH", "引擎返回了不受控的图片路径")
        try:
            guarded(self.folders["output"] / job_id / filename)
        except OSError as exc:
            raise AppError(422, "INVALID_IMAGE_PATH", "引擎返回了不受控的图片路径") from exc
        content = bytearray()
        limit = self.settings.max_image_mib * 2**20
        async with self.http.stream("GET", "/view", params={"filename": filename, "subfolder": job_id,
                                                          "type": "output"}) as response:
            response.raise_for_status()
            if response.headers.get("content-type", "").split(";")[0] != "image/png":
                raise AppError(422, "INVALID_IMAGE", "引擎返回了非 PNG 格式")
            async for chunk in response.aiter_bytes(65536):
                content.extend(chunk)
                if len(content) > limit:
                    raise AppError(422, "INVALID_IMAGE", "结果图片大小超限")
        self.failures = 0
        return Observation(image=bytes(content), progress=self.latest.get(prompt_id))

    async def cancel(self, prompt_id):
        if self.moss_job is not None:
            return self.moss_job.job_id == prompt_id and await self.recycle()
        deadline = time.monotonic() + self.settings.cancel_timeout
        async with asyncio.timeout(self.settings.cancel_timeout):
            interrupted = False
            while time.monotonic() < deadline:
                self.require_alive()
                running, waiting = await self.queue()
                if (running | waiting) - {prompt_id}:
                    logger.warning("ENGINE_FOREIGN_PROMPT")
                    return False
                if prompt_id in waiting:
                    await self.post("/queue", {"delete": [prompt_id]})
                elif prompt_id in running:
                    if not interrupted:
                        await self.post("/interrupt", {"prompt_id": prompt_id})
                        interrupted = True
                else:
                    return True
                await asyncio.sleep(0.2)
        return False

    def cleanup(self, job_id):
        try:
            job_id = str(UUID(job_id))
            folder = guarded(self.folders["output"] / job_id)
            if folder.is_dir():
                shutil.rmtree(folder)
        except (OSError, ValueError):
            logger.warning("ENGINE_OUTPUT_CLEANUP_FAILED job=%s", job_id)
