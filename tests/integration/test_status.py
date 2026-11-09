"""/api/v1/status：一条接口回答"现在在干嘛、我排第几"。"""
from fastapi.testclient import TestClient

from localgen.api.app import make_app
from localgen.config import Settings
from localgen.engine.mock import MockEngine

BASE = "http://127.0.0.1:8765"
TOKEN = "status-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def make(tmp_path, **overrides):
    settings = Settings.model_validate({"engine_mode": "mock", "min_free_mib": 0, **overrides})
    app = make_app(tmp_path, settings, TOKEN, engine=MockEngine(1.2))
    return TestClient(app, base_url=BASE, headers=AUTH)


def test_status_is_idle_on_a_fresh_data_dir(tmp_path):
    with make(tmp_path) as client:
        body = client.get("/api/v1/status").json()
        assert body["active_job"] is None
        assert body["queue_length"] == 0
        assert body["position"] is None
        assert body["stage"] is None and body["elapsed_s"] is None


def test_status_reports_the_active_job_stage_and_elapsed(tmp_path):
    with make(tmp_path) as client:
        job = client.post("/api/v1/jobs", json={
            "workflow_id": "baseline", "prompt": "status"}).json()
        body = client.get("/api/v1/status").json()
        assert body["active_job"] == job["job_id"]
        assert body["workflow_id"] == "baseline"      # 界面要靠它显示"在跑哪个模型"
        assert body["position"] == 0
        assert body["elapsed_s"] is not None and body["elapsed_s"] >= 0
        assert body["queue_length"] == 0


def test_status_requires_the_token(tmp_path):
    with make(tmp_path) as client:
        denied = client.get("/api/v1/status", headers={"Authorization": "Bearer nope"})
        assert denied.status_code in (401, 403)
        assert denied.json()["error"]["code"] in {"UNAUTHORIZED", "LOCAL_POLICY_DENIED"}
