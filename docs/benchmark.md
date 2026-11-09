# LocalGen V1 实机验收

测试日期为 2026 年 11 月 8 日。测试机为 Windows 11，显卡 NVIDIA GeForce RTX 4050 Laptop GPU，驱动 621.14，独立显存 6141 MiB，系统内存约 16 GiB。使用 ComfyUI Portable 0.3.76、内置节点、PyTorch 2.9.1+cu130 和 CUDA 设备 0。Portable 压缩包 SHA-256 为 `3bb5a3ca6772cff3dc1dd7137826db560d686aa7915ec57d3faf2b77f50241cb`。版本与安装入口见 [ComfyUI 发行页](https://github.com/Comfy-Org/ComfyUI/releases/tag/v0.3.76)。

## 模型和尺寸

| 工作流 | 结果 | 尺寸 |
| --- | --- | --- |
| SD 1.5 FP16 | 通过本地单图实测 | 512×512 |
| SDXL-Lightning 4 步 | 通过本地单图实测 | 512×512 |
| Z-Image-Turbo GGUF Q4_K_M | 通过本地单图实测 | 768×768、1024×1024 |

SD 1.5 FP16 使用 `v1-5-pruned-emaonly-fp16.safetensors`，SHA-256 为 `e9476a13728cd75d8279f6ec8bad753a66a1957ca375a1464dc63b37db6e3916`。工作流为 `sd15/1.0.0`，Euler、normal、20 步、CFG 7、batch 1。测试提示词为 “A small lighthouse above a rocky coast at sunrise, warm light, detailed oil painting”。不同种子让采样器每次重新执行。结果均成功解码为完整 512×512 PNG。种子 42 的样张有岩岸、海面和暖光，灯塔主体不明显。该结果用于功能与稳定性验收。 [模型文件及许可](https://huggingface.co/Comfy-Org/stable-diffusion-1.5/blob/main/v1-5-pruned-emaonly-fp16.safetensors)

| 次序 | 种子 | 客户端 | 结果 | 提交到终态 |
| --- | ---: | --- | --- | ---: |
| 冷启动后首张 | 42 | REST | 成功 | 18.66 秒 |
| 热生成 1 | 43 | MCP | 成功 | 5.61 秒 |
| 运行时取消 | 44 | REST | 已取消 | 0.64 秒 |
| 热生成 2 | 45 | REST | 成功 | 4.94 秒 |
| 热生成 3 | 46 | REST | 成功 | 12.56 秒 |
| 热生成 4 | 47 | REST | 成功 | 5.33 秒 |
| 热生成 5 | 48 | REST | 成功 | 5.33 秒 |

以 200 ms 间隔采样，GPU 总独立显存使用量从约 1942 MiB 到峰值 5523 MiB，包含当时的其他 GPU 使用。Windows 报告系统 RAM 使用峰值约 15607 MiB，引擎工作集峰值 2540 MiB、进程提交量峰值 6616 MiB。测试期间另有打包和下载工作。此轮未采集硬分页计数或 AMD 集显上的显示负载。出图耗时从引擎就绪后提交开始计时。

## SDXL-Lightning

使用 ByteDance 完整 4 步权重 `sdxl_lightning_4step.safetensors`，SHA-256 为 `e0d996ee0013e79d9d3561f50fcafb9a17e3ff07b780358e3b66d67932c4d490`。工作流 `sdxl-lightning/1.0.0` 使用 Euler、sgm_uniform、4 步、CFG 1、batch 1。样张显示灯塔、岩岸、海面和暖光。 [模型文件](https://huggingface.co/ByteDance/SDXL-Lightning/blob/main/sdxl_lightning_4step.safetensors) · [模型卡](https://huggingface.co/ByteDance/SDXL-Lightning)

| 次序 | 种子 | 结果 | 提交到终态 |
| --- | ---: | --- | ---: |
| 冷启动后首张 | 42 | 成功 | 43.33 秒 |
| 热生成 1 · MCP | 43 | 成功 | 6.31 秒 |
| 运行时取消 | 44 | 已取消 | 1.05 秒 |
| 热生成 2 | 45 | 成功 | 4.31 秒 |
| 热生成 3 | 46 | 成功 | 4.95 秒 |
| 热生成 4 | 47 | 成功 | 4.92 秒 |
| 热生成 5 | 48 | 成功 | 5.99 秒 |

独立显存总使用峰值为 5818 MiB，引擎工作集峰值为 5566 MiB，进程提交量峰值为 15986 MiB，系统 RAM 使用峰值约 15608 MiB。ComfyUI 自动卸载了部分模型权重到主存。本轮只放行 512×512。系统内存占用较高时，可在同一界面选择已验证的 SD 1.5 512×512。

以相同提示词、种子和次数追加测试 ComfyUI `--lowvram`。首张 84.45 秒，五张热生成 21.34–31.16 秒，取消 1.84 秒。独立显存总使用峰值为 5852 MiB，进程提交量峰值为 15987 MiB。发布版采用 ComfyUI 自动显存管理。

## Z-Image-Turbo

同一台 RTX 4050 使用现有的 ComfyUI 0.3.76。工作流 `z-image-turbo/1.0.0` 使用 GGUF Q4_K_M 扩散模型和 Qwen3-4B Q4_K_M 文本编码器，搭配 Flux VAE。采样沿用该版本内置 Z-Image-Turbo 模板的 9 步、CFG 1、res_multistep、simple 和 shift 3。发布的默认分辨率是 768×768，可选 1024×1024。 [官方工作流](https://docs.comfy.org/tutorials/image/z-image/z-image-turbo) · [本机安装文件](windows-setup.md#z-image-turbo)

| 文件 | SHA-256 |
| --- | --- |
| `z-image-turbo-Q4_K_M.gguf` | `4ab6bceb1f0b82c9e725f123848018eac662a6fc1d467c019e13e701afb7b55c` |
| `Qwen3-4B-Q4_K_M.gguf` | `f6f851777709861056efcdad3af01da38b31223a3ba26e61a4f8bf3a2195813a` |
| `ae.safetensors` | `afc8e28272cd15db3919bacdb6918ce9c1ed22e96cb12c4d5ed0fba823529e38` |

对照使用种子 42。中文提示要求澳门雨夜街角、写着「深夜咖啡」的霓虹招牌、红雨衣、透明雨伞和路面倒影。英文提示要求在雨天电车上读书的女士、纸杯和车窗雨滴。Z-Image-Turbo 在这两组样张中保留了上述细节，SDXL-Lightning 的中文图偏离场景，英文图遗漏了纸杯。

| 尺寸 | 中文 | 英文 |
| --- | ---: | ---: |
| 512×512 | 42.31 秒，冷任务 | 26.72 秒 |
| 768×768 | 27.72 秒 | |
| 1024×1024 | 47.05 秒 | 43.91 秒 |

正常配置下独立显存总使用峰值为 5517 MiB，系统 RAM 使用峰值为 15592 MiB。旧版 LocalGen 同时运行时首次 512×512 遇到显存不足。退出旧窗口后，低显存试验以 54.69 秒和 37.14 秒完成两张 512×512，随后正常配置完成了表中各档。用户版采用自动显存管理。最终桌面包通过 768×768 的生成、PNG 预览、取消及取消后再次生成。此次 1024×1024 使用两条提示词分别生成一次，连续多张 1024×1024 的压力测试留待后续。

## MOSS-Image

同一台 RTX 4050 上使用固定版本的 stable-diffusion.cpp（0b3b98d，Windows CUDA12 x64）运行 MOSS-Image SFT。`runner = "moss"` 的任务先停 ComfyUI 释放显存，由 LocalGen 直接运行 `localgen-moss/bin/sd-cli.exe`，结束后自动恢复 ComfyUI。文本编码器 Qwen3.5-9B 走 CPU（`te=cpu`），扩散与 VAE 走 CUDA。采样沿用模板内置 euler、discrete、50 步、CFG 3、flow-shift 5。发布尺寸为 768×768 与 1024×1024。

| 文件 | 大小（字节） |
| --- | ---: |
| `MOSS-Image-SFT-Q4_K.gguf` | 3327400800 |
| `Qwen3.5-9B-Q4_K_M.gguf` | 5680522464 |
| `flux2-vae.safetensors` | 336211292 |
| `tokenizer.json` | 19989325 |

下载来源与 SHA-256 记录于 `.runtime/moss-download.log`；运行时按文件名字节数钉死校验（`src/localgen/engine/moss.py`）。

| 场景 | 尺寸 | 提交/启动到终态 | RAM 峰值 | RAM 最低可用 |
| --- | --- | ---: | ---: | ---: |
| sd-cli 直跑 | 768×768 | 115.19 秒 | 15607 MiB | 1 MiB |
| 源码应用内（含取消与引擎恢复） | 1024×1024 | 215.84 秒 | 15482 MiB | 125 MiB |
| dist 打包版应用内 | 768×768 | 115.52 秒 | 15606 MiB | 2 MiB |

直跑的引擎工作集峰值为 7739 MiB，进程提交量峰值 5425 MiB。16 GB 系统内存是当前瓶颈：MOSS 出图期间系统可用内存几乎归零，出图期间应避免并行大内存负载。打包版验收同时覆盖运行中取消（sd-cli 进程确认终止）、MOSS 结束后 ComfyUI 自动恢复监听，以及随后 z-image-turbo 任务成功。样张中雨夜街角、蓝绿霓虹「深夜咖啡」招牌、红雨衣、透明雨伞与湿石板路倒影均正确呈现，中文招牌文字准确。1024×1024 连续多张压力测试留待后续。

## Qwen-Image-2.1

同一台 RTX 4050 上**不升级引擎**接入：Qwen-Image-2.1（7B 单流 DiT，32 层）直接跑已钉死的
stable-diffusion.cpp `0b3b98d`（`localgen-moss/bin/sd-cli.exe`），与 MOSS / Kontext 共用同一运行时。
文本编码器 Qwen3-VL-8B 走 CPU（`te=cpu`），扩散与 VAE 走 CUDA；DiT 用 Q4_K GGUF，VAE 是
Qwen-Image-2.1 专用 VAE（与一代 Qwen-Image / Wan2.2 的 VAE **不可互换**）。采样沿用模板内置
euler 30 步 CFG 6。工作流 `qwen-image-21/1.0.0`（runner=`sdcpp`），发布尺寸 1024×1024。

| 文件 | 字节数 |
| --- | ---: |
| `qwen_image_2.1-Q4_K.gguf` | 4197494816 |
| `Qwen3VL-8B-Instruct-Q4_K_M.gguf` | 5027784800 |
| `mmproj-Qwen3VL-8B-Instruct-F16.gguf` | 1159029824 |
| `qwen_image_2.1_vae_bf16.safetensors` | 675509688 |

来源 commit 与 SHA-256 见 `.runtime/qwen21-download.log`；字节数钉死在 `engine/sdcpp.py`
的 `QwenImageJob.MODEL_FILES`。

| 场景 | 尺寸 | 提交/启动到终态 | GPU 峰值 | RAM 峰值 | RAM 最低可用 | 引擎 RSS 峰值 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 文生图（sd-cli 直跑） | 1024×1024 | 337.52 秒 | 5605 MiB | 15605 MiB | 2 MiB | 6973 MiB |
| 编辑 · 前缀缓存启用（首次） | 1024×1024 | 835.09 秒 | 5457 MiB | 15606 MiB | 1 MiB | 6980 MiB |
| 编辑 · 前缀缓存因显存不足回退 | 1024×1024 | 604.26 秒 | 5529 MiB | 15607 MiB | 0 MiB | 7056 MiB |
| 编辑 · 打包版复核（VAE 解码 OOM → 空间分块重试） | 1024×1024 | 825.63 秒 | — | — | — | — |

- 文生图采样 30 步；编辑档 30 步实测 14.95 秒/步（采样 465.47 秒）+ VAE 解码 9.31 秒。
  16 GB 内存仍是硬瓶颈：出图期间系统可用内存降到 0–2 MiB（与 MOSS 同级），出图期间
  不得并行其他大内存任务。
- **编辑耗时接近 10 分钟，超过旧默认 `job_timeout` 600 秒**——默认值已提到 1200 秒
  （见维护日志 A 区 2026-11-09 条），既有数据目录的 `config.toml` 需自行同步。
- 6 GB 显存下前缀缓存经常拿不到（日志 `insufficient memory for prefix caching`），
  sd.cpp 会自动回退到无缓存路径，结果仍正常；此时反而更快（835 秒 → 604 秒）。
- 参考图**只裁不拉**在本模型上复验：1200×800 源图里直径 280 的红圆，经
  `imaging.reference_png` 裁到 1:1 再缩到 1024² 后为 **359×359（宽高比 1.0000）**；
  生成图里该圆仍是正圆、白/黑横条位置与宽度不变。
- 打包版（14:46 构建）在同一台机器上复跑同一条编辑任务：`generate_image completed in
  825.63s`，全程 **825.63 秒**、产物 1024×1024/692 KB，提示词要求「把圆改成蓝色」后
  生成图里圆变蓝且仍是正圆、上下横条保持原位。这一跑比直跑慢 220 秒，日志里能对上两处
  额外开销：`wan_vae segment 1/1 (graph) failed during weight preparation` →
  `Reducing VAE decode tiles from 1024x1024 to 512x512` 的显存不足重试（多一次 VAE 权重装载），
  以及前缀缓存回退。**结论：编辑档的真实上限就在 10–14 分钟，600 秒默认值会杀掉正常任务，
  1200 秒是必要的。**
- 画质：中文霓虹招牌「深夜咖啡」四字准确，红雨衣、透明雨伞、湿石板路倒影均正确（种子 42）。
- 编辑是**指令式**（无 `strength`）：参考图里不存在的对象若写进提示词，模型会自行编造
  （首测把合成色块里的红圆当伞面、编了"人物与雨伞"）；测试用合成几何图时应只描述几何与背景。

## 验收复现

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\tools\build-windows.ps1
# 在一个终端启动应用，另一个终端执行探测或基准。
.\.venv\Scripts\python.exe -m localgen --headless --engine-root "D:\ComfyUI_windows_portable" --data-dir .\.runtime\verify
# 模型目录为空时，确认 CUDA 设备和缺失依赖。
.\.venv\Scripts\python.exe .\tools\verify-engine.py .\.runtime\verify
# 安装经校验的 SD 1.5 模型后，重启应用并运行。
.\.venv\Scripts\python.exe .\tools\verify-engine.py .\.runtime\verify --workflow sd15 --sample .\.runtime\sd15-sample.png
# 安装经校验的 SDXL-Lightning 模型后，可按相同方法运行第二个工作流。
.\.venv\Scripts\python.exe .\tools\verify-engine.py .\.runtime\verify --workflow sdxl-lightning --sample .\.runtime\sdxl-sample.png
# Z-Image-Turbo 可用时，可复测 768×768。
.\.venv\Scripts\python.exe .\tools\verify-engine.py .\.runtime\verify --workflow z-image-turbo --size 768
# MOSS-Image：备齐 localgen-moss 运行时后（来源与哈希见 .runtime/moss-download.log）。
.\.venv\Scripts\python.exe .\.runtime\moss_trial.py 768
.\.venv\Scripts\python.exe .\.runtime\moss_localgen.py 768 --cancel --switch --exe dist\LocalGen\LocalGen.exe
```

- M0 验证 REST 鉴权、提交、重试键、队列上限、FIFO、取消、PNG 下载和持久历史。Windows 上通过 20 项自动化测试。
- M1 在空模型目录启动真实 ComfyUI。LocalGen 和 ComfyUI 均为独立回环监听。强制退出后引擎 PID 与监听消失。
- M2 MCP Python SDK 在精确的 `/mcp` 地址完成初始化、发现和调用。Windows 桌面离屏验证提交、取消与 PNG 预览。真实 GPU 任务也从桌面发起并完成预览，重启后可下载前次生成的 PNG。
- M3 SD 1.5 FP16 与 SDXL-Lightning 4 步均发布 512×512。Z-Image-Turbo 增加 768×768 和 1024×1024。 [Z-Image-Turbo 模型卡](https://huggingface.co/Tongyi-MAI/Z-Image-Turbo)
- M4 使用 PyInstaller 6.19.0 生成 Windows x64 文件夹版。打包使用 Python 3.11.7 和 PySide6 6.10.2。在目标机器的普通用户环境，桌面包通过演示、真实引擎空模型启动、GPU 出图及 PNG 下载。同一次运行内依次使用 SD 1.5、SDXL-Lightning 和 SD 1.5 出图，三个任务均在桌面预览成功。全新 Windows 安装环境的复验留待后续。
