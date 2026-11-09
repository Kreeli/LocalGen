# LocalGen V1 项目规格

**用途**　交给 Codex 的实现任务书  
**目标平台**　Windows 11，单用户、单机、本地文生图  
**硬件基线**　NVIDIA RTX 4050 Laptop GPU（6 GB 独立显存），AMD Ryzen 7 7735 系列（带集显），16 GB 系统内存。用户已明确维持现有硬件。  
**验证状态**　架构已定。模型、工作流、性能与 Windows 打包仍需实机验证。

## 1. 交付范围

用户打开独立的 PySide6 桌面应用后，可以查看引擎状态和可用工作流、输入提示词、提交和取消任务、浏览历史和预览图片。其他本机程序可以通过 REST 或 MCP 提交同类任务并查询同一份历史。应用负责启动、监控和关闭独立的 ComfyUI 进程。

V1 只做单张文生图，暂不纳入视频、图生图、批量出图、多 GPU、远程访问、模型市场或工作流编辑器。初始安装、首次启动和自动化测试均可在模型目录为空时完成。模型权重的下载及手动导入安排在后续实机验证阶段。首次启动无模型时仍可查看诊断和历史，生成入口应明确显示缺失的工作流依赖。

## 2. 进程与数据架构

~~~text
本机 REST 客户端 ────────────────┐
本机 MCP 客户端 ── /mcp ────────┤
PySide6 桌面界面 ── 异步 REST ───┤
                                ▼
        LocalGen 进程（Qt 主线程 + 单个 FastAPI/Uvicorn 服务线程）
        应用服务 → SQLite 任务库 + 持久 FIFO → 单个调度器
                                                    │ 本机 HTTP / WebSocket
                                                    ▼
                                      独立 ComfyUI 子进程
                                          RTX 4050 → 图片
~~~

- Qt 主线程只处理交互与界面更新。FastAPI 生命周期拥有调度器、数据库资源、MCP 会话管理器和引擎管理器。CLI 可用同一启动代码运行无界面开发模式，桌面与无界面模式互斥。
- 同一数据目录只允许一个 LocalGen 实例，使用 Windows 实例锁并在启动调度前取得。FastAPI 单 worker。SQLite 是任务状态和队列的唯一事实来源，启用 WAL、外键和有限的 busy timeout。用短事务提交，按持久递增序号取最早的 queued 任务。
- ComfyUI 使用自己的 Python、PyTorch 与依赖。LocalGen 的 Python 环境只安装应用依赖。两者通过 ComfyUI 的公开服务接口通信。ComfyUI Portable 只是候选发行形态，启动路径、参数与目录布局随锁定版本验证。
- 建议默认端口 LocalGen 8765、ComfyUI 8188，均绑定 127.0.0.1，可在启动前修改。检测冲突并给出端口与诊断。不得接管占用端口的未知进程。
- 运行数据默认存于 %LOCALAPPDATA%/LocalGen，包括配置、数据库、日志、暂存和成品。引擎目录独立配置。模型存于引擎认可的位置，数据库只保留任务、工作流快照引用和结果元数据。

## 3. 项目目录

~~~text
LocalGen/
├── pyproject.toml
├── README.md
├── config/
│   └── default.toml
├── src/localgen/
│   ├── __main__.py            # 桌面启动、开发用无界面模式
│   ├── desktop/               # Qt 窗口、API 客户端、界面状态
│   ├── api/                   # FastAPI 应用、路由与请求模型
│   ├── mcp/                   # MCP 工具与传输装配
│   ├── jobs/                  # 应用服务、状态机、调度器
│   ├── storage/               # SQLite、迁移、结果文件
│   ├── engine/                # Windows 子进程、ComfyUI 客户端与模拟引擎
│   └── workflows/             # 工作流清单、校验及参数映射
├── workflows/
│   └── <workflow-id>/<revision>/
│       ├── manifest.toml
│       └── prompt.api.json
├── tests/
│   ├── unit/
│   ├── integration/
│   └── windows/
└── docs/
    ├── windows-setup.md
    └── benchmark.md
~~~

## 4. REST 契约

REST 前缀为 /api/v1。除不含环境细节的存活探针外，REST 和 MCP 均使用本机 Bearer 令牌。提交只负责校验和持久入队，调用方轮询任务状态。

| 方法及路径 | 契约 |
|---|---|
| GET /health | 返回 API 存活状态。引擎不可用时仍可返回 200。 |
| GET /engine | 返回引擎状态、可用性及脱敏诊断码。 |
| GET /workflows | 返回工作流 ID、版本、可用性、已验证尺寸和支持的参数。 |
| POST /jobs | 返回 202 和 Job，Location 指向任务详情。 |
| GET /jobs | 支持 status、limit（默认 20，上限 100）、不透明 cursor。按创建顺序倒序分页，返回 items 和 next_cursor。 |
| GET /jobs/{job_id} | 返回 Job，包括终态错误或图片元数据。 |
| POST /jobs/{job_id}/cancel | 活动任务接受取消时返回 202，排队取消已落库或任务已取消时返回 200。已成功或失败返回 409。 |
| GET /jobs/{job_id}/artifacts/{artifact_id} | 下载或预览已成功提交的 PNG。未知 ID 或结果文件缺失返回 404。 |

最小提交示例。此处的工作流 ID 和尺寸用于契约测试，生产可用值由工作流列表公布。

~~~json
{
  "workflow_id": "baseline",
  "prompt": "雨后街道，暖色窗灯",
  "width": 512,
  "height": 512,
  "seed": 42
}
~~~

- workflow_id、prompt 必填，提示词去除首尾空白后长度 1–2000 字符。尺寸必须成对提供，省略时用工作流默认值。只接收该版本清单中已启用的尺寸组合。seed 可省略，由服务生成并保存实际使用值。对外限制为 0 到 4294967295。negative_prompt 只在支持该输入的工作流中可用。未知字段或不支持的参数返回 422。
- Job 字段包括 job_id（UUID）、status、workflow_id、workflow_revision、实际 parameters、created_at、started_at、finished_at、progress、artifacts、error。时间用 UTC ISO 8601，尚无值的时间和错误用 null。progress 可为 null，或含 stage、current、total 的对象。artifact 含 artifact_id、mime_type、width、height、size_bytes 和相对下载路径。API 不暴露磁盘绝对路径。
- 建议支持 Idempotency-Key。带键重试时，同键同规范化请求返回原任务，同键异参返回 409。比较和插入在同一事务内完成。比较客户端提交的规范化请求，生成的 seed 和选择的工作流版本随首个任务持久化。
- 统一应用错误为 {"error":{"code":"ENGINE_UNAVAILABLE","message":"引擎暂不可用","retryable":true}}。未知资源 404，状态冲突 409，语义校验失败 422，队列满 429，引擎或工作流依赖暂不可用 503。鉴权和安全策略分别使用 401 与 403。进程日志与内部堆栈仅写本机诊断日志。
- 初始工程默认最多 20 个等待任务，另有最多 1 个活动任务。只在引擎就绪且工作流可用时接收新任务。此前入队的任务在引擎离线时保留，查询和取消仍可用。

## 5. MCP 工具

以 Streamable HTTP 提供精确入口 http://127.0.0.1:8765/mcp。锁定 Python SDK 版本后，按对应版本装配挂载路由，在 FastAPI lifespan 显式启动 MCP session manager。验收必须用 MCP 客户端走初始化、工具发现与调用，确认无需路径重定向。工具统一调用应用服务，沿用 REST 的校验、权限、任务 ID 和错误码。协议级错误由 SDK 处理。 [Python SDK ASGI 集成](https://py.sdk.modelcontextprotocol.io/run/asgi/)

| 工具 | 输入与返回 |
|---|---|
| list_workflows | 无参数。返回工作流能力与可用性。 |
| generate_image | 同 POST /jobs，额外允许可选 idempotency_key。立即返回 Job。 |
| get_job | job_id。返回 Job 和可用的结果下载路径。 |
| list_jobs | status、limit、cursor 均可选。返回分页结果。 |
| cancel_job | job_id。返回取消后的 Job 或结构化错误。 |

模型生成可跨越一次 MCP 调用的生命周期，查询进度走 get_job。客户端自行配置 API 地址与令牌来取图。可选 stdio 桥仅作为现有服务的客户端，复用同一 REST 契约与令牌，标准输出只承载协议消息。HTTP 验收通过后再做 stdio 兼容性验证。

## 6. 工作流适配与图片交付

每个工作流是经审查、不可变版本的 ComfyUI API 图加清单。清单描述节点与输入字段映射、输出节点、模型及节点依赖、默认参数、已验证尺寸、资源档位与模板摘要。入队时固定版本、摘要及实际参数。工作流升级后，旧任务仍用原版本。原版本缺失时以 WORKFLOW_REVISION_MISSING 结束该任务。

适配器克隆模板并只填允许的字段，输出前缀由任务 ID 派生。通过 /object_info 或版本支持的公开接口检查节点和依赖。通过 POST /prompt 提交，记录 prompt_id。通过 /ws 获取可用的进度事件，断线后结合 GET /queue 和 GET /history/{prompt_id} 判定执行情况。成功结果按预期输出节点读取，使用 /view 获取图片。固定版本的队列删除、interrupt 参数和确认行为须以端到端用例核实。 [ComfyUI 服务路由](https://docs.comfy.org/development/comfyui-server/comms_routes)

只接收预期节点产生的单张 PNG。限制下载大小和像素数，检查文件类型、完整性及尺寸。结果先写入应用私有暂存目录，再在同卷原子移动到按 job_id 命名的最终目录，最后事务写入结果记录和 succeeded 状态。下载只根据数据库中的 artifact_id 解析到受控路径。启动时隔离无记录成品与残留暂存文件，已记录但丢失的图片标记不可用并提供诊断，保留原任务状态。

## 7. 任务状态、取消与恢复

~~~text
queued → submitting → running → succeeded
   │         │          ├──────→ failed
   │         ├──────────┴──────→ cancelling → cancelled / failed
   └─────────→ cancelled
submitting / running / cancelling → interrupted（重启时无法确认结果）
~~~

- 调度器通过条件更新，在同一事务中认领最早的 queued 任务并标记 submitting。提交超时属于结果未知。V1 对这种情况回收自有引擎以清空遗留工作，再以明确错误结束任务。禁止盲目重发 /prompt。确认旧进程退出后才可启动新进程并继续出队。
- queued 的取消通过条件更新直接落库。submitting 的取消先写入 cancelling，再等待提交结果或回收引擎。拿到 prompt_id 后，通过队列检查定位目标。目标仍在 ComfyUI 等待时仅删除该项，已运行时仅在独占引擎确认目标归属后发 interrupt。外部客户端不得直接向该实例提交任务。
- 运行中取消受理后立即进入 cancelling，先确认目标不再运行或等待，再记为 cancelled 并释放槽位。无法确认时回收引擎。若无法回收，暂停调度并报告故障。取消与成功由条件更新仲裁。成功先提交时取消返回冲突，取消先提交时后续产物不发布。超时走同一停止流程，终态为 failed，记录 JOB_TIMEOUT。
- LocalGen 重启时保留 queued 的顺序。在确认上一代受管引擎已经退出后，把遗留的 submitting、running、cancelling 标为 interrupted。V1 不自动重跑已认领任务，用户可明确创建新任务。WebSocket 单独断开时继续查询当前引擎的队列和历史，单次断线不会改变任务终态。
- 引擎异常退出时结束活动任务，记录 ENGINE_EXITED，暂停出队，按有限次数退避重启。检测到显存不足时记 GPU_OOM，检查引擎健康，必要时重启。重试由用户显式发起，应用可提示选择已验证的小尺寸。

## 8. Windows 子进程与本机安全

- 引擎状态为 stopped、starting、ready、stopping、error。启动使用固定版本验证过的 Python 可执行文件与参数数组、明确的工作目录以及独立环境。移除继承的 PYTHONPATH、虚拟环境变量和 LocalGen 令牌。将标准输出和错误输出持续排空至轮转日志。
- 通过 Windows Job Object 管理进程树并启用关闭句柄时终止。启动时避免子进程在加入 Job Object 前开始派生进程。用有截止时间的探测确认进程存活、监听归属、API 可用和 NVIDIA CUDA 设备。引擎就绪与工作流就绪分别显示。记录进程标识和启动身份用于诊断，不凭 PID 单独杀进程。
- 正常退出时先停止接收新任务，保留排队记录，按取消流程收尾活动任务。在限定时间内使用已验证的停止方式，超时终止自有进程树。验证强制关闭 LocalGen 后不会遗留监听和 GPU 工作。
- 对 REST 和 MCP 校验 Host 以及存在的 Origin，允许显式配置的本机地址及实际端口。默认关闭跨域访问和公开 API 文档。令牌使用高熵随机值，存储在当前用户可读的受保护配置中。日志不记录令牌，默认也不记录提示词全文。令牌和回环绑定用于隔离网页和非授权 API 调用。本机同账户进程仍属于信任边界内。
- ComfyUI 同样只监听回环。其端口是引擎内部接口，V1 在本机信任边界内运行，发布文档明确要求不共用该实例。拒绝调用方提供的任意工作流、执行命令、远程 URL、任意文件路径和节点安装请求。对路径穿越、符号链接或目录联接做约束。配置文件与 API 输入均校验，设置请求体、队列和日志上限。磁盘余量低于配置门槛时拒绝新任务，已有历史可继续查看。

## 9. 固定硬件上的模型验证

6 GB 独立显存是设计预算。Windows 显示的共享 GPU 内存来自系统内存，与独立显存分开记录，且与应用、操作系统和 CPU 卸载共同争用 16 GB RAM。AMD 集显用于系统显示场景，生成由 RTX 4050 承担。分页和卸载可能影响速度与系统响应，需实测。所有工作流默认 batch=1，尺寸以实测白名单为准。

| 顺序 | 验证任务 | 放行条件 |
|---|---|---|
| A | 用模拟引擎完成任务、取消和结果 API。随后用空模型目录启动真实 ComfyUI 并验证公开 API、设备及缺依赖提示。 | 不安装模型即可完成应用开发与启动验收。 |
| B | 在后续验证环境准备一个小型、已知可在该环境完成出图的工作流，优先内置节点。从小尺寸、单张、固定提示词与种子开始。 | 桌面、REST 和 MCP 均可触发同一引擎出图，结果可在重启后读取。 |
| C | 测试 SDXL-Lightning 基准与回退配置。从小尺寸开始，候选如 512×512，按模型卡及选用工作流匹配权重变体、采样器、步数和调度。逐档扩展。 | 仅发布稳定性和画质达标的尺寸。小型验证工作流保留为回归夹具，SDXL-Lightning 未达标时记录阻塞。 |
| D | 实验验证 Z-Image-Turbo。先核实候选量化权重、加载器、文本编码器、VAE、节点版本和整图兼容性，再从小档开始。 | 仅启用实测合格的组合。量化格式由兼容性证据决定，Q4_K_M 只是待验证选项。失败时保留已通过的基准。 |

每档记录 ComfyUI、驱动、PyTorch/CUDA、节点和模型版本或哈希、实际参数、首张耗时、热启动耗时、独立显存峰值、系统 RAM 峰值、分页迹象和输出质量。建议发布门槛为一次冷启动加至少五次连续热生成，包含取消后再生成检查。逐档检查 OOM、系统响应与画质，若失败则停在上一个通过的档位。目标延迟在实测后写入发布配置。两个候选模型的可用性及分辨率上限均待验证。 [Z-Image-Turbo 模型卡](https://huggingface.co/Tongyi-MAI/Z-Image-Turbo) · [SDXL-Lightning 模型卡](https://huggingface.co/ByteDance/SDXL-Lightning)

## 10. Windows 部署顺序

1. 在 Windows 11 上记录 NVIDIA 驱动和 GPU 识别情况，确认工作盘余量。选择与驱动兼容且可锁定的 ComfyUI NVIDIA 发行版。若使用 Portable，按该发行版文档解压并先验证启动入口、解释器位置和命令参数。 [Windows Portable 文档](https://docs.comfy.org/installation/comfyui_portable_windows)
2. 开发环境中为 LocalGen 建立独立虚拟环境，安装锁定的应用依赖。发布版交付可直接运行的 Windows 桌面程序，以普通用户权限启动。两种方式都复用同一运行配置及数据布局。
3. 在 LocalGen 设置中指定引擎根目录及独立解释器，检查回环地址、端口和数据目录。首次运行创建数据库和令牌，启动引擎并显示健康检查与工作流依赖。使用空模型目录完成初始冒烟测试。
4. 进入实机基准阶段后，才按选定工作流清单准备模型文件并验证版本及哈希。依次运行小型工作流、SDXL-Lightning 和 Z-Image-Turbo 实验。把通过的版本、尺寸与启动参数写回清单。
5. 用发布包重跑安装、退出、重启、数据库迁移和端到端验收。升级引擎版本前保留原版本及配置，先在隔离数据目录测试。备份 SQLite 使用一致性备份方式，并同时保留成品目录。

## 11. 里程碑与验收

| 里程碑 | 开发任务 | 完成条件 |
|---|---|---|
| M0 · 契约与持久队列 | 初始化工程、数据迁移、状态机、模拟引擎与 REST。 | 重复键只创建一个任务，队列满返回 429，并发提交严格串行，重启后等待顺序保持。 |
| M1 · 引擎进程与适配 | 完成 Windows 进程管理、启动诊断、API 图适配及原子交付。 | 空模型启动可用。端口冲突、错误解释器、引擎崩溃、强制退出及提交响应丢失均有确定结果。 |
| M2 · 桌面和 MCP | 实现任务表单、历史、预览、取消与本机配置。接入 MCP。 | 三个入口共享任务 ID 和状态，Qt 界面在生成中可操作，真实 MCP 客户端可初始化、列举工具及提交。 |
| M3 · 实机验证 | 完成小工作流闭环和两候选模型基准。 | 小工作流出图并可在重启后读取。只发布通过实测的模型配置。Z-Image-Turbo 实验可独立延期。 |
| M4 · Windows 交付 | 打包、部署手册、依赖清单与验收记录。 | 普通用户从空模型目录启动桌面程序成功。至少一个实机通过的文生图配置完成交付验证。 |

必须覆盖以下集成场景，模拟引擎用于重现时序和故障，Windows 实机用于验证进程管理与 GPU 行为。

- queued、submitting、running 三个阶段分别取消。覆盖取消与成功的竞争、重复取消和取消后下一任务正常运行。
- 在提交响应丢失、运行中引擎退出、LocalGen 被强制结束及图片移动后事务提交前注入故障。验证无重复推理、无孤儿进程、队列可恢复及结果无错误发布。
- 验证未授权 REST/MCP 请求、异常 Host/Origin、非回环监听检查、伪造 artifact_id、恶意文件路径、超大请求、缺模型、显存不足和磁盘不足。
- 在真实 Windows 环境验证 RTX 4050 设备选择、监听端口隔离、路径含空格或中文、SQLite 持久化、PNG 下载与桌面预览。

## 12. 待定技术项

| 决策 | 落地时点与依据 |
|---|---|
| LocalGen Python、PySide6、FastAPI、MCP SDK 版本与 Windows 打包工具 | M0 锁定开发依赖，M4 根据普通用户启动、Qt 插件加载和干净 Windows 冒烟测试锁定打包方案。 |
| ComfyUI 发行版、启动参数与 CUDA 环境 | M1 通过空模型启动、进程树回收和公开 API 契约测试后锁定。低显存相关参数以基准结果决定。 |
| 小型工作流与两候选模型配置 | M3 确定权重来源、许可、哈希、节点依赖、默认尺寸和实际资源上限。 |
| 启动、推理、取消、重启时限及磁盘门槛 | M1 设可配置的有限默认值。M3 按实测冷启动、任务耗时及故障恢复时间调优。 |
| MCP 会话模式与客户端兼容 | M2 优先验证无状态 JSON 响应模式及精确 /mcp 路径。客户端兼容确有需求时再评估有状态模式或 stdio。 |

实现从 M0 开始。每个里程碑记录可复现的验证方式和剩余阻塞，模型和硬件结论必须来自目标机器上的基准记录。
