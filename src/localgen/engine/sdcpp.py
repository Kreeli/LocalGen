"""Shared sd-cli runner pieces and the FLUX.1 Kontext dev job."""
import io
import re
from pathlib import Path
from uuid import UUID

from PIL import Image

from localgen.config import atomic_write, guarded
from localgen.jobs.models import AppError

# Shared binary set of the pinned stable-diffusion.cpp runtime (localgen-moss/bin).
BIN_FILES = (
    "sd-cli.exe", "stable-diffusion.dll", "ggml-base.dll", "ggml.dll", "ggml-cuda.dll",
    "cudart64_12.dll", "cublas64_12.dll", "cublasLt64_12.dll",
)


def missing_assets(root, model_names):
    """Model/bin files that are absent from the pinned sd-cli runtime."""
    missing = []
    for folder, files in (("bin", dict.fromkeys(BIN_FILES)),
                          ("models", dict.fromkeys(model_names))):
        for name in files:
            try:
                path = guarded(root / folder / name)
                if path.is_file():
                    continue
            except OSError:
                pass
            missing.append(name)
    return missing


def _stage_reference(token: str, input_root: Path, folder: Path, width: int, height: int) -> Path:
    """Copy the staged input image into the private task folder, resized to the
    output dimensions (sd.cpp keeps reference images at their native size, and a
    12-megapixel phone photo would need gigabytes of latent VRAM)."""
    if not re.match(r"^[0-9a-f]{32}\.(png|jpg|webp)$", token):
        raise AppError(422, "INVALID_INPUT_IMAGE", "输入图引用无效")
    source = guarded(Path(input_root) / token)
    if not source.is_file():
        raise AppError(422, "INPUT_IMAGE_MISSING", "输入图不存在或已过期，请重新上传")
    ref = guarded(folder / "input.png")
    try:
        with Image.open(io.BytesIO(source.read_bytes())) as image:
            image = image.convert("RGB")
            image = image.resize((width, height), Image.LANCZOS)
            buffer = io.BytesIO()
            image.save(buffer, "PNG")
    except AppError:
        raise
    except Exception:
        raise AppError(400, "INVALID_INPUT_IMAGE", "参考图不是可解码的 PNG/JPEG/WebP 图片") from None
    atomic_write(ref, buffer.getvalue())
    return ref


class KontextJob:
    """One FLUX.1 Kontext dev job: prompt + reference image(s) via --ref-image."""

    MODEL_FILES = {
        "flux1-kontext-dev-Q4_K_M.gguf": 6931817760,
        "clip_l.safetensors": 246144152,
        "t5-v1_1-xxl-encoder-Q4_K_S.gguf": 2738574528,
        "ae.safetensors": 335304388,
    }

    def __init__(self, job_id, output_root, runtime, parameters, device, input_root):
        self.job_id = str(UUID(job_id))
        self.folder = guarded(output_root / self.job_id)
        self.folder.mkdir(exist_ok=True)
        self.output = guarded(self.folder / "image.png")
        self.progress = None
        self.steps = parameters["steps"]
        self.gpu_oom = False
        prompt = self.folder / "prompt.txt"
        negative = self.folder / "negative.txt"
        atomic_write(prompt, parameters["prompt"].encode("utf-8"))
        atomic_write(negative, parameters.get("negative_prompt", "").encode("utf-8"))
        token = parameters.get("image") or ""
        ref_image = _stage_reference(token, Path(input_root), self.folder,
                                     int(parameters["width"]), int(parameters["height"]))
        self.cwd = guarded(runtime / "bin")
        model_names = [parameters[key] for key in ("diffusion_model", "clip_l", "t5xxl", "vae")]
        if tuple(model_names) != tuple(self.MODEL_FILES):
            raise AppError(503, "WORKFLOW_DEPENDENCIES_MISSING", "Kontext 模板需要经验证的模型文件")
        diffusion, clip_l, t5xxl, vae = (str(guarded(runtime / "models" / name)) for name in model_names)
        self.command = [
            str(guarded(self.cwd / "sd-cli.exe")),
            "--diffusion-model", diffusion, "--clip_l", clip_l, "--t5xxl", t5xxl, "--vae", vae,
            "--prompt-file", str(prompt), "--negative-prompt-file", str(negative),
            "--ref-image", str(ref_image),
            "--output", str(self.output),
            "--width", str(parameters["width"]), "--height", str(parameters["height"]),
            "--seed", str(parameters["seed"]),
            "--steps", str(self.steps), "--cfg-scale", str(parameters["cfg_scale"]),
            "--guidance", str(parameters["guidance"]), "--sampling-method", parameters["sampling_method"],
            "--mmap", "--disable-image-metadata",
            "--backend", f"diffusion=cuda{device},te=cpu,vae=cuda{device}", "--max-vram", "-1",
            "--flash-attn", "auto", "--threads", "6",
        ]

    def on_line(self, line):
        # Keep prompts and subprocess details out of the application log.
        progress = re.search(r"\|\s*(\d+)/(\d+)\b", line)
        if progress and int(progress[2]) == self.steps:
            self.progress = (int(progress[1]), self.steps)
        lower = line.lower()
        if "cuda" in lower and "out of memory" in lower:
            self.gpu_oom = True

    def read_result(self, returncode, limit):
        if returncode != 0 or not self.output.is_file():
            if self.gpu_oom:
                raise AppError(503, "GPU_OOM", "Kontext 显存不足", True)
            raise AppError(503, "ENGINE_EXECUTION_FAILED", "Kontext 出图失败", True)
        with guarded(self.output).open("rb") as stream:
            image = stream.read(limit + 1)
        if len(image) > limit:
            raise AppError(422, "INVALID_IMAGE", "结果图片大小超限")
        return image
