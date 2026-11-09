# LocalGen

LocalGen 是 Windows 上的本地图片生成工作台。桌面界面、REST 和 MCP 共用同一份任务历史，可查看进度、取消任务、预览和另存 PNG。核心引擎跑在本机 ComfyUI Portable 与 stable-diffusion.cpp（sd-cli）上，全部计算不出本机。

## 项目结构

```
local_image/
├── src/localgen/          软件：FastAPI 服务 + PySide6 桌面 + 引擎与任务调度
│   ├── api/               REST /v1（OpenAI Images 兼容）+ /api/v1 + MCP + ComfyUI 网关
│   ├── engine/            双引擎：ComfyUI 与 sd-cli（moss / kontext / qwen-image-2.1 / pose-swap）
│   ├── desktop/           桌面端（编辑器、图库选择器、历史与详情面板）
│   ├── jobs/              任务队列、语义编辑路由（/api/v1/edit）
│   ├── storage/           SQLite 任务库、PNG 发布、姿势/服装图库
│   └── workflows/…        —— 工作流注册与校验
├── workflows/             工作流模板（不可变 manifest.toml + prompt.api.json，按 semver 目录）
├── plugins/localgen-studio/  插件：DSH 侧边栏（姿势 / 服装 / 参考图选择，调用 LocalGen）
├── docs/                  文档：方案、安装手册、验收记录、维护日志、交接文档
├── tools/                 构建与工具脚本（打包、图库导入、ComfyUI 工作流转换）
├── tests/                 单元 / Windows / 集成测试
└── data/libraries/        第三方素材库（不入库，见下方许可说明）
```

## 能力一览

| 工作流 | 引擎 | 类型 | 说明 |
| --- | --- | --- | --- |
| `qwen-image-21` | sd-cli | 文生图 + 编辑 | Qwen-Image-2.1 GGUF，8 步 turbo，单参考图指令编辑 |
| `pose-swap` | sd-cli | 换姿势 / 换装 | 双/三参考图（人物 + 姿势骨骼图 / 服装图），姿势 LoRA |
| `kontext-dev` | sd-cli | 指令编辑 | FLUX.1 Kontext，单参考图 |
| `moss-image` | moss | 文生图 | MOSS-Image SFT |
| `z-image-turbo` 等 | ComfyUI | 文生图 | ComfyUI 0.3.76 网关 |

- **换姿势 / 换服装**：内置姿势库（1439 条，多角度）与服装库（1407 条，8 分类），REST 见 `GET /api/v1/libraries/...`，语义入口 `POST /api/v1/edit`（自动分派引擎）。素材不出本机。
- **6 GB 显存适配**：8 步 + `qwen-image-2.1-turbo` sigma 预设 + 前缀缓存，换姿势 253–312 s。
- **长任务不超时**：`/v1/images/*` 为流式响应，响应头即时返回 + 心跳保活，客户端侧 300 s 的 headers 超时不再触发。
- **DSH 插件**：`plugins/localgen-studio` 在 DSH 侧边栏提供图库选择与生成面板，经同源代理路由访问本机 API。

## 打开应用

双击 [Launch-LocalGen.cmd](Launch-LocalGen.cmd)。如果项目内的 `.runtime/ComfyUI_windows_portable` 已准备好，首次启动会使用它。也可以打开打包目录里的 `LocalGen.exe`，在「设置」中选择 ComfyUI Windows Portable 0.3.76 的解压目录，保存后重新打开。引擎就绪后，工作流会显示所需的模型和可选分辨率。

没有模型时，可以查看历史和引擎诊断。若要体验完整流程，在设置中选择「演示 · 程序绘图」，保存后重新打开。演示任务也会进入历史。

## 本机接口

- REST 根路径 `http://127.0.0.1:8765/api/v1`，存活探针为 `GET /api/v1/health`。
- OpenAI Images 兼容层在 `http://127.0.0.1:8765/v1`（`/models`、`/images/generations`、`/images/edits`）。
- MCP 使用 Streamable HTTP，端点为 `http://127.0.0.1:8765/mcp`。
- 桌面「设置 → 复制 API 令牌」可取得两种接口共用的 Bearer 令牌。提交后可按返回的任务 ID 查询状态和下载 PNG。
- 本机数据默认存于 `%LOCALAPPDATA%\LocalGen`，包括配置、SQLite 历史、PNG 和诊断日志。关闭桌面应用后可用同一目录运行无界面服务。

## 图库素材许可（重要）

姿势 / 服装图库来自第三方开源项目 [comfyui-aicg3d-PoseSwap](https://github.com/JGRFW/comfyui-aicg3d-PoseSwap)，**本仓库不携带素材**（姿势库限制商业使用，服装库未找到覆盖再分发的许可）。如需使用：

```powershell
git clone https://github.com/JGRFW/comfyui-aicg3d-PoseSwap.git <本地路径>
.\.venv\Scripts\python.exe tools\import_pose_libraries.py --src <本地路径> --dst data\libraries
```

导入前请阅读该项目内的 `LEGAL_AND_SAFETY.md`、`THIRD_PARTY_NOTICES.md` 与各图库的 `LICENSE` / `NOTICE`，确认你的用途获得授权。人像图库因原作者请求避免再分发，未包含在导入器中。

## 文档

- [Windows 安装手册](docs/windows-setup.md) —— 开发安装、MCP 调用、模型部署和打包
- [项目说明](docs/项目说明.md) —— 架构与设计全景
- [验收记录](docs/benchmark.md) —— 硬件与里程碑实测
- [维护日志](docs/维护日志.md) —— 接口基线与变更记录
- [姿势服装图库接入方案](docs/姿势服装图库接入方案.md) —— 引擎选型、实测数据与取舍
- [交接文档](docs/交接文档.md) —— 各批次交付与遗留事项
