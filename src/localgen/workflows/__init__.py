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
    # 必填输入：目前用于声明"这个工作流必须带参考图"（kontext-dev）。
    # 桌面据此锁定生成按钮，API/网关照旧逐项校验。
    required_inputs: list[str] = []
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
                                 "output_prefix", "image", "strength",
                                 "pose_id", "garment_id"}:
            raise ValueError("工作流包含未知映射")
        if set(spec.required_inputs) - spec.inputs.keys():
            raise ValueError("工作流必填输入不在映射表中")
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

    def input_default(self, name: str):
        """模板为该输入写死的默认值；没有映射时返回 None。"""
        mapping = self.spec.inputs.get(name)
        if mapping is None:
            return None
        return self.graph[mapping[0]]["inputs"].get(mapping[1])

    # 引擎侧真正吃到的固定参数（模板里写死、用户改不了的那些）。只读，零迁移。
    ENGINE_FIELDS = ("steps", "cfg_scale", "sampling_method", "scheduler", "flow_shift")

    def engine_parameters(self):
        """步数 / CFG / 采样器 / 调度器 / 模型文件名 —— 回答"这张图是怎么跑出来的"。

        这些值只存在于不可变的 `prompt.api.json`，`/api/v1/workflows` 原本不返回，
        所以历史里说不清"当时用了多少步"。这里按 `workflow_id + revision` 读出来，
        缺字段就跳过（不同 runner 的字段名不一样，不许猜）。
        """
        values = {}
        for node in self.graph.values():
            inputs = node.get("inputs") or {}
            for field in self.ENGINE_FIELDS:
                if field in inputs and field not in values:
                    values[field] = inputs[field]
        models = [filename for _node, _field, filename in self.spec.models]
        if models:
            values["models"] = models
        return values

    def parameters(self, request: Submission) -> dict:
        width, height = self.spec.default_size
        if request.width is not None:
            width, height = request.width, request.height
        if request.negative_prompt is not None and "negative_prompt" not in self.spec.inputs:
            raise AppError(422, "UNSUPPORTED_PARAMETER", "此工作流不支持负面提示词")
        if request.image is not None and "image" not in self.spec.inputs:
            raise AppError(422, "UNSUPPORTED_PARAMETER", "此工作流不支持图生图输入")
        for name, value in (("pose_id", request.pose_id), ("garment_id", request.garment_id)):
            if value is not None and name not in self.spec.inputs:
                raise AppError(422, "UNSUPPORTED_PARAMETER", "此工作流不支持姿势/服装图库")
        if request.strength is not None and "strength" not in self.spec.inputs:
            raise AppError(422, "UNSUPPORTED_PARAMETER", "此工作流不支持重绘强度")
        if request.strength is not None and "image" not in self.spec.inputs:
            raise AppError(422, "UNSUPPORTED_PARAMETER", "重绘强度仅在图生图时可用")
        for name in self.spec.required_inputs:
            if name == "image" and not request.image:
                raise AppError(422, "REQUIRED_INPUT_MISSING", "此工作流必须提供参考图")
            if name in ("pose_id", "garment_id") and not getattr(request, name, None):
                raise AppError(422, "REQUIRED_INPUT_MISSING", "此工作流必须从图库选择素材")
        # 换姿势/换装至少要一张参考图（人像上传或图库选中，满足任一即可）。
        if ("pose_id" in self.spec.inputs or "garment_id" in self.spec.inputs):
            if not (request.image or request.pose_id or request.garment_id):
                raise AppError(422, "REQUIRED_INPUT_MISSING",
                               "换姿势/换装需要人物图、姿势或服装中的至少一项")
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
        for name, given in (("pose_id", request.pose_id), ("garment_id", request.garment_id)):
            if name in self.spec.inputs:
                values[name] = given or ""
        if "strength" in self.spec.inputs:
            node, field = self.spec.inputs["strength"]
            values["strength"] = (request.strength if request.strength is not None
                                  else self.graph[node]["inputs"][field])
        # 模板里那些「不暴露给用户、但引擎要读」的字段：sigmas 这类加速预设就靠它传下去。
        for field in self.ENGINE_FIELDS + ("sigmas", "guidance",
                                           "lora_model_dir", "lora_apply_mode"):
            if field in values:
                continue
            for node in self.graph.values():
                inputs = node.get("inputs") or {}
                if field in inputs:
                    values[field] = inputs[field]
                    break
        return values

    def render(self, parameters: dict, job_id: str) -> dict:
        graph = json.loads(json.dumps(self.graph))
        values = {**parameters, "output_prefix": f"{job_id}/image"}
        for name, (node, field) in self.spec.inputs.items():
            graph[node]["inputs"][field] = values[name]
        # 未映射给用户的引擎字段（sigmas/guidance/steps 等）也要落到节点上：
        # 它们决定实际采样，不能只存在于模板里。
        for field in self.ENGINE_FIELDS + ("sigmas", "guidance",
                                           "lora_model_dir", "lora_apply_mode"):
            if field not in parameters:
                continue
            for node in graph.values():
                inputs = node.get("inputs") or {}
                if field in inputs:
                    inputs[field] = parameters[field]
                    break
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
