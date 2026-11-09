"""One MOSS-Image job using the pinned Windows stable-diffusion.cpp runtime."""
from pathlib import Path
from uuid import UUID

from localgen.config import atomic_write, guarded
from localgen.engine.sdcpp import (  # noqa: F401  (BIN_FILES 等是再导出)
    BIN_FILES, missing_assets, stage_reference, track)
from localgen.jobs.models import AppError

MODEL_FILES = {
    "MOSS-Image-SFT-Q4_K.gguf": 3327400800,
    "Qwen3.5-9B-Q4_K_M.gguf": 5680522464,
    "flux2-vae.safetensors": 336211292,
    "tokenizer.json": 19989325,
}


class MossJob:
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
            # 与 Kontext 同一套规则：按输出比例裁切再缩放，交给 sd.cpp 的
            # --init-img 使用（不让 sd.cpp 把自己的 init 图拉伸变形）。
            init_image = stage_reference(token, Path(input_root), self.folder,
                                         int(parameters["width"]), int(parameters["height"]))
        else:
            init_image = None
        self.cwd = guarded(runtime / "bin")
        model_names = [parameters[key] for key in ("diffusion_model", "text_encoder", "vae", "tokenizer")]
        if tuple(model_names) != tuple(MODEL_FILES):
            raise AppError(503, "WORKFLOW_DEPENDENCIES_MISSING", "MOSS-Image 模板需要经验证的模型文件")
        diffusion, encoder, vae, tokenizer = (str(guarded(runtime / "models" / name)) for name in model_names)
        self.command = [
            str(guarded(self.cwd / "sd-cli.exe")),
            "--diffusion-model", diffusion, "--llm", encoder, "--vae", vae, "--tokenizer", tokenizer,
            "--prompt-file", str(prompt), "--negative-prompt-file", str(negative), "--output", str(self.output),
            "--width", str(parameters["width"]), "--height", str(parameters["height"]), "--seed", str(parameters["seed"]),
            "--steps", str(self.steps), "--cfg-scale", str(parameters["cfg_scale"]),
            "--flow-shift", str(parameters["flow_shift"]), "--sampling-method", parameters["sampling_method"],
            "--scheduler", parameters["scheduler"], "--mmap", "--disable-image-metadata",
            "--backend", f"diffusion=cuda{device},te=cpu,vae=cuda{device}", "--max-vram", "-1",
            "--flash-attn", "auto", "--threads", "6",
        ]
        if init_image is not None:
            self.command.extend(["--init-img", str(init_image),
                                 "--strength", str(parameters.get("strength", 0.75))])

    def on_line(self, line):
        # Keep prompts and subprocess details out of the application log.
        track(self, line)

    def read_result(self, returncode, limit):
        if returncode != 0 or not self.output.is_file():
            if self.gpu_oom:
                raise AppError(503, "GPU_OOM", "MOSS-Image 显存不足", True)
            raise AppError(503, "ENGINE_EXECUTION_FAILED", "MOSS-Image 出图失败", True)
        with guarded(self.output).open("rb") as stream:
            image = stream.read(limit + 1)
        if len(image) > limit:
            raise AppError(422, "INVALID_IMAGE", "结果图片大小超限")
        return image
