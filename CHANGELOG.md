# 更新日志

## v4.0.0

首个发布版本。整合了两条解析链路：

- **B站 / 抖音 / 小红书**：沿用 `astrbot_plugin_link_resolver` 的原生解析实现
  （原项目作者 acacia，AGPL-3.0），支持多 P、画质与编码选择、无水印原图优先、Cookie 登录。
- **微博 / X / YouTube / Instagram / TikTok**：沿用 social_parser 原有的 yt-dlp 解析链路。

本次合并要点：

- 平台路由明确化：B站 / 抖音 / 小红书 走原生解析，微博 / X / YouTube / Instagram / TikTok 走 yt-dlp，
  避免两条链路争抢同一链接。
- 新增 `proxy_url` 配置，供 yt-dlp 链路的海外平台使用，留空即直连。
- 修复 Cookie 未接入解析链路的问题：配置面板填写的 `youtube_settings.cookies` 与
  `weibo_settings.cookies` 现会落盘到插件数据目录并被 yt-dlp 链路读取；
  未配置或文件缺失时回退到插件目录 `cookies/<平台>/cookies.txt`。
- 补回微博 / X 的分享卡片（JSON 卡片）识别：卡片中抽出的链接会直接注入 yt-dlp 链路解析，
  与 B站 / 抖音 / 小红书 的卡片识别行为保持一致。
- 修复合并后 mixin 方法重名导致的解析失败：`SocialYtdlpMixin` 的 Cookie 状态检查与
  `BilibiliMixin` 的同名异步方法冲突（MRO 中后者优先），微博 / X 等 yt-dlp 链路会拿到
  协程对象并抛出 `'coroutine' object is not subscriptable`；已重命名为 `_sp_check_cookie_status`。
- 支持分享卡片识别、文字摘要 / 渲染卡片、合并转发、表情回应、群黑白名单、
  媒体数量与最大视频体积限制、卡片渲染字体管理。
