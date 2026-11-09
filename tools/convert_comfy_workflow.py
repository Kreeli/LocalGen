"""把别人（第三方）的 ComfyUI 工作流转成 LocalGen 能跑的本地工作流。

存在的理由
----------
用户从网上拿到别人的 ComfyUI 工作流 JSON，但对方用的是 bf16 / int8 全精度权重和
我们没有的自定义节点。本机是 RTX 4050 / 6 GB 显存，只有 Q4_K 量化版和固定几个
sd-cli 运行时。所以「转换」不是逐节点搬运，而是：

    看懂对方用的是什么类别的模型 → 换成我们手头同类别的那一份 → 把能搬的参数搬过来
    → 把搬不了的东西明确写进报告

三条硬纪律（违反任何一条就拒绝，绝不产出"能转但跑不起来"的工作流）
----------------------------------------------------------------
1. **模型按类别匹配，不按文件名**。`qwen_image_2.1_bf16.safetensors`、
   `qwen_image_2.1_int8_convrot.safetensors`、`qwen_image_2.1-Q4_K.gguf` 是同一类别，
   都能落到我们的 `qwen_image_2.1-Q4_K.gguf`。反过来，`flux2` / `sd3.5` 我们一分钱
   权重都没有，必须拒绝而不是硬凑。
2. **两种落点分得很清楚**：
   - `runner = "sdcpp"`：我们不用 ComfyUI 跑，模板是 LocalGen 自己的两节点图
     （`LocalGen*` 派发节点 + SaveImage）。所以**对方用了什么 ComfyUI 节点都无所谓**，
     我们只继承"意图 + 参数"。
   - `runner = "comfy"`：真在 ComfyUI 里跑，节点必须本机都有、模型文件必须本机都在，
     否则跑不起来 → 拒绝。
3. **降级必须留痕**。分辨率、步数、CFG、采样器、被丢掉的节点链，全部进报告。

用法
----
    # 转换（默认写进 workflows/，可以直接被 LocalGen 扫到）
    python tools/convert_comfy_workflow.py 别人的工作流.json

    # 只看会怎么转、不改盘
    python tools/convert_comfy_workflow.py 别人的工作流.json --dry-run

    # 指定输出根与 id
    python tools/convert_comfy_workflow.py x.json --root workflows --workflow-id my-wf --json

输入支持两种格式（ComfyUI 自己也有两套）：
- **API 格式**：`{"1": {"class_type": ..., "inputs": {...}}, ...}`（/prompt 吃的那种）
- **UI 格式**：`{"nodes": [...], "links": [...], "definitions": {"subgraphs": [...]}}`
  （从 ComfyUI 界面「导出」出来的那种，官方模板和绝大多数网上分享的工作流都是它）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# 一、本机能跑什么（能力表）
#
# 每条都对应一个已验收的工作流目录；`job` 是 sd-cli 的派发 class_type
# （见 src/localgen/engine/comfy.py 的 SDCPP_JOBS）。
# `sizes` 必须和该工作流 manifest 的 verified_sizes 一致，否则转出来的东西
# 会在 Workflow.parameters() 里被 422 掉。
# ---------------------------------------------------------------------------

SDCPP_TARGETS = {
    "qwen-image-21": {
        "job": "LocalGenQwenImage21",
        "title": "Qwen-Image-2.1 · GGUF",
        "revision": "1.0.0",
        "sizes": [(1024, 1024)],
        "steps": (4, 40, 30),        # (下限, 上限, 默认)
        "cfg": (1.0, 8.0, 6.0),
        "samples": ("euler",),
        "negative": True,
        "ref_image": True,           # 可选单张参考图（指令式编辑）
        "library": False,
        # 源步数 > turbo_steps 时切「少步 + turbo sigma 预设」（实测 −40%，画质无差别）
        "turbo_steps": 8,
        "turbo_opt_in": True,
        "speed_note": "1024² 文生图约 338 s（8 步 turbo 约 253 s）/ 单图编辑约 604–835 s",
    },
    "pose-swap": {
        "job": "LocalGenPoseSwap",
        "title": "换姿势 / 换服装 · Qwen-Image-2.1",
        "revision": "1.0.0",
        "sizes": [(768, 768)],
        "steps": (4, 20, 8),
        "cfg": (1.0, 6.0, 4.0),
        "samples": ("euler",),
        "negative": True,
        "ref_image": True,
        "library": True,             # 可以吃图库的 pose_id / garment_id
        "speed_note": "768² 8 步 turbo 约 253 s（挂姿势 LoRA 约 312 s）",
    },
    "kontext-dev": {
        "job": "LocalGenKontextImage",
        "title": "FLUX.1 Kontext dev · GGUF",
        "revision": "1.0.0",
        "sizes": [(1024, 1024)],
        "steps": (4, 30, 20),
        "cfg": (1.0, 4.0, 1.0),
        "samples": ("euler",),
        "negative": True,
        "ref_image": True,
        "library": False,
        "requires_image": True,      # manifest 的 required_inputs = ["image"]
        "speed_note": "1024² 20 步约 377 s",
    },
    "moss-image": {
        "job": "LocalGenMossImage",
        "title": "MOSS-Image SFT · GGUF",
        "revision": "1.1.0",
        "sizes": [(512, 512), (768, 768), (768, 1024), (1024, 768), (1024, 1024)],
        "steps": (10, 50, 50),
        "cfg": (1.0, 5.0, 3.0),
        "samples": ("euler",),
        "negative": True,
        "ref_image": True,
        "strength": True,
        "library": False,
        "speed_note": "768² 约 115 s / 1024² 约 216 s（吃满 16 GB 内存）",
    },
}

# runner=comfy 的落点：真在 ComfyUI 0.3.76 里跑，所以节点与模型都必须本机齐备。
COMFY_TARGETS = {
    "z-image-turbo": {
        "title": "Z-Image-Turbo · GGUF",
        "revision": "1.0.0",
        "sizes": [(512, 512), (768, 768), (768, 1024), (1024, 768), (1024, 1024)],
        "models": ["z-image-turbo-Q4_K_M.gguf", "Qwen3-4B-Q4_K_M.gguf", "ae.safetensors"],
        "speed_note": "768² 约 28 s / 1024² 约 47 s",
    },
    "sd15": {
        "title": "Stable Diffusion 1.5",
        "revision": "1.0.0",
        "sizes": [(512, 512), (512, 768), (768, 512)],
        "models": ["v1-5-pruned-emaonly-fp16.safetensors"],
        "speed_note": "512² 冷启 19 s / 热 5 s",
    },
    "sdxl-lightning": {
        "title": "SDXL-Lightning · 4 步",
        "revision": "1.0.0",
        "sizes": [(512, 512), (768, 768), (1024, 1024)],
        "models": ["sdxl_lightning_4step.safetensors"],
        "speed_note": "512² 冷启 43 s / 热 5 s",
    },
}

# ---------------------------------------------------------------------------
# 二、模型「类别」识别
#
# 只认类别，不认精度。文件名里的 bf16 / fp16 / int8 / fp8 / Q4_K / Q8_0 一律吃掉。
# 顺序重要：先判更具体的（edit_2511 要在 qwen_image 之前）。
# ---------------------------------------------------------------------------

FAMILIES = (
    # (正则, 类别, 说明)
    # 「编解码器」不是扩散模型族：先摘出来，否则 qwen3vl 会被算成 qwen，让整条判定走错。
    (r"qwen[._-]?\d([._-]?\d)?[._-]?(vl|4b|8b|9b)", "text_encoder", "Qwen 系文本/视觉编码器"),
    (r"^t5|t5xxl|t5-v1", "text_encoder", "T5 文本编码器"),
    (r"^clip[._-]?l|^clip[._-]?g|openclip", "text_encoder", "CLIP 文本编码器"),
    (r"vae|^ae[._-]|tae[sf]", "vae", "VAE"),
    (r"qwen[_-]?image[_-]?edit[_-]?2511|qwenimageedit2511", "qwen_edit_2511", "Qwen-Image-Edit-2511"),
    (r"qwen[_-]?image[_-]?edit[_-]?2509|qwenimageedit2509", "qwen_edit_2509", "Qwen-Image-Edit-2509"),
    (r"qwen[_-]?image[_-]?2[._-]?1", "qwen21", "Qwen-Image-2.1"),
    (r"qwen[_-]?image[_-]?edit", "qwen_edit", "Qwen-Image-Edit"),
    (r"qwen[_-]?image", "qwen_image_v1", "Qwen-Image（一代）"),
    (r"flux[._-]?1?[._-]?kontext|kontext", "kontext", "FLUX.1 Kontext"),
    (r"flux[._-]?2", "flux2", "FLUX.2"),
    (r"flux[._-]?1?[._-]?(dev|schnell|krea)|^flux", "flux1", "FLUX.1 dev/schnell"),
    (r"moss[_-]?image|moss", "moss", "MOSS-Image"),
    (r"z[_-]?image", "zimage", "Z-Image / Z-Image-Turbo"),
    (r"sd[._-]?3[._-]?5|sd3\.5|stable[_-]?diffusion[_-]?3", "sd3", "SD 3.5"),
    (r"sd[._-]?xl|sdxl|sd_xl", "sdxl", "SDXL"),
    (r"v1[._-]5|sd[._-]?1[._-]?5|stable[_-]?diffusion[_-]?1[._-]?5", "sd15", "SD 1.5"),
    (r"^wan|wan[._-]?\d", "wan", "Wan 视频模型"),
    (r"chrono|omnigen|mage[_-]?flow|boogu|firered|longcat|ming[_-]?image|joyai|hunyuan", "other", "其它模型族"),
)


# 这些家族不是「用来出图的扩散模型」，识别出来只用于说明，不参与落点选择。
NON_DIFFUSION = {"text_encoder", "vae", "lora"}


# 类别 → 落点。`None` 表示本机没有替代品，必须拒绝。
FAMILY_TARGET = {
    "qwen21": ("sdcpp", "qwen-image-21", None),
    "qwen_edit": ("sdcpp", "qwen-image-21", "对方是 Qwen-Image-Edit 系列，我们用 Qwen-Image-2.1 顶替：同为"
                                               "指令式编辑模型，但权重代次不同，细节风格会有差异"),
    "qwen_edit_2509": ("sdcpp", "qwen-image-21", "对方是 Qwen-Image-Edit-2509，本机没有该权重，改用 "
                                                  "Qwen-Image-2.1（同类别指令式编辑，代次不同）"),
    "qwen_edit_2511": ("sdcpp", "qwen-image-21", "对方是 Qwen-Image-Edit-2511，本机没有该权重，改用 "
                                                  "Qwen-Image-2.1；实测 2.1 能完成换装/姿势跟随，但速度与细节不同"),
    "qwen_image_v1": ("sdcpp", "qwen-image-21", "对方是 Qwen-Image 一代，本机只有 2.1；同族升级替换，"
                                                 "提示词理解更强，但输出风格会变"),
    "kontext": ("sdcpp", "kontext-dev", None),
    "moss": ("sdcpp", "moss-image", None),
    "zimage": ("comfy", "z-image-turbo", None),
    "sdxl": ("comfy", "sdxl-lightning", "对方是 SDXL（可能含自定义 LoRA/refiner），本机只有 SDXL-Lightning "
                                        "4 步权重；LoRA 无法继承，风格会变"),
    "sd15": ("comfy", "sd15", None),
    "flux2": (None, None, "FLUX.2 权重约 20 GB 起，6 GB 显存装不下，本机也没有该权重"),
    "flux1": (None, None, "FLUX.1 dev/schnell 权重（约 12–24 GB）本机没有，改用 Kontext 会丢掉"
                          "纯文生图能力，不属于同类别替代"),
    "sd3": (None, None, "SD 3.5 权重本机没有"),
    "wan": (None, None, "Wan 是视频模型，LocalGen 只做单张静态图"),
    "other": (None, None, "本机没有同类别模型"),
    "unknown": (None, None, "无法从模型文件名判断类别"),
    # 下面三类不是扩散模型，识别出来只是为了让报告说清楚；不会走到落点决策
    "text_encoder": (None, None, "文本编码器不是扩散模型，随主模型一起替换"),
    "vae": (None, None, "VAE 不是扩散模型，随主模型一起替换"),
    "lora": (None, None, "LoRA 是适配器，不是扩散模型"),
}

# 姿势 LoRA：任何版本/精度都落到本机这一份（rank 32，base=qwen_image_2）
POSE_LORA_RE = re.compile(r"vnccs|pose[_-]?studio|posestudio", re.I)

# ---------------------------------------------------------------------------
# 三、节点知识
#
# 只写「参数提取要用到」和「判定要拦住」的部分，不做全量节点库。
# WIDGETS 的顺序是 ComfyUI 各节点的 widgets_values 顺序（界面导出格式没有字段名，
# 只能靠位置；这份顺序来自本机 0.3.76 的节点定义，长期稳定）。
# ---------------------------------------------------------------------------

WIDGETS = {
    "UNETLoader": ["unet_name", "weight_dtype"],
    "UnetLoaderGGUF": ["unet_name"],
    "CheckpointLoaderSimple": ["ckpt_name"],
    "CheckpointLoader": ["config_name", "ckpt_name"],
    "CLIPLoader": ["clip_name", "type", "device"],
    "CLIPLoaderGGUF": ["clip_name", "type"],
    "DualCLIPLoader": ["clip_name1", "clip_name2", "type", "device"],
    "VAELoader": ["vae_name"],
    "LoraLoaderModelOnly": ["lora_name", "strength_model"],
    "LoraLoader": ["lora_name", "strength_model", "strength_clip"],
    "CLIPTextEncode": ["text"],
    "KSampler": ["seed", "control_after_generate", "steps", "cfg", "sampler_name", "scheduler", "denoise"],
    "KSamplerAdvanced": ["add_noise", "noise_seed", "control_after_generate", "steps", "cfg",
                         "sampler_name", "scheduler", "start_at_step", "end_at_step",
                         "return_with_leftover_noise"],
    "EmptyLatentImage": ["width", "height", "batch_size"],
    "EmptySD3LatentImage": ["width", "height", "batch_size"],
    "SaveImage": ["filename_prefix"],
    "LoadImage": ["image", "upload"],
    "ImageScale": ["upscale_method", "width", "height", "crop"],
    "CFGGuider": ["cfg"],
    "BasicScheduler": ["scheduler", "steps", "denoise"],
    "KSamplerSelect": ["sampler_name"],
    "RandomNoise": ["noise_seed"],
    "ModelSamplingFlux": ["max_shift", "base_shift", "width", "height"],
    "ModelSamplingAuraFlow": ["shift"],
    "FluxGuidance": ["guidance"],
    "ConditioningZeroOut": [],
    "VAEDecode": [],
    "VAEEncode": [],
    "ReferenceLatent": [],
    "FluxKontextImageScale": [],
    "FluxKontextMultiReferenceLatentMethod": ["reference_latent_method"],
    "ImageStitch": ["direction", "match_image_size", "spacing_width", "spacing_color"],
}

# 只影响界面、不影响出图的节点：转换时可以静默丢弃。
BENIGN = {
    "PreviewImage", "PreviewAny", "MarkdownNote", "Note", "ShowText", "ShowAnything",
    "PrimitiveStringMultiline", "PrimitiveString", "PrimitiveInt", "PrimitiveFloat",
    "PrimitiveBoolean", "StringConcatenate", "ResolutionSelector", "SaveImageAdvanced",
    "Reroute", "PrimitiveNode",
}

# 语义上搬不动的管线：一出现就必须拒绝（这些不是"少个节点"，是"少个能力"）。
BLOCKING = {
    "ControlNetLoader": "ControlNet 结构控制，sd-cli 侧没有对应输入",
    "ControlNetApplyAdvanced": "ControlNet 结构控制，sd-cli 侧没有对应输入",
    "ControlNetApply": "ControlNet 结构控制，sd-cli 侧没有对应输入",
    "InpaintModelConditioning": "inpaint 专用 conditioning，我们的工作流没有遮罩通道",
    "SetLatentNoiseMask": "遮罩重绘，我们的工作流没有遮罩通道",
    "ImageCompositeMasked": "遮罩合成，我们的工作流没有遮罩通道",
    "SaveVideo": "视频输出，LocalGen 只做单张静态图",
    "CreateVideo": "视频输出，LocalGen 只做单张静态图",
    "SaveAnimatedWEBP": "动图输出，LocalGen 只做单张静态图",
    "SaveAudio": "音频输出",
    "VHS_VideoCombine": "视频输出，LocalGen 只做单张静态图",
}

# 出图节点：命中任何一个就把这张图当成品。
OUTPUT_NODES = {"SaveImage", "SaveImageAdvanced"}

LOADER_MODEL_FIELDS = {
    "unet_name": "diffusion", "ckpt_name": "diffusion",
    "clip_name": "text_encoder", "clip_name1": "text_encoder", "clip_name2": "text_encoder",
    "vae_name": "vae",
    "lora_name": "lora",
}

IMAGE_SOURCE_NODES = {"LoadImage", "LoadImageMask", "LoadImageOutput", "LoadImageFromUrl"}


# ---------------------------------------------------------------------------
# 四、数据结构
# ---------------------------------------------------------------------------

class ConvertError(Exception):
    """转换被拒。`reason` 是给用户看的人话，`code` 是给程序判定的稳定码。"""

    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


@dataclass
class Report:
    source: str = ""
    source_format: str = ""
    workflow_id: str = ""
    accepted: bool = False
    code: str = ""
    reason: str = ""
    target: str = ""
    runner: str = ""
    job_class: str = ""
    family: str = ""
    family_label: str = ""
    models: list = field(default_factory=list)      # 源里的模型文件（角色 + 名字）
    mapped_models: list = field(default_factory=list)  # 落到本机的文件名
    parameters: dict = field(default_factory=dict)  # 最终写进模板的引擎参数
    inherited: list = field(default_factory=list)   # 从源里搬过来的
    degraded: list = field(default_factory=list)    # 降级/替换，必须告知
    dropped: list = field(default_factory=list)     # 丢掉的节点链，必须告知
    node_types: list = field(default_factory=list)
    unknown_nodes: list = field(default_factory=list)
    unsupported_nodes: list = field(default_factory=list)
    output_path: str = ""

    def note_degraded(self, line: str):
        """降级说明去重：同一件事被多轮计算碰到时只报一次。"""
        if line not in self.degraded:
            self.degraded.append(line)

    def note_inherited(self, line: str):
        if line not in self.inherited:
            self.inherited.append(line)

    def note_dropped(self, line: str):
        if line not in self.dropped:
            self.dropped.append(line)

    def as_dict(self) -> dict:
        return {
            "source": self.source, "source_format": self.source_format,
            "accepted": self.accepted, "code": self.code, "reason": self.reason,
            "workflow_id": self.workflow_id, "target": self.target, "runner": self.runner,
            "job_class": self.job_class, "family": self.family, "family_label": self.family_label,
            "models": self.models, "mapped_models": self.mapped_models,
            "parameters": self.parameters, "inherited": self.inherited,
            "degraded": self.degraded, "dropped": self.dropped,
            "node_types": self.node_types, "unknown_nodes": self.unknown_nodes,
            "unsupported_nodes": self.unsupported_nodes, "output_path": self.output_path,
        }


# ---------------------------------------------------------------------------
# 五、读入：两种格式统一成 {node_id: {class_type, inputs}}
# ---------------------------------------------------------------------------

def _is_api_format(data) -> bool:
    return (isinstance(data, dict) and data
            and all(isinstance(v, dict) and "class_type" in v for v in data.values()))


def _links_of(container) -> list:
    """把 links 统一成 [link_id, origin_id, origin_slot, target_id, target_slot, type]。"""
    rows = []
    for link in container or []:
        if isinstance(link, dict):
            rows.append([link.get("id"), link.get("origin_id"), link.get("origin_slot"),
                         link.get("target_id"), link.get("target_slot"), link.get("type")])
        elif isinstance(link, (list, tuple)) and len(link) >= 5:
            rows.append(list(link[:6]) + [None] * (6 - len(link)))
    return rows


def _widget_values(node) -> dict:
    """UI 格式的 widgets_values → {字段名: 值}。不认识的节点返回空字典。"""
    names = WIDGETS.get(node.get("type"))
    values = node.get("widgets_values")
    if not names or not isinstance(values, list):
        return {}
    return {name: value for name, value in zip(names, values)}


def _raw_widgets(node) -> dict:
    """未知节点的原始 widgets：没有字段名，只能按下标存，用于诊断模型文件名。"""
    values = node.get("widgets_values")
    if isinstance(values, list):
        return {f"widget[{i}]": value for i, value in enumerate(values)}
    if isinstance(values, dict):
        return dict(values)
    return {}



def _ui_to_graph(data: dict) -> tuple[dict, list]:
    """UI 格式 → API 格式。同时把子图（subgraph）里的节点一并摊平。

    ComfyUI 的界面导出格式没有字段名，只有 `widgets_values` 的位置数组；
    子图实例则通过 `definitions.subgraphs` 定义、用 UUID 作为节点类型引用。
    这里递归摊平，节点 id 加前缀防止撞车；子图内部 `-10` 表示「来自外部输入槽 N」，
    摊平时顺着实例节点在父图里的连线接回真实来源。
    """
    graph: dict = {}
    subgraphs = {s.get("id"): s for s in (data.get("definitions") or {}).get("subgraphs") or []}
    notes: list = []
    # 子图实例的输出改写表：实例节点 id → [输出槽位 → 内部真实来源]
    instance_outputs: dict[str, dict[int, list]] = {}

    def walk(container: dict, prefix: str, outer_links: dict, depth: int,
             external: dict | None = None):
        if depth > 6:
            notes.append("子图嵌套超过 6 层，已停止展开")
            return
        external = external or {}
        links = {row[0]: row for row in _links_of(container.get("links"))}
        links.update(outer_links)
        # 被静音/旁路的节点不参与执行，指向它们的连线一并视为断开
        muted = {str(n.get("id")) for n in container.get("nodes") or [] if n.get("mode") in (2, 4)}

        def resolve(link_id):
            row = links.get(link_id)
            if row is None:
                return None
            origin = row[1]
            if origin == -10:                      # 子图外部输入 → 接回父图的真实来源
                return external.get(row[2], "EXTERNAL")
            if str(origin) in muted:
                return None
            return [f"{prefix}{origin}", row[2]]

        for node in container.get("nodes") or []:
            node_type = node.get("type")
            node_id = f"{prefix}{node.get('id')}"
            if node.get("mode") in (2, 4):         # 静音 / 旁路：ComfyUI 不执行
                notes.append(f"节点 {node_id}（{node_type}）在源工作流里被静音或旁路，已跳过")
                continue
            inputs = dict(_widget_values(node))
            for socket in node.get("inputs") or []:
                name = socket.get("name")
                source = resolve(socket.get("link")) if socket.get("link") is not None else None
                if source == "EXTERNAL":
                    # 子图把某个控件提升成了外部输入。若实例的 widgets 里带了值就用它；
                    # 真正没值（父图用节点连线喂进来）才值得记录 —— 那种参数我们定不了。
                    if name and name not in inputs:
                        notes.append(f"节点 {node_id}.{name} 由子图外部输入驱动，参数无法静态确定")
                    continue
                if isinstance(source, list) and name:
                    inputs[name] = source
            sub = subgraphs.get(node_type)
            if sub is not None:
                # 子图实例：内部节点摊平进来（id 前缀不同），实例本身不占节点位。
                # 实例的 inputs 槽位与子图声明的 inputs 一一同名对应，
                # 里面连着的父图连线就是子图内部 -10 的真实来源。
                binding = {}
                by_name = {s.get("name"): s for s in node.get("inputs") or []}
                for slot, declared in enumerate(sub.get("inputs") or []):
                    socket = by_name.get(declared.get("name"))
                    if socket is None or socket.get("link") is None:
                        continue
                    row = links.get(socket["link"])
                    if row is not None and row[1] != -10 and str(row[1]) not in muted:
                        binding[slot] = [f"{prefix}{row[1]}", row[2]]
                walk(sub, f"{node_id}/", links, depth + 1, binding)
                # 输出侧：子图里 target_id == -20 的连线指明「输出槽 N ← 内部某节点某槽」，
                # 父图里所有指向实例的引用（["实例id", 槽]）最后统一改写成内部来源。
                outputs: dict[int, list] = {}
                for row in _links_of(sub.get("links")):
                    if row[3] == -20 and row[1] != -10:
                        outputs[row[4]] = [f"{node_id}/{row[1]}", row[2]]
                instance_outputs[node_id] = outputs
                continue
            body = {"class_type": node_type, "inputs": inputs}
            # 只有「不认识的节点」才保留原始 widgets：那是判断它是不是模型加载器的
            # 唯一线索（本机没装这个节点，也就没有字段名可用）。
            if node_type not in KNOWN_NODES:
                body["_widgets"] = _raw_widgets(node)
            graph[node_id] = body

    walk(data, "", {}, 0)

    # 把「引用子图实例输出」的输入全部改写成实例内部的真实来源
    def rewrite(value, depth=0):
        if not isinstance(value, list) or depth > 8:
            return value
        if len(value) == 2 and isinstance(value[0], str) and value[0] in instance_outputs:
            target = instance_outputs[value[0]].get(value[1])
            return rewrite(target, depth + 1) if target else value
        return [rewrite(item, depth + 1) if isinstance(item, list) else item for item in value]

    for body in graph.values():
        inputs = body.get("inputs") or {}
        for key, value in list(inputs.items()):
            inputs[key] = rewrite(value)
    return graph, notes


def load_workflow(path: Path) -> tuple[dict, str, list]:
    try:
        raw = path.read_text("utf-8-sig")
    except OSError as exc:
        raise ConvertError("READ_FAILED", f"读不到文件：{exc}") from None
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ConvertError("INVALID_JSON", f"不是合法 JSON：{exc}") from None

    if _is_api_format(data):
        graph = {}
        for node_id, body in data.items():
            graph[str(node_id)] = {"class_type": body.get("class_type"),
                                   "inputs": dict(body.get("inputs") or {})}
        return graph, "api", []
    if isinstance(data, dict) and isinstance(data.get("nodes"), list):
        graph, notes = _ui_to_graph(data)
        return graph, "ui", notes
    raise ConvertError("UNRECOGNIZED_FORMAT",
                       "既不是 ComfyUI 的 API 格式（节点 → class_type），"
                       "也不是界面导出格式（含 nodes/links）")


# ---------------------------------------------------------------------------
# 六、分析：节点、模型、参考图、参数
# ---------------------------------------------------------------------------

def _filename(value) -> str:
    return "" if not isinstance(value, str) else value.replace("\\", "/").rsplit("/", 1)[-1]


def classify_family(filename: str) -> tuple[str, str]:
    """按类别识别扩散模型文件名，精度后缀一概忽略。"""
    name = filename.lower()
    for pattern, family, label in FAMILIES:
        if re.search(pattern, name):
            return family, label
    return "unknown", "未知模型"


def collect_models(graph: dict) -> list:
    """挑出所有模型文件名：[{node, field, role, filename, family}]。

    两条来源都要看：
    1. 标准 Loader 节点的具名输入（`unet_name` / `ckpt_name` / `clip_name` ...）；
    2. **自定义节点把模型名塞在 `widgets_values` 里**的情况 —— 本机这些节点根本没装，
       我们不知道它的字段布局，所以只能把 `_widgets` 里「看着像模型文件名的字符串」
       全捞出来。它们不会被用于替换（本来就要拒绝），只用于把拒绝理由说清楚。
    """
    found = []
    seen = set()

    def add(node_id, field, value, role):
        key = (node_id, field, value)
        if key in seen:
            return
        seen.add(key)
        if POSE_LORA_RE.search(value):
            role = "lora"
        family, label = classify_family(value)
        if role == "lora":
            family, label = "lora", "LoRA 适配器"
        found.append({"node": node_id, "field": field, "role": role,
                      "filename": _filename(value), "family": family, "family_label": label})

    for node_id, body in graph.items():
        inputs = body.get("inputs") or {}
        for key, value in inputs.items():
            role = LOADER_MODEL_FIELDS.get(key)
            if role is None or not isinstance(value, str) or not value.strip():
                continue
            if value.startswith(("{{", "EXTERNAL")):
                continue
            add(node_id, key, value, role)
        for key, value in (body.get("_widgets") or {}).items():
            if not isinstance(value, str) or not MODEL_FILE_RE.search(value):
                continue
            add(node_id, key or "widget", value, "unknown")
    return found


# 一个字符串看起来像不像模型权重文件名
MODEL_FILE_RE = re.compile(r"\.(safetensors|gguf|ckpt|pt|pth|bin)$", re.I)



def extract_parameters(graph: dict) -> dict:
    """从图里抽能搬的参数。同名键取第一个出现的（ComfyUI 图一般只有一处）。"""
    params: dict = {}

    def take(key, value):
        if value is None:
            return
        params.setdefault(key, value)

    for body in graph.values():
        cls = body.get("class_type")
        inputs = body.get("inputs") or {}
        if cls in ("KSampler", "KSamplerAdvanced"):
            take("seed", inputs.get("seed", inputs.get("noise_seed")))
            take("steps", inputs.get("steps"))
            take("cfg", inputs.get("cfg"))
            take("sampler", inputs.get("sampler_name"))
            take("scheduler", inputs.get("scheduler"))
            if isinstance(inputs.get("denoise"), (int, float)) and inputs["denoise"] < 1:
                take("strength", inputs["denoise"])
        elif cls in ("EmptyLatentImage", "EmptySD3LatentImage"):
            take("width", inputs.get("width"))
            take("height", inputs.get("height"))
        elif cls == "CLIPTextEncode":
            text = inputs.get("text")
            if isinstance(text, str) and text.strip():
                take("prompt", text)
        elif cls in ("RandomNoise",):
            take("seed", inputs.get("noise_seed"))
        elif cls == "BasicScheduler":
            take("steps", inputs.get("steps"))
            take("scheduler", inputs.get("scheduler"))
        elif cls == "CFGGuider":
            take("cfg", inputs.get("cfg"))
        elif cls == "KSamplerSelect":
            take("sampler", inputs.get("sampler_name"))
        elif cls == "FluxGuidance":
            take("guidance", inputs.get("guidance"))
        elif cls == "ImageScale":
            take("width", inputs.get("width"))
            take("height", inputs.get("height"))
    return params


def output_nodes(graph: dict) -> list:
    return [node_id for node_id, body in graph.items()
            if body.get("class_type") in OUTPUT_NODES]


def count_reference_images(graph: dict) -> int:
    """数一数有几张「用户上传的图」参与生成。"""
    return sum(1 for body in graph.values() if body.get("class_type") in IMAGE_SOURCE_NODES)


# ---------------------------------------------------------------------------
# 七、决策：落点 or 拒绝
# ---------------------------------------------------------------------------

# 每个 sd-cli Job 类从模板读哪些模型字段（顺序即 MODEL_FILES 的比对顺序，
# 见 engine/sdcpp.py 与 engine/moss.py；manifest 的 models 三元组照抄一遍即可）。
SPEC_MODEL_FIELDS = {
    "LocalGenQwenImage21": ("diffusion_model", "text_encoder", "vision_encoder", "vae"),
    "LocalGenPoseSwap": ("diffusion_model", "text_encoder", "vision_encoder", "vae"),
    "LocalGenKontextImage": ("diffusion_model", "clip_l", "t5xxl", "vae"),
    "LocalGenMossImage": ("diffusion_model", "text_encoder", "vae", "tokenizer"),
}


def decide(graph: dict, report: Report) -> tuple[str, str]:
    """返回 (kind, target)：kind 是 "sdcpp" / "comfy"。拒绝时抛 ConvertError。

    两条落点的判定口径不同，这不是疏漏而是语义差异：

    * `sdcpp`：我们**根本不在 ComfyUI 里跑**这张图，模板是 LocalGen 自己的派发节点。
      所以「对方用了某个 ComfyUI 内置节点我们没装」并不致命 —— 出图不经过它。
      致命的是两类东西：
        1. BLOCKING 里的管线（遮罩 / ControlNet / 视频），是要照搬的**能力**，我们没有；
        2. **不认识的自定义节点**。`PoseQwen21` 这类节点内部把人物图 + 姿势图 +
           服装图做多段融合，我们既不知道它怎么算，也不知道它出几张图 ——
           静默把它当成普通节点，就会产出"能转但跑不起来"的工作流。
    * `comfy`：真在 ComfyUI 里跑，节点少一个就是 400，所以必须全齐。
    """
    report.node_types = sorted({b.get("class_type") for b in graph.values() if b.get("class_type")})

    blocking = [(n, BLOCKING[n]) for n in report.node_types if n in BLOCKING]
    if blocking:
        names = "、".join(f"{n}（{why}）" for n, why in blocking)
        raise ConvertError("BLOCKING_NODE", f"源工作流用了我们搬不动的东西：{names}")

    models = collect_models(graph)
    report.models = [{"role": m["role"], "filename": m["filename"], "family": m["family"]} for m in models]
    # 主模型只认「Loader 的扩散模型输入」（unet_name / ckpt_name）。
    # 文本编码器 / VAE / LoRA 不是出图模型族 —— 用 family 过滤会把 ae.safetensors 这类
    # 没写 "vae" 字样的 VAE 误当成主模型，整条判定就歪了。
    diffusion = [m for m in models if m["role"] == "diffusion"]

    # 未知节点要先判：很多第三方工作流把模型名藏在自定义节点肚子里，
    # 不先判就会报成"没有扩散模型"，把真正的原因（缺节点）盖掉。
    unknown = [n for n in report.node_types if n not in KNOWN_NODES]
    if unknown:
        report.unknown_nodes = unknown
        hint = ""
        if not diffusion and report.models:
            names = "、".join(m["filename"] for m in report.models[:3])
            hint = f"（模型名 {names} 是写在自定义节点里的，本机无从解析）"
        raise ConvertError(
            "MISSING_NODES",
            "源工作流用了本机没有、且适配器不认识的自定义节点："
            + "、".join(unknown) + hint
            + "。这类节点（例如 PoseQwen21 系列）内部自己做多图融合/多段串联，"
              "我们既不知道它怎么算、也不知道它出几张图，硬转必然产出跑不起来的工作流")

    if not diffusion:
        raise ConvertError("NO_DIFFUSION_MODEL",
                           "找不到扩散模型加载节点（UNETLoader / UnetLoaderGGUF / CheckpointLoader）"
                           "——无法判断这是哪一类模型，也就无法替代")


    family = diffusion[0]["family"]
    label = diffusion[0]["family_label"]
    report.family, report.family_label = family, label
    if family == "unknown":
        raise ConvertError("UNKNOWN_MODEL_FAMILY",
                           f"认不出模型类别：{diffusion[0]['filename']}。"
                           "适配器只按类别替换，认不出就不硬转")

    kind, target, note = FAMILY_TARGET.get(family, (None, None, "本机没有同类别模型"))
    if kind is None:
        raise ConvertError("NO_LOCAL_SUBSTITUTE", f"{label} 在本机没有同类别替代：{note}")
    if note:
        report.note_degraded(note)

    refs = count_reference_images(graph)
    if kind == "sdcpp":
        spec = SDCPP_TARGETS[target]
        unknown = [n for n in report.node_types if n not in KNOWN_NODES]
        if unknown:
            report.unknown_nodes = unknown
            raise ConvertError(
                "MISSING_NODES",
                "源工作流用了本机没有、且适配器不认识的自定义节点："
                + "、".join(unknown)
                + "。这类节点（例如 PoseQwen21 系列）内部自己做多图融合，"
                  "我们既不知道它怎么算、也不知道它出几张图，硬转必然产出跑不起来的工作流")
        if refs > 1:
            raise ConvertError(
                "MULTI_REFERENCE_UNSUPPORTED",
                f"源工作流要 {refs} 张上传图同时参与（如「人物 + 姿势」双参考）。"
                f"本机 {target} 的契约只允许 1 张上传参考图"
                + ("（姿势/服装只能走图库选择器）" if spec.get("library") else "")
                + "，硬转会丢图或报错，所以拒绝")
    else:
        if refs > 0:
            raise ConvertError(
                "IMAGE_INPUT_UNSUPPORTED",
                f"源工作流是图生图（{refs} 张上传图）。本机 comfy 落点（{target}）的工作流契约"
                "只暴露 prompt/尺寸/种子，没有参考图输入通道；硬转会丢掉参考图语义，所以拒绝")
        missing = [n for n in report.node_types if n not in KNOWN_NODES]
        if missing:
            report.unknown_nodes = missing
            raise ConvertError("MISSING_NODES",
                               f"ComfyUI 落点要求节点本机齐备，缺：{'、'.join(missing)}")
    return kind, target


# 本机认得（能安全处理）的全部节点类型。不在这个集合里的一律当作"未知自定义节点"。
KNOWN_NODES = (set(WIDGETS) | BENIGN | OUTPUT_NODES | IMAGE_SOURCE_NODES
               | set(BLOCKING)) - {"SaveImageAdvanced"}
KNOWN_NODES.add("SaveImageAdvanced")



# ---------------------------------------------------------------------------
# 八、尺寸 / 参数收敛
# ---------------------------------------------------------------------------

def pick_size(spec: dict, request: dict, report: Report) -> tuple[int, int]:
    sizes = [tuple(size) for size in spec["sizes"]]
    width, height = request.get("width"), request.get("height")
    if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
        if (width, height) in sizes:
            return width, height
        # 挑「长宽比最接近、面积最接近」的已验证档，而不是随便取第一个
        def score(size):
            ratio_gap = abs((size[0] / size[1]) - (width / height))
            area_gap = abs(size[0] * size[1] - width * height) / max(width * height, 1)
            return (round(ratio_gap, 3), area_gap)

        best = min(sizes, key=score)
        report.note_degraded(
            f"尺寸 {width}×{height} 不在已验证白名单（{'、'.join(f'{w}×{h}' for w, h in sizes)}），"
            f"改为最接近的 {best[0]}×{best[1]}")
        return best
    return sizes[0]


def clamp(value, low, high, default, label, report: Report):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if value < low:
        report.note_degraded(f"{label} {value} 低于本机验证下限，提到 {low}")
        return low
    if value > high:
        report.note_degraded(f"{label} {value} 高于本机验证上限（{high}），降到 {high}")
        return high
    return int(value) if isinstance(low, int) else float(value)


# ---------------------------------------------------------------------------
# 九、产出：模板图
# ---------------------------------------------------------------------------

TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "workflows"


def _load_verified_template(workflow_id: str, revision: str) -> dict:
    path = TEMPLATE_ROOT / workflow_id / revision / "prompt.api.json"
    if not path.is_file():
        raise ConvertError("TEMPLATE_MISSING", f"本机缺少基准模板：{path}")
    return json.loads(path.read_text("utf-8"))


def turbo_speedup(target: str, spec: dict, request: dict, inputs: dict, report: Report) -> bool:
    """源工作流步数偏高时，换成「少步 + turbo sigma 预设」而不是砍分辨率。

    实测（6 GB 卡，Q4_K 常规量化权重，768²）：
        euler 默认 sigma，20 步：420.2 s
        euler + qwen-image-2.1-turbo 预设，8 步：253.0 s（−40%，画质肉眼无差别）
    这条比降分辨率划算得多：分辨率一降，构图和细节直接掉一档；步数降下来只是采样点数少了，
    而 turbo 预设把 sigma 曲线压缩成少步可用的形状。

    基准模板里没有 sigmas 字段（默认 euler 曲线），加速档要**显式写进节点**：
    QwenImageJob 读 parameters["sigmas"] 拼 --sigmas，Workflow.parameters()/render()
    会把模板节点里的 sigmas 键带进任务参数，所以只要落到模板上就会生效。
    """
    if not spec.get("turbo_opt_in"):
        return False
    source_steps = request.get("steps")
    if not isinstance(source_steps, int) or isinstance(source_steps, bool):
        return False
    want = spec.get("turbo_steps", 8)
    if source_steps <= want:
        return False
    inputs["sigmas"] = "qwen-image-2.1-turbo"
    inputs["steps"] = want
    report.note_inherited(f"改用加速档 sigmas={inputs['sigmas']}（源 {source_steps} 步 → {want} 步）")
    return True


def build_sdcpp_graph(target: str, spec: dict, request: dict, report: Report) -> tuple[dict, dict]:
    """以已验收的模板为底，只覆盖能用参数表达的旋钮。

    这样做而不是"自己拼一张图"：模板里的模型文件名、字段名必须和
    sd-cli 直跑的 Job 类（QwenImageJob.MODEL_FILES 等）逐字节一致，抄模板是
    唯一不会抄错的办法。
    """
    graph = _load_verified_template(target, spec["revision"])
    node_id = next(nid for nid, body in graph.items()
                   if body.get("class_type") == spec["job"])
    inputs = graph[node_id]["inputs"]

    width, height = pick_size(spec, request, report)
    steps_low, steps_high, steps_default = spec["steps"]
    cfg_low, cfg_high, cfg_default = spec["cfg"]
    steps = clamp(request.get("steps"), steps_low, steps_high, steps_default, "步数", report)
    cfg = clamp(request.get("cfg"), cfg_low, cfg_high, cfg_default, "CFG", report)

    inputs["width"], inputs["height"] = width, height
    if "steps" in inputs:
        inputs["steps"] = steps
    if "cfg_scale" in inputs:
        inputs["cfg_scale"] = cfg
    seed = request.get("seed")
    if isinstance(seed, int) and not isinstance(seed, bool) and 0 <= seed <= 4294967295:
        inputs["seed"] = seed
        report.note_inherited(f"种子 {seed}")

    sampler = request.get("sampler")
    if isinstance(sampler, str):
        if sampler in spec["samples"]:
            inputs["sampling_method"] = sampler
            report.note_inherited(f"采样器 {sampler}")
        else:
            report.note_degraded(
                f"采样器 {sampler} 未在本机验证过（只验证了 {'、'.join(spec['samples'])}），"
                f"改用 {inputs.get('sampling_method', 'euler')}")

    if spec["job"] == "LocalGenPoseSwap":
        # 换姿势/换装固定 8 步 + turbo 预设：实测与 20 步画质无差别，快 40%
        inputs["steps"] = min(steps, 8) if "steps" in inputs else inputs.get("steps")
        report.note_inherited("姿势工作流固定 8 步 + qwen-image-2.1-turbo sigma 预设")
    elif spec.get("turbo_opt_in"):
        # 加速档：优先降步数 + turbo 预设，而不是降分辨率（分辨率一降，画质直接掉一档；
        # 实测 20 步 420 s → 8 步 turbo 253 s，肉眼无差别）。
        applied = turbo_speedup(target, spec, request, inputs, report)
        if applied:
            report.note_degraded(
                f"源工作流 {request.get('steps')} 步 → 本机 {inputs.get('steps')} 步 + "
                f"{inputs.get('sigmas')} sigma 预设；实测提速约 40%，画质肉眼无差别")

    if isinstance(request.get("negative"), str) and inputs.get("negative_prompt") == "":
        inputs["negative_prompt"] = request["negative"]
        report.note_inherited("负面提示词")

    report.parameters = {
        "width": width, "height": height, "steps": inputs.get("steps"),
        "cfg_scale": inputs.get("cfg_scale"), "sampling_method": inputs.get("sampling_method"),
    }
    if "sigmas" in inputs:
        report.parameters["sigmas"] = inputs["sigmas"]
    return graph, inputs


def build_comfy_graph(target: str, spec: dict, graph: dict, request: dict,
                      report: Report) -> dict:
    """comfy 落点：真在 ComfyUI 里跑，所以尽量保留源图，只换模型文件名与旋钮。

    换权重不是改个文件名就完事，三件事必须一起动：
    1. **.gguf 权重要换 GGUF 专用 Loader**。本机 ComfyUI 的 UNETLoader 读不了 gguf，
       必须换成白名单里的 UnetLoaderGGUF / CLIPLoaderGGUF（见 engine/gguf.py）。
    2. **CLIP type 要跟模型走**。Z-Image 的文本编码器是 lumina2 型，源图里若是
       别的 type，加载会直接报错。
    3. **蒸馏模型的采样配方是硬约束**。SDXL base → SDXL-Lightning 后必须
       4 步 / CFG 1 / euler+sgm_uniform，沿用源图的 25 步 CFG7 会出纯噪声。
    """
    emitted = _strip_benign_nodes(graph, report)
    for body in emitted.values():
        body.pop("_widgets", None)
        inputs = body.get("inputs") or {}
        for key in list(inputs):
            if key in UI_ONLY_WIDGETS:
                # UI 格式才有的控件位（noise 种子后的随机策略、上传按钮），API 图没有这些输入
                del inputs[key]

    for body in emitted.values():
        inputs = body.get("inputs") or {}
        for key, value in list(inputs.items()):
            role = LOADER_MODEL_FIELDS.get(key)
            if role is None or not isinstance(value, str) or not value.strip():
                continue
            new = _map_comfy_model(target, role, value)
            if not new:
                # comfy 落点必须保证图里引用的每个文件本机都有，否则就是跑不起来的工作流
                raise ConvertError(
                    "MODEL_NOT_AVAILABLE",
                    f"本机 ComfyUI 没有 {role} 对应的替代文件：{value}（目标 {target}）。"
                    "comfy 落点不允许引用不存在的模型，请改用 sdcpp 落点的同类别工作流")
            if new != value:
                inputs[key] = new
                report.mapped_models.append(f"{value} → {new}")
            if not new.lower().endswith(".gguf"):
                continue
            # gguf 权重 → 换 GGUF 专用加载节点（本机唯一能读 gguf 的路径）
            if key == "unet_name" and body.get("class_type") == "UNETLoader":
                body["class_type"] = "UnetLoaderGGUF"
                report.note_degraded("UNETLoader → UnetLoaderGGUF（.gguf 权重只有 GGUF 加载器能读）")
            elif key == "clip_name" and body.get("class_type") == "CLIPLoader":
                body["class_type"] = "CLIPLoaderGGUF"
                inputs.pop("device", None)          # CLIPLoaderGGUF 没有 device 档位
                report.note_degraded("CLIPLoader → CLIPLoaderGGUF（.gguf 权重只有 GGUF 加载器能读）")

        if target == "z-image-turbo" and body.get("class_type") in ("CLIPLoader", "CLIPLoaderGGUF"):
            if inputs.get("type") not in (None, "lumina2"):
                report.note_degraded(
                    f"CLIP type {inputs.get('type')!r} → 'lumina2'（Z-Image 文本编码器的固定类型）")
                inputs["type"] = "lumina2"

        if target == "sdxl-lightning" and body.get("class_type") in ("KSampler", "KSamplerAdvanced"):
            before = {k: inputs.get(k) for k in ("steps", "cfg", "sampler_name", "scheduler")}
            inputs.update(steps=4, cfg=1, sampler_name="euler", scheduler="sgm_uniform")
            report.note_degraded(
                "SDXL base → SDXL-Lightning 蒸馏版，采样配方必须跟着换："
                f"steps {before['steps']}→4 · cfg {before['cfg']}→1 · "
                f"{before['sampler_name']}/{before['scheduler']}→euler/sgm_uniform"
                "（Lightning 是 4 步蒸馏，沿用源配方只会出噪声）")

    width, height = pick_size(spec, request, report)
    for body in emitted.values():
        inputs = body.get("inputs") or {}
        if body.get("class_type") in ("EmptyLatentImage", "EmptySD3LatentImage"):
            inputs["width"], inputs["height"] = width, height
        if body.get("class_type") in ("KSampler", "KSamplerAdvanced") and target != "sdxl-lightning":
            inputs["seed"] = request.get("seed", inputs.get("seed", 0))
            if isinstance(request.get("steps"), int):
                steps_low, steps_high, steps_default = _comfy_steps(target)
                inputs["steps"] = clamp(request["steps"], steps_low, steps_high, steps_default,
                                        "步数", report)

    report.parameters = {"width": width, "height": height}
    return emitted



def _comfy_steps(target: str):
    return {"z-image-turbo": (4, 12, 9), "sd15": (10, 30, 20),
            "sdxl-lightning": (4, 8, 4)}.get(target, (4, 30, 20))


# 界面专用的 widget 字段：只存在于 UI 格式，ComfyUI 的 API 图没有这些输入，
# 提交 /prompt 时必须剥掉（原版 z-image-turbo 模板里就没有 control_after_generate）。
UI_ONLY_WIDGETS = {"control_after_generate", "upload"}


def _strip_benign_nodes(emitted: dict, report: Report) -> dict:
    """把纯界面节点从落盘图里拿掉，并解析 Reroute 引用。

    MarkdownNote / Note / PreviewImage 这类节点**不在 ComfyUI 的执行注册表里**
    （它们是前端虚拟节点），留在 API 图里会让 dependencies() 报
    WORKFLOW_DEPENDENCIES_MISSING —— 真机当场验证过。Reroute 不产生数据，
    引用它的输入要改写成直连真实来源。
    """
    emitted = json.loads(json.dumps(emitted))
    reroutes = {nid for nid, body in emitted.items() if body.get("class_type") == "Reroute"}

    def follow(ref, depth=0):
        """ref = [node_id, slot]；穿过 Reroute 链，找到真实来源。"""
        if not isinstance(ref, list) or len(ref) != 2 or depth > 8:
            return ref
        if ref[0] not in reroutes:
            return ref
        source = (emitted[ref[0]].get("inputs") or {}).get("source")
        return follow(source, depth + 1)

    for body in emitted.values():
        inputs = body.get("inputs") or {}
        for key, value in list(inputs.items()):
            if (isinstance(value, list) and len(value) == 2 and isinstance(value[0], str)
                    and value[0] in reroutes):
                inputs[key] = follow(value)

    drop = [nid for nid, body in emitted.items()
            if body.get("class_type") in BENIGN or body.get("class_type") == "Reroute"]
    for nid in drop:
        emitted.pop(nid, None)

    dangling = sorted({value[0] for body in emitted.values()
                       for value in (body.get("inputs") or {}).values()
                       if isinstance(value, list) and len(value) == 2
                       and isinstance(value[0], str) and value[0] not in emitted})
    if dangling:
        raise ConvertError("DANGLING_REFERENCE",
                           f"去掉界面节点后仍有输入引用不存在的节点：{dangling}")
    if drop:
        report.note_dropped(f"丢弃纯界面节点（不参与 ComfyUI 执行）：{', '.join(sorted(drop))}")
    return emitted
COMFY_MODEL_DIRS = ("checkpoints", "diffusion_models", "text_encoders", "clip", "vae")


def _verify_comfy_models(report: Report, manifest_models):
    """写盘前核对 comfy 落点引用的模型文件在本机真实存在。

    这是"不许产出能转但跑不起来"的最后一道网：路径找不到（比如在别的机器上跑）
    就跳过检查并写明，找到了却缺文件就必须拒绝。
    """
    root = TEMPLATE_ROOT.parent / ".runtime" / "ComfyUI_windows_portable" / "ComfyUI" / "models"
    if not root.is_dir():
        report.note_dropped(f"未找到本机 ComfyUI 模型目录（{root}），跳过文件存在性核对")
        return
    for _node, _field, filename in manifest_models:
        if any((root / folder / filename).is_file() for folder in COMFY_MODEL_DIRS):
            continue
        raise ConvertError("MODEL_FILE_MISSING",
                           f"本机 ComfyUI 模型目录里找不到 {filename}（找过 "
                           f"{'、'.join(COMFY_MODEL_DIRS)}），转出来的工作流无法运行，所以拒绝")



def _map_comfy_model(target: str, role: str, filename: str) -> str | None:
    """源文件名 → 本机 ComfyUI 侧的同类别文件；没有替代就返回 None。

    comfy 落点不允许"引用一个不存在的文件"，所以 None 会在调用方变成拒绝，
    而不是把源文件名原样留在图里等着 400。
    """
    family, _label = classify_family(filename)
    if target == "z-image-turbo":
        if role == "diffusion" and family == "zimage":
            return "z-image-turbo-Q4_K_M.gguf"
        if role == "text_encoder" and family == "text_encoder":
            return "Qwen3-4B-Q4_K_M.gguf"
        if role == "vae":
            return "ae.safetensors"
    elif target == "sd15":
        if role == "diffusion" and family == "sd15":
            return "v1-5-pruned-emaonly-fp16.safetensors"
    elif target == "sdxl-lightning":
        if role == "diffusion" and family == "sdxl":
            return "sdxl_lightning_4step.safetensors"
    return None


# ---------------------------------------------------------------------------
# 十、写盘：manifest.toml + prompt.api.json
# ---------------------------------------------------------------------------

def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise TypeError(f"TOML 不支持的值类型：{type(value)!r}")


def render_manifest(*, workflow_id: str, revision: str, title: str, runner: str,
                    template_sha256: str, default_size, verified_sizes,
                    output_node: str, models, inputs, required_inputs,
                    tier: str = "high") -> str:
    lines = [
        f"workflow_id = {_toml_value(workflow_id)}",
        f"revision = {_toml_value(revision)}",
        f"title = {_toml_value(title)}",
        'engine = "comfy"',
        f"runner = {_toml_value(runner)}",
        f"tier = {_toml_value(tier)}",
        "enabled = true",
        f"template_sha256 = {_toml_value(template_sha256)}",
        f"default_size = {_toml_value(list(default_size))}",
        f"verified_sizes = {_toml_value([list(size) for size in verified_sizes])}",
        f"output_node = {_toml_value(output_node)}",
    ]
    if required_inputs:
        lines.append(f"required_inputs = {_toml_value(list(required_inputs))}")
    if models:
        lines.append("models = [")
        for row in models:
            lines.append("  " + _toml_value(list(row)) + ",")
        lines.append("]")
    else:
        lines.append("models = []")
    lines.append("")
    lines.append("[inputs]")
    for name, mapping in inputs.items():
        lines.append(f"{name} = {_toml_value(list(mapping))}")
    return "\n".join(lines) + "\n"


def inputs_for_sdcpp(target: str, spec: dict, graph: dict) -> tuple[dict, list]:
    """按 spec 里的 job 决定暴露哪些可提交参数（必须是白名单里的键）。"""
    node_id = next(nid for nid, body in graph.items() if body.get("class_type") == spec["job"])
    save_id = next(nid for nid, body in graph.items() if body.get("class_type") == "SaveImage")
    inputs = {
        "prompt": [node_id, "prompt"],
        "width": [node_id, "width"],
        "height": [node_id, "height"],
        "seed": [node_id, "seed"],
        "output_prefix": [save_id, "filename_prefix"],
    }
    if spec.get("negative"):
        inputs["negative_prompt"] = [node_id, "negative_prompt"]
    if spec.get("ref_image"):
        inputs["image"] = [node_id, "image"]
    if spec.get("strength"):
        inputs["strength"] = [node_id, "strength"]
    if spec.get("library"):
        inputs["pose_id"] = [node_id, "pose_id"]
        inputs["garment_id"] = [node_id, "garment_id"]
    required = ["image"] if spec.get("requires_image") else []
    return inputs, required


def sanitize_id(text: str) -> str:
    """workflow_id 的硬约束：^[a-z0-9][a-z0-9_-]{0,63}$（见 jobs/models.py）。"""
    lowered = re.sub(r"[^a-z0-9_-]+", "-", (text or "").lower()).strip("-_")
    if not lowered or not lowered[0].isalnum():
        lowered = "wf-" + lowered
    return lowered[:64]


# ---------------------------------------------------------------------------
# 十一、主流程
# ---------------------------------------------------------------------------

def convert(path: Path, *, root: Path | None = None, workflow_id: str | None = None,
            write: bool = True) -> Report:
    report = Report(source=str(path))
    graph, source_format, notes = load_workflow(path)
    report.source_format = source_format
    for note in notes:
        report.note_dropped(note)

    kind, target = decide(graph, report)
    report.target, report.runner = target, kind
    spec = SDCPP_TARGETS[target] if kind == "sdcpp" else COMFY_TARGETS[target]
    report.job_class = spec.get("job", "")

    request = extract_parameters(graph)
    request["negative"] = request.get("negative")

    if kind == "sdcpp":
        emitted, engine_inputs = build_sdcpp_graph(target, spec, request, report)
        inputs, required = inputs_for_sdcpp(target, spec, emitted)
        output_node = next(nid for nid, body in emitted.items()
                           if body.get("class_type") == "SaveImage")
        # manifest 的 models 必须是 (node, field, filename) 三元组，且与模板逐字一致。
        # 直接从刚落盘的模板里读回来，而不是在这里再列一遍字段名 —— 少一处会走样的地方。
        job_node = next(nid for nid, body in emitted.items()
                        if body.get("class_type") == spec["job"])
        manifest_models = [[job_node, field, engine_inputs[field]]
                           for field in SPEC_MODEL_FIELDS[spec["job"]]
                           if field in engine_inputs]
        report.mapped_models = [row[2] for row in manifest_models]
        # default_size 必须是已验证尺寸之一（Workflow.__init__ 会查），取最贴近源请求的那档
        default_size = pick_size(spec, request, report)
        verified = spec["sizes"]
    else:
        emitted = build_comfy_graph(target, spec, graph, request, report)
        outputs = output_nodes(emitted)
        if not outputs:
            raise ConvertError("NO_OUTPUT_NODE", "转出来的图没有 SaveImage 出图节点")
        output_node = outputs[0]
        save_prefix = emitted[output_node].setdefault("inputs", {})
        save_prefix.setdefault("filename_prefix", "image")
        inputs = {
            "prompt": _find_field(emitted, "text"),
            "width": _find_field(emitted, "width"),
            "height": _find_field(emitted, "height"),
            "seed": _find_field(emitted, "seed"),
            "output_prefix": [output_node, "filename_prefix"],
        }
        inputs = {k: v for k, v in inputs.items() if v is not None}
        required = []
        manifest_models = []
        for body in emitted.values():
            for key, value in (body.get("inputs") or {}).items():
                role = LOADER_MODEL_FIELDS.get(key)
                if role and isinstance(value, str):
                    manifest_models.append([_node_of(emitted, body), key, value])
        report.mapped_models = [row[2] for row in manifest_models]
        default_size = (report.parameters["width"], report.parameters["height"])
        verified = spec["sizes"]

    if not {"prompt", "width", "height", "seed", "output_prefix"} <= set(inputs):
        missing = {"prompt", "width", "height", "seed", "output_prefix"} - set(inputs)
        raise ConvertError("PARAMETER_MAPPING_INCOMPLETE",
                           f"无法把源工作流的参数映射到本机（缺 {sorted(missing)}）")

    if kind == "comfy":
        _verify_comfy_models(report, manifest_models)

    payload = json.dumps(emitted, ensure_ascii=False, indent=1)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()

    wid = sanitize_id(workflow_id or path.stem)
    report.workflow_id = wid
    revision = spec["revision"]
    title = f"{spec['title']} · 导入（{path.stem}）"

    manifest = render_manifest(
        workflow_id=wid, revision=revision, title=title, runner=kind,
        template_sha256=digest, default_size=default_size, verified_sizes=verified,
        output_node=output_node, models=manifest_models, inputs=inputs,
        required_inputs=required,
    )

    if write:
        base = (root or TEMPLATE_ROOT) / wid / revision
        base.mkdir(parents=True, exist_ok=True)
        # 必须按字节写：write_text 在 Windows 上会把 \n 翻译成 \r\n，
        # 而 template_sha256 是按原字节算的 —— 走文本写入必然"摘要不一致"。
        (base / "prompt.api.json").write_bytes(payload.encode("utf-8"))
        (base / "manifest.toml").write_bytes(manifest.encode("utf-8"))
        report.output_path = str(base)

    report.accepted = True
    report.code = "OK"
    report.reason = "已转换"
    return report


def _node_of(emitted: dict, target_body: dict) -> str:
    for node_id, body in emitted.items():
        if body is target_body:
            return node_id
    return next(iter(emitted))


def _find_field(emitted: dict, field: str):
    for node_id, body in emitted.items():
        inputs = body.get("inputs") or {}
        if field in inputs and not isinstance(inputs[field], list):
            return [node_id, field]
    return None


# ---------------------------------------------------------------------------
# 十二、命令行
# ---------------------------------------------------------------------------

def _print_report(report: Report, verbose: bool = True):
    state = "✅ 已转换" if report.accepted else "⛔ 拒绝"
    print(f"\n{state}  {Path(report.source).name}")
    print(f"  源格式     : {report.source_format or '-'}")
    if not report.accepted:
        print(f"  拒绝码     : {report.code}")
        print(f"  原因       : {report.reason}")
        if report.unsupported_nodes:
            print(f"  缺失节点   : {', '.join(report.unsupported_nodes)}")
        return
    print(f"  类别       : {report.family_label}  →  {report.target}（runner={report.runner}"
          f"{'，' + report.job_class if report.job_class else ''}）")
    print(f"  落盘       : {report.output_path}")
    if verbose:
        if report.mapped_models:
            print(f"  本机模型   : {', '.join(report.mapped_models)}")
        if report.parameters:
            pretty = ", ".join(f"{k}={v}" for k, v in report.parameters.items())
            print(f"  引擎参数   : {pretty}")
        for line in report.inherited:
            print(f"  继承       : {line}")
        for line in report.degraded:
            print(f"  降级/替换  : {line}")
        for line in report.dropped:
            print(f"  丢弃       : {line}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="convert_comfy_workflow",
        description="把第三方 ComfyUI 工作流转成 LocalGen 能跑的工作流（按模型类别替换成本机权重）")
    parser.add_argument("sources", nargs="+", type=Path, help="第三方工作流 JSON（API 或界面导出格式）")
    parser.add_argument("--root", type=Path, default=None, help="输出根目录（默认项目 workflows/）")
    parser.add_argument("--workflow-id", default=None, help="覆盖输出 workflow_id（默认取文件名）")
    parser.add_argument("--dry-run", action="store_true", help="只分析不写盘")
    parser.add_argument("--json", action="store_true", help="额外输出机器可读 JSON")
    parser.add_argument("--quiet", action="store_true", help="不打印继承/降级明细")
    args = parser.parse_args(argv)

    results = []
    for source in args.sources:
        try:
            report = convert(source, root=args.root, workflow_id=args.workflow_id,
                             write=not args.dry_run)
        except ConvertError as exc:
            report = Report(source=str(source), accepted=False, code=exc.code, reason=exc.reason)
        _print_report(report, verbose=not args.quiet)
        results.append(report)

    if args.json:
        print(json.dumps([report.as_dict() for report in results], ensure_ascii=False, indent=1))

    return 0 if all(report.accepted for report in results) else 1


if __name__ == "__main__":
    sys.exit(main())
