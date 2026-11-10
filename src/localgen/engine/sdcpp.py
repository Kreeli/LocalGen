"""Shared sd-cli runner pieces plus the FLUX.1 Kontext dev and Qwen-Image-2.1 jobs."""
import re
from pathlib import Path
from uuid import UUID

from localgen.config import atomic_write, guarded
from localgen.imaging import contain_reference_png, reference_png
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
                         name: str, fit: str = "cover") -> Path:
    """Stage a library picture (pose skeleton / garment sheet) as a reference.

    `fit` decides how a non-square source meets the output frame:

    - "cover"   crop to the output aspect ratio (person photos: filling the frame
                avoids letterbox bars the model would otherwise imitate);
    - "contain" letterbox the whole picture (pose skeletons and garment sheets:
                cropping a 3:4 portrait to 1:1 removes ~25% of the height, i.e.
                the head and the feet, which is exactly the pose information the
                reference exists to carry).
    """
    ref = guarded(Path(folder) / name)
    try:
        content = guarded(source).read_bytes()
        if fit == "contain":
            content = contain_reference_png(content, (int(width), int(height)))
        else:
            content = reference_png(content, (int(width), int(height)))
    except AppError:
        raise
    except Exception:
        raise AppError(400, "INVALID_INPUT_IMAGE",
                       "图库素材不是可解码的图片") from None
    atomic_write(ref, content)
    return ref


# Kontext 是"指令编辑"模型：它认的是 "change X to Y, keep everything else"
# 这种句式。中文短句进 prompt 时，模型经常当文生图处理——输出的构图、主体、
# 光线全换掉，用户看到的就是"提示词完全没生效"。补一段英文角色说明即可，
# 采样参数（guidance / cfg / steps）保持不变，那是验证过的组合。
KONTEXT_ROLE = (
    "The attached image is the source photo to edit, not inspiration.\n"
    "Edit THAT image: keep its composition, framing, subject, pose, background "
    "layout and art style unchanged, and apply only the change described below. "
    "Do not draw a new picture and do not ignore the instruction.\n\n"
)


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
        # 角色说明：sd-cli 只把 --ref-image 当成一张"图"喂给编码器，从不说它是
        # 原图还是素材。只给一句"改成黄昏色调"时，实测模型会把它当文生图的
        # 额外条件、直接重新画一张构图全新的大图——提示词里的改动全被忽略。
        # 补一句英文角色说明（Kontext 的训练语料就是这种写法），明确"照这张
        # 原图改、保留构图"。只动提示词，采样参数一律不碰。
        atomic_write(prompt, (KONTEXT_ROLE + parameters["prompt"]).encode("utf-8"))
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
    """Qwen-Image-2.1 换姿势 / 换装：单段或两段链式 sd-cli。

    单段（只选姿势或只选服装）：人物图 + 一张约束参考图，一次出图。

    两段（姿势 + 服装同时选中）：姿势与服装是两种强度的编辑——换装是局部编辑
    （模型低风险就能满足），换姿势是全局重排（要重画整个人体骨架）；同一次生成里
    两个指令竞争时，模型永远走低风险的那条路，实测表现就是"衣服换了、姿势没换"
    （同种子对照：job cb689751 纯姿势成功 vs cdfd8157 加服装后姿势失效）。
    插件原版（comfyui-aicg3d-PoseSwap）正是两段流水线：先姿势段、再服装段。
    这里把同样的架构做进引擎：第一段（姿势段）人物图 + 骨骼图 → 中间产物
    stage-pose.png；第二段（换装段）拿中间产物当人物图 + 服装参考 → 最终 image.png。
    两段共用同一份 sd-cli 运行时与模型权重，段间由 ComfyEngine.observe_sdcpp
    无缝接续，调度器和 REST/MCP 契约完全无感。

    分段步数：pose_steps / garment_steps（manifest 1.1.0 起声明），默认跟随模板
    steps（8 步配 turbo sigma 预设；步数被改走时引擎自动弃用预设，见
    turbo_safe_sigmas）。两段同用提交种子，保证可复现。姿势 LoRA 只挂姿势段——
    换装段挂它反而会把服装往训练集的样子上拽。
    """

    STAGE_OUTPUT = "stage-pose.png"   # 姿势段产物（两段式才有）
    S2_PREFIX = "s2-"                 # 第二段参考图文件名前缀，避免与第一段互相覆盖

    def __init__(self, job_id, output_root, runtime, parameters, device, input_root,
                 library_root: Path | None = None):
        self.job_id = str(UUID(job_id))
        self.folder = guarded(output_root / self.job_id)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.output = guarded(self.folder / "image.png")
        self.progress = None
        self.stage = "loading"
        self.note = None
        self.gpu_oom = False
        self.eta_marks = []
        self.steps = 8
        self.parameters = parameters
        self.device = device
        self.input_root = input_root
        self.width, self.height = int(parameters["width"]), int(parameters["height"])
        self.cwd = guarded(runtime / "bin")
        self.models_root = guarded(runtime / "models")
        self.stage_index = 0
        self.stages: list[dict] = []
        self.command: list[str] = []

        model_names = [parameters[key] for key in
                       ("diffusion_model", "text_encoder", "vision_encoder", "vae")]
        if tuple(model_names) != tuple(self.MODEL_FILES):
            raise AppError(503, "WORKFLOW_DEPENDENCIES_MISSING", "Qwen-Image-2.1 模板需要经验证的模型文件")
        self.diffusion, self.encoder, self.vision, self.vae = (
            str(guarded(self.models_root / name)) for name in model_names)
        self.lora_dir = parameters.get("lora_model_dir")
        self.lora_mode = parameters.get("lora_apply_mode")

        # 来源解析在提交期一次做完：坏图库 id 不许拖到引擎中途才炸。
        sources: dict[str, dict] = {}
        token = parameters.get("image") or ""
        if token:
            sources["person"] = {"kind": "person", "value": ("token", token)}
        if parameters.get("pose_id"):
            sources["pose"] = {"kind": "pose",
                               "value": self.library_picture(library_root, "poses", parameters["pose_id"]),
                               "entry": self.library_entry(library_root, "poses", parameters["pose_id"])}
        if parameters.get("garment_id"):
            sources["garment"] = {"kind": "garment",
                                  "value": self.library_picture(library_root, "garments", parameters["garment_id"]),
                                  "entry": self.library_entry(library_root, "garments", parameters["garment_id"])}
        if not sources:
            raise AppError(422, "INVALID_INPUT_IMAGE", "换姿势/换装至少需要一张参考图")

        pose_steps = self._stage_steps(parameters, "pose")
        garment_steps = self._stage_steps(parameters, "garment")
        two_stage = "pose" in sources and "garment" in sources

        if not two_stage:
            picked = [sources[kind] for kind in ("person", "pose", "garment") if kind in sources]
            label = ("姿势段" if "pose" in sources
                     else "换装段" if "garment" in sources else "")
            self.stages = [{
                "label": label, "steps": pose_steps if "pose" in sources else garment_steps,
                "sources": picked, "output": self.output,
                "prompt_file": self.folder / "prompt.txt",
                "negative_file": self.folder / "negative.txt",
                "log_name": f"sdcpp-{self.job_id}.log",
                "lora": bool(self.lora_dir), "ref_prefix": "",
            }]
        else:
            self.stages = [
                {"label": "姿势段", "steps": pose_steps,
                 "sources": [sources["person"], sources["pose"]],
                 "output": guarded(self.folder / self.STAGE_OUTPUT),
                 "prompt_file": self.folder / "prompt.txt",
                 "negative_file": self.folder / "negative.txt",
                 "log_name": f"sdcpp-{self.job_id}-s1.log",
                 "lora": bool(self.lora_dir), "ref_prefix": ""},
                {"label": "换装段", "steps": garment_steps,
                 "sources": [sources["garment"]],
                 "output": self.output,
                 "prompt_file": self.folder / "prompt-s2.txt",
                 "negative_file": self.folder / "negative-s2.txt",
                 "log_name": f"sdcpp-{self.job_id}.log",
                 "lora": False, "ref_prefix": self.S2_PREFIX,
                 "person_from_previous": True},
            ]
        self._materialize_stage()

    @staticmethod
    def _stage_steps(parameters, prefix: str) -> int:
        """分段步数：分段值 > 全局 steps > 模板默认（15）。范围在 Submission 已限 1–25。

        用户实测 8 步出图偏模糊，默认提到 15（turbo 预设是固定 8 档表，15 步
        自动回落 euler 常规 schedule）。
        """
        value = parameters.get(f"{prefix}_steps") or parameters.get("steps") or 15
        return max(1, min(25, int(value)))

    def _legend(self, sources: list[dict], person_note: str = "") -> str:
        """参考图角色说明 + 图库语义注入。

        sd-cli 只按顺序编号图片，不告诉模型"第几张管什么"；缺这段说明时模型会把
        骨骼图当原图主体直接复刻（实测）。图库条目的文字语义也在这里注入：
        姿势图是线条人偶，靠姿势名补语义；服装描述是英文服装清单，鞋袜包这类
        小件不点名就不会被替换（实测 SWM-0002 的 mesh bag）。插件原版就是把
        garment_description 接进编辑节点的，这里补上等价通道。
        """
        roles = []
        for index, source in enumerate(sources, start=1):
            if source["kind"] == "person":
                roles.append(f"image {index}: the person to keep "
                             f"(identity, face, hair, art style){person_note}")
            elif source["kind"] == "pose":
                roles.append(f"image {index}: a pose reference ONLY — copy its body pose, "
                             f"limb positions and camera angle; never copy its look, "
                             f"clothing or background")
            elif source["kind"] == "garment":
                roles.append(f"image {index}: a garment reference ONLY — copy what the "
                             f"person wears; never copy its background or its person")
        legend = ("Reference images, in order:\n" + "\n".join(roles)
                  + "\nDo NOT output any reference image itself. Draw a new picture where the "
                    "person from image 1 keeps their identity and adopts the pose / clothing "
                    "shown by the other references.\n\n")
        for source in sources:
            entry = source.get("entry") or {}
            description = str(entry.get("description") or "").strip()
            title = str(entry.get("title") or "").strip()
            if source["kind"] == "pose" and title:
                legend += f"Pose to reproduce: {title}\n"
            if source["kind"] == "garment" and description:
                legend += (f"Outfit to dress the person in (replace ALL of these items): "
                           f"{description}\n")
        return legend

    def _materialize_stage(self):
        """把当前段落成命令：staging 参考图、写分段提示词、拼 sd-cli 命令行。

        两段式的第二段"人物图"是第一段产物（stage-pose.png），只有第一段跑完才
        存在，所以第二段在 advance() 里才 materialize。
        """
        stage = self.stages[self.stage_index]
        sources = list(stage["sources"])
        if stage.get("person_from_previous"):
            sources.insert(0, {"kind": "person",
                               "value": ("file", str(self.stages[self.stage_index - 1]["output"]))})
        person_note = ("; their pose is already final — do not change it"
                       if stage.get("person_from_previous") else "")
        prefix = stage.get("ref_prefix", "")

        references = []
        for index, source in enumerate(sources):
            # 约束图（姿势骨骼 / 服装平铺）加信箱边距（contain），身体任何部分
            # 都不许被裁掉；人物照片照旧填满画框（cover）。
            fit = "cover" if source["kind"] == "person" else "contain"
            name = f"{prefix}ref{index}-{source['kind']}.png"
            if isinstance(source["value"], tuple):
                kind, value = source["value"]
                if kind == "token":
                    references.append(stage_reference(value, Path(self.input_root), self.folder,
                                                      self.width, self.height, name))
                else:
                    references.append(stage_reference_file(Path(value), self.folder,
                                                           self.width, self.height, name, fit))
            else:
                references.append(stage_reference_file(source["value"], self.folder,
                                                       self.width, self.height, name, fit))

        atomic_write(stage["prompt_file"],
                     (self._legend(sources, person_note) + self.parameters["prompt"]).encode("utf-8"))
        atomic_write(stage["negative_file"],
                     self.parameters.get("negative_prompt", "").encode("utf-8"))

        stage_params = {**self.parameters, "steps": stage["steps"]}
        command = [
            str(guarded(self.cwd / "sd-cli.exe")),
            "--diffusion-model", self.diffusion, "--llm", self.encoder, "--vae", self.vae,
            "--prompt-file", str(stage["prompt_file"]),
            "--negative-prompt-file", str(stage["negative_file"]),
            "--output", str(stage["output"]),
            "--width", str(self.width), "--height", str(self.height),
            "--seed", str(self.parameters["seed"]),
            "--steps", str(stage["steps"]), "--cfg-scale", str(self.parameters["cfg_scale"]),
            "--sampling-method", self.parameters["sampling_method"],
            "--mmap", "--disable-image-metadata",
            "--backend", f"diffusion=cuda{self.device},te=cpu,vae=cuda{self.device}",
            "--max-vram", "-1", "--flash-attn", "auto", "--threads", "6",
        ]
        sigmas = turbo_safe_sigmas(stage_params)
        if sigmas:
            command.extend(["--sigmas", sigmas])
        # 姿势 LoRA 只挂姿势段：实测 V1.1（rank 32，base=qwen_image_2）在 8 步 turbo
        # 下能跟随骨骼姿势，代价约 +59 s（253 → 312 s）。换装段不挂——它会把服装
        # 往训练集的样子上拽。
        if stage["lora"] and self.lora_dir:
            command.extend(["--lora-model-dir", str(guarded(self.models_root / self.lora_dir))])
            if self.lora_mode:
                command.extend(["--lora-apply-mode", self.lora_mode])
        for reference in references:
            command.extend(["--ref-image", str(reference)])
        # 多张参考图时给它们编号，避免模型把两张图混成一张的语义。
        if len(references) > 1:
            command.append("--increase-ref-index")
        command.extend(["--llm_vision", self.vision])

        self.command = command
        self.output = stage["output"]
        self.steps = stage["steps"]
        self.log_name = stage["log_name"]

    def advance_ready(self, returncode: int) -> bool:
        """当前段成功且还有下一段 → 引擎应立即接续下一段。段失败时走正常失败路径。"""
        return returncode == 0 and self.stage_index + 1 < len(self.stages)

    def advance(self):
        """姿势段 → 换装段：staging 第二段参考图并拼好新命令，供引擎重新拉起。

        ETA 窗口与进度一并清零——两段的每步耗时不可比，混算会把 ETA 推歪。
        """
        self.stage_index += 1
        self.eta_marks = []
        self.progress = None
        if not self.stages[self.stage_index - 1]["output"].is_file():
            # sd-cli 退出码 0 但没写中间图：比 FileNotFoundError 更能说明问题。
            raise AppError(503, "ENGINE_EXECUTION_FAILED", "姿势段没有产出中间图", True)
        self._materialize_stage()

    def on_line(self, line):
        # track() 只在行匹配进度模式时才更新 note；sd-cli 的其余输出行不动它。
        # 只在 note 真的被刷新时加段落前缀，否则同一句 note 会被重复包上
        # "姿势段 · 姿势段 · …"（真机第一次两段跑实测踩到）。
        previous = self.note
        track(self, line)
        label = self.stages[self.stage_index]["label"]
        if (label and len(self.stages) > 1 and self.note
                and self.note is not previous and not self.note.startswith(f"{label} ·")):
            self.note = f"{label} · {self.note}"

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

    @staticmethod
    def library_entry(library_root: Path | None, kind: str, item_id: str) -> dict:
        """图库条目的元数据（title / description），给提示词注入用。

        与 library_picture 同源（同一个 LibraryStore 缓存），查不到同一套错误码。
        """
        if library_root is None:
            raise AppError(503, "LIBRARY_NOT_INSTALLED", "本机图库不可用", True)
        from localgen.storage.libraries import LibraryStore

        try:
            return LibraryStore(Path(library_root), Path(library_root)).entry(kind, item_id)
        except AppError:
            raise
        except (OSError, ValueError) as exc:
            raise AppError(503, "LIBRARY_NOT_INSTALLED", "本机图库不可用", True) from exc
