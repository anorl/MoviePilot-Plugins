# MoviePilot-Plugins

这是一个 MoviePilot 第三方插件库。  
开发说明：https://github.com/jxxghp/MoviePilot-Plugins/blob/main/README.md

## 插件列表

- `deepfloodsign`：DeepFlood 论坛每日签到
- `nodeseeksigncc`：NodeSeek 论坛每日签到

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

说明：

- 若使用 CookieCloud，请确保浏览器扩展已把目标站点域名同步到 CookieCloud 服务端。
- 插件支持 MoviePilot 本地 CookieCloud 和远端 CookieCloud 两种模式。

## nodeseeksigncc（NodeSeek）

用于 NodeSeek 论坛签到，支持 CookieCloud。  
基础配置：

- `site_url`：站点地址（默认 `https://www.nodeseek.com`）
- `cookie_source`：Cookie 来源（手工填写 / CookieCloud）
- `cookie`：浏览器登录态 Cookie（仅手工模式使用）
- `member_id`：用户 ID（可选，用于获取用户名/等级/鸡腿）
- `cron`：定时表达式（默认 `0 9 * * *`）

说明：

- 为避免与其他仓库中的同名 NodeSeek 插件冲突，本仓库内部插件标识使用 `nodeseeksigncc`。
- 若使用 CookieCloud，请确保浏览器扩展已把 `nodeseek` 相关域名同步到 CookieCloud 服务端。

## 免责声明

本仓库插件仅用于个人自动化，请遵守目标站点服务条款与相关法律法规。
