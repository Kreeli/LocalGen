import base64
import json
import time
from io import BytesIO
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from localgen.api.app import make_app
from localgen.config import Settings
from localgen.engine import EngineStatus, Observation
from localgen.engine.mock import MockEngine
from localgen.jobs.models import AppError

BASE = "http://127.0.0.1:8765"
TOKEN = "test-local-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
JOB = {"workflow_id": "baseline", "prompt": "  雨后街道  "}


def client(data, engine=None, **overrides):
    settings = Settings.model_validate({"engine_mode": "mock", "min_free_mib": 0, **overrides})
    return TestClient(make_app(data, settings, TOKEN, engine=engine), base_url=BASE, headers=AUTH)


def post(api, payload=None, key=None):
    headers = {"Idempotency-Key": key} if key else None
    return api.post("/api/v1/jobs", json=payload or JOB, headers=headers)


def wait_for(api, location, *states, seconds=8):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        result = api.get(location).json()
        if result["status"] in states:
            return result
        time.sleep(0.025)
    raise AssertionError(result)


def test_contract_history_and_missing_artifact(tmp_path):
    with client(tmp_path, MockEngine(0.1)) as api:
        first = post(api, key="once")
        assert first.status_code == 202
        same = post(api, JOB | {"prompt": "雨后街道"}, key="once")
        assert first.json()["job_id"] == same.json()["job_id"]
        assert first.json()["parameters"] == same.json()["parameters"]
        assert post(api, JOB | {"seed": 42}, key="once").status_code == 409
        assert post(api, JOB | {"width": 512}).status_code == 422
        assert post(api, JOB | {"width": 1024, "height": 1024}).status_code == 422
        assert post(api, JOB | {"seed": "42"}).status_code == 422
        assert post(api, JOB | {"command": "unknown"}).status_code == 422
        assert post(api, JOB | {"prompt": " \t"}).status_code == 422
        path = first.headers["Location"]
        completed = wait_for(api, path, "succeeded")
        artifact = completed["artifacts"][0]
        download = api.get(artifact["download_path"])
        assert download.status_code == 200
        assert download.content.startswith(b"\x89PNG\r\n\x1a\n")
        assert download.headers["content-type"] == "image/png"
        assert api.get(f"{path}/artifacts/{uuid4()}").status_code == 404
        assert api.post(f"{path}/cancel").status_code == 409
        second = post(api, JOB | {"seed": 7})
        page = api.get("/api/v1/jobs?limit=1").json()
        assert page["items"][0]["job_id"] == second.json()["job_id"]
        next_page = api.get("/api/v1/jobs", params={"cursor": page["next_cursor"], "limit": 1}).json()
        assert next_page["items"][0]["job_id"] == completed["job_id"]
        assert api.get("/api/v1/jobs?cursor=invalid").status_code == 422

    orphan = tmp_path / "results" / str(uuid4())
    orphan.mkdir()
    (orphan / "uncommitted.png").write_bytes(download.content)
    (tmp_path / "staging" / "stale.tmp").write_bytes(download.content)
    with client(tmp_path) as api:
        assert api.get(artifact["download_path"]).content == download.content
        assert not orphan.exists()
        assert len(list((tmp_path / "quarantine").iterdir())) == 2
        (tmp_path / "results" / completed["job_id"] / f"{artifact['artifact_id']}.png").unlink()
        assert api.get(artifact["download_path"]).status_code == 404
        assert not api.get(path).json()["artifacts"][0]["available"]
        assert api.get(path).json()["status"] == "succeeded"


def test_security_and_limits(tmp_path):
    with client(tmp_path) as api:
        assert api.get("/api/v1/health", headers={"Authorization": ""}).json() == {"status": "ok"}
        assert api.get("/api/v1/jobs", headers={"Authorization": ""}).status_code == 401
        assert api.post("/mcp", headers={"Authorization": ""}).status_code == 401
        assert api.get("/health", headers={"Host": "example.com"}).status_code == 403
        assert api.post("/mcp", headers={"Origin": "https://example.com"}).status_code == 403
        assert api.get("/api/v1/jobs", headers={"Origin": BASE}).status_code == 200
        assert api.get("/docs").status_code == 404
        response = api.post("/api/v1/jobs", content=b"x" * 70000)
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "REQUEST_TOO_LARGE"
    with client(tmp_path, min_free_mib=1000000) as api:
        assert post(api).json()["error"]["code"] == "DISK_SPACE_LOW"


class FaultEngine(MockEngine):
    def __init__(self, phase):
        super().__init__(0.1)
        self.phase = phase
        self.submitted = 0
        self.recycled = 0
        self.cancelled = 0

    async def submit(self, graph, job_id):
        import asyncio

        self.submitted += 1
        result = await super().submit(graph, job_id)
        if self.submitted == 1 and self.phase == "submitting":
            await asyncio.sleep(0.6)
        if self.submitted == 1 and self.phase == "lost":
            raise httpx.ReadTimeout("simulated response loss")
        return result

    async def observe(self, *args):
        import asyncio

        if self.submitted == 1:
            if self.phase == "running":
                await asyncio.sleep(0.6)
            elif self.phase == "oom":
                raise AppError(503, "GPU_OOM", "显存不足", True)
            elif self.phase == "crash":
                raise AppError(503, "ENGINE_EXITED", "引擎已退出", True)
            elif self.phase == "timeout":
                await asyncio.sleep(2)
        return await super().observe(*args)

    async def cancel(self, prompt_id):
        self.cancelled += 1
        return await super().cancel(prompt_id)

    async def recycle(self):
        self.recycled += 1
        return await super().recycle()


class FakeMoss(MockEngine):
    def __init__(self):
        super().__init__(0.1)
        self.moss_parameters = []

    async def submit_moss(self, parameters, job_id):
        self.moss_parameters.append(dict(parameters))
        return job_id

    async def observe_moss(self, prompt_id):
        buffer = BytesIO()
        Image.new("RGB", (768, 768), (24, 48, 96)).save(buffer, "PNG")
        return Observation(image=buffer.getvalue(), progress=(50, 50))

    async def observe(self, prompt_id, workflow, job_id):
        return await self.observe_moss(prompt_id)


@pytest.mark.parametrize("phase", ["submitting", "running"])
def test_active_and_queued_cancellation(tmp_path, phase):
    engine = FaultEngine(phase)
    with client(tmp_path, engine, queue_limit=1) as api:
        first = post(api)
        path = first.headers["Location"]
        wait_for(api, path, phase)
        waiting = post(api)
        assert waiting.status_code == 202
        assert post(api).status_code == 429
        assert api.post(waiting.headers["Location"] + "/cancel").status_code == 200
        assert api.post(path + "/cancel").status_code == 202
        assert wait_for(api, path, "cancelled")["artifacts"] == []
        assert api.post(path + "/cancel").status_code == 200
        second = post(api)
        wait_for(api, second.headers["Location"], "succeeded")
        assert engine.cancelled == 1
        assert engine.submitted == 2


@pytest.mark.parametrize(("phase", "code"), [
    ("lost", "SUBMISSION_UNKNOWN"), ("oom", "GPU_OOM"), ("crash", "ENGINE_EXITED"), ("timeout", "JOB_TIMEOUT"),
])
def test_recoverable_engine_errors(tmp_path, phase, code):
    engine = FaultEngine(phase)
    with client(tmp_path, engine, job_timeout=1) as api:
        first = post(api)
        assert wait_for(api, first.headers["Location"], "failed")["error"]["code"] == code
        assert engine.recycled == int(phase == "lost")
        assert engine.submitted == 1
        second = post(api)
        wait_for(api, second.headers["Location"], "succeeded")


def test_missing_model_and_offline_probe(tmp_path):
    class Missing(MockEngine):
        def missing(self, workflow):
            return ["v1-5-pruned-emaonly-fp16.safetensors"]

    with client(tmp_path, Missing(), engine_mode="comfy") as api:
        assert not api.get("/api/v1/workflows").json()["items"][0]["available"]
        response = post(api, JOB | {"workflow_id": "sd15"})
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "WORKFLOW_DEPENDENCIES_MISSING"
    with client(tmp_path, engine_mode="comfy") as api:
        assert api.get("/health").status_code == 200
        assert not api.get("/api/v1/engine").json()["available"]


def test_restart_preserves_waiting_order(tmp_path):
    engine = FaultEngine("running")
    with client(tmp_path, engine) as api:
        first = post(api)
        wait_for(api, first.headers["Location"], "running")
        second = post(api)
        third = post(api)
    with client(tmp_path, MockEngine(0.1)) as api:
        before = wait_for(api, second.headers["Location"], "succeeded")
        after = wait_for(api, third.headers["Location"], "succeeded")
        assert before["finished_at"] <= after["started_at"]
        assert api.get(first.headers["Location"]).json()["status"] == "cancelled"


def test_cancel_wins_while_png_is_staged(tmp_path, monkeypatch):
    import threading

    from localgen.storage.images import ImageStore

    original = ImageStore.stage
    staged = threading.Event()
    release = threading.Event()

    def hold_png(*args):
        result = original(*args)
        staged.set()
        assert release.wait(8)
        return result

    monkeypatch.setattr(ImageStore, "stage", hold_png)
    with client(tmp_path, MockEngine(0.1)) as api:
        path = post(api).headers["Location"]
        try:
            assert staged.wait(8)
            assert api.post(path + "/cancel").status_code == 202
        finally:
            release.set()
        assert wait_for(api, path, "cancelled")["artifacts"] == []
        assert not list((tmp_path / "results").iterdir())


def test_moved_file_without_commit_is_quarantined(tmp_path, monkeypatch):
    from localgen.storage.database import Database

    def failed_commit(*args):
        raise RuntimeError("simulated failure after PNG move")

    with monkeypatch.context() as patch:
        patch.setattr(Database, "succeed", failed_commit)
        with client(tmp_path, MockEngine(0.1)) as api:
            path = post(api).headers["Location"]
            assert wait_for(api, path, "failed")["artifacts"] == []
            assert len(list((tmp_path / "results").rglob("*.png"))) == 1
    with client(tmp_path) as api:
        assert api.get(path).json()["artifacts"] == []
        assert not list((tmp_path / "results").rglob("*.png"))
        assert len(list((tmp_path / "quarantine").rglob("*.png"))) == 1


def test_dsh_plugin_export(tmp_path):
    with client(tmp_path, MockEngine(0.1), engine_mode="comfy") as api:
        ids = {item["workflow_id"] for item in api.get("/api/v1/workflows").json()["items"]}
        assert {"sd15", "z-image-turbo", "moss-image"} <= ids
        t2i = api.get("/api/v1/workflows/z-image-turbo/dsh-plugin")
        assert t2i.status_code == 200
        assert "attachment" in t2i.headers["content-disposition"]
        graph = t2i.json()
        assert graph["4"]["inputs"]["text"] == "{{prompt}}"
        assert graph["8"]["inputs"]["seed"] == "{{seed}}"
        assert graph["7"]["inputs"]["width"] == 768
        assert graph["10"]["inputs"]["filename_prefix"] == "dsh-image-gen-z-image-turbo"
        edit = api.get("/api/v1/workflows/z-image-turbo/dsh-plugin", params={"variant": "image2image"})
        assert edit.status_code == 200
        egraph = edit.json()
        assert egraph["dsh-load"]["inputs"]["image"] == "{{image}}"
        assert egraph["8"]["inputs"]["latent_image"] == ["dsh-encode", 0]
        assert egraph["8"]["inputs"]["denoise"] == 0.6
        moss = api.get("/api/v1/workflows/moss-image/dsh-plugin")
        assert moss.status_code == 200
        mgraph = moss.json()
        assert mgraph["1"]["class_type"] == "LocalGenMossImage"
        assert mgraph["1"]["inputs"]["prompt"] == "{{prompt}}"
        assert mgraph["1"]["_meta"]["title"] == "dsh-image-gen:moss-image:text2image"
        moss_edit = api.get("/api/v1/workflows/moss-image/dsh-plugin",
                            params={"variant": "image2image"})
        assert moss_edit.status_code == 200
        assert moss_edit.json()["1"]["inputs"]["image"] == "{{image}}"
        assert moss_edit.json()["1"]["inputs"]["strength"] == 0.5
        assert api.get("/api/v1/workflows/nope/dsh-plugin").status_code == 404
        assert api.get("/api/v1/workflows/z-image-turbo/dsh-plugin",
                       params={"variant": "nope"}).status_code == 422


def test_comfyui_gateway(tmp_path):
    engine = FakeMoss()
    with client(tmp_path, engine, engine_mode="comfy") as api:
        exported = api.get("/api/v1/workflows/z-image-turbo/dsh-plugin").json()
        assert exported["1"]["_meta"]["title"] == "dsh-image-gen:z-image-turbo:text2image"
        graph = json.loads(json.dumps(exported))
        graph["4"]["inputs"]["text"] = "网关测试"
        graph["8"]["inputs"]["seed"] = 7
        submitted = api.post("/comfyui/prompt", json={"prompt": graph})
        assert submitted.status_code == 200, submitted.text
        prompt_id = submitted.json()["prompt_id"]
        deadline = time.monotonic() + 8
        entry = {}
        while time.monotonic() < deadline:
            entry = api.get(f"/comfyui/history/{prompt_id}").json()
            if entry.get(prompt_id, {}).get("status", {}).get("completed"):
                break
            time.sleep(0.025)
        assert entry[prompt_id]["status"]["status_str"] == "success"
        image = list(entry[prompt_id]["outputs"].values())[0]["images"][0]
        assert image["subfolder"] == prompt_id and image["type"] == "output"
        view = api.get("/comfyui/view", params=image)
        assert view.status_code == 200 and view.content.startswith(b"\x89PNG")
        moss_graph = json.loads(json.dumps(
            api.get("/api/v1/workflows/moss-image/dsh-plugin").json()))
        moss_graph["1"]["inputs"]["prompt"] = "网关 moss"
        moss_graph["1"]["inputs"]["seed"] = 9
        moss = api.post("/comfyui/prompt", json={"prompt": moss_graph})
        assert moss.status_code == 200, moss.text
        moss_id = moss.json()["prompt_id"]
        deadline = time.monotonic() + 8
        entry = {}
        while time.monotonic() < deadline:
            entry = api.get(f"/comfyui/history/{moss_id}").json()
            if entry.get(moss_id, {}).get("status", {}).get("completed"):
                break
            time.sleep(0.025)
        assert entry[moss_id]["status"]["status_str"] == "success"
        # 图生图：上传源图 → moss image2image 图 → 提交（FakeMoss 收到 image 引用）
        buffer = BytesIO()
        Image.new("RGB", (64, 64), (10, 10, 10)).save(buffer, "PNG")
        upload = api.post("/comfyui/upload/image",
                          files={"image": ("src.png", buffer.getvalue(), "image/png")})
        assert upload.status_code == 200, upload.text
        token = upload.json()["name"]
        assert upload.json()["subfolder"] == ""
        edit_graph = json.loads(json.dumps(
            api.get("/api/v1/workflows/moss-image/dsh-plugin",
                    params={"variant": "image2image"}).json()))
        edit_graph["1"]["inputs"]["prompt"] = "编辑测试"
        edit_graph["1"]["inputs"]["seed"] = 3
        edit_graph["1"]["inputs"]["image"] = token
        edit_graph["1"]["inputs"]["strength"] = 0.7
        edit = api.post("/comfyui/prompt", json={"prompt": edit_graph})
        assert edit.status_code == 200, edit.text
        edit_id = edit.json()["prompt_id"]
        deadline = time.monotonic() + 8
        entry = {}
        while time.monotonic() < deadline:
            entry = api.get(f"/comfyui/history/{edit_id}").json()
            if entry.get(edit_id, {}).get("status", {}).get("completed"):
                break
            time.sleep(0.025)
        assert entry[edit_id]["status"]["status_str"] == "success"
        assert engine.moss_parameters[-1]["image"] == token
        assert engine.moss_parameters[-1]["strength"] == 0.7
        # z-image 不支持图生图：网关明确拒绝
        zedit = json.loads(json.dumps(
            api.get("/api/v1/workflows/z-image-turbo/dsh-plugin",
                    params={"variant": "image2image"}).json()))
        zedit["4"]["inputs"]["text"] = "x"
        zedit["8"]["inputs"]["seed"] = 1
        zedit["dsh-load"]["inputs"]["image"] = token
        zrejected = api.post("/comfyui/prompt", json={"prompt": zedit})
        assert zrejected.status_code == 400
        assert "不支持图生图" in zrejected.json()["error"]["message"]
        assert api.post("/comfyui/prompt",
                        json={"prompt": {"1": {"class_type": "X", "inputs": {}}}}).status_code == 400
        bad = json.loads(json.dumps(exported))
        bad["4"]["inputs"]["text"] = "x"
        bad["8"]["inputs"]["seed"] = 1
        bad["7"]["inputs"]["width"] = 900
        bad["7"]["inputs"]["height"] = 900
        assert api.post("/comfyui/prompt", json={"prompt": bad}).status_code == 422
        anon = api.get(f"/comfyui/history/{prompt_id}", headers={"Authorization": ""})
        assert anon.status_code == 200


def test_openai_compat_api(tmp_path):
    engine = FakeMoss()
    with client(tmp_path, engine, engine_mode="comfy") as api:
        models = api.get("/v1/models").json()
        assert {"moss-image", "z-image-turbo"} <= {m["id"] for m in models["data"]}
        gen = api.post("/v1/images/generations",
                       json={"model": "moss-image", "prompt": "兼容层测试", "seed": 5})
        assert gen.status_code == 200, gen.text
        payload = gen.json()
        assert base64.b64decode(payload["data"][0]["b64_json"]).startswith(b"\x89PNG")
        buffer = BytesIO()
        Image.new("RGB", (64, 64), (5, 5, 5)).save(buffer, "PNG")
        edit = api.post("/v1/images/edits",
                        files={"image": ("src.png", buffer.getvalue(), "image/png")},
                        data={"model": "moss-image", "prompt": "编辑", "strength": "0.6"})
        assert edit.status_code == 200, edit.text
        assert base64.b64decode(edit.json()["data"][0]["b64_json"]).startswith(b"\x89PNG")
        assert engine.moss_parameters[-1]["image"] and engine.moss_parameters[-1]["strength"] == 0.6
        unknown = api.post("/v1/images/generations", json={"model": "nope", "prompt": "x"})
        assert unknown.status_code == 400 and "未知模型" in unknown.json()["error"]["message"]
        assert api.post("/v1/images/generations",
                        json={"model": "moss-image", "prompt": "x", "n": 2}).status_code == 400
        big = api.post("/v1/images/generations",
                       json={"model": "moss-image", "prompt": "x", "size": "900x900"})
        assert big.status_code == 422 and "尺寸" in big.json()["error"]["message"]
        zedit = api.post("/v1/images/edits",
                         files={"image": ("src.png", buffer.getvalue(), "image/png")},
                         data={"model": "z-image-turbo", "prompt": "x"})
        assert zedit.status_code == 400 and "不支持图生图编辑" in zedit.json()["error"]["message"]
        anon = api.get("/v1/models", headers={"Authorization": ""})
        assert anon.status_code == 200
        assert anon.json()["object"] == "list"


def test_image_parameter_requires_capability(tmp_path):
    with client(tmp_path, MockEngine(0.1)) as api:
        response = post(api, JOB | {"image": f"{uuid4().hex}.png"})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "UNSUPPORTED_PARAMETER"
        response = post(api, JOB | {"strength": 0.7})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "UNSUPPORTED_PARAMETER"


def test_progress_and_delete(tmp_path):
    with client(tmp_path, MockEngine(0.3)) as api:
        first = post(api)
        path = first.headers["Location"]
        deadline = time.monotonic() + 3
        while True:
            probe = api.get("/api/v1/progress").json()
            if probe["job"] or time.monotonic() > deadline:
                break
            time.sleep(0.02)
        assert probe["job"]["job_id"] == first.json()["job_id"]
        assert probe["queued"] == 0
        wait_for(api, path, "succeeded")
        assert api.get("/api/v1/progress").json() == {"job": None, "queued": 0}
        second = post(api)
        active_path = second.headers["Location"]
        wait_for(api, active_path, "submitting", "running")
        assert api.delete(active_path).status_code == 409
        done = wait_for(api, active_path, "succeeded")
        artifact = done["artifacts"][0]
        stored = tmp_path / "results" / done["job_id"] / f"{artifact['artifact_id']}.png"
        assert stored.exists()
        assert api.delete(active_path).status_code == 204
        assert api.get(active_path).status_code == 404
        assert api.get(artifact["download_path"]).status_code == 404
        assert not stored.exists()
        assert api.delete(active_path).status_code == 404
        assert api.get("/api/v1/progress").json() == {"job": None, "queued": 0}
