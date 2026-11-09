"""desktop.describe() 的字段与降级测试。

对应《界面与任务可观测性方案》§7 的第四组：老数据缺 `negative_prompt`、
缺 `finished_at`、带 `error` 三种情况都必须降级显示，不能抛错。
"""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from localgen.desktop import Window

WORKFLOWS = [{"workflow_id": "qwen-image-21", "title": "Qwen-Image-2.1 · GGUF"}]


def described(payload, workflows=None):
    entries = workflows if workflows is not None else WORKFLOWS
    stub = SimpleNamespace(
        workflow_title=lambda wid: next((item["title"] for item in entries
                                         if item["workflow_id"] == wid), wid),
        moment=Window.moment,
        span=Window.span,
        workflows=entries,
        engine_line=lambda job: Window.engine_line(
            SimpleNamespace(workflows=entries), job),
    )
    return Window.describe(stub, payload)


def job(**overrides):
    base = {
        "status": "succeeded",
        "workflow_id": "qwen-image-21",
        "workflow_revision": "1.0.0",
        "parameters": {"prompt": "p", "negative_prompt": "", "width": 1024, "height": 1024,
                       "seed": 7, "image": "", "strength": None},
        "created_at": "2026-11-09T06:00:00+00:00",
        "started_at": "2026-11-09T06:00:03+00:00",
        "finished_at": "2026-11-09T06:05:40+00:00",
        "artifacts": [{"width": 1024, "height": 1024, "size_bytes": 2_300_000}],
        "error": None,
    }
    base.update(overrides)
    return base


def test_describe_shows_model_three_moments_and_artifact():
    text = described(job())
    assert "Qwen-Image-2.1 · GGUF" in text and "1.0.0" in text
    assert "1024 × 1024" in text and "种子 7" in text
    assert "排队 3 s" in text
    assert "生成 5:37" in text
    assert "2.2 MB" in text
    # 窄详情栏里一行一件事，别拼成读不懂的长句
    assert all(len(line) <= 60 for line in text.split("\n"))


def test_describe_hides_absent_capabilities():
    """纯文生图任务不该出现"负面"「重绘强度」「参考图」这些占位字眼。"""
    text = described(job())
    assert "负面" not in text
    assert "重绘强度" not in text
    assert "参考图" not in text


def test_describe_shows_negative_strength_and_reference():
    parameters = {"prompt": "p", "negative_prompt": "lowres, blurry", "width": 768, "height": 1024,
                  "seed": 11, "image": "b726b578119a4edba0eeb7b79078935e.png", "strength": 0.6}
    text = described(job(workflow_id="moss-image", parameters=parameters))
    assert "负面 lowres, blurry" in text
    assert "重绘强度 0.60" in text
    assert "参考图 b726b578" in text


def test_describe_running_job_shows_elapsed_instead_of_duration():
    started = (datetime.now(UTC) - timedelta(seconds=192)).isoformat()
    text = described(job(status="running", started_at=started, finished_at=None, artifacts=[]))
    assert "已用 3:12" in text
    assert "生成 " not in text


def test_describe_degrades_on_old_rows_without_timestamps():
    """老数据只有 created_at（甚至没有）时仍要能说清状态，不能抛错。"""
    text = described(job(status="queued", started_at=None, finished_at=None, artifacts=[]))
    assert text.startswith("排队中")   # 状态词本身带"排队"，别和耗时混为一谈
    assert "等待开始" in text          # 没开始跑 → 明确写等待，而不是编一个 0 s
    assert "生成 " not in text         # 耗时标签不会在"还没开始"时出现

    # created_at 不可解析 → 时间那一行整行跳过，只留状态与尺寸两行，不许抛异常
    broken = described(job(created_at="not-a-time", started_at=None, finished_at=None, artifacts=[]))
    assert broken.count("\n") == 1
    assert "提交" not in broken and "排队" not in broken.split("\n")[1]
    assert broken.startswith("已完成")


def test_describe_appends_error_code_and_message():
    error = {"code": "GPU_OOM", "message": "Qwen-Image-2.1 显存不足", "retryable": True}
    text = described(job(status="failed", started_at=None, finished_at=None, artifacts=[], error=error))
    assert "GPU_OOM" in text
    assert "显存不足" in text


def test_describe_tolerates_parameters_missing_optional_keys():
    """老工作的 parameters 里可能连 image / strength 键都没有（不是空值），不许 KeyError。"""
    parameters = {"prompt": "p", "width": 512, "height": 512, "seed": 1}
    text = described(job(parameters=parameters))
    assert "512 × 512" in text
    assert "参考图" not in text and "重绘强度" not in text and "负面" not in text


# ---------- 进度行文字 ----------

@pytest.fixture(scope="module")
def qt_app():
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def progress_widgets():
    from PySide6.QtWidgets import QLabel, QProgressBar

    return SimpleNamespace(progress=QProgressBar(), progress_text=QLabel(),
                           moment=Window.moment, span=Window.span)


def test_progress_text_shows_steps_rate_and_eta(qt_app):
    stub = progress_widgets()
    payload = job(status="running", finished_at=None, artifacts=[], progress={
        "stage": "sampling", "current": 12, "total": 30, "detail": "1.93s/it · ETA 00:59"})
    Window.update_progress(stub, payload)
    text = stub.progress_text.text()
    assert text.startswith("采样 12 / 30")
    assert "1.93s/it" in text and "ETA 00:59" in text and "已用" in text
    assert (stub.progress.minimum(), stub.progress.maximum(), stub.progress.value()) == (0, 30, 12)


def test_progress_text_omits_step_numbers_while_decoding(qt_app):
    """解码阶段的 50 / 50 是采样步数，印成"解码 50 / 50"会骗人。"""
    stub = progress_widgets()
    payload = job(status="running", finished_at=None, artifacts=[], progress={
        "stage": "decoding", "current": 50, "total": 50, "detail": "9.5 s"})
    Window.update_progress(stub, payload)
    text = stub.progress_text.text()
    assert text.startswith("解码")
    assert "50 / 50" not in text
    assert "9.5 s" in text
    # 解码是收尾的固定动作，停在满格会像卡住 → 改忙碌态还在动
    assert stub.progress.maximum() == 0


def test_progress_bar_stays_full_while_sampling_is_done(qt_app):
    """采样跑满但还没进解码时，条必须是满格 —— 别把"已完成 50/50"画成忙碌态。"""
    stub = progress_widgets()
    payload = job(status="running", finished_at=None, artifacts=[], progress={
        "stage": "sampling", "current": 50, "total": 50, "detail": None})
    Window.update_progress(stub, payload)
    assert (stub.progress.minimum(), stub.progress.maximum(), stub.progress.value()) == (0, 50, 50)


def test_progress_text_falls_back_when_stage_unknown(qt_app):
    stub = progress_widgets()
    payload = job(status="running", finished_at=None, artifacts=[], progress=None)
    Window.update_progress(stub, payload)
    assert stub.progress_text.text().startswith("准备中")
    assert stub.progress.maximum() == 0          # 忙碌态
