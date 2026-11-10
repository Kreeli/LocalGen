# localgen-studio · 接口速查（改代码前先读这个）

插件分两半：Host `index.js`（Node，带令牌转发）+ Client `client.js`（浏览器，同源 fetch）。
LocalGen 的 `LocalSecurity` 会按 Host/Origin 白名单 403 掉浏览器直连，**任何**请求都必须走 Host 路由。

---

## 一、Host 路由（index.js，挂在 DSH 自己的 webServer 上）

前缀 `const PREFIX = '/plugins/localgen-studio'`；注册方式：

```js
ctx.inject(['webServer'], (web) => {
  const route = (routePath, handler, label) =>
    web.effect(() => web.webServer.register({ kind: 'exact', path: routePath, handler }), label);
  route(ROUTE_XXX, async (req, res) => { ... }, 'localgen-studio: xxx route');
});
```

| 常量 | 路径 | 方法 | 入参 → 出参 |
|---|---|---|---|
| `ROUTE_STATE` | `/state` | GET | → `{version, baseUrl, exePath, tokenConfigured, tokenSource}` |
| `ROUTE_CONFIG` | `/config` | POST | `{baseUrl?, token?, exePath?}` → 同上（空 token = 不改） |
| `ROUTE_OPEN` | `/open` | POST | 无 → `{status:'running'\|'launched'\|'missing', message, baseUrl}`；先探 `/api/v1/health` 2s，通就返回 running，否则 `spawn(DEFAULT_EXE, {detached, stdio:'ignore'})` |
| `ROUTE_OPEN_IMAGE` | `/open-image` | POST | `{path, name?}` → `{status:'opened', file}`；Host 侧下载到 `os.tmpdir()` 再 `cmd /c start` 交给系统查看器 |
| `ROUTE_RPC` | `/rpc` | POST | `{path, method?, body?}` → `{status, ok, data}`（**永远 200，错误在 ok/data 里**） |
| `ROUTE_RAW` | `/raw` | GET | `?path=/api/v1/...` → 原始字节（给 `<img src>`，thumb 走 86400 缓存） |
| `ROUTE_UPLOAD` | `/upload` | POST | `?filename=xxx` + 裸字节 body → `{status, ok, data}`，Host 组 multipart 打到 `/api/v1/images` |

通用约束（每个 handler 第一行）：
- `sameOrigin(req)` —— Origin 的 host ≠ req.headers.host 就 403（没有 Origin 才放行）
- `safeApiPath(v)` —— 只认 `/api/v1/` 前缀，拒 `..` `\` `\n`，失败返 null
- 令牌缺失返 **428** `{error:{code:'TOKEN_MISSING'}}`

常量：`DEFAULT_BASE='http://127.0.0.1:12138'`、`DEFAULT_EXE='D:\garbage pile\local_image\dist\LocalGen\LocalGen.exe'`、`RECORD_KEY='localgen-studio/default'`、`JSON_LIMIT=2MiB`、`UPLOAD_LIMIT=64MiB`、`RPC_TIMEOUT_MS=30s`、`UPLOAD_TIMEOUT_MS=300s`、`RAW_TIMEOUT_MS=60s`。

令牌存储：`credentials` 服务 `api-key` 记录（`key` + `env.LOCALGEN_BASE_URL`）→ 回落 `$DSH_HOME/localgen-studio/settings.json`（0600）。
`readStored` 令牌可从文件兜底；`writeStored` **空 token = 保持不变**（不要改回"清空"，那会丢令牌）。

---

## 二、Client 侧传输函数（client.js）

```js
async function rpc(path, method, body)            // POST /rpc，非 2xx 抛错
async function callApi(path, method, body)        // 包 rpc，!result.ok 抛 Error（带 code/routedTo/routeReason）
function rawUrl(apiPath)                          // → `${ROUTE_RAW}?path=…`，给 <img src>
async function uploadReference(file)              // POST /upload，返回 data（含 .name = 图片 token）
async function openImage(apiPath, name)           // POST /open-image
async function openLocalGen()                     // POST /open
```

草稿持久化（`conversation.view` 切换视图会卸载组件，必须靠这个）：
```js
const DRAFT_KEY = 'localgen-studio:draft:v1';
readDraft() → object
writeDraft(patch)                                 // 合并写，null/undefined 自动删键
```

---

## 三、后端 API 调用点（改后端时照这张表核对）

| 位置 | 调用 |
|---|---|
| L555 | `GET /api/v1/libraries/{poses\|garments}?…` 列表 |
| L628 / L649 / L1237 / L1255 | `GET /api/v1/libraries/{poses\|garments}/{id}/thumb`（走 rawUrl） |
| L698 | `GET /api/v1/workflows` → `{items:[{workflow_id,title,verified_sizes,required_parameters,supported_parameters,…}]}` |
| L863 | `GET /api/v1/jobs/{id}` |
| L978 / L988 | `POST /api/v1/jobs`（提交，body 至少有 `workflow_id`/`prompt`/`width`/`height`/`seed`，可选 `steps`/`cfg_scale`/`image`/`pose_id`/`garment_id`/`negative_prompt`/`strength`/`pose_steps`/`garment_steps`） |
| L1374 | `GET /api/v1/jobs?…` 分页画廊 |
| L1394 | `POST /api/v1/jobs/{id}/cancel` |
| L1406 | `DELETE /api/v1/jobs/{id}` |
| L1431 | `GET /api/v1/libraries/{kind}/{id}` 单条详情 |
| L1641 | `GET /api/v1/settings` |
| L1680 | `PUT /api/v1/settings` `{[key]: value}` |
| L1756 | `GET /api/v1/progress` → `{job, queued}`（**只有这个有 current/total**；`/api/v1/status` 没有） |
| L82 / L751 / L987 / L1459 / L1504-1505 | `GET /api/v1/jobs/{id}/attachments/reference` → PNG 字节（**本次新增**，见 §九） |

---

## 四、Slot 注册（client.js 末尾 `return {name, inject:['slots'], apply(ctx)}`）

| slot | id / key | order | 组件 |
|---|---|---|---|
| `conversation.view` | `id='localgen'` | 30 | `Workbench`（顶部三 tab：生成/历史/设置） |
| `sidebar.panellist` | `id='localgen-studio'` | 40 | `StudioGlyph` |
| `main` | `key='localgen-studio'` | — | `StudioPage`（侧边栏全屏面板） |

```js
ctx.slots.inject('conversation.view', () => ctx.slots.register({
  name: 'conversation.view', id: 'localgen', order: 30,
  label: () => 'LocalGen', inject: () => ({}),
}, (props) => { ... }));
```

## 五、Client 组件清单（client.js，1950 行）

`StudioGlyph`(284) / `groupPoses`(299) / `CropDialog`(335) / `Picker`(530) / `useConnection`(663) /
`GeneratePanel`(687) / `HistoryPanel`(1359) / `SettingsPanel`(1617) / `StatusCard`(1746) /
`Workbench`(1807) / `StudioPage`(1895)

关键约定：
- `requiredImage = required_parameters.includes('image')`；`needsRefInput = requiredImage || supportsPose || supportsGarment`（**别用 supported_parameters 判断**）
- **通用步数 / CFG 框对所有工作流都显示**（全局覆盖机制，详见第六节警告）——别按 `supported_parameters` 收窄
- 三个 tab 全部常驻渲染，非活动 tab 用 `display:none`（切 tab 不丢输入）
- 主按钮配色用 `color: var(--dsw-alias-bg-base)`，**不能写死 `#fff`**（亮色主题下看不见）
- `CropDialog` 客户端裁切后上传（后端 `/api/v1/images` 不收裁切框参数）
- 参考图状态是 `{name, label, preview, previewKind, originJobId}`；`name` 是提交用 token，
  `preview` 是显示用，`originJobId` 用于 token 过期后重取。**别把 `blob:` 存进草稿**（见第九节）。

## 六、pose-swap 1.1.0 两段式与分段步数

同时选中姿势 **和** 服装时，引擎自动跑两段（姿势段 → 换装段），一次提交，调度器/REST/MCP 契约无感。

**新字段**（`Submission`，`src/localgen/jobs/models.py:47`）：`pose_steps` / `garment_steps`，int 1–25，均可选。
- 只由 pose-swap 的 manifest 声明 → 其他工作流传了返回 **422 UNSUPPORTED_PARAMETER**
- 不传 = 引擎回落到 `steps` → 再回落到**模板默认 15**（`sdcpp.py:473` `_stage_steps`；1.1.0 前是 8）
- **10b 起模板默认 8 → 15**：用户实测 8 步出图偏模糊，`qwen-image-2.1-turbo` 8 档 sigma 预设已从模板移除，15 步走 euler 常规 schedule（`sampling_method = euler`）
- 两段共用提交 seed

**插件侧 UI 规则**：
- `supportsStageSteps` 为真（pose_steps + garment_steps 都声明）时，**通用「步数」框隐藏** —— 分段控件已覆盖它，它的值会同时喂给两段当基线，留着误导。**CFG 框保留不动。**
- 隐藏时提交侧也不再发 `steps`（`if (!supportsStageSteps && ...)`），否则草稿残留值会变成看不见的全局基线；`stepsError` 校验、历史重跑透传同样跳过
- 占位文案**从 `engine_parameters.steps` 动态推导**（`stageStepsPlaceholder`），不写死数字 —— 以后模板再改步数，插件自动跟随

**进度**：`detail` 带前缀 `姿势段 · …` / `换装段 · …`，`stage` 仍是 loading/sampling/decoding。
**日志**：`logs/sdcpp-<job>-s1.log`（姿势段）+ `logs/sdcpp-<job>.log`（换装段）。

**插件侧实现**（client.js）：
- 能力位 `supportsPoseSteps` / `supportsGarmentSteps` → 合成 `supportsStageSteps`，两者都声明才显示 UI
- `poseSteps` / `garmentSteps` 两个 state，进草稿、进历史回填/复用/重跑、进详情
- 只在该工作流声明且用户真填了才写进 body（`Number(...)`，留空不传）
- 校验 `stageStepsError`：整数 1–25，已并入 `canGenerate` 与 `disabledReason`
- UI 位置：模型与参数卡片里，紧跟在「种子/步数/CFG」那一行之后

**⚠️ `steps` / `cfg_scale` 是有意不在 `supported_parameters` 里的 —— 不要"修"它。**
它们是**全局覆盖机制**：对所有声明了该引擎字段的工作流生效，不受 `supported_parameters` 约束。
后端 `workflows/__init__.py:178` 无条件接受。两段式回落链是 `pose_steps > steps > 模板默认 8`，
所以插件发的通用 `steps` 会成为两段的**共同基线**，`pose_steps`/`garment_steps` 再各自精调。
若按 `supported_parameters` 收窄通用步数框，qwen-image-21 等工作流会直接失去步数覆盖能力。
（此结论由后端维护方 session-84fa9b80 明确确认为设计意图。）

**验收记录**（2026-11-11，用户实例 12138）：

*第一轮（8 步时代，pose_steps 显式=12）：*
```
POST pose_steps=12, garment_steps 缺省, pose_id=upper80v2_P044_diag, garment_id=LNG-0032
→ 202 queued，parameters 回显 pose_steps:12 / garment_steps:8
→ progress detail「姿势段 · 19.24s/it · ETA 00:19」11/12
→ 「换装段 · 加载权重 397/397」→「换装段 · 11.93s/it」→ succeeded
→ 产物 768×768 875KB；s1.log 采样 245.52s，s2.log 采样 109.43s
→ 反向验证：qwen-image-21 + pose_steps → 422
```

*第二轮（15 步模板默认，**全部步数缺省**，job 9cad0a64）：*
```
POST 只给 workflow_id/prompt/width/height/image/pose_id/garment_id，不传任何 steps
→ 202 queued，parameters 回显 pose_steps:15 / garment_steps:15 / steps:15 / sampling_method:euler
→ **sigmas 字段已消失**（turbo 8 档预设确认从模板移除）
→ 姿势段 sampling 1/15 … 15/15 → 换装段 loading → sampling 1/15 … 15/15 → succeeded
→ 产物 768×768；s1.log 采样 304.14s、s2.log 采样 205.65s，两份日志均无 turbo 字样
```

**dist 部署注意**：用户桌面实例从 `dist/LocalGen/_internal/workflows/` 读模板，**不是**源目录。
后端改模板后必须重打包（`tools/build-windows.ps1` + `tools/verify_packed.py`），并重启实例才生效——
Registry 在进程启动时加载，改完 manifest 不重启，API 仍报旧值。

## 七、驱动 LocalGen 的坑（后端维护方归档，务必遵守）

**坑 3 —— 不要用 `CloseMainWindow` 关桌面实例。**
缩托盘是它的默认行为，`CloseMainWindow()` 会触发托盘/重启逻辑而不是干净退出，新版进程随即被拉起，
出现「旧进程没了、新进程半启动、两个端口都不监听」的交接窗口期，ComfyUI 子进程还会残留。
**正确做法**：`Stop-Process -Id <pid> -Force`（等价 `taskkill /F`），杀完必须确认
`Get-Process python* | ? { $_.Path -like '*ComfyUI*' }` 已无残留再重启。
正常情况 kill-on-close Job Object 会带走 ComfyUI 子进程，残留只出现在交接窗口期。

**坑 8 —— 不要用 `Get-NetTCPConnection` 判断本机服务是否在跑。**
它对 LocalGen 进程查不到 `-State Listen` 行，即使服务完全正常。
**改成**：`netstat -ano | findstr :12138`，或直接发 HTTP 请求
（`Invoke-RestMethod http://127.0.0.1:12138/api/v1/workflows -Headers @{Authorization="Bearer $t"}`）。
误判会导致在服务健康时反复重启，白白丢掉已加载的模型。

**坑 9 —— 改了 `src/` 不等于改了跑着的程序。**
桌面实例跑的是 `dist/LocalGen/LocalGen.exe`，`localgen` 包被打进 exe 内部（不是
`_internal/localgen/` 目录），所以源码改动必须 `tools\build-windows.ps1` 重新打包 +
重启实例才生效。本次查附件路由时连着两次 404 就是踩了这个：`src` 里明明有路由，
`dist` 里还是旧字节码。该脚本末尾带 PYZ 字节码与 `src/` 的逐条比对，
输出 `SAME n DIFF 0 MISSING 0` 才算真打进去了。

**坑 10 —— `Invoke-WebRequest` 在本机 PowerShell 会话里可能直接拒绝执行**
（报「处于非交互模式」）。要探测 HTTP 就用
`.venv\Scripts\python.exe` 写 urllib 脚本，别在 PowerShell 里跟它较劲。
顺带：控制台中文输出会乱码，但**这只会影响显示，不影响断言**；
要看清中文就设 `$env:PYTHONIOENCODING="utf-8"`。

## 八、改完必做

```bash
node --check index.js && node --check client.js
```
`index.js` 改动需 **重启 DSH** 才生效；`client.js` 改动刷新页面即可。

UI 改动（显隐/占位/校验）若走了条件分支，务必同时检查**四条路径**是否一致对齐，
否则会出现"界面看不见但仍在生效"的幽灵参数：
1. 界面渲染分支
2. `generate()` 提交体构造
3. 校验 memo（以及它进没进 `canGenerate` / `disabledReason`）
4. `buildBodyFromFill()` 历史重跑透传

---

## 九、参考图的三个身份（改参考图相关代码前必读）

这次修的 bug：**复用参数 / 参数复制时参考图是空的**。根因不是 UI，是「参考图」
在后端有三个互不相同的身份，插件以前只用了最脆的那一个。

| 身份 | 值形如 | 生命周期 | 用途 |
|---|---|---|---|
| 上传 token | `35876395aaea475d8bf7ceb0b853e974.png` | `gateway-input/` 有 TTL，**会过期** | 提交时的 `parameters.image` |
| 浏览器预览 | `blob:http://…` | 只活在当前页面内存，刷新即死 | 仅用于刚上传完那一下的缩略图 |
| 任务附属物 | `results/<job_id>/reference.png` | **永久**（发布成功那刻从引擎目录转存） | 历史回看 + 复用时的图片来源 |

三条铁律：

1. **持久化只认 token + job_id，绝不存 `blob:`**。草稿恢复时用
   `rawUrl('/api/v1/jobs/<job>/attachments/reference')` 重建预览。
2. **提交仍用 token**（`body.image`），附属物只负责显示和兜底重取。
3. **token 失效要能自愈**：提交收到 422 `INPUT_IMAGE_MISSING` 且知道来源任务时，
   用 `restageReference(jobId)` 取回附属物字节重新上传，换新 token 重试一次。

### 后端那半个 bug（本次已修）

`ImageStore.keep_reference()` 原来硬编码读 `comfy-output/<job>/input.png`。
但 `sdcpp._stage()` 实际按 `{prefix}ref{index}-{kind}.png` 落盘 —— pose-swap 这类
两段式任务根本没有 `input.png`，只有 `ref0-person.png` / `ref1-pose.png`，
于是**参考图从未被保存过**（诊断证据：所有 pose-swap 任务都有 `pose.png` /
`garment.png`，却一个 `reference.png` 都没有）。

修法：`_pick_person_reference()` 按优先级挑人 —— `ref*-person` → `input.png` →
唯一的 `ref*`，且**绝不用 `pose`/`garment` 约束图冒充人物图**。
回归测试在 `tests/unit/test_reference_keep.py`（新增 3 例）。
