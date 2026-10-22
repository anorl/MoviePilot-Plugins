# MoviePilot-Plugins

这是一个 MoviePilot V3 第三方插件库。
开发说明：https://github.com/jxxghp/MoviePilot-Plugins/blob/main/README.md

插件使用 `plugins.v3/` 与 `package.v3.json`，并通过 MoviePilot 稳定 SDK
接入 V3。本仓库不再提供 MoviePilot V2 版本。

## 插件列表

- `deepfloodsign`：DeepFlood 论坛每日签到
- `nodeseeksigncc`：NodeSeek 论坛每日签到
- `fengsigncc`：威锋网每日签到（手工 Cookie / CookieCloud）
- `neteasemusicsigncc`：网易云音乐每日签到（手工 Cookie / CookieCloud、MP 通知）
- `tiebasigncc`：百度贴吧逐吧签到（手工 Cookie / CookieCloud、历史详情和 MP 通知）
- `enshansigncc`：恩山无线论坛签到（手工 Cookie / CookieCloud、浏览器验证兜底、历史和 MP 通知）

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

## neteasemusicsigncc（网易云音乐）

适用于 MoviePilot V3，插件和市场使用 `icons/NetEaseMusic.png`（230×230，保留原图透明背景）。支持移动端、网页端或两端每日签到，默认每天 9 点执行两端签到，时区沿用 MP 配置。

1. 浏览器登录 `https://music.163.com`。
2. 选择 Cookie 来源：手工粘贴完整 Cookie（必须包含 `MUSIC_U`，建议连同 `__csrf` 等字段一起复制）；或使用 MP 已配置的本地/远端 CookieCloud，同步 `music.163.com` 登录态。
3. 默认开启通知，通过 MP 的 `SiteMessage`（站点消息）渠道发送成功、已签到、部分完成或失败结果。接收 TG 消息需要在 MP 中配置 Telegram，并允许该渠道接收站点消息，无需在插件重复填写 Bot Token。
4. 开启「立即运行一次」测试，查看插件详情页的历史及 TG 通知，再启用定时任务。

- 支持 `sign_type`：`both`（默认，两端）、`0`（移动端）、`1`（网页端）；两端结果独立判断，一端失败不覆盖另一端的结果。
- `cookie_source` 默认 `cookiecloud`，可切换为 `manual`；手工输入框掩码显示，不会自动回退到另一来源的账号。
- 详情页保留最近 100 次运行，展示时间、总结果、各端结果和接口返回的本次点数。保存历史或发送通知失败不会覆盖签到结果。
- 使用内置 WEAPI 加密直连网易云，不依赖额外搭建音乐 API 服务。签到请求不自动重试，超时或断线会明确标记“结果未确认”，下次运行仍以服务端响应判断。
- CookieCloud 按实际 Cookie 域名、有效期、路径筛选并消除同名冲突；不将 Cookie、CSRF、加密请求或服务器原始响应写入历史或日志。
- `use_proxy` 默认关闭，可按需使用 MP 系统代理。

协议实现依据：[每日签到与 Cookie/CSRF 请求流程](https://github.com/chen310/NeteaseCloudMusicTasks/blob/main/api.py)、[WEAPI 参数说明实现](https://github.com/chen310/NeteaseCloudMusicTasks/blob/main/encrypt.py)、[两端返回码处理参考](https://github.com/jrh520/wyy-action/blob/master/checkin.py)。本插件仅完成每日签到；真实账户签到和 TG 送达需安装到 MP 后验证。

## tiebasigncc（百度贴吧）

适用于 MoviePilot V3，自动签到当前账号关注的贴吧。使用从贴吧官方静态资源获取的 `icons/tiebasigncc.ico`。

1. 浏览器登录 `https://tieba.baidu.com`。
2. 选择 **手工填写** 并粘贴包含 `BDUSS` 的完整 Cookie，或使用默认的 **MoviePilot CookieCloud**，在 MP 配置本地/远端服务并同步贴吧登录 Cookie。
3. 按需开启「立即运行一次」。默认每天 9 点执行，逐吧间隔 5 秒（可配置 3～60 秒），时区沿用 MP。
4. 「发送通知」默认开启，通过 MP 的站点消息渠道投递。TG 请在 MP 配置 Telegram 并允许该渠道接收站点消息，无需重复填写 Bot Token。

插件先验证 `BDUSS` 登录态并获取 TBS，再分页读取全部关注贴吧、去重后逐吧签到；不自动关注新贴吧。CookieCloud 读取 `tieba.baidu.com` / `.baidu.com` 的根路径 Cookie，过滤过期、无关域名和同名冲突；不同 Cookie 来源不会自动相互回退。

格式异常的非必要 Cookie 会被过滤，日志只记录名称/值异常的数量，不记录字段名称或内容；`BDUSS` 缺失或含无法发送的字符仍会明确报错。这样无关统计 Cookie 不会阻断有效登录态的验证。

- 通知含时间、总结果、关注数量、新签到、已签到、失败、未确认和未提交数量，配有相应图标。异常详情最多展示 15 条，完整结果在历史页查看。
- 历史保留最近 100 次运行，展开每次记录可查看逐吧结果；登录失效、网络失败等也会记录并通知。
- 不自动重试签到提交；超时或断线标记“结果未确认”。遇到验证码或风控、HTTP异常或非JSON响应时停止后续提交，剩余贴吧标记为未提交；请在浏览器检查后再运行。
- 关闭或重载插件会在当前请求返回后停止后续签到；并发触发时跳过重复任务。
- 保存历史或发送消息失败不会覆盖已确认结果；日志、历史和通知不记录 Cookie、BDUSS、TBS、签名或服务器原始响应。
- `use_proxy` 默认关闭，可选择 MP 系统代理。登录验证、关注列表和签到统一使用实测可访问的 `https://c.tieba.baidu.com`。所有贴吧接口均通过 HTTPS 请求；最多处理 3 次贴吧域名之间的同名 API 规范地址跳转，并保持原请求方法及表单，不跳转到登录页、验证页、HTTP 或其他网站。错误提示会标明请求阶段，不输出重定向URL中的凭据。

接口参考：[贴吧客户端列表与签到](https://github.com/starudream/sign-task/blob/master/pkg/tieba/api/forum.go)、[请求签名](https://github.com/starudream/sign-task/blob/master/pkg/tieba/api/sign.go)、[关注列表分页及返回码格式](https://github.com/Alcatraz-Zhang/TiebaSignIn/blob/feat/keepalive-liskin/tieba_client.py)。本地测试不代表真实账号签到成功，安装后应查看一次实际运行结果和消息送达。

## enshansigncc（恩山无线论坛）

适用于 MoviePilot V3，使用恩山当前 `erling_qd` 签到接口，图标为站点原始 `icons/enshansigncc.ico`。

1. 浏览器登录 `https://www.right.com.cn/forum/`。
2. 选择 **手工填写** 并粘贴完整 Cookie（包含 `TWcq_2132_auth` 等登录字段，保留盐值和站点验证 Cookie）；或使用 MP 已配置的本地/远端 CookieCloud，同步 `right.com.cn` / `www.right.com.cn` Cookie。
3. 默认每天 9 点执行，沿用 MP 时区。可先打开「立即运行一次」，确认历史与消息送达。
4. 「发送通知」默认开启，使用 MP 的站点消息渠道；TG 需在 MP 中配置 Telegram 并允许接收站点消息。

运行时先读取登录页面，确认退出登录链接及 `formhash`，再调用 `plugin.php?id=erling_qd:action&action=sign`。匿名页面即使存在 `formhash` 也不会提交签到。服务端明确返回成功或今日重复签到时才记为成功；只展示明确取得的用户名、连续/累计天数、恩山币和积分，不推算缺失奖励。历史保留最近 100 次运行，成功、已签到、失败和结果未确认都会记录。

**站点真人验证处理：**「站点验证时使用MP浏览器」默认开启。提交前先读取签到页，若遇到阿里云验证页或HTTP 403，使用 `app.sdk.browser` 的浏览器上下文加载站点脚本并在同一浏览器会话中签到。需要 MP 本身的浏览器环境可用，不额外安装另一套浏览器。

「自动尝试阿里云滑块」默认开启，适配恩山已实测的“按住滑块，拖到最右边”控件：读取当前控件位置，按下、拖动、释放，再等待页面离开验证页。每次运行最多操作一次，滑块移动到终点或出现“验证中”均不直接视为通过。

该滑块已在本地 Chrome 中实测通过一次，随后进入已登录的恩山签到页；这不代表 MP 无头浏览器或所有网络出口都能通过。当前没有适配图片点选、拼图或文字识别验证码，也没有接入打码服务。未知题型或验证仍失败时停止并通知，不误报 Cookie 失效或签到成功。请在已登录浏览器完成人工验证后同步完整 Cookie；验证状态可能受出口或会话约束，不能保证复制后在另一环境中通用。

- 浏览器兜底只在尚未提交签到前触发。签到 POST 超时或返回非JSON时，只做一次只读状态确认，不再次POST；无法确认则明确记为“结果未确认”。
- 页面只认明确的签到按钮状态，脚本中的“签到成功”、排行榜文字和历史连续天数不会被当成今天已签到。
- `use_proxy` 默认关闭；开启后HTTP和浏览器均使用MP系统代理。浏览器上下文运行后关闭，Cookie和验证状态不另存为本地文件。
- CookieCloud按实际域名、路径、有效期筛选并消除同名冲突，过滤格式异常的非必要Cookie；不自动回退到其他来源的账号。
- 通知、历史保存或账号积分查询失败，不覆盖已确认的签到结果。日志、历史及消息不记录Cookie、formhash、签名、站点原始响应或Request ID。

接口参考：[恩山erling_qd请求与返回字段](https://github.com/Sitoi/dailycheckin/blob/main/dailycheckin/enshan/main.py)、[恩山页面状态与站点验证处理参考](https://github.com/HughRyu/SignMate/blob/main/src/drivers/right.js)、[MP浏览器SDK](https://github.com/jxxghp/MoviePilot/blob/v3/app/sdk/browser.py)。真实账号签到及真人验证处理效果需要在 MP 中实测。

## 免责声明

本仓库插件仅用于个人自动化，请遵守目标站点服务条款与相关法律法规。
