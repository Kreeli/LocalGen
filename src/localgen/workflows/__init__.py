import hashlib
import json
import re
import secrets
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from localgen.config import guarded
from localgen.jobs.models import AppError, Identifier, Submission


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: Identifier
    revision: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    title: str
    engine: Literal["mock", "comfy"]
    runner: Literal["comfy", "moss", "sdcpp"] = "comfy"
    tier: str
    enabled: bool
    template_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    default_size: tuple[int, int]
    verified_sizes: list[tuple[int, int]]
    output_node: str
    inputs: dict[str, tuple[str, str]]
    models: list[tuple[str, str, str]]


class Workflow:
    def __init__(self, directory: Path):
        manifest = guarded(directory / "manifest.toml")
        self.spec = Manifest.model_validate(tomllib.loads(manifest.read_text("utf-8")))
        raw = guarded(directory / "prompt.api.json").read_bytes()
        self.digest = hashlib.sha256(raw).hexdigest()
        self.graph = json.loads(raw)
        spec = self.spec
        if (spec.workflow_id, spec.revision) != (directory.parent.name, directory.name):
            raise ValueError("工作流目录与清单标识不一致")
        if spec.runner in ("moss", "sdcpp") and spec.engine != "comfy":
            raise ValueError("sdcpp runner 需要真实引擎")
        if self.digest != spec.template_sha256:
            raise ValueError("工作流模板摘要不一致")
        if not {"prompt", "width", "height", "seed", "output_prefix"} <= spec.inputs.keys():
            raise ValueError("工作流缺少必填映射")
        if spec.inputs.keys() - {"prompt", "negative_prompt", "width", "height", "seed",
                                 "output_prefix", "image", "strength"}:
            raise ValueError("工作流包含未知映射")
        if (spec.default_size not in spec.verified_sizes and spec.enabled) or any(
            not 0 < side <= 8192 for size in spec.verified_sizes for side in size
        ):
            raise ValueError("工作流默认尺寸或白名单无效")
        self.nodes = {node["class_type"] for node in self.graph.values()}
        if self.graph[spec.output_node]["class_type"] != "SaveImage":
            raise ValueError("输出节点必须是 SaveImage")
        for node, field in spec.inputs.values():
            if field not in self.graph[node]["inputs"]:
                raise ValueError("工作流字段映射无效")
        for node, field, value in spec.models:
            if not re.fullmatch(r"[\w. -]+", value) or self.graph[node]["inputs"][field] != value:
                raise ValueError("工作流模型依赖与模板不一致")

    def dependencies(self, object_info: dict) -> list[str]:
        missing = sorted(self.nodes - object_info.keys())
        for node, field, filename in self.spec.models:
            class_name = self.graph[node]["class_type"]
            options = object_info.get(class_name, {}).get("input", {})
            values = options.get("required", {}).get(field) or options.get("optional", {}).get(field)
            if not values or not isinstance(values[0], list) or filename not in values[0]:
                missing.append(filename)
        return missing

    def parameters(self, request: Submission) -> dict:
        width, height = self.spec.default_size
        if request.width is not None:
            width, height = request.width, request.height
        if request.negative_prompt is not None and "negative_prompt" not in self.spec.inputs:
            raise AppError(422, "UNSUPPORTED_PARAMETER", "此工作流不支持负面提示词")
        if request.image is not None and "image" not in self.spec.inputs:
            raise AppError(422, "UNSUPPORTED_PARAMETER", "此工作流不支持图生图输入")
        if request.strength is not None and "strength" not in self.spec.inputs:
            raise AppError(422, "UNSUPPORTED_PARAMETER", "此工作流不支持重绘强度")
        if request.strength is not None and "image" not in self.spec.inputs:
            raise AppError(422, "UNSUPPORTED_PARAMETER", "重绘强度仅在图生图时可用")
        if self.spec.enabled and (width, height) not in self.spec.verified_sizes:
            raise AppError(422, "UNSUPPORTED_SIZE", "此尺寸未在工作流中启用")
        values = {"prompt": request.prompt, "width": width, "height": height,
                  "seed": request.seed if request.seed is not None else secrets.randbits(32)}
        if "negative_prompt" in self.spec.inputs:
            node, field = self.spec.inputs["negative_prompt"]
            values["negative_prompt"] = (
                request.negative_prompt if request.negative_prompt is not None
                else self.graph[node]["inputs"][field]
            )
        if "image" in self.spec.inputs:
            values["image"] = request.image or ""
        if "strength" in self.spec.inputs:
            node, field = self.spec.inputs["strength"]
            values["strength"] = (request.strength if request.strength is not None
                                  else self.graph[node]["inputs"][field])
        return values

    def render(self, parameters: dict, job_id: str) -> dict:
        graph = json.loads(json.dumps(self.graph))
        values = {**parameters, "output_prefix": f"{job_id}/image"}
        for name, (node, field) in self.spec.inputs.items():
            graph[node]["inputs"][field] = values[name]
        return graph

    def dsh_plugin_graph(self, mode: str = "text2image") -> dict:
        """Render this workflow as a dsh-image-gen (ComfyUI adapter) workflow JSON.

        runner=moss exports contain the LocalGenMossImage marker node and only
        work through the LocalGen /comfyui gateway, never on a real ComfyUI.
        """
        if mode not in ("text2image", "image2image"):
            raise AppError(422, "VALIDATION_ERROR", "未知的导出模式")
        graph = json.loads(json.dumps(self.graph))
        prompt_node, prompt_field = self.spec.inputs["prompt"]
        graph[prompt_node]["inputs"][prompt_field] = "{{prompt}}"
        seed_node, seed_field = self.spec.inputs["seed"]
        graph[seed_node]["inputs"][seed_field] = "{{seed}}"
        prefix_node, prefix_field = self.spec.inputs["output_prefix"]
        graph[prefix_node]["inputs"][prefix_field] = f"dsh-image-gen-{self.spec.workflow_id}"
        width, height = self.spec.default_size
        width_node, width_field = self.spec.inputs["width"]
        height_node, height_field = self.spec.inputs["height"]
        graph[width_node]["inputs"][width_field] = width
        graph[height_node]["inputs"][height_field] = height
        if mode == "image2image":
            if self.spec.runner in ("moss", "sdcpp"):
                if "image" not in self.spec.inputs:
                    raise AppError(422, "EXPORT_UNSUPPORTED", "此工作流不支持图生图")
                image_node, image_field = self.spec.inputs["image"]
                graph[image_node]["inputs"][image_field] = "{{image}}"
            else:
                samplers = [node for node, body in graph.items()
                            if isinstance(body.get("inputs"), dict) and "latent_image" in body["inputs"]]
                if len(samplers) != 1:
                    raise AppError(422, "EXPORT_UNSUPPORTED", "未找到唯一的采样节点，无法生成图生图变体")
                decoders = [body["inputs"]["vae"] for body in graph.values()
                            if body.get("class_type") == "VAEDecode" and "vae" in body.get("inputs", {})]
                if len(decoders) != 1:
                    raise AppError(422, "EXPORT_UNSUPPORTED", "未找到唯一的 VAE 解码来源，无法生成图生图变体")
                sampler = samplers[0]
                graph["dsh-load"] = {"class_type": "LoadImage", "inputs": {"image": "{{image}}"}}
                graph["dsh-scale"] = {"class_type": "ImageScale", "inputs": {
                    "upscale_method": "lanczos", "width": width, "height": height,
                    "crop": "disabled", "image": ["dsh-load", 0]}}
                graph["dsh-encode"] = {"class_type": "VAEEncode", "inputs": {
                    "pixels": ["dsh-scale", 0], "vae": decoders[0]}}
                graph[sampler]["inputs"]["latent_image"] = ["dsh-encode", 0]
                if "denoise" in graph[sampler]["inputs"]:
                    graph[sampler]["inputs"]["denoise"] = 0.6
        for body in graph.values():
            body["_meta"] = {"title": f"dsh-image-gen:{self.spec.workflow_id}:{mode}"}
        return graph


class Registry:
    def __init__(self, root: Path, mode: str):
        self.mode = mode
        self.versions = {}
        self.current = {}
        for manifest in sorted(root.glob("*/*/manifest.toml")):
            workflow = Workflow(manifest.parent)
            spec = workflow.spec
            self.versions[spec.workflow_id, spec.revision] = workflow
            if spec.engine != mode:
                continue
            previous = self.current.get(spec.workflow_id)
            if previous is None or self.version_key(spec.revision) > self.version_key(previous.spec.revision):
                self.current[spec.workflow_id] = workflow

    @staticmethod
    def version_key(value: str):
        return tuple(map(int, value.split(".")))
