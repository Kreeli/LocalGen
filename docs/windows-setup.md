# Windows 安装和运行

## 桌面版

1. 保持整个 `dist\LocalGen` 文件夹，双击其中的 `LocalGen.exe`。从项目目录也可以双击 `Launch-LocalGen.cmd`。
2. 解压 [ComfyUI 0.3.76 Windows NVIDIA Portable](https://github.com/Comfy-Org/ComfyUI/releases/tag/v0.3.76)。确认解压目录包含 `ComfyUI\main.py` 和 `python_embeded\python.exe`。首次运行可保持模型目录为空。
3. 在「设置」中选择解压目录。保存并重新打开，状态栏应显示 NVIDIA CUDA 设备，工作流会列出缺失依赖。应用会启动和关闭自己的 ComfyUI 进程。请为 LocalGen 使用独立的 Portable 目录和端口。
4. 将 [Stable Diffusion 1.5 FP16](https://huggingface.co/Comfy-Org/stable-diffusion-1.5/blob/main/v1-5-pruned-emaonly-fp16.safetensors) 放在 Portable 的 `ComfyUI\models\checkpoints`。文件名为 `v1-5-pruned-emaonly-fp16.safetensors`。预期 SHA-256 为 `e9476a13728cd75d8279f6ec8bad753a66a1957ca375a1464dc63b37db6e3916`。适用许可见模型卡。开启的分辨率以 [验收记录](benchmark.md) 和应用列表为准。
5. 如需 SDXL-Lightning，将 [ByteDance 的完整 4 步权重](https://huggingface.co/ByteDance/SDXL-Lightning/blob/main/sdxl_lightning_4step.safetensors) 放在同一目录。文件名为 `sdxl_lightning_4step.safetensors`。预期 SHA-256 为 `e0d996ee0013e79d9d3561f50fcafb9a17e3ff07b780358e3b66d67932c4d490`。

遇到端口冲突时，可在设置中修改 LocalGen 端口（默认 8765）或 ComfyUI 端口（默认 8188）。LocalGen 的「设置 → 打开诊断日志」能查看本机错误记录。

## Z-Image-Turbo

当前项目的 Portable 已装好以下文件。迁移到另一份 ComfyUI 0.3.76 时，按模型路径复制。

| 模型 | 相对 `ComfyUI\models` 的位置 |
| --- | --- |
| [Z-Image-Turbo Q4_K_M](https://huggingface.co/unsloth/Z-Image-Turbo-GGUF/blob/b40f712c569ecfff42f2307dcc43399ae1d71318/z-image-turbo-Q4_K_M.gguf) | `diffusion_models\z-image-turbo-Q4_K_M.gguf` |
| [Qwen3-4B Q4_K_M](https://huggingface.co/unsloth/Qwen3-4B-GGUF/blob/22c9fc8a8c7700b76a1789366280a6a5a1ad1120/Qwen3-4B-Q4_K_M.gguf) | `text_encoders\Qwen3-4B-Q4_K_M.gguf` |
| [VAE](https://huggingface.co/Comfy-Org/Z-Image-Turbo/blob/6fc90a3b1b653e935a0d175e260736de25b84df5/split_files/vae/ae.safetensors) | `vae\ae.safetensors` |

从 [固定版本的 ComfyUI-GGUF ZIP](https://github.com/city96/ComfyUI-GGUF/archive/fa48477de8643e70506676ea3d08969e646e3372.zip) 原样复制 `__init__.py`、`nodes.py`、`loader.py`、`ops.py`、`dequant.py` 和 `LICENSE` 到 `ComfyUI\custom_nodes\localgen_gguf`。LocalGen 会校验加载器的源文件哈希。在 Portable 根目录安装模型读取依赖后，重新打开 LocalGen。

```powershell
.\python_embeded\python.exe -s -m pip install --no-deps gguf==0.17.1
```

## 源码运行与打包

在项目目录用 Windows x64 Python 3.11 创建独立环境。若 `py -3.11` 不可用，将该命令替换为本机 Python 3.11 的完整路径。

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-windows.lock.txt
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
.\.venv\Scripts\python.exe -m localgen
.\.venv\Scripts\python.exe -m pytest -q
.\tools\build-windows.ps1
```

打包结果在 `dist\LocalGen`。将该目录作为一个整体分发。ComfyUI、模型和用户生成的图片独立部署。修改程序后需要重新打包。

```powershell
# 独立演示数据。启动后按 Ctrl+C 停止服务。
.\.venv\Scripts\python.exe -m localgen --headless --mock --data-dir .\.runtime\demo --port 8766
# 另开 PowerShell，读取同一目录的令牌。
$token = .\.venv\Scripts\python.exe -m localgen --data-dir .\.runtime\demo --show-token
$auth = @{ Authorization = "Bearer $token"; "Idempotency-Key" = [guid]::NewGuid().ToString() }
$body = @{ workflow_id = "baseline"; prompt = "A lighthouse at sunrise"; seed = 42 } | ConvertTo-Json
$job = Invoke-RestMethod http://127.0.0.1:8766/api/v1/jobs -Method Post -Headers $auth -ContentType "application/json" -Body $body
Invoke-RestMethod "http://127.0.0.1:8766/api/v1/jobs/$($job.job_id)" -Headers $auth
```

MCP 客户端的传输类型选择 Streamable HTTP，地址填 `http://127.0.0.1:8765/mcp`，请求头填 `Authorization: Bearer <令牌>`。MCP 的六个工具为 `list_workflows`、`generate_image`、`get_job`、`list_jobs`、`cancel_job` 和 `delete_job`。图片用相同令牌从返回的 REST 路径下载。

## 数据和备份

配置文件位于 `%LOCALAPPDATA%\LocalGen\config.toml`。端口、队列大小、启动与取消超时、磁盘门槛均可在关闭应用后编辑。自定义目录可用 `--data-dir`。同一数据目录只允许一个 LocalGen 实例。

备份时先正常关闭应用，确认引擎已停止，再复制整个数据目录。SQLite 使用 WAL，运行中如需单独备份数据库，请使用 SQLite Backup API，并同时保存成品目录。API 令牌由 Windows DPAPI 绑定到当前 Windows 用户，跨用户迁移时应在新账户生成新的令牌。ComfyUI 端口仅供 LocalGen 使用。两个端口都绑定本机回环地址。
