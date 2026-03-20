# MoviePilot-Plugins

这是一个 MoviePilot 第三方插件库。

## 插件列表

- `deepfloodsign`：DeepFlood 论坛每日签到

---

# deepfloodsign（DeepFlood 论坛签到）

> 自动完成 DeepFlood 论坛每日签到。
> 
> 说明：DeepFlood 站点通常有 Cloudflare/WAF 防护，插件内置 `cloudscraper` / `curl_cffi` 兜底，但不同站点的真实签到接口可能不一样，所以本插件把「签到 API 路径」和「请求方法」做成了可配置项。

## 1. 安装

1) 把本仓库添加到 MoviePilot 的「插件市场」第三方仓库（或通过界面安装本仓库中的插件）。

2) 安装插件：`deepfloodsign`

3) 安装完成后，到 **插件配置** 页面填写参数并启用。

## 2. 配置说明（重要）

插件配置项：

- **启用插件**：开启后才会运行定时任务
- **开启通知**：开启后签到成功/失败会发送站内通知
- **立即运行一次**：打开后会在 3 秒内执行一次签到（执行后会自动关闭）
- **定时任务(cron)**：默认 `0 9 * * *`（每天 9 点）
- **DeepFlood站点URL**：例如 `https://www.deepflood.com`（不要以 `/` 结尾）
- **站点Cookie**：从浏览器已登录状态复制 Cookie（见下方获取方法）
- **签到API路径**：默认 `/api/attendance`（通常需要你根据实际站点修改）
- **签到请求方法**：`POST` 或 `GET`（通常是 `POST`）

> 重点：如果你填了 Cookie 但仍提示未登录/失败，几乎都是 **签到 API 路径不对** 或者该站点接口不是 `application/json`。

## 3. 如何获取 Cookie

1) 用浏览器正常登录 DeepFlood
2) 打开开发者工具（F12）→ Network
3) 刷新页面或访问 `/board`
4) 任选一个请求，在 Request Headers 里找到 `Cookie:`，复制其值

> 建议：Cookie 很长，直接整段复制粘贴到插件配置里即可。

## 4. 如何确定真实签到接口（attendance_path）

DeepFlood 的签到按钮/动作一般会触发一个 API 请求。

获取方式：

1) 浏览器登录后打开 F12 → Network
2) 点击站点的「签到」按钮
3) 在 Network 里找到刚刚新增的请求（一般是 XHR/Fetch）
4) 记录以下信息：
   - Request URL（完整 URL）
   - Request Method（GET/POST）
   - Status Code
   - Response Body（返回内容，最好复制一份）

然后把 **URL 中域名后面的路径部分** 填到插件的「签到API路径」里。

例子：

- 如果 Request URL 是：
  - `https://www.deepflood.com/api/attendance?random=true`
  - 那么签到API路径填：`/api/attendance`
  - 请求方法选：`POST`（或按实际请求）

- 如果 Request URL 是：
  - `https://www.deepflood.com/attendance.php`
  - 那么签到API路径填：`/attendance.php`

我也建议你把抓到的请求信息发我，我可以把默认值/解析逻辑改成更“开箱即用”。

## 5. 运行与排错

- **最常见错误 1：未配置 Cookie**
  - 解决：在插件配置里填 Cookie

- **最常见错误 2：Cookie 失效/未登录**
  - 解决：重新登录后复制最新 Cookie

- **最常见错误 3：接口路径不对**
  - 现象：返回非 JSON、或者提示未知响应
  - 解决：按第 4 节抓包确定真正的签到 API

- **Cloudflare/WAF 导致失败**
  - 插件会优先使用 `cloudscraper`，不行再尝试 `curl_cffi`。
  - 如果你环境里某个依赖安装失败，请到 MoviePilot 日志里看具体报错。

## 6. 免责声明

本插件仅用于个人自动化，需遵守目标站点的服务条款与法律法规。
