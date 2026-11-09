"""服务设置 API 与步数/CFG 覆盖。

设置分两类验证：运行时字段（job_timeout 等）写盘后热更内存实例立即生效；
restart 字段（端口等）只写盘并提示重启。步数覆盖的用户要求：默认用工作流
模板原始值，上限 25；turbo 预设是 8 档 sigma 表，步数不是 8 时必须丢弃。
"""
import json

import pytest
from fastapi.testclient import TestClient

from localgen.api.app import make_app
from localgen.config import Settings
from localgen.jobs.edit import EditRequest, to_submission
from localgen.workflows import Registry

TOKEN = "x" * 40


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    data = tmp_path_factory.mktemp("settings-data")
    settings = Settings(api_port=18796, engine_port=18111, engine_mode="comfy",
                        allowed_hosts=["127.0.0.1"], job_timeout=600, queue_limit=4)
    with TestClient(make_app(data, settings, TOKEN)) as c:
        c.base_headers = {"Authorization": f"Bearer {TOKEN}",
                          "Host": f"127.0.0.1:{settings.api_port}"}
        c.data_dir = data
        yield c


def get(client):
    response = client.get("/api/v1/settings", headers=client.base_headers)
    assert response.status_code == 200
    return response.json()


def put(client, payload):
    response = client.put("/api/v1/settings", json=payload, headers=client.base_headers)
    return response


def test_get_returns_settings_and_field_kinds(client):
    body = get(client)
    assert body["settings"]["api_port"] > 0
    assert "api_port" in body["restart_fields"]
    assert "job_timeout" in body["runtime_fields"]


def test_runtime_field_applies_immediately(client):
    response = put(client, {"job_timeout": 1800, "queue_limit": 8})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["restart_required"] is False
    assert body["note"] == "已立即生效"
    # 内存实例热更：同一 app 再读返回新值，且宿主 Settings 对象也被更新
    assert get(client)["settings"]["job_timeout"] == 1800
    assert get(client)["settings"]["queue_limit"] == 8


def test_restart_field_warns_without_hot_reload(client):
    before = get(client)["settings"]["engine_port"]
    response = put(client, {"engine_port": before + 1})
    body = response.json()
    assert body["restart_required"] is True
    assert "engine_port" in body["restart_fields"]
    assert "重启" in body["note"]
    # 落盘 + 内存一致（GET 回读所见即所得），requires_restart 提醒重启
    assert get(client)["settings"]["engine_port"] == before + 1


def test_partial_update_keeps_other_fields(client):
    before = get(client)["settings"]["engine_mode"]
    put(client, {"job_timeout": 900})
    assert get(client)["settings"]["engine_mode"] == before


def test_invalid_value_rejected(client):
    response = put(client, {"api_port": 80})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    response = put(client, {"engine_mode": "banana"})
    assert response.status_code == 422
    assert get(client)["settings"]["engine_mode"] == "comfy"


def test_empty_patch_rejected(client):
    response = put(client, {})
    assert response.status_code == 400


def test_extra_field_rejected(client):
    response = put(client, {"banana": 1})
    assert response.status_code == 422


def test_config_toml_written_without_bom(client):
    import pathlib

    put(client, {"job_timeout": 1700})          # 先触发一次写盘
    config = pathlib.Path(client.data_dir) / "config.toml"
    raw = config.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    import tomllib

    parsed = tomllib.loads(raw.decode("utf-8"))
    assert parsed["job_timeout"] == 1700


# ---------- 步数 / CFG 覆盖 ----------


@pytest.fixture(scope="module")
def registry():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent.parent / "workflows"
    return Registry(root, "comfy")


def test_steps_default_is_template_value(registry):
    workflow = registry.current["pose-swap"]
    values = workflow.parameters(_submit(pose_id="P001"))
    assert values["steps"] == 8            # 模板默认（turbo 档）
    assert values["cfg_scale"] == 4


def _submit(**overrides):
    from localgen.jobs.models import Submission

    base = dict(workflow_id="pose-swap", prompt="改姿势", pose_id="P001")
    base.update(overrides)
    return Submission(**base)


def test_steps_override_passes_through(registry):
    values = registry.current["pose-swap"].parameters(_submit(steps=25, cfg_scale=6.5))
    assert values["steps"] == 25
    assert values["cfg_scale"] == 6.5


def test_steps_rendered_into_graph(registry):
    workflow = registry.current["pose-swap"]
    values = workflow.parameters(_submit(steps=15))
    graph = workflow.render(values, "11111111-1111-1111-1111-111111111111")
    assert graph["1"]["inputs"]["steps"] == 15


def test_turbo_sigma_dropped_when_steps_differ(registry):
    """turbo 预设是 8 档 sigma 表；步数改走时必须丢弃，否则错拍。"""
    from localgen.engine.sdcpp import turbo_safe_sigmas

    assert turbo_safe_sigmas({"sigmas": "qwen-image-2.1-turbo", "steps": 8}) == \
        "qwen-image-2.1-turbo"
    assert turbo_safe_sigmas({"sigmas": "qwen-image-2.1-turbo", "steps": 20}) is None
    assert turbo_safe_sigmas({"sigmas": "qwen-image-2.1-turbo", "steps": None}) == \
        "qwen-image-2.1-turbo"


def test_steps_upper_bound_25():
    from pydantic import ValidationError
    from localgen.jobs.models import Submission

    with pytest.raises(ValidationError):
        Submission(workflow_id="pose-swap", prompt="x", pose_id="P", steps=50)
    with pytest.raises(ValidationError):
        Submission(workflow_id="pose-swap", prompt="x", pose_id="P", cfg_scale=30)


def test_edit_route_passes_steps(registry):
    submission = to_submission(EditRequest(prompt="改姿势", pose="P001",
                                           steps=12, cfg_scale=3), registry)
    assert submission.steps == 12
    assert submission.cfg_scale == 3
    values = registry.current[submission.workflow_id].parameters(submission)
    assert values["steps"] == 12
    assert values["cfg_scale"] == 3


def test_settings_get_via_http_scope_matches_direct():
    """GET/PUT 与桌面端共用 Settings 模型：字段集合一致。"""
    import io
    import sys

    sys.path.insert(0, r"D:\garbage pile\local_image\src")
    from localgen.api.settings import RESTART_FIELDS
    from localgen.config import Settings

    assert set(RESTART_FIELDS) <= set(Settings.model_fields)
