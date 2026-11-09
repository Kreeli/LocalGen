from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Status = Literal["queued", "submitting", "running", "cancelling", "succeeded", "failed", "cancelled", "interrupted"]
ACTIVE = ("submitting", "running", "cancelling")
TERMINAL = ("succeeded", "failed", "cancelled", "interrupted")
Identifier = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]
Prompt = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
Negative = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]
Size = Annotated[int, Field(strict=True, ge=1, le=8192)]
Seed = Annotated[int, Field(strict=True, ge=0, le=4294967295)]
Key = Annotated[str, StringConstraints(pattern=r"^[!-~]{1,128}$")]
ImageToken = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}\.(png|jpg|webp)$")]
# 扩散步数覆盖：留空 = 工作流模板默认。上限 25 —— 用户要求"默认用模型原始的
# 25"，且实测 8 步 turbo 在 6 GB 上与 20 步画质无差别，更高只是慢。
Steps = Annotated[int, Field(strict=True, ge=1, le=25)]
# CFG 覆盖：模板默认 1（kontext/pose-swap）到 6（qwen t2i），>15 无意义。
CfgScale = Annotated[float, Field(strict=True, ge=1, le=15)]
# 图库条目 id：来源是第三方索引，含中文、点、横线、日文分类前缀，放宽到「不许有路径分隔符」。
LibraryItemId = Annotated[str, StringConstraints(
    strip_whitespace=True, min_length=1, max_length=80,
    pattern=r"^[^/\\\x00]+$")]
Strength = Annotated[float, Field(strict=True, gt=0, le=1)]


class Submission(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    workflow_id: Identifier
    prompt: Prompt
    width: Size | None = None
    height: Size | None = None
    seed: Seed | None = None
    negative_prompt: Negative | None = None
    image: ImageToken | None = None
    strength: Strength | None = None
    # 换姿势 / 换服装：图库条目 id，引擎据此取素材当参考图。
    pose_id: LibraryItemId | None = None
    garment_id: LibraryItemId | None = None
    # 扩散步数 / CFG 覆盖：None = 用工作流模板默认值（推荐）。上限见类型注释。
    steps: Steps | None = None
    cfg_scale: CfgScale | None = None

    @model_validator(mode="after")
    def paired_dimensions(self):
        if (self.width is None) != (self.height is None):
            raise ValueError("宽度和高度必须同时提供")
        return self


class JobQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Status | None = None
    limit: int = Field(20, ge=1, le=100)
    cursor: str | None = Field(None, min_length=1, max_length=180)


class ErrorDetail(BaseModel):
    code: str
    message: str
    retryable: bool = False


class Progress(BaseModel):
    stage: str
    current: int | None = None
    total: int | None = None
    # 引擎自报的速度/剩余时间（如 "22.4 s/it · ETA 09:08"），只用于展示。
    detail: str | None = None


class Artifact(BaseModel):
    artifact_id: UUID
    mime_type: Literal["image/png"] = "image/png"
    width: int
    height: int
    size_bytes: int
    download_path: str
    available: bool = True


class Job(BaseModel):
    job_id: UUID
    status: Status
    workflow_id: str
    workflow_revision: str
    parameters: dict
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    progress: Progress | None = None
    artifacts: list[Artifact]
    error: ErrorDetail | None = None


class JobPage(BaseModel):
    items: list[Job]
    next_cursor: str | None = None


class AppError(Exception):
    def __init__(self, status: int, code: str, message: str, retryable: bool = False):
        super().__init__(code)
        self.status = status
        self.detail = ErrorDetail(code=code, message=message, retryable=retryable)

    def body(self) -> dict:
        return {"error": self.detail.model_dump()}


def validation_error() -> AppError:
    return AppError(422, "VALIDATION_ERROR", "请求参数格式或取值无效")
