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
