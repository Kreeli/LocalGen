import json
import logging
import os
import secrets
import stat
import sys
import tomllib
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def resources() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)
    installed = Path(__file__).parent / "resources"
    return installed if installed.is_dir() else Path(__file__).resolve().parents[2]


def default_data_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share")) / "LocalGen"


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    api_port: int = Field(8765, ge=1024, le=65535)
    engine_port: int = Field(8188, ge=1024, le=65535)
    engine_mode: Literal["comfy", "mock"] = "comfy"
    engine_root: str = Field("", max_length=4096)
    engine_python: str = Field("", max_length=4096)
    engine_device: int = Field(0, ge=0, le=15)
    startup_timeout: int = Field(120, ge=5, le=600)
    job_timeout: int = Field(600, ge=1, le=3600)
    cancel_timeout: int = Field(15, ge=1, le=60)
    request_timeout: int = Field(8, ge=1, le=30)
    queue_limit: int = Field(20, ge=1, le=100)
    min_free_mib: int = Field(512, ge=0, le=1000000)
    max_image_mib: int = Field(24, ge=1, le=64)
    max_pixels: int = Field(4194304, ge=262144, le=16777216)
    max_request_bytes: int = Field(65536, ge=16384, le=1048576)
    allowed_hosts: list[Literal["127.0.0.1", "localhost"]] = ["127.0.0.1", "localhost"]

    @model_validator(mode="after")
    def check_paths_and_ports(self):
        if self.api_port == self.engine_port:
            raise ValueError("LocalGen 与 ComfyUI 端口必须不同")
        if "127.0.0.1" not in self.allowed_hosts:
            raise ValueError("allowed_hosts 必须包含 127.0.0.1")
        for value in (self.engine_root, self.engine_python):
            if value and (
                not Path(value).is_absolute() or Path(value).drive.startswith("\\")
                or any(ord(char) < 32 for char in value)
            ):
                raise ValueError("引擎路径必须是本机绝对路径")
        return self


def guarded(path: Path) -> Path:
    """Reject redirects at every component, including Windows junctions."""
    for part in reversed((path, *path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise OSError(f"不允许符号链接或目录联接：{part.name}")
    return path


def atomic_write(path: Path, data: bytes) -> None:
    guarded(path)
    temp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        with os.fdopen(os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def load_settings(data: Path) -> Settings:
    path = guarded(data / "config.toml")
    if not path.exists():
        atomic_write(path, (resources() / "config" / "default.toml").read_bytes())
    return Settings.model_validate(tomllib.loads(path.read_text("utf-8")))


def save_settings(data: Path, settings: Settings) -> None:
    entries = settings.model_dump()
    encoded = "".join(
        f"{key} = {json.dumps(value, ensure_ascii=False)}\n" for key, value in entries.items()
    )
    atomic_write(data / "config.toml", encoded.encode("utf-8"))


def load_token(data: Path, create: bool = True) -> str:
    path = guarded(data / "api-token.bin")
    if os.name == "nt":
        import win32crypt

        if not path.exists() and create:
            token = secrets.token_urlsafe(32).encode()
            atomic_write(path, win32crypt.CryptProtectData(token, "LocalGen", None, None, None, 1))
        return win32crypt.CryptUnprotectData(path.read_bytes(), None, None, None, 1)[1].decode()
    if not path.exists() and create:
        atomic_write(path, secrets.token_urlsafe(32).encode())
    if path.stat().st_mode & 0o077:
        raise PermissionError("令牌文件必须仅允许当前账户读取")
    return path.read_text("ascii")


def setup_logging(data: Path) -> None:
    folder = guarded(data / "logs")
    folder.mkdir(exist_ok=True)
    logger = logging.getLogger()
    handler = RotatingFileHandler(guarded(folder / "localgen.log"), maxBytes=2**20, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    for name in ("httpx", "httpcore", "mcp", "websockets"):
        logging.getLogger(name).setLevel(logging.WARNING)


class InstanceLock:
    def __init__(self, data: Path):
        self.path = data / "instance.lock"

    def __enter__(self):
        self.file = guarded(self.path).open("a+b")
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise RuntimeError("此数据目录已有一个 LocalGen 实例在运行") from exc
        return self

    def __exit__(self, *_):
        self.file.close()
