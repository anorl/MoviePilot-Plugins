# MoviePilot-Plugins

这是一个 MoviePilot V3 第三方插件库。
开发说明：https://github.com/jxxghp/MoviePilot-Plugins/blob/main/README.md

插件使用 `plugins.v3/` 与 `package.v3.json`，并通过 MoviePilot 稳定 SDK
接入 V3。本仓库不再提供 MoviePilot V2 版本。

## 插件列表

- `deepfloodsign`：DeepFlood 论坛每日签到
- `nodeseeksigncc`：NodeSeek 论坛每日签到
- `fengsigncc`：威锋网每日签到（手工 Cookie / CookieCloud）

## 安装

1. 在 MoviePilot 中添加本仓库为第三方插件仓库。
2. 在插件市场安装目标插件。
3. 到插件配置页填写参数并启用。

## deepfloodsign（DeepFlood）

用于 DeepFlood 论坛签到，支持 CookieCloud。  
基础配置：

- `site_url`：站点地址（如 `https://www.deepflood.com`）
- `cookie_source`：Cookie 来源（手工填写 / CookieCloud）
- `cookie`：浏览器登录态 Cookie（仅手工模式使用）
- `member_id`：用户 ID（可选，用于获取用户名/等级/鸡腿）
- `cron`：定时表达式（默认 `0 9 * * *`）
- `use_proxy`：是否使用 MoviePilot 系统代理（默认关闭）

说明：

- 若使用 CookieCloud，请确保浏览器扩展已把目标站点域名同步到 CookieCloud 服务端。
- 插件支持 MoviePilot 本地 CookieCloud 和远端 CookieCloud 两种模式。
- Cloudflare Cookie 可能与出口 IP 绑定；浏览器直连获取 Cookie 时，签到也应优先直连。

## nodeseeksigncc（NodeSeek）

用于 NodeSeek 论坛签到，支持 CookieCloud。  
基础配置：

- `site_url`：站点地址（默认 `https://www.nodeseek.com`）
- `cookie_source`：Cookie 来源（手工填写 / CookieCloud）
- `cookie`：浏览器登录态 Cookie（仅手工模式使用）
- `member_id`：用户 ID（可选，用于获取用户名/等级/鸡腿）
- `cron`：定时表达式（默认 `0 9 * * *`）
- `use_proxy`：是否使用 MoviePilot 系统代理（默认关闭）

说明：

- 为避免与其他仓库中的同名 NodeSeek 插件冲突，本仓库内部插件标识使用 `nodeseeksigncc`。
- 若使用 CookieCloud，请确保浏览器扩展已把 `nodeseek` 相关域名同步到 CookieCloud 服务端。
- Cloudflare Cookie 可能与出口 IP 绑定；浏览器直连获取 Cookie 时，签到也应优先直连。

## fengsigncc（威锋网）

用于 `www.feng.com` 的网页每日签到，支持手工填写 Cookie 或从 CookieCloud 获取登录态，适用于 MoviePilot V3。插件和市场图标使用仓库内的 `icons/feng.jpg`（225×225）。

1. 在浏览器登录 `https://www.feng.com`。
2. 在插件配置中选择 Cookie 来源：
   - **手工填写**：粘贴该站点登录后的完整 Cookie 请求头内容（如 `userInfo=...; other=...`），需包含 `userInfo` 或 `userInfo-shared`。无需配置或安装 CookieCloud。
   - **MoviePilot CookieCloud**（默认）：在 MP 系统设置中配置用户 KEY、加密密码及本地同步或远端服务地址，确认扩展已同步 `feng.com` / `www.feng.com` 的登录 Cookie。
3. 启用插件，按需开启通知和「立即运行一次」。默认每天 9 点执行，使用 MoviePilot 配置的时区。

插件从 Cookie 中的 JSON 登录信息提取 `accessToken`，按威锋网页协议调用 API；无需另填 Token、账号密码或用户 ID。优先使用 `userInfo`，共享 Cookie 作为备用。CookieCloud 模式每次运行重新读取同步数据并过滤过期 Cookie；手工模式仅使用配置中保存的 Cookie。登录失效时需重新登录，再手工更新 Cookie 或同步 CookieCloud。切换来源后不会自动使用另一来源的账号。

- `cron`：定时表达式，默认 `0 9 * * *`。
- `cookie_source`：`manual`（手工填写）或 `cookiecloud`（默认，兼容已有配置）。
- `cookie`：手工模式使用的 Cookie，在配置输入框中掩码显示。
- `use_proxy`：是否使用 MoviePilot 系统代理，默认关闭。
- 签到前检查当日状态；已签到时不重复提交。签到 POST 超时或断线后只查询一次状态，不重复提交；无法确认时明确提示“结果未确认”。
- 签到成功后查询最新数据失败时，保留成功结果并隐藏旧的天数、经验；保存历史或发送通知失败也不会覆盖签到结果。
- 远端 CookieCloud 候选地址返回无效数据时继续尝试其他兼容地址，全部失败后汇总脱敏错误。
- 通知包含结果及接口可用时的连续签到天数、当前经验；详情页保留最近 100 条记录。
- 不记录 Cookie、Token、CookieCloud 密码或带 KEY 的服务 URL。

协议依据：威锋官网当前加载的网页脚本 `/_nuxt/6a68991.js`（2026-10-21 核对），使用 `GET /v1/user/experience`、`POST /v1/attendance/userSignIn` 及 `X-Access-Token` / `X-Request-Id` 请求头。匿名接口检查返回 `1001 / 先登录`；本地测试不代表真实账户签到成功，安装后应查看一次实际运行结果。

## 免责声明

本仓库插件仅用于个人自动化，请遵守目标站点服务条款与相关法律法规。
