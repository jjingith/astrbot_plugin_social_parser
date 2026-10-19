# 更新日志

## v4.0.1

本次为 4.0.0 合并后的功能修复与体验完善：

- 修复 Cookie 未接入解析链路的问题：配置面板填写的 `youtube_settings.cookies` 与
  `weibo_settings.cookies` 现会落盘到插件数据目录并被 yt-dlp 链路读取；
  未配置或文件缺失时回退到插件目录 `cookies/<平台>/cookies.txt`。
- 补回微博 / X 的分享卡片（JSON 卡片）识别：卡片中抽出的链接会直接注入 yt-dlp 链路解析，
  与 B站 / 抖音 / 小红书 的卡片识别行为保持一致。
- 修复合并后 mixin 方法重名导致的解析失败：`SocialYtdlpMixin` 的 Cookie 状态检查与
  `BilibiliMixin` 的同名异步方法冲突（MRO 中后者优先），微博 / X 等 yt-dlp 链路会拿到
  协程对象并抛出 `'coroutine' object is not subscriptable`；已重命名为 `_sp_check_cookie_status`。
- 修复 Instagram 失败时的误导性输出：未加载到登录态 Cookie 时不再返回「Cookies缺失」占位内容
  被渲染成「解析成功」，改为明确报错并给出可操作的提示（配置第三方中转或改用登录态 Cookie）。
- 新增 `instagram_settings.mirror_url` 可选配置：Instagram 官方通道对匿名请求一律返回
  `login_required`，而长期挂载登录态 Cookie 又容易导致账号被封；填写第三方解析 / 中转地址后
  （`{url}` / `{code}` 占位符），插件会在尝试 Cookie 之前优先走它，并从返回的 JSON / HTML 中
  自动提取文案与图片、视频直链。未填写时不改变原有行为。
- 修复 X 纯图片推文 / 敏感内容推文解析不出内容：
  - `cdn.syndication.twimg.com/tweet-result` 需要附带由推文 id 推导的 `token`，
    缺失时接口只返回空对象或 tweet tombstone，导致只有文字、没有媒体；
  - 第三方兜底节点从 `api.vxtwitter.com`（已被 Cloudflare 拦截返回 403）改为
    `api.fxtwitter.com`（v2 结构），并保留旧结构的兜底解析。
- X 视频的时长 / 分辨率改为优先读取第三方节点返回的 `duration` / `width` / `height`，
  不再固定为 `0`（缺失时仍由 ffmpeg 探测兜底）。
- 修复时长 / 分辨率显示不对：Instagram 等平台的 yt-dlp 元数据与直链下载通道都不带回
  `duration` / `width` / `height`，原先统一显示为「00:00 / 未知」；现在视频下载完成后会用
  ffmpeg 对实际文件探测一次，缺失时补全这两个字段。
- 修复视频下载失败时把 yt-dlp 缩略图（视频封面 / 作者头像）当成图文内容发出的问题：
  视频帖下载失败会丢弃这类缩略图，并明确提示「视频下载失败」及失败原因。

## v4.0.0

首个发布版本。整合了两条解析链路：

- **B站 / 抖音 / 小红书**：沿用 `astrbot_plugin_link_resolver` 的原生解析实现
  （原项目作者 acacia，AGPL-3.0），支持多 P、画质与编码选择、无水印原图优先、Cookie 登录。
- **微博 / X / YouTube / Instagram / TikTok**：沿用 social_parser 原有的 yt-dlp 解析链路。

本次合并要点：

- 平台路由明确化：B站 / 抖音 / 小红书 走原生解析，微博 / X / YouTube / Instagram / TikTok 走 yt-dlp，
  避免两条链路争抢同一链接。
- 新增 `proxy_url` 配置，供 yt-dlp 链路的海外平台使用，留空即直连。
- 支持分享卡片识别、文字摘要 / 渲染卡片、合并转发、表情回应、群黑白名单、
  媒体数量与最大视频体积限制、卡片渲染字体管理。
