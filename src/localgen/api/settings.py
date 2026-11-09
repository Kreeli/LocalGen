"""服务设置读写：GET /api/v1/settings、PUT /api/v1/settings。

设计要点
--------
- PUT 是**部分更新**：只传要改的字段，其余保留。整份 model_validate 兜底校验，
  坏值整单拒绝，不会写出一半新一半旧的 config.toml。
- 字段分两类：
    * 运行时生效（job_timeout / queue_limit / max_pixels / max_image_mib /
      max_request_bytes / cancel_timeout / request_timeout）：写盘 + 热更内存
      实例，立即生效；
    * requires_restart（端口 / 引擎模式 / 引擎目录 / 设备 / 启动超时 /
      allowed_hosts）：进程启动时读定，只写盘，返回体里提示"重启 LocalGen
      生效"。绝不在此热重启——服务进程由桌面持有，热改端口必死锁。
- 桌面端"设置"对话框与这里写同一份 config.toml，共用 save_settings 的
  原子写；两边改谁的都算数。
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from localgen.config import save_settings
from localgen.jobs.models import AppError

# 进程启动时读定、改了必须重启 LocalGen 才生效的字段。
RESTART_FIELDS = {"api_port", "engine_port", "engine_mode", "engine_root",
                  "engine_python", "engine_device", "startup_timeout", "allowed_hosts"}

INT_FIELDS = {"api_port": (1024, 65535), "engine_port": (1024, 65535),
              "engine_device": (0, 8), "startup_timeout": (30, 600),
              "job_timeout": (60, 86400), "cancel_timeout": (5, 120),
              "request_timeout": (5, 120), "queue_limit": (1, 64),
              "min_free_mib": (64, 65536), "max_image_mib": (1, 64),
              "max_pixels": (262144, 16777216), "max_request_bytes": (4096, 10485760)}

TEXT_FIELDS = {"engine_mode": ("comfy", "mock"), "engine_root": 512, "engine_python": 512}


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_port: int | None = Field(default=None, ge=1024, le=65535)
    engine_port: int | None = Field(default=None, ge=1024, le=65535)
    engine_mode: str | None = None
    engine_root: str | None = Field(default=None, max_length=512)
    engine_python: str | None = Field(default=None, max_length=512)
    engine_device: int | None = Field(default=None, ge=0, le=8)
    startup_timeout: int | None = Field(default=None, ge=30, le=600)
    job_timeout: int | None = Field(default=None, ge=60, le=86400)
    cancel_timeout: int | None = Field(default=None, ge=5, le=120)
    request_timeout: int | None = Field(default=None, ge=5, le=120)
    queue_limit: int | None = Field(default=None, ge=1, le=64)
    min_free_mib: int | None = Field(default=None, ge=64, le=65536)
    max_image_mib: int | None = Field(default=None, ge=1, le=64)
    max_pixels: int | None = Field(default=None, ge=262144, le=16777216)
    max_request_bytes: int | None = Field(default=None, ge=4096, le=10485760)
    allowed_hosts: list[str] | None = None


def validate_patch(patch: dict) -> dict:
    """字段级前置校验，给出可读的错误而不是 pydantic 原始堆栈。"""
    for key, value in patch.items():
        if key in INT_FIELDS:
            low, high = INT_FIELDS[key]
            if not isinstance(value, int) or not low <= value <= high:
                raise AppError(422, "VALIDATION_ERROR", f"{key} 必须在 {low}–{high} 之间")
        if key == "engine_mode" and value not in TEXT_FIELDS["engine_mode"]:
            raise AppError(422, "VALIDATION_ERROR", "engine_mode 只支持 comfy 或 mock")
        if key in ("engine_root", "engine_python") and len(str(value)) > TEXT_FIELDS[key]:
            raise AppError(422, "VALIDATION_ERROR", f"{key} 过长")
        if key == "allowed_hosts":
            if not isinstance(value, list) or not value or any(
                    host not in ("127.0.0.1", "localhost") for host in value):
                raise AppError(422, "VALIDATION_ERROR",
                               "allowed_hosts 只支持 127.0.0.1 与 localhost")
    return patch


def build_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    @router.get("/settings")
    async def read_settings(request: Request):
        settings = request.app.state.settings
        payload = settings.model_dump()
        return {"settings": payload,
                "restart_fields": sorted(RESTART_FIELDS),
                "runtime_fields": sorted(set(INT_FIELDS) - RESTART_FIELDS)}

    @router.put("/settings")
    async def write_settings(request: Request, update: SettingsUpdate):
        patch = update.model_dump(exclude_none=True, exclude_unset=True)
        if not patch:
            raise AppError(400, "VALIDATION_ERROR", "没有提供任何要修改的字段")
        validate_patch(patch)
        settings = request.app.state.settings
        merged = settings.model_dump() | patch
        try:
            new_settings = type(settings).model_validate(merged)
        except Exception as exc:                              # noqa: BLE001
            raise AppError(422, "VALIDATION_ERROR", f"组合后的配置无效：{exc}") from None
        save_settings(request.app.state.data, new_settings)
        # 全部字段热更到内存实例（GET 回读保持一致）。restart 字段进程不重读
        # （端口/引擎目录在启动时定死），所以响应体仍标注 requires_restart
        # 提醒"重启 LocalGen 后真正生效"。
        for key, value in patch.items():
            try:
                setattr(settings, key, value)
            except Exception:                                 # noqa: BLE001
                pass
        restart = sorted(set(patch) & RESTART_FIELDS)
        return {"settings": new_settings.model_dump(), "changed": sorted(patch),
                "restart_required": bool(restart), "restart_fields": restart,
                "note": ("以下字段需重启 LocalGen 后生效："
                         + "、".join(restart)) if restart else "已立即生效"}

    return router
