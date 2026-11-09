"""Shared sd-cli runner pieces plus the FLUX.1 Kontext dev and Qwen-Image-2.1 jobs."""
import re
from pathlib import Path
from uuid import UUID

from localgen.config import atomic_write, guarded
from localgen.imaging import reference_png
from localgen.jobs.models import AppError

# Shared binary set of the pinned stable-diffusion.cpp runtime (localgen-moss/bin).
BIN_FILES = (
    "sd-cli.exe", "stable-diffusion.dll", "ggml-base.dll", "ggml.dll", "ggml-cuda.dll",
    "cudart64_12.dll", "cublas64_12.dll", "cublasLt64_12.dll",
)


def track(job, line):
    """Fold one sd-cli output line into the job's progress state.

    sd-cli writes everything to one stream: a weight-loading bar
    (`| 340/352 - 1.83GB/s`), then the sampling bar
    (`|===>  |  2/30 - 22.37s/it - ETA 13:10`). The denominator is what tells
    them apart: it only equals the real step count once sampling has started.

    注意分母不等于 `--steps`：图生图时 sd-cli 按 strength 推 `t_enc`，实际只跑
    31 步（`target t_enc is 30 steps`），而加载权重阶段的分母是几百个张量。
    所以判定不能只看 `total == steps`：加载条后面跟的是**吞吐**（`1.83GB/s`），
    采样条后面跟的是**每步耗时**（`22.37s/it` / `1.03it/s`）。两者互斥，按这个分。
    """
    match = re.search(r"\|\s*(\d+)/(\d+)\b(.*)", line)
    if match:
        current, total = int(match[1]), int(match[2])
        tail = match[3]
        # 速率必须紧贴 `- ` 之后：`1.83GB/s` 里也有 `B/s`，不锚定会把它当成 it/s 系。
        rate = re.search(r"-\s*([\d.]+)\s*(s/it|it/s)\b", tail)
        throughput = re.search(r"[\d.]+\s*[kKmMgG]B/s", tail)
        # 兜底只在"既没有速率也没有吞吐"时用；再要求还没跑满，堵死 100% 仍算采样。
        sampling = bool(rate) or (not throughput and total == job.steps and current < total)
        if sampling:
            job.progress = (current, total)
            job.stage = "sampling"
            parts = []
            if rate:
                parts.append(f"{rate[1]}{rate[2]}")
                per = float(rate[1]) if rate[2] == "s/it" else 1.0 / float(rate[1])
                marks = getattr(job, "eta_marks", None)
                if marks is None:
                    marks = job.eta_marks = []
                marks.append(per)
                remaining = _eta(marks, current, total)
                if remaining is not None:
                    parts.append(f"ETA {_clock(remaining)}")
            job.note = " · ".join(parts) or None
        elif job.progress is None:
            # 采样前那几十秒就是加载权重/编码提示词，界面得说明白在干什么。
            job.stage = "loading"
            job.note = f"加载权重 {current}/{total}"
    lower = line.lower()
    if "decoding" in lower and "latents" in lower:
        # 采样结束后 sd-cli 先解码 VAE（约 9 秒），日志里明写这一步，别让界面停在"采样 30/30"。
        if job.progress is not None:
            job.stage = "decoding"
            job.note = None
    elif "decoded," in lower:
        # 同样的守卫：sd-cli 在加载阶段也会"解码"参考图，那时还没开始采样，
        # 说成"解码阶段"会骗人（两个分支必须一样对待，否则前后不对称）。
        if job.progress is not None:
            timing = re.search(r"taking\s+([\d.]+)s", line)
            job.stage = "decoding"
            # 只留时长：界面自己会加"解码"这个阶段名，别在这里重复一遍。
            job.note = f"{float(timing[1]):.1f} s" if timing else None
    if _out_of_memory(lower):
        job.gpu_oom = True


# sd.cpp / ggml 报显存不足有好几种措辞，没有一种是统一的：
#   - CUDA 后端：`CUDA error: out of memory`
#   - ggml 分配器：`failed to allocate ... out of memory`（不含 "cuda"）
#   - sd.cpp 自己的显存管理器：`cannot make enough memory available on CUDA0`
#     `failed during workspace capacity check`（6 GB 卡跑 768×1024 + 参考图就是这条）
OOM_PATTERNS = ("cannot make enough memory available", "workspace capacity check",
                "allocated capacity check")
OOM_HINTS = ("cuda", "vram", "ggml_backend", "alloc")


def _out_of_memory(lower):
    if any(pattern in lower for pattern in OOM_PATTERNS):
        return True
    return "out of memory" in lower and any(hint in lower for hint in OOM_HINTS)


def _eta(marks, current, total):
    """剩余秒数：丢掉首步冷启动，用近 8 步均值推；不足 2 笔就不猜。

    sd-cli 自己打的 `ETA` 是按**累计均值**推的，首帧那一笔（冷启动 4.5 s/it）
    会把 ETA 抬到真实值的两倍以上（实测 +111%）。首步剔除后误差落到 10 s 内。
    """
    window = marks[1:][-8:]
    if len(window) < 2 or total <= current:
        return None
    return sum(window) / len(window) * (total - current)


def _clock(seconds):
    return f"{int(seconds) // 60:02d}:{int(seconds) % 60:02d}"


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


def reference_source(token: str, input_root: Path) -> Path:
    """Validate the staged-image token and return the uploaded file."""
    if not isinstance(token, str) or not re.match(r"^[0-9a-f]{32}\.(png|jpg|webp)$", token):
        raise AppError(422, "INVALID_INPUT_IMAGE", "输入图引用无效")
    source = guarded(Path(input_root) / token)
    if not source.is_file():
        raise AppError(422, "INPUT_IMAGE_MISSING", "输入图不存在或已过期，请重新上传")
    return source


def stage_reference(token: str, input_root: Path, folder: Path, width: int, height: int,
                    name: str = "input.png") -> Path:
    """Copy the staged input image into the private task folder as a reference.

    The image is cropped to the output aspect ratio first (no stretching) and
    then resized to the output size: sd.cpp keeps reference images at their
    native size, so a 12-megapixel phone photo would otherwise need gigabytes
    of latent VRAM, and a mismatched aspect ratio would get squashed.

    `name` lets one task stage several references side by side (person.png /
    pose.png); the default keeps every existing caller unchanged.
    """
    source = reference_source(token, input_root)
    ref = guarded(Path(folder) / name)
    try:
        content = reference_png(source.read_bytes(), (int(width), int(height)))
    except AppError:
        raise
    except Exception:
        raise AppError(400, "INVALID_INPUT_IMAGE", "参考图不是可解码的 PNG/JPEG/WebP 图片") from None
    atomic_write(ref, content)
    return ref


def stage_reference_file(source: Path, folder: Path, width: int, height: int,
                         name: str) -> Path:
    """Same staging rules, but the source is a local file (a picture pulled
    straight from the pose/garment library instead of an uploaded token)."""
    ref = guarded(Path(folder) / name)
    try:
        content = reference_png(guarded(source).read_bytes(), (int(width), int(height)))
    except AppError:
        raise
    except Exception:
        raise AppError(400, "INVALID_INPUT_IMAGE",
                       "图库素材不是可解码的图片") from None
    atomic_write(ref, content)
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
        self.stage = "loading"
        self.note = None
        self.steps = parameters["steps"]
        self.gpu_oom = False
        self.eta_marks = []
        prompt = self.folder / "prompt.txt"
        negative = self.folder / "negative.txt"
        atomic_write(prompt, parameters["prompt"].encode("utf-8"))
        atomic_write(negative, parameters.get("negative_prompt", "").encode("utf-8"))
        token = parameters.get("image") or ""
        ref_image = stage_reference(token, Path(input_root), self.folder,
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
        track(self, line)

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


class QwenImageJob:
    """One Qwen-Image-2.1 job: Qwen3-VL-8B text encoder, unified t2i + editing.

    Editing is instruction-based: the reference image goes in via --ref-image and
    the vision projector (--llm_vision) feeds it to the Qwen3-VL encoder. There is
    no strength parameter; the prompt describes the change. Optional `sigmas`
    (template key) selects a schedule preset such as qwen-image-2.1-turbo, which
    is only meaningful with the Turbo diffusion weights.
    """

    MODEL_FILES = {
        "qwen_image_2.1-Q4_K.gguf": 4197494816,
        "Qwen3VL-8B-Instruct-Q4_K_M.gguf": 5027784800,
        "mmproj-Qwen3VL-8B-Instruct-F16.gguf": 1159029824,
        "qwen_image_2.1_vae_bf16.safetensors": 675509688,
    }

    def __init__(self, job_id, output_root, runtime, parameters, device, input_root):
        self.job_id = str(UUID(job_id))
        self.folder = guarded(output_root / self.job_id)
        self.folder.mkdir(exist_ok=True)
        self.output = guarded(self.folder / "image.png")
        self.progress = None
        self.stage = "loading"
        self.note = None
        self.steps = parameters["steps"]
        self.gpu_oom = False
        self.eta_marks = []
        prompt = self.folder / "prompt.txt"
        negative = self.folder / "negative.txt"
        atomic_write(prompt, parameters["prompt"].encode("utf-8"))
        atomic_write(negative, parameters.get("negative_prompt", "").encode("utf-8"))
        token = parameters.get("image") or ""
        if token:
            # Same crop-then-scale rule as Kontext/MOSS: sd.cpp encodes the
            # reference at its native size, so stage it at the output size.
            ref_image = stage_reference(token, Path(input_root), self.folder,
                                       int(parameters["width"]), int(parameters["height"]))
        else:
            ref_image = None
        self.cwd = guarded(runtime / "bin")
        model_names = [parameters[key] for key in
                       ("diffusion_model", "text_encoder", "vision_encoder", "vae")]
        if tuple(model_names) != tuple(self.MODEL_FILES):
            raise AppError(503, "WORKFLOW_DEPENDENCIES_MISSING", "Qwen-Image-2.1 模板需要经验证的模型文件")
        diffusion, encoder, vision, vae = (str(guarded(runtime / "models" / name)) for name in model_names)
        self.command = [
            str(guarded(self.cwd / "sd-cli.exe")),
            "--diffusion-model", diffusion, "--llm", encoder, "--vae", vae,
            "--prompt-file", str(prompt), "--negative-prompt-file", str(negative),
            "--output", str(self.output),
            "--width", str(parameters["width"]), "--height", str(parameters["height"]),
            "--seed", str(parameters["seed"]),
            "--steps", str(self.steps), "--cfg-scale", str(parameters["cfg_scale"]),
            "--sampling-method", parameters["sampling_method"],
            "--mmap", "--disable-image-metadata",
            "--backend", f"diffusion=cuda{device},te=cpu,vae=cuda{device}", "--max-vram", "-1",
            "--flash-attn", "auto", "--threads", "6",
        ]
        sigmas = turbo_safe_sigmas(parameters)
        if sigmas:
            self.command.extend(["--sigmas", sigmas])
        if ref_image is not None:
            self.command.extend(["--ref-image", str(ref_image), "--llm_vision", vision])

    def on_line(self, line):
        # Keep prompts and subprocess details out of the application log.
        track(self, line)

    def read_result(self, returncode, limit):
        if returncode != 0 or not self.output.is_file():
            if self.gpu_oom:
                raise AppError(503, "GPU_OOM", "Qwen-Image-2.1 显存不足", True)
            raise AppError(503, "ENGINE_EXECUTION_FAILED", "Qwen-Image-2.1 出图失败", True)
        with guarded(self.output).open("rb") as stream:
            image = stream.read(limit + 1)
        if len(image) > limit:
            raise AppError(422, "INVALID_IMAGE", "结果图片大小超限")
        return image


def turbo_safe_sigmas(parameters) -> str | None:
    """qwen-image-2.1-turbo 是固定 8 档 sigma 表，步数不是 8 时会错拍。

    用户可经 Submission.steps 覆盖步数（上限 25）；模板默认 8 步配 turbo。
    步数被改走时丢掉预设、回落 euler 默认 schedule——慢一点，但不会出坏图。
    """
    sigmas = parameters.get("sigmas")
    if not sigmas:
        return None
    if "turbo" in str(sigmas) and int(parameters.get("steps") or 8) != 8:
        return None
    return str(sigmas)


class PoseSwapJob(QwenImageJob):
    """Qwen-Image-2.1 with TWO references: one locks identity, one locks pose.

    Measured on this machine (RTX 4050 6 GB, Q4_K, 768², 20 steps):

    - both orders work: the model matches content, not position — the skeleton
      figure is read as the pose and the photo as the person either way;
    - pose-first is FASTER (415 s vs 571 s), so that is the default here.

    Steps stay at the caller's value (20 by default): going higher only costs
    time, the pose is already following at 20.
    """

    def __init__(self, job_id, output_root, runtime, parameters, device, input_root,
                 library_root: Path | None = None):
        self.job_id = str(UUID(job_id))
        self.folder = guarded(output_root / self.job_id)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.output = guarded(self.folder / "image.png")
        self.progress = None
        self.stage = "loading"
        self.note = None
        self.steps = parameters["steps"]
        self.gpu_oom = False
        self.eta_marks = []
        prompt = self.folder / "prompt.txt"
        negative = self.folder / "negative.txt"
        atomic_write(prompt, parameters["prompt"].encode("utf-8"))
        atomic_write(negative, parameters.get("negative_prompt", "").encode("utf-8"))

        width, height = int(parameters["width"]), int(parameters["height"])
        self.sources: list[Path] = []
        # 顺序有意义：第一条 --ref-image 建立 prefix cache，姿势图 token 少、建得更省。
        if parameters.get("pose_id"):
            self.sources.append(self.library_picture(library_root, "poses", parameters["pose_id"]))
        if parameters.get("garment_id"):
            self.sources.append(self.library_picture(library_root, "garments", parameters["garment_id"]))
        token = parameters.get("image") or ""
        if token:
            self.sources.append(("token", token))
        if not self.sources:
            raise AppError(422, "INVALID_INPUT_IMAGE", "换姿势/换装至少需要一张参考图")

        references = []
        for index, source in enumerate(self.sources):
            if isinstance(source, tuple):
                references.append(stage_reference(source[1], Path(input_root), self.folder,
                                                  width, height, f"ref{index}-person.png"))
            else:
                references.append(stage_reference_file(source, self.folder, width, height,
                                                       f"ref{index}-library.png"))

        self.cwd = guarded(runtime / "bin")
        model_names = [parameters[key] for key in
                       ("diffusion_model", "text_encoder", "vision_encoder", "vae")]
        if tuple(model_names) != tuple(self.MODEL_FILES):
            raise AppError(503, "WORKFLOW_DEPENDENCIES_MISSING", "Qwen-Image-2.1 模板需要经验证的模型文件")
        diffusion, encoder, vision, vae = (str(guarded(runtime / "models" / name)) for name in model_names)
        self.command = [
            str(guarded(self.cwd / "sd-cli.exe")),
            "--diffusion-model", diffusion, "--llm", encoder, "--vae", vae,
            "--prompt-file", str(prompt), "--negative-prompt-file", str(negative),
            "--output", str(self.output),
            "--width", str(width), "--height", str(height),
            "--seed", str(parameters["seed"]),
            "--steps", str(self.steps), "--cfg-scale", str(parameters["cfg_scale"]),
            "--sampling-method", parameters["sampling_method"],
            "--mmap", "--disable-image-metadata",
            "--backend", f"diffusion=cuda{device},te=cpu,vae=cuda{device}", "--max-vram", "-1",
            "--flash-attn", "auto", "--threads", "6",
        ]
        sigmas = turbo_safe_sigmas(parameters)
        if sigmas:
            self.command.extend(["--sigmas", sigmas])
        # 姿势 LoRA：sd-cli 扫目录自动加载，不需要点名文件。
        # 实测 V1.1（rank 32，base=qwen_image_2）在 8 步 turbo 下能跟随骨骼姿势，
        # 代价约 +59 s（253 → 312 s），换取更稳的服装/背景保持。
        if parameters.get("lora_model_dir"):
            self.command.extend(["--lora-model-dir",
                                 str(guarded(runtime / "models" / parameters["lora_model_dir"]))])
            if parameters.get("lora_apply_mode"):
                self.command.extend(["--lora-apply-mode", str(parameters["lora_apply_mode"])])
        for reference in references:
            self.command.extend(["--ref-image", str(reference)])
        # 多张参考图时给它们编号，避免模型把两张图混成一张的语义。
        if len(references) > 1:
            self.command.append("--increase-ref-index")
        self.command.extend(["--llm_vision", vision])

    @staticmethod
    def library_picture(library_root: Path | None, kind: str, item_id: str) -> Path:
        """Resolve a pose/garment id to its picture, or fail with a clear code."""
        if library_root is None:
            raise AppError(503, "LIBRARY_NOT_INSTALLED", "本机图库不可用", True)
        from localgen.storage.libraries import LibraryStore

        try:
            return LibraryStore(Path(library_root), Path(library_root)).source_path(kind, item_id)
        except AppError:
            raise
        except (OSError, ValueError) as exc:
            raise AppError(503, "LIBRARY_NOT_INSTALLED", "本机图库不可用", True) from exc
