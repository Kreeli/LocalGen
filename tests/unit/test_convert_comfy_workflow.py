"""第三方 ComfyUI 工作流适配器：模型类别替换、节点缺失拒绝、降级留痕。

fixture 全部来自真实文件，不是手造的：
- `aicg3d_*.json` 三个来自 comfyui-aicg3d-PoseSwap 插件的 example_workflows/，
  用的都是本机没有的自定义节点（PoseQwen21* / TextEncodeQwenImage21）——拒绝路径的真样本；
- `flux_kontext_dev_basic.json` / `image_z_image_turbo.json` / `image_sdxl_simple.json`
  是 ComfyUI 官方模板库里的工作流，节点本机全有、模型可按类别替换 —— 接受路径的真样本；
- `flux_schnell.json` 是"本机没有同类别权重"的拒绝样本。
"""
import hashlib
import json
from pathlib import Path

import pydantic
import pytest
from pydantic import TypeAdapter

from localgen.engine.comfy import SDCPP_JOBS
from localgen.jobs.models import Identifier, Submission
from localgen.workflows import Registry, Workflow
from tools.convert_comfy_workflow import (
    COMFY_TARGETS, ConvertError, SDCPP_TARGETS, convert, sanitize_id)

IDENTIFIER = TypeAdapter(Identifier)
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "comfy"


def _convert(name: str, tmp_path: Path, workflow_id: str | None = None):
    return convert(FIXTURES / name, root=tmp_path,
                   workflow_id=workflow_id, write=True)


def _loaded(tmp_path: Path, report) -> Workflow:
    """用 LocalGen 真身的校验器加载转换产物 —— 这是"能跑"的硬门。"""
    directory = Path(report.output_path)
    return Workflow(directory)


# ---------------------------------------------------------------------------
# 拒绝路径：三份真实第三方工作流
# ---------------------------------------------------------------------------

def test_aicg3d_pose_swap_edit_rejected_for_unknown_custom_nodes(tmp_path):
    """换姿+换装工作流：模型名全在自定义节点肚子里，节点本机没有 → 必须拒绝。"""
    with pytest.raises(ConvertError) as exc:
        _convert("aicg3d_poseswap_pose_swap_edit.json", tmp_path)
    assert exc.value.code == "MISSING_NODES"
    assert "PoseQwen21" in exc.value.reason
    # 报告要说清模型名藏在哪，而不是误报"没有扩散模型"
    assert "qwen_image_2.1_bf16.safetensors" in exc.value.reason


def test_aicg3d_three_stage_rejected_and_names_the_missing_nodes(tmp_path):
    """三段式工作流：TextEncodeQwenImage21（本机 ComfyUI 0.3.76 没有）必须被点名。"""
    with pytest.raises(ConvertError) as exc:
        _convert("aicg3d_qwen21_three_stage.json", tmp_path)
    assert exc.value.code == "MISSING_NODES"
    assert "TextEncodeQwenImage21" in exc.value.reason
    assert "QwenImage21Cache" in exc.value.reason


def test_aicg3d_simple_replace_rejected(tmp_path):
    with pytest.raises(ConvertError) as exc:
        _convert("aicg3d_qwen21_simple_replace.json", tmp_path)
    assert exc.value.code == "MISSING_NODES"
    assert "PoseQwen21OutfitStage" in exc.value.reason


def test_flux_schnell_rejected_no_local_substitute(tmp_path):
    """FLUX.1 schnell 权重本机没有，也不属于任何已有类别 → 拒绝而不是硬凑。"""
    with pytest.raises(ConvertError) as exc:
        _convert("flux_schnell.json", tmp_path)
    assert exc.value.code == "NO_LOCAL_SUBSTITUTE"


def test_official_qwen21_t2i_rejected_for_missing_qwen_nodes(tmp_path):
    """官方 Qwen-Image-2.1 模板依赖 0.43+ 节点；本机 0.3.76 装不上，明确拒绝。"""
    with pytest.raises(ConvertError) as exc:
        _convert("image_qwen_image_2_1_t2i.json", tmp_path)
    assert exc.value.code == "MISSING_NODES"
    assert "TextEncodeQwenImage21" in exc.value.reason


def test_broken_json_rejected(tmp_path):
    bad = tmp_path / "broken.json"
    bad.write_text("{not json", "utf-8")
    with pytest.raises(ConvertError) as exc:
        convert(bad, root=tmp_path)
    assert exc.value.code == "INVALID_JSON"


def test_non_workflow_json_rejected(tmp_path):
    bad = tmp_path / "not-a-workflow.json"
    bad.write_text(json.dumps({"hello": "world"}), "utf-8")
    with pytest.raises(ConvertError) as exc:
        convert(bad, root=tmp_path)
    assert exc.value.code == "UNRECOGNIZED_FORMAT"


# ---------------------------------------------------------------------------
# 接受路径 · sdcpp：官方 FLUX Kontext dev 编辑工作流
# ---------------------------------------------------------------------------

def test_flux_kontext_basic_converts_to_kontext_dev(tmp_path):
    report = _convert("flux_kontext_dev_basic.json", tmp_path)
    assert report.accepted
    assert report.runner == "sdcpp"
    assert report.target == "kontext-dev"
    assert report.job_class == "LocalGenKontextImage"
    # 模型按类别替换：fp8 → 我们的 Q4_K，文本编码器/VAE 一并落到位
    assert report.mapped_models == ["flux1-kontext-dev-Q4_K_M.gguf", "clip_l.safetensors",
                                    "t5-v1_1-xxl-encoder-Q4_K_S.gguf", "ae.safetensors"]


def test_kontext_product_passes_localgen_validation(tmp_path):
    """转换产物必须能被 LocalGen 的 Registry 真身加载 —— 这一条就是'能跑'的硬门。"""
    report = _convert("flux_kontext_dev_basic.json", tmp_path, workflow_id="imported-kontext")
    registry = Registry(tmp_path, "comfy")
    workflow = registry.current["imported-kontext"]
    assert workflow.spec.runner == "sdcpp"
    assert workflow.spec.required_inputs == ["image"]      # kontext 必须带参考图
    assert workflow.spec.workflow_id == "imported-kontext"
    # 引擎侧 Job 类真的认得这份模板：模型三元组逐字一致才不会被 503
    engine_inputs = workflow.render(workflow.parameters(
        Submission(workflow_id="imported-kontext", prompt="把车改成红色", seed=1,
                   image="a" * 32 + ".png")),
        "job")["1"]["inputs"]
    assert engine_inputs["diffusion_model"] == "flux1-kontext-dev-Q4_K_M.gguf"
    assert engine_inputs["guidance"] == 2.5
    assert engine_inputs["image"] == "a" * 32 + ".png"


def test_kontext_inherits_source_parameters(tmp_path):
    """源图里的步数 / CFG / 采样器要被继承，prompt 要被提出来。"""
    report = _convert("flux_kontext_dev_basic.json", tmp_path, workflow_id="imported-kontext-2")
    assert report.parameters["steps"] == 20            # 源 KSampler 20 步
    assert report.parameters["cfg_scale"] == 1.0
    assert report.parameters["sampling_method"] == "euler"
    assert "采样器 euler" in report.inherited


def test_kontext_template_sha256_matches_written_template(tmp_path):
    report = _convert("flux_kontext_dev_basic.json", tmp_path, workflow_id="imported-kontext-3")
    directory = Path(report.output_path)
    template = json.loads((directory / "prompt.api.json").read_text("utf-8"))
    manifest = (directory / "manifest.toml").read_text("utf-8")
    digest = hashlib.sha256(
        json.dumps(template, ensure_ascii=False, indent=1).encode("utf-8")).hexdigest()
    assert f'template_sha256 = "{digest}"' in manifest


# ---------------------------------------------------------------------------
# 接受路径 · comfy：官方 Z-Image-Turbo 与 SDXL 工作流
# ---------------------------------------------------------------------------

def test_z_image_turbo_converts_with_gguf_loaders(tmp_path):
    report = _convert("image_z_image_turbo.json", tmp_path)
    assert report.accepted
    assert report.runner == "comfy"
    assert report.target == "z-image-turbo"
    # bf16 权重 → 我们的 GGUF；加载节点也要跟着换，否则 ComfyUI 读不了 gguf
    assert "UNETLoader → UnetLoaderGGUF" in "".join(report.degraded)
    assert "CLIPLoader → CLIPLoaderGGUF" in "".join(report.degraded)
    workflow = _loaded(tmp_path, report)
    classes = {body["class_type"] for body in workflow.graph.values()}
    assert "UnetLoaderGGUF" in classes and "CLIPLoaderGGUF" in classes
    names = {value for body in workflow.graph.values()
             for key, value in body["inputs"].items()
             if key in ("unet_name", "clip_name", "vae_name") and isinstance(value, str)}
    assert names == {"z-image-turbo-Q4_K_M.gguf", "Qwen3-4B-Q4_K_M.gguf", "ae.safetensors"}
    assert report.mapped_models  # 有替换记录


def test_z_image_product_passes_localgen_validation(tmp_path):
    report = _convert("image_z_image_turbo.json", tmp_path, workflow_id="imported-z-image")
    registry = Registry(tmp_path, "comfy")
    workflow = registry.current["imported-z-image"]
    assert workflow.spec.runner == "comfy"
    assert (report.parameters["width"], report.parameters["height"]) == (1024, 1024)


def test_sdxl_simple_switches_to_lightning_recipe(tmp_path):
    """SDXL base → Lightning 是蒸馏替换，采样配方必须一起换，且要大声告知。"""
    report = _convert("image_sdxl_simple.json", tmp_path)
    assert report.accepted
    assert report.target == "sdxl-lightning"
    joined = "".join(report.degraded)
    assert "25→4" in joined and "cfg 7→1" in joined
    workflow = _loaded(tmp_path, report)
    sampler = next(body["inputs"] for body in workflow.graph.values()
                   if body["class_type"] == "KSampler")
    assert (sampler["steps"], sampler["cfg"]) == (4, 1)
    assert (sampler["sampler_name"], sampler["scheduler"]) == ("euler", "sgm_uniform")


def test_sdxl_product_passes_localgen_validation(tmp_path):
    report = _convert("image_sdxl_simple.json", tmp_path, workflow_id="imported-sdxl")
    registry = Registry(tmp_path, "comfy")
    workflow = registry.current["imported-sdxl"]
    assert workflow.spec.default_size == (1024, 1024)


# ---------------------------------------------------------------------------
# 降级留痕
# ---------------------------------------------------------------------------

def test_qwen_high_steps_switch_to_turbo_preset(tmp_path):
    """源 25 步 → 本机 8 步 + turbo sigma 预设（实测 -40%，画质无差别），必须写明。"""
    source = {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": "qwen_image_2.1_int8_convrot.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": "qwen3vl_8b_bf16.safetensors", "type": "qwen_image", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_2.1_vae_bf16.safetensors"}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": "雨夜霓虹", "clip": ["2", 0]}},
        "5": {"class_type": "EmptySD3LatentImage", "inputs": {"width": 1024, "height": 1024, "batch_size": 1}},
        "6": {"class_type": "KSampler", "inputs": {
            "model": ["1", 0], "positive": ["4", 0], "negative": ["4", 0],
            "latent_image": ["5", 0], "seed": 424242, "steps": 25, "cfg": 6,
            "sampler_name": "euler", "scheduler": "simple", "denoise": 1}},
        "7": {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["3", 0]}},
        "8": {"class_type": "SaveImage", "inputs": {"filename_prefix": "q", "images": ["7", 0]}},
    }
    source_path = tmp_path / "src"
    source_path.mkdir()
    (source_path / "qwen-t2i-25steps.api.json").write_text(
        json.dumps(source, ensure_ascii=False, indent=1), "utf-8")
    report = convert(source_path / "qwen-t2i-25steps.api.json", root=source_path,
                     workflow_id="imported-qwen-turbo")
    assert report.accepted
    assert report.target == "qwen-image-21"
    assert report.parameters["steps"] == 8
    assert report.parameters["sigmas"] == "qwen-image-2.1-turbo"
    joined = "".join(report.degraded)
    assert "25" in joined and "8" in joined


def test_oversized_size_falls_back_to_verified(tmp_path):
    """2048² 超出 qwen-image-21 已验证档 → 收敛到 1024²，且必须留痕。"""
    source = {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": "qwen_image_2.1-Q4_K.gguf", "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": "Qwen3VL-8B-Instruct-Q4_K_M.gguf", "type": "qwen_image", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_2.1_vae_bf16.safetensors"}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": "夜市", "clip": ["2", 0]}},
        "5": {"class_type": "EmptySD3LatentImage", "inputs": {"width": 2048, "height": 2048, "batch_size": 1}},
        "6": {"class_type": "KSampler", "inputs": {
            "model": ["1", 0], "positive": ["4", 0], "negative": ["4", 0],
            "latent_image": ["5", 0], "seed": 7, "steps": 8, "cfg": 6,
            "sampler_name": "euler", "scheduler": "simple", "denoise": 1}},
        "7": {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["3", 0]}},
        "8": {"class_type": "SaveImage", "inputs": {"filename_prefix": "q", "images": ["7", 0]}},
    }
    base = tmp_path / "src2"
    base.mkdir()
    path = base / "qwen-2048.api.json"
    path.write_text(json.dumps(source, ensure_ascii=False), "utf-8")
    report = convert(path, root=base, workflow_id="imported-qwen-2048")
    assert report.parameters["width"] == 1024
    assert any("2048" in line and "1024" in line for line in report.degraded)


def test_multi_reference_workflow_is_rejected(tmp_path):
    """「人物 + 姿势」双上传图超出单参考图契约 → 拒绝，不静默丢图。"""
    source = {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": "qwen_image_2.1-Q4_K.gguf", "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": "Qwen3VL-8B-Instruct-Q4_K_M.gguf", "type": "qwen_image", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_2.1_vae_bf16.safetensors"}},
        "4": {"class_type": "LoadImage", "inputs": {"image": "person.png", "upload": "image"}},
        "5": {"class_type": "LoadImage", "inputs": {"image": "pose.png", "upload": "image"}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "换成站姿", "clip": ["2", 0]}},
        "7": {"class_type": "KSampler", "inputs": {
            "model": ["1", 0], "positive": ["6", 0], "negative": ["6", 0],
            "latent_image": ["5", 0], "seed": 1, "steps": 20, "cfg": 4,
            "sampler_name": "euler", "scheduler": "simple", "denoise": 1}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "q", "images": ["8", 0]}},
    }
    base = tmp_path / "src3"
    base.mkdir()
    path = base / "qwen-two-refs.api.json"
    path.write_text(json.dumps(source, ensure_ascii=False), "utf-8")
    with pytest.raises(ConvertError) as exc:
        convert(path, root=base)
    assert exc.value.code == "MULTI_REFERENCE_UNSUPPORTED"


def test_controlnet_workflow_is_rejected(tmp_path):
    source = {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": "qwen_image_2.1-Q4_K.gguf", "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": "Qwen3VL-8B-Instruct-Q4_K_M.gguf", "type": "qwen_image", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_2.1_vae_bf16.safetensors"}},
        "4": {"class_type": "ControlNetLoader", "inputs": {"control_net_name": "depth.safetensors"}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": "x", "clip": ["2", 0]}},
        "6": {"class_type": "KSampler", "inputs": {
            "model": ["1", 0], "positive": ["5", 0], "negative": ["5", 0],
            "latent_image": ["3", 0], "seed": 1, "steps": 8, "cfg": 4,
            "sampler_name": "euler", "scheduler": "simple", "denoise": 1}},
        "7": {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["3", 0]}},
        "8": {"class_type": "SaveImage", "inputs": {"filename_prefix": "q", "images": ["7", 0]}},
    }
    base = tmp_path / "src4"
    base.mkdir()
    path = base / "qwen-controlnet.api.json"
    path.write_text(json.dumps(source, ensure_ascii=False), "utf-8")
    with pytest.raises(ConvertError) as exc:
        convert(path, root=base)
    assert exc.value.code == "BLOCKING_NODE"


# ---------------------------------------------------------------------------
# 契约面
# ---------------------------------------------------------------------------

def test_all_sdcpp_job_classes_are_registered():
    """转换器产出的 class_type 必须都在 SDCPP_JOBS 里，否则 sd-cli 派发不到。"""
    for spec in SDCPP_TARGETS.values():
        assert spec["job"] in SDCPP_JOBS


def test_sanitize_id_matches_identifier_contract():
    for raw in ("flux_kontext_dev_basic", "Image_Qwen 2.1 T2I", "-abc-", "中文id", ""):
        # 兜底前缀（"wf-"）也是合法标识符 —— 包括空串，绝不能产出空 workflow_id
        value = sanitize_id(raw)
        assert IDENTIFIER.validate_python(value) == value


def test_dry_run_writes_nothing(tmp_path):
    before = set(tmp_path.rglob("*"))
    from tools.convert_comfy_workflow import convert as run
    report = run(FIXTURES / "image_z_image_turbo.json", root=tmp_path, write=False)
    assert report.accepted
    assert report.output_path == ""
    assert set(tmp_path.rglob("*")) == before


def test_manifest_targets_reference_real_verified_sizes():
    """落点声明必须与本机已验收工作流的 verified_sizes 一致，否则转换产物会被 422。"""
    registry = Registry(Path(__file__).resolve().parents[2] / "workflows", "comfy")
    for target, spec in {**SDCPP_TARGETS, **COMFY_TARGETS}.items():
        workflow = registry.current.get(target)
        assert workflow is not None, f"本机没有基准工作流 {target}"
        assert [tuple(s) for s in spec["sizes"]] == [tuple(s) for s in workflow.spec.verified_sizes]
