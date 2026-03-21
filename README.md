# MoviePilot-Plugins

这是一个 MoviePilot 第三方插件库。  
开发说明：https://github.com/jxxghp/MoviePilot-Plugins/blob/main/README.md

## 插件列表

- `deepfloodsign`：DeepFlood 论坛每日签到
- `wuaipojiesign`：吾爱破解（52pojie）每日打卡签到

## 安装

1. 在 MoviePilot 中添加本仓库为第三方插件仓库。
2. 在插件市场安装目标插件。
3. 到插件配置页填写参数并启用。

## deepfloodsign（DeepFlood）

用于 DeepFlood 论坛签到，支持定时任务、通知、重试、历史记录。  
基础配置：

- `site_url`：站点地址（如 `https://www.deepflood.com`）
- `cookie`：浏览器登录态 Cookie
- `cron`：定时表达式（默认 `0 9 * * *`）
- `attendance_path` + `attendance_method`：签到接口路径与方法

排错建议：

- 失败多数来自 Cookie 失效或签到接口路径不正确。
- 先在浏览器 Network 抓“签到按钮”真实请求，再回填路径与方法。

## wuaipojiesign（52pojie）

用于吾爱破解论坛自动打卡签到，支持定时任务、通知、重试、历史记录。  
基础配置：

- `base_url`：站点地址（默认 `https://www.52pojie.cn`）
- `cookie`：浏览器登录态 Cookie
- `task_id`：任务 ID（默认 `2`）
- `cron`：定时表达式（默认 `0 9 * * *`）

说明：

- 插件会先解析首页打卡入口（`task apply`），必要时继续执行 `task draw`。
- 若站点页面结构调整，可能需要更新 `task_id` 或适配解析规则。

## 免责声明

本仓库插件仅用于个人自动化，请遵守目标站点服务条款与相关法律法规。
