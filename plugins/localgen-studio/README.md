# LocalGen Studio

给 LocalGen（本机文生图服务）加一个专用的 DSH 侧边栏面板：在姿势 / 服装图库里挑素材、上传参考图、发起换姿势 / 换装生成并查看进度。

## 是什么

LocalGen 的 API 绑在 `127.0.0.1:12138`（可在面板设置里改），并且它的 `LocalSecurity` 中间件会校验 Host / Origin 白名单——DSH 网页从自己的端口访问它会被 403 拒掉。所以这个插件分成两半：

- **Host 半**（`index.js`）：在 DSH 自己的 web 服务器上注册同源路由 `/plugins/localgen-studio/*`，由 Node 侧带着 Bearer 令牌转发到 LocalGen，浏览器只跟 DSH 同源通信，令牌不出现在页面里。
- **Client 半**（`client.js`）：注册侧边栏图标 + 主面板（React），负责姿势 / 服装选择、参考图上传、提交与进度轮询。

## 路由（同源）

| 路由 | 作用 |
|---|---|
| `GET /plugins/localgen-studio/state` | 返回 baseUrl、是否已配置令牌 |
| `POST /plugins/localgen-studio/config` | 保存 baseUrl + 令牌 |
| `POST /plugins/localgen-studio/rpc` | JSON 代理（`{path, method, body}`），只允许 `/api/v1/` 下的读/写接口 |
| `GET /plugins/localgen-studio/raw?path=…` | 二进制代理，供 `<img src>` 加载缩略图 / 成品 |
| `POST /plugins/localgen-studio/upload?filename=…` | 把上传的图片字节转成 multipart，打到 `/api/v1/images` |

## 安全边界

- 令牌是 secret：优先存 `credentials` 服务的 `api-key` 记录，缺省回落到 `$DSH_HOME/localgen-studio/settings.json`（0600）。
- 跨源请求（Origin ≠ 页面 Host）一律 403。
- 只代理 `/api/v1/` 前缀，拒绝 `..` / 反斜杠等路径穿越。
- baseUrl 只接受本机 `http(s)` 地址（127.0.0.1 / localhost / [::1]）。

## 使用

1. 侧边栏点 LocalGen 图标打开面板。
2. 首次会弹出「连接设置」：填入服务地址（默认 `http://127.0.0.1:12138`）和访问令牌（LocalGen 的「设置 → 复制 API 令牌」，或 `python -m localgen --show-token`）。
3. 上传参考图（人物）、从图库选姿势 / 服装、写提示词，点「生成一张」。
4. 面板会轮询任务状态，显示 `loading / sampling / decoding` 阶段和步数，出图后直接展示。

单张 5–8 分钟（Qwen-Image-2.1 · 8 步 turbo），期间请勿重复点击。

## 图库数据

面板不携带任何图库数据，全部从 LocalGen 的 `/api/v1/libraries` 实时拉取：

- 姿势 1439 条（4 体系 / 26 分类，同姿势多角度用 `code` 聚合）
- 服装 1407 条（8 分类，`description` 由服务端自动拼进提示词）

## 目录

```
localgen-studio/
├── package.json        # dsh.bundle.patch + dsh.client 声明
├── cordis.patch.yml    # 插入 localgen-studio 行
├── index.js            # Host 半：代理路由 + 令牌管理
├── client.js           # Client 半：侧边栏面板
├── icon.svg
└── locale/{zh,en}.json
```

纯 JS，无构建步骤。安装后经 `plugin_manager` 的 `install_bundle` 挂进 profile。
