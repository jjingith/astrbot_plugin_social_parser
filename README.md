# astrbot_plugin_social_parser

AstrBot 链接解析插件：自动识别聊天中的视频 / 图文链接，下载后直接发送到群聊或私聊。

支持 **B站 · 抖音 · 小红书 · 微博 · X (Twitter) · YouTube · Instagram · TikTok**。

---

## 支持平台

| 平台 | 识别域名 | 解析方式 | 备注 |
| --- | --- | --- | --- |
| B站 | `bilibili.com`、`b23.tv` | 原生 API | 多 P 支持、画质 / 编码选择、Cookie 登录 |
| 抖音 | `douyin.com`、`iesdouyin.com` | 原生 API | 视频 / 图集 / 动图 |
| 小红书 | `xiaohongshu.com`、`xhslink.com` | 原生解析 | 优先无水印原图 |
| 微博 | `weibo.com`、`weibo.cn`、`t.cn` | yt-dlp + 访客 Cookie 回退 | `t.cn` 短链自动还原 |
| X (Twitter) | `x.com`、`twitter.com` | yt-dlp | 需可访问对应站点 |
| YouTube | `youtube.com`、`youtu.be` | yt-dlp | 需可访问对应站点 |
| Instagram | `instagram.com` | yt-dlp | 需可访问对应站点 |
| TikTok | `tiktok.com` | yt-dlp | 需可访问对应站点 |

> 海外平台统一走 `proxy_url` 配置的代理（留空则直连）。

## 功能特性

- **链接监听**：识别文本消息中的链接，也识别 B站 / 抖音 / 小红书分享卡片（JSON 卡片）里的链接。
- **摘要与合并转发**：摘要可选「文字摘要」或「渲染卡片」；可设置合并转发是否显示为发送者。
- **表情回应**：识别到链接后先点一个表情提示「开始处理」，表情 ID 与策略可配置。
- **群过滤**：黑白名单模式，私聊不受影响。
- **错误通知**：静默 / 脱敏 / 报错 三档。
- **限额保护**：统一的最大视频体积限制，以及各平台的时长 / 媒体数量上限。
- **字体管理**：中文主字体与 Emoji 字体（可自动安装，也可指定本机字体路径），用于卡片渲染。
- **任务清理**：插件重载时中断上一轮未完成的解析任务，并管理下载缓存与临时文件生命周期。

## 安装

1. 将本仓库整个目录放入 AstrBot 的插件目录，**目录名保持为 `social_parser`**：

   ```
   AstrBot/data/plugins/social_parser/
   ```

   > 目录名决定了配置文件路径 `data/config/social_parser_config.json`，请勿改名。

2. 依赖会由 AstrBot 依据 `requirements.txt` 自动安装。
3. 安装 **FFmpeg**（音视频分离流合流 / 转码需要）：

   - Windows：`winget install Gyan.FFmpeg`，或手动解压后加入 PATH；
   - Linux：`apt install ffmpeg` / `dnf install ffmpeg`；
   - 未加入 PATH 时，插件会尝试以下路径：`C:\ffmpeg\bin\ffmpeg.exe`、`C:\ffmpeg\ffmpeg.exe`、`/usr/bin/ffmpeg`、`/usr/local/bin/ffmpeg`。

4. 重启 AstrBot，或在 WebUI 插件管理页重载插件。

## 配置

所有配置在 **WebUI → 插件管理 → social_parser** 中修改（由 `_conf_schema.json` 自动生成表单）。

| 配置组 | 作用 |
| --- | --- |
| `enable_platforms` | 勾选要启用解析的平台 |
| `proxy_url` | yt-dlp 链路（微博 / X / YouTube / Instagram / TikTok）使用的代理，留空直连 |
| `bili_settings` | B站：画质、编码、多 P 数量、时长上限、Cookie、摘要模式、合并发送 |
| `douyin_settings` | 抖音：媒体数量上限、摘要模式、合并发送 |
| `xhs_settings` | 小红书：原图、大图转文件阈值、摘要模式 |
| `weibo_settings` | 微博：媒体数量上限、原图、合并发送、Cookie |
| `twitter_settings` | X：媒体数量上限、合并发送 |
| `youtube_settings` | YouTube：最高分辨率、时长上限、Cookie、合并发送 |
| `general_settings` | 通用：重试次数、表情回应、字体、最大视频体积、错误通知 |
| `group_filter` | 群黑白名单 |

常用项：

- `general_settings.max_video_size_mb`（默认 `200`）：超过该体积的视频不下载，`0` 表示不限。
- `general_settings.retry_count`（默认 `3`）：解析失败重试次数。
- `general_settings.error_notify_mode`（默认「静默」）。
- `general_settings.auto_install_fonts`（默认关闭）：开启后自动下载中文字体与 Emoji 字体到数据目录。

## Cookie 与代理

| 用途 | 存储位置 |
| --- | --- |
| B站 Cookie | WebUI `bili_settings.cookies` 填写，自动写入 `data/plugin_data/social_parser/cookies/bili_cookies.txt` |
| YouTube Cookie | WebUI `youtube_settings.cookies` 填写，自动写入 `data/plugin_data/social_parser/cookies/youtube_cookies.txt` |
| yt-dlp 链路 Cookie | 插件目录 `cookies/<平台>/cookies.txt`（`x` / `youtube` / `weibo`） |

- 微博在没有配置 Cookie 时会先尝试生成访客 Cookie 解析公开内容。
- 插件目录下的 `cookies/` 已在 `.gitignore` 中排除，**请勿把个人 Cookie 提交到仓库**。
- 代理示例：`http://127.0.0.1:7896`。

## 目录结构

```
social_parser/
├── main.py                 # 插件入口：事件分发、平台路由、配置加载
├── metadata.yaml           # 插件元信息
├── requirements.txt        # 运行依赖
├── _conf_schema.json       # WebUI 配置表单定义
├── LICENSE                 # AGPL-3.0
├── logo.png
├── core/
│   ├── bilibili/           # B站解析与卡片渲染
│   ├── douyin/             # 抖音解析（含图集、签名、访客 API）
│   ├── xiaohongshu/        # 小红书解析与卡片渲染
│   ├── social/             # yt-dlp 链路（微博 / X / YouTube / Instagram / TikTok）
│   ├── weibo/  twitter/  youtube/   # 提取器与 handler（快照、正则等复用）
│   └── common/             # 路径、字体、卡片渲染器、文件生命周期等公共模块
└── tests/                  # 单元测试
```

运行时数据目录（由 AstrBot 提供）：

```
data/plugin_data/social_parser/
├── cache/                  # 各平台的视频 / 图片 / 卡片缓存
├── cookies/                # 由 WebUI 配置写入的 Cookie
└── fonts/                  # 自动安装的字体
```

## 依赖

见 `requirements.txt`：

```
httpx
bilibili-api-python>=17.4.0,<18.0.0
msgspec
Pillow
aiohttp
gmssl>=3.2.2,<4.0.0
yt-dlp>=2026.8.19
requests
urllib3
```

## 许可证

本项目以 **AGPL-3.0** 许可证发布，详见 [LICENSE](LICENSE)。

## 致谢

架构基座与 B站 / 抖音 / 小红书解析来自开源项目
[astrbot_plugin_link_resolver](https://github.com/vacacia/astrbot_plugin_link_resolver)（作者 acacia）；
海外平台与微博的 yt-dlp 解析链路由 social_parser 原有实现提供。

## 免责声明

本项目仅供学习与个人使用。请遵守各平台服务条款与著作权法规，不要用于商业用途或传播侵权内容。
