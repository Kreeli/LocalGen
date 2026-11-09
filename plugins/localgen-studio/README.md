# LocalGen Studio

给 LocalGen（本机文生图服务）加一个专用的 DSH 工作台：顶部「LocalGen」视图里挑姿势 / 服装、上传参考图、按工作流生成、浏览与复用历史、读写服务设置。

## 是什么

LocalGen 的 API 绑在 `127.0.0.1:12138`（可在面板设置里改），并且它的 `LocalSecurity` 中间件会校验 Host / Origin 白名单——DSH 网页从自己的端口访问它会被 403 拒掉。所以这个插件分成两半：

- **Host 半**（`index.js`）：在 DSH 自己的 web 服务器上注册同源路由 `/plugins/localgen-studio/*`，由 Node 侧带着 Bearer 令牌转发到 LocalGen，浏览器只跟 DSH 同源通信，令牌不出现在页面里。
- **Client 半**（`client.js`）：注册顶部 `conversation.view` 视图（主入口）+ 侧边栏面板（保留），纯 React。

## 入口

| 入口 | slot | id/order |
|---|---|---|
| 顶部「LocalGen」视图 | `conversation.view` | id=`localgen`, order=30（画廊 20 之后） |
| 侧边栏图标 | `sidebar.panellist` | id=`localgen-studio`, order=40 |
| 侧边栏主面板 | `main` | key=`localgen-studio` |

顶部视图三个 tab：

- **生成**：工作流下拉（全部工作流，默认 qwen-image-21）、尺寸下拉、种子、负面提示词、步数 / CFG 覆盖（留空 = 模板默认）、姿势 / 服装选择器、参考图上传（含重绘强度滑条）、提交与进度轮询、结果展示。提交统一走 `POST /api/v1/jobs`（带 workflow_id）。
- **历史**：`GET /api/v1/jobs` 分页画廊，状态着色、缩略图、取消 / 删除（confirm）/ 另存 PNG / 展开详情 / 复用参数 / 同参重跑。
- **设置**：插件侧（服务地址、令牌、轮询间隔）+ 服务侧（`GET/PUT /api/v1/settings`，即时生效字段与需重启字段分开标注）。

## 路由（同源）

| 路由 | 作用 |
|---|---|
| `GET /plugins/localgen-studio/state` | 返回 baseUrl、是否已配置令牌 |
| `POST /plugins/localgen-studio/config` | 保存 baseUrl + 令牌 + 轮询间隔 |
| `POST /plugins/localgen-studio/rpc` | JSON 代理（`{path, method, body}`），只允许 `/api/v1/` 下的读/写接口（GET/POST/PUT/DELETE） |
| `GET /plugins/localgen-studio/raw?path=…` | 二进制代理，供 `<img src>` 加载缩略图 / 成品 |
| `POST /plugins/localgen-studio/upload?filename=…` | 把上传的图片字节转成 multipart，打到 `/api/v1/images` |

## 安全边界

- 令牌是 secret：优先存 `credentials` 服务的 `api-key` 记录，缺省回落到 `$DSH_HOME/localgen-studio/settings.json`（0600）。
- 跨源请求（Origin ≠ 页面 Host）一律 403。
- 只代理 `/api/v1/` 前缀，拒绝 `..` / 反斜杠等路径穿越。
- baseUrl 只接受本机 `http(s)` 地址（127.0.0.1 / localhost / [::1]）。

## 使用

1. 顶部（或侧边栏）点 LocalGen 打开工作台，切到「设置」。
2. 填服务地址（默认 `http://127.0.0.1:12138`）和访问令牌（LocalGen 的「设置 → 复制 API 令牌」，或 `python -m localgen --show-token`）。
3. 切到「生成」，选工作流 → 填提示词 → 选姿势 / 服装 / 上传参考图 → 生成。
4. 切到「历史」浏览、复用、删除任务。

编辑类单张 5–8 分钟（Qwen-Image-2.1 · 8 步 turbo），期间请勿重复点击。

## 目录

```
localgen-studio/
├── package.json        # dsh.bundle.patch + dsh.client 声明
├── cordis.patch.yml    # 插入 localgen-studio 行
├── index.js            # Host 半：代理路由 + 令牌管理
├── client.js           # Client 半：顶部工作台 + 侧边栏面板
├── icon.svg
├── README.md
└── locale/{zh,en}.json
```

纯 JS，无构建步骤。安装后经 `plugin_manager` 的 `install_bundle` 挂进 profile。
