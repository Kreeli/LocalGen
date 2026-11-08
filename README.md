# LocalGen

LocalGen 是 Windows 上的本地单张文生图工作台。桌面界面、REST 和 MCP 共用同一份任务历史，可查看进度、取消任务、预览和另存 PNG。

## 打开应用

双击 [Launch-LocalGen.cmd](Launch-LocalGen.cmd)。如果项目内的 `.runtime/ComfyUI_windows_portable` 已准备好，首次启动会使用它。也可以打开打包目录里的 `LocalGen.exe`，在「设置」中选择 ComfyUI Windows Portable 0.3.76 的解压目录，保存后重新打开。引擎就绪后，工作流会显示所需的模型和可选分辨率。

没有模型时，可以查看历史和引擎诊断。若要体验完整流程，在设置中选择「演示 · 程序绘图」，保存后重新打开。演示任务也会进入历史。

工作流可选 **Z-Image-Turbo · GGUF**。默认 768×768，可选 1024×1024。当前项目的 ComfyUI 已装好所需权重和加载器。

## 本机接口

- REST 根路径 `http://127.0.0.1:8765/api/v1`，存活探针为 `GET /api/v1/health`。
- MCP 使用 Streamable HTTP，端点为 `http://127.0.0.1:8765/mcp`。
- 桌面「设置 → 复制 API 令牌」可取得两种接口共用的 Bearer 令牌。提交后可按返回的任务 ID 查询状态和下载 PNG。
- 本机数据默认存于 `%LOCALAPPDATA%\LocalGen`，包括配置、SQLite 历史、PNG 和诊断日志。关闭桌面应用后可用同一目录运行无界面服务。

开发安装、MCP 调用、模型部署和打包见 [Windows 安装手册](docs/windows-setup.md)。硬件与里程碑实测见 [验收记录](docs/benchmark.md)。项目全景见 [项目说明](docs/项目说明.md)；接口基线与变更记录见 [维护日志](docs/维护日志.md)。
