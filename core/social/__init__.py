"""social_parser 原有 yt-dlp 解析逻辑（X/YouTube/Instagram/TikTok/微博），以 Mixin 形式并入 link_resolver 架构。"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

import requests
import urllib3
from yt_dlp import YoutubeDL

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Image, Video

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]


class SocialYtdlpMixin:
    def _sp_setup(self) -> None:
        self.config = self.config or {}
        self.social_pattern = re.compile(
            r"(?:https?://)?(?:[a-zA-Z0-9\-]+\.)?(?:"
            r"youtube\.com|youtu\.be|"
            r"x\.com|twitter\.com|"
            r"instagram\.com|"
            r"weibo\.com|weibo\.cn|t\.cn|"
            r"tiktok\.com|"
            r"douyin\.com|iesdouyin\.com"
            r")",
            re.IGNORECASE,
        )
        # 抖音链接由 link_resolver 解析，本插件继承其正则用于识别后跳过
        self.douyin_pattern = re.compile(
            r"(?:https?://)?(?:[a-zA-Z0-9\-]+\.)?(?:"
            r"douyin\.com|iesdouyin\.com"
            r")",
            re.IGNORECASE,
        )
        # 代理地址可在插件配置中修改，留空表示不使用代理
        self.proxy_url = str(
            self.config.get("proxy_url", "http://127.0.0.1:7896")
        ).strip()
        self.user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            " (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        )
        self.ffmpeg_exe = self._detect_ffmpeg()
        self.max_video_size_mb = 300
        # 配置面板写入的 Cookie 文件映射（平台 -> 数据目录下的文件路径），
        # 由 main.py 的 _refresh_config() 填充；未配置时为插件目录下的默认路径。
        self._sp_configured_cookies = {}
        # Instagram 可选第三方解析中转地址，由 main.py 的 _refresh_config() 填充。
        self.ig_mirror_url = ""
        logger.info(
            f"[social_parser] v4.0.0 插件已加载 | FFmpeg 路径:"
            f" {self.ffmpeg_exe or '未找到'} | 代理: {self.proxy_url or '未启用'}"
        )

    def _detect_ffmpeg(self) -> str:
        system_ffmpeg = shutil.which("ffmpeg")
        if system_ffmpeg:
            return system_ffmpeg
        custom_paths = [
            r"C:\ffmpeg\bin\ffmpeg.exe",
            r"C:\ffmpeg\ffmpeg.exe",
            "/usr/bin/ffmpeg",
            "/usr/local/bin/ffmpeg",
        ]
        for p in custom_paths:
            if os.path.exists(p):
                return p
        return "ffmpeg"

    def _sp_resolve_cookie_path(self, platform: str) -> str:
        """解析该平台要使用的 Cookie 文件路径。

        优先使用配置面板写入数据目录的 Cookie 文件，未配置或文件不存在时，
        回退到插件目录下的 cookies/<平台>/cookies.txt。
        """
        configured = getattr(self, "_sp_configured_cookies", None) or {}
        path = configured.get(platform)
        if path and Path(path).is_file():
            return str(path)
        return str(_PLUGIN_ROOT / "cookies" / platform / "cookies.txt")

    async def handle_social_links(
        self,
        event: AstrMessageEvent,
        override_url: str | None = None,
    ):
        if override_url:
            # 来自 JSON 卡片等场景的链接不在消息文本里，由调用方直接指定
            url = override_url
        else:
            message_str = getattr(event, "message_str", "") or ""
            if not self.social_pattern.search(message_str):
                return

            urls = re.findall(r"https?://[^\s]+", message_str)
            if not urls:
                return

            # 抖音链接交由 link_resolver 解析，这里过滤掉避免重复处理与报错
            urls = [u for u in urls if not self.douyin_pattern.search(u)]
            if not urls:
                logger.debug("[social_parser] 检测到抖音链接，已交由抖音解析器处理")
                return

            url = urls[0]
        logger.info(f"[social_parser] 检测到链接: {url}")

        local_files_to_clean = []
        cookie_warn_msg = ""
        platform_name = "未知平台"
        msg_id = None

        try:
            reply_msg = await event.send_reply("正在解析链接，请稍候...")
            msg_id = getattr(reply_msg, "message_id", None)
        except Exception:
            # 某些适配器没有 send_reply，忽略即可
            pass

        try:
            if "t.cn/" in url.lower():
                url = await asyncio.to_thread(self._expand_short_url, url)
                logger.info(f"[social_parser] 短链还原真实地址: {url}")

            platform = None
            url_lower = url.lower()
            if "youtube.com" in url_lower or "youtu.be" in url_lower:
                platform = "youtube"
                platform_name = "YouTube"
            elif "tiktok.com" in url_lower:
                platform = "tiktok"
                platform_name = "TikTok"
            elif "douyin.com" in url_lower or "iesdouyin.com" in url_lower:
                platform = "douyin"
                platform_name = "抖音"
            elif "instagram.com" in url_lower:
                platform = "instagram"
                platform_name = "Instagram"
            elif "x.com" in url_lower or "twitter.com" in url_lower:
                platform = "x"
                platform_name = "Twitter/X"
            elif "weibo.com" in url_lower or "weibo.cn" in url_lower:
                platform = "weibo"
                platform_name = "新浪微博"

            enabled_flags = {
                "x": getattr(self, "twitter_enabled", True),
                "youtube": getattr(self, "youtube_enabled", True),
                "weibo": getattr(self, "weibo_enabled", True),
            }
            if not enabled_flags.get(platform, True):
                logger.info(f"[social_parser] {platform_name} 未启用，跳过")
                return

            cookie_path = None
            if platform:
                cookie_path = self._sp_resolve_cookie_path(platform)
                cookie_status = self._sp_check_cookie_status(cookie_path)
                if cookie_status["exists"]:
                    if cookie_status["expired"]:
                        cookie_warn_msg = (
                            f"检测到您的 [{platform_name}] Cookie 已有"
                            f" {cookie_status['expired_count']}/{cookie_status['total_count']}"
                            " 个过期！"
                        )
                    else:
                        logger.info(
                            f"[social_parser] 成功载入 [{platform_name}]"
                            " Cookie（状态健康）"
                        )

            probe_opts = {
                "quiet": True,
                "no_warnings": True,
                "proxy": self.proxy_url or None,
                "cookiefile": (
                    cookie_path
                    if (cookie_path and os.path.exists(cookie_path))
                    else None
                ),
                "extractor_args": {"youtube": {"player_client": ["android"]}},
                "sleep_interval_requests": 1,
                "max_sleep_interval_requests": 3,
            }

            info = None
            tweet_data = None
            ins_data = None
            weibo_data = None
            douyin_data = None

            try:
                info = await asyncio.to_thread(
                    self._extract_info, url, probe_opts
                )
            except Exception as e:
                err_str = str(e).lower()
                if platform == "x" and "no video" in err_str:
                    logger.info(
                        "[social_parser] yt-dlp 提示无视频，激活"
                        " Twitter 官方 Syndication 管道..."
                    )
                elif platform == "instagram":
                    logger.info(
                        "[social_parser] yt-dlp 确认非视频流，激活"
                        " Instagram 纯图文直取管道..."
                    )
                elif platform == "weibo":
                    logger.info(
                        "[social_parser] yt-dlp 解析微博失败，将回退到微博 API"
                    )
                elif platform == "douyin":
                    logger.info(
                        f"[social_parser] yt-dlp 解析抖音失败，将回退到抖音 API: {e}"
                    )
                else:
                    logger.error(
                        f"[social_parser] yt-dlp 元数据获取失败: {e}"
                    )

            if platform == "x":
                tweet_id = self._extract_tweet_id(url)
                if tweet_id:
                    tweet_data = await asyncio.to_thread(
                        self._parse_twitter_syndication, tweet_id
                    )
                    # 数据不完整则补充 vxtwitter
                    need_vx = (
                        not tweet_data
                        or not tweet_data.get("text")
                        or (
                            not tweet_data.get("photos")
                            and not tweet_data.get("video_url")
                        )
                    )
                    if need_vx:
                        logger.info(
                            "[social_parser] Syndication 数据不完整，"
                            "切入 vxtwitter 深度解密节点..."
                        )
                        vx_data = await asyncio.to_thread(
                            self._parse_vxtwitter_fallback, tweet_id
                        )
                        if vx_data:
                            if not tweet_data:
                                tweet_data = vx_data
                            else:
                                for k in ("text", "video_url", "user"):
                                    if not tweet_data.get(k) and vx_data.get(k):
                                        tweet_data[k] = vx_data[k]
                                existing = tweet_data.get("photos") or []
                                existing_urls = {
                                    p.get("url")
                                    for p in existing
                                    if isinstance(p, dict)
                                }
                                for p in vx_data.get("photos") or []:
                                    if (
                                        isinstance(p, dict)
                                        and p.get("url") not in existing_urls
                                    ):
                                        existing.append(p)
                                        existing_urls.add(p["url"])
                                tweet_data["photos"] = existing
            elif platform == "instagram":
                if not info:
                    # 优先走用户配置的第三方中转；未配置或失败时再落到 cookies 链路。
                    ins_data = await asyncio.to_thread(
                        self._parse_instagram_mirror, url
                    )
                    if not ins_data:
                        ins_data = await asyncio.to_thread(
                            self._parse_instagram_fallback, url, cookie_path
                        )
            elif platform == "weibo":
                logger.info("[social_parser] 切入微博 API 专属解析管道...")
                weibo_data = await asyncio.to_thread(
                    self._parse_weibo_fallback, url, cookie_path
                )
                if not weibo_data and not info:
                    logger.error("[social_parser] 微博 API 与 yt-dlp 均解析失败")
            elif platform == "douyin":
                # 抖音：yt-dlp 失败时走第三方 API 兜底
                if not info:
                    logger.info("[social_parser] 切入抖音 API 专属解析管道...")
                    douyin_data = await asyncio.to_thread(
                        self._parse_douyin_fallback, url
                    )
                    if douyin_data:
                        info = {
                            "title": douyin_data.get("text", ""),
                            "uploader": douyin_data.get("uploader", "抖音用户"),
                            "description": douyin_data.get("text", ""),
                        }

            # 3. 整合基础文本及作者核心字段
            if platform == "x" and tweet_data:
                uploader = (
                    tweet_data.get("user", {}).get("name")
                    or tweet_data.get("user", {}).get("screen_name")
                    or "未知作者"
                )
                text_content = tweet_data.get("text") or ""
                title = ""
            elif platform == "instagram" and ins_data:
                uploader = ins_data.get("uploader") or "未知作者"
                text_content = ins_data.get("text") or ""
                title = ""
            elif platform == "weibo" and weibo_data:
                uploader = weibo_data.get("uploader") or "未知作者"
                text_content = weibo_data.get("text") or ""
                title = ""
            elif info:
                title = info.get("title") or ""
                description = info.get("description") or ""
                text_content = (
                    description.strip() if description else title.strip()
                )
                uploader = (
                    info.get("uploader") or info.get("channel") or "未知作者"
                )
            else:
                hint = ""
                if platform == "instagram":
                    hint = (
                        "。Instagram 官方通道对匿名请求一律返回 login_required，"
                        "可在插件配置「Instagram 设置」中填写第三方解析中转地址，"
                        "或改用登录态 Cookie 解析"
                    )
                raise Exception(
                    f"该 {platform_name}"
                    " 链接受网络限制或已被删除，所有节点均无法获取有效元数据"
                    f"{hint}"
                )

            # 4. 判断是否存在可下载的视频流
            has_video = False
            weibo_direct_video = None
            if platform == "weibo" and weibo_data and weibo_data.get("video_url"):
                has_video = True
                weibo_direct_video = weibo_data.get("video_url")
            elif (
                platform == "instagram" and ins_data and ins_data.get("video_url")
            ):
                has_video = True
            elif platform == "x" and tweet_data and tweet_data.get("video_url"):
                has_video = True
            elif platform in ("youtube", "tiktok") and info:
                has_video = True
            elif platform == "douyin" and (
                info or (douyin_data and douyin_data.get("video_url"))
            ):
                has_video = True
            elif info:
                is_playlist = info.get("_type") == "playlist"
                entries = info.get("entries", []) if is_playlist else []
                if is_playlist and entries:
                    for entry in entries:
                        if entry and any(
                            fmt.get("vcodec") and fmt.get("vcodec") != "none"
                            for fmt in entry.get("formats", [])
                        ):
                            has_video = True
                            break
                else:
                    has_video = any(
                        fmt.get("vcodec") and fmt.get("vcodec") != "none"
                        for fmt in info.get("formats", [])
                    )

            # 5. 平行提取所有潜在的关联图片 URL 列表
            image_urls = []
            thumb_only = False
            if platform == "x" and tweet_data:
                photos = tweet_data.get("photos", []) or []
                for p in photos:
                    p_url = p.get("url") if isinstance(p, dict) else None
                    if p_url and not any(
                        p_url.lower().endswith(ext)
                        for ext in [".mp4", ".webm", ".mov", ".m3u8"]
                    ):
                        if (
                            "pbs.twimg.com/media/" in p_url
                            and "?" not in p_url
                        ):
                            base, ext = os.path.splitext(p_url)
                            fmt = ext.replace(".", "") if ext else "jpg"
                            if len(fmt) > 4 or not fmt.isalnum():
                                fmt = "jpg"
                            p_url = f"{base}?format={fmt}&name=large"
                        if p_url not in image_urls:
                            image_urls.append(p_url)
            elif platform == "instagram" and ins_data:
                for img in ins_data.get("images", []):
                    if img and img not in image_urls:
                        image_urls.append(img)
            elif platform == "weibo" and weibo_data:
                for img in weibo_data.get("images", []):
                    if img and img not in image_urls:
                        image_urls.append(img)
            elif platform == "douyin" and douyin_data:
                for img in douyin_data.get("images", []) or []:
                    if img and img not in image_urls:
                        image_urls.append(img)

            if not image_urls and info:
                self._extract_images_from_info(info, image_urls)
                # 这批图片来自 yt-dlp 的 thumbnails（视频封面 / 作者头像），
                # 视频下载失败时不能把它们当作图文内容发送。
                thumb_only = bool(image_urls)

            # 6. 统一执行图片下载
            downloaded_images = []
            if image_urls:
                logger.info(
                    f"[social_parser] 成功提取到 {len(image_urls)}"
                    " 张关联图片，准备下载..."
                )
                for img_url in image_urls[:9]:
                    try:
                        img_path = await asyncio.to_thread(
                            self._download_image, img_url
                        )
                        if img_path and os.path.exists(img_path):
                            downloaded_images.append(img_path)
                            local_files_to_clean.append(img_path)
                    except Exception as img_err:
                        logger.warning(
                            f"[social_parser] 下载图片失败 ({img_url}):"
                            f" {img_err}"
                        )

            # 7. 精准分流下载视频
            local_file_path = None
            download_info = {}
            video_failed_msg = None
            if has_video:
                logger.info(
                    "[social_parser] 检测到视频内容，启动"
                    f" [{platform_name}] 专属下载通道..."
                )
                try:
                    if platform == "weibo" and weibo_direct_video:
                        local_file_path = await asyncio.to_thread(
                            self._download_direct_file, weibo_direct_video, ".mp4"
                        )
                        download_info = {
                            "title": text_content[:30],
                            "uploader": uploader,
                            "duration": 0,
                        }
                    elif platform == "x" and tweet_data and tweet_data.get("video_url"):
                        local_file_path = await asyncio.to_thread(
                            self._download_direct_file,
                            tweet_data["video_url"],
                            ".mp4",
                            "https://x.com/",
                        )
                        download_info = {
                            "title": text_content[:30],
                            "uploader": uploader,
                            "duration": 0,
                        }
                    elif platform == "douyin" and douyin_data and douyin_data.get("video_url"):
                        local_file_path = await asyncio.to_thread(
                            self._download_direct_file, douyin_data["video_url"], ".mp4"
                        )
                        download_info = {
                            "title": text_content[:30],
                            "uploader": uploader,
                            "duration": 0,
                        }
                    elif (
                        platform == "instagram"
                        and ins_data
                        and ins_data.get("video_url")
                    ):
                        local_file_path = await asyncio.to_thread(
                            self._download_direct_file,
                            ins_data["video_url"],
                            ".mp4",
                            "https://www.instagram.com/",
                            True,
                        )
                        download_info = {
                            "title": text_content[:30],
                            "uploader": uploader,
                            "duration": 0,
                        }
                    elif platform == "instagram":
                        local_file_path, download_info = await asyncio.to_thread(
                            self._download_video_direct, url, cookie_path
                        )
                    else:
                        # YouTube / TikTok / X(yt-dlp 兜底) / 抖音(yt-dlp 兜底)
                        local_file_path, download_info = await asyncio.to_thread(
                            self._download_video_manual_merge, url, cookie_path
                        )
                except Exception as e:
                    logger.warning(f"[social_parser] 视频下载失败，将回退为图文: {e}")
                    has_video = False
                    local_file_path = None
                    download_info = {}
                    video_failed_msg = str(e)[:150]
                    if thumb_only:
                        # thumbnails 只是视频封面 / 作者头像，直接丢弃，
                        # 避免把头像当成图文内容发给用户。
                        image_urls.clear()
                        downloaded_images = []
                    elif info:
                        self._extract_images_from_info(info, image_urls)
                        downloaded_images = []
                        for img_url in image_urls[:9]:
                            try:
                                img_path = await asyncio.to_thread(
                                    self._download_image, img_url
                                )
                                if img_path and os.path.exists(img_path):
                                    downloaded_images.append(img_path)
                                    local_files_to_clean.append(img_path)
                            except Exception as img_err:
                                logger.warning(
                                    f"[social_parser] 下载图片失败 ({img_url}):"
                                    f" {img_err}"
                                )

                if local_file_path and os.path.exists(local_file_path):
                    local_files_to_clean.append(local_file_path)
                    file_size_mb = os.path.getsize(local_file_path) / (
                        1024 * 1024
                    )
                    if file_size_mb > self.max_video_size_mb:
                        yield event.plain_result(
                            f"运输错误: 视频体积 ({file_size_mb:.1f}MB)"
                            f" 过大，超出 {self.max_video_size_mb}MB 下载限制"
                        )
                        return
                    # 元数据可能缺失（Instagram / 直链下载通道不会带回
                    # duration 与 width/height），对实际文件做一次探测补全。
                    if not download_info.get("duration") or not (
                        download_info.get("width") and download_info.get("height")
                    ):
                        probe = await asyncio.to_thread(
                            self._sp_probe_media_file, local_file_path
                        )
                        if not download_info.get("duration") and probe.get("duration"):
                            download_info["duration"] = probe["duration"]
                        if not download_info.get("width") and probe.get("width"):
                            download_info["width"] = probe["width"]
                            download_info["height"] = probe["height"]

            # 纯视频帖子不发送图片（封面图）
            if has_video and local_file_path:
                for img_path in downloaded_images:
                    if img_path in local_files_to_clean:
                        local_files_to_clean.remove(img_path)
                    try:
                        os.remove(img_path)
                    except Exception:
                        pass
                downloaded_images.clear()

            # ======================== 极简紧凑排版拼接 ========================
            if has_video and local_file_path:
                v_title = download_info.get("title") or title or "无标题"
                uploader = (
                    download_info.get("uploader")
                    or download_info.get("channel")
                    or uploader
                )
                duration = download_info.get("duration", 0)
                time_str = self._sp_format_duration(duration)
                width = download_info.get("width")
                height = download_info.get("height")
                resolution_str = (
                    f"{width}x{height}" if width and height else "未知"
                )

                plain_text = (
                    f"[{platform_name}] 解析成功\n作者: {uploader}\n时长:"
                    f" {time_str}\n分辨率: {resolution_str}"
                )

                clean_desc = (text_content if text_content else v_title).strip()
                if clean_desc:
                    if len(clean_desc) > 400:
                        clean_desc = clean_desc[:400] + "..."
                    plain_text += f"\n内容: {clean_desc}"
            else:
                headline = (
                    f"[{platform_name}] 视频下载失败"
                    if video_failed_msg
                    else f"[{platform_name}] 解析成功"
                )
                plain_text = f"{headline}\n作者: {uploader}"
                if video_failed_msg:
                    plain_text += f"\n原因: {video_failed_msg}"
                if text_content:
                    clean_text = text_content.strip()
                    if len(clean_text) > 600:
                        clean_text = clean_text[:600] + "..."
                    plain_text += f"\n内容: {clean_text}"

            if cookie_warn_msg:
                plain_text += f"\n提示: {cookie_warn_msg}"

            yield event.plain_result(plain_text)

            if (
                has_video
                and local_file_path
                and os.path.exists(local_file_path)
            ):
                await asyncio.sleep(0.5)
                video_chain = []
                if hasattr(Video, "fromFileSystem"):
                    video_chain.append(Video.fromFileSystem(local_file_path))
                elif hasattr(Video, "fromPath"):
                    video_chain.append(Video.fromPath(local_file_path))
                else:
                    video_chain.append(Video(file=local_file_path))
                try:
                    yield event.chain_result(video_chain)
                except Exception as send_err:
                    logger.error(f"[social_parser] 视频发送失败: {send_err}")

            if downloaded_images:
                await asyncio.sleep(0.5)
                image_chain = []
                for img_path in downloaded_images:
                    if hasattr(Image, "fromFileSystem"):
                        image_chain.append(Image.fromFileSystem(img_path))
                    elif hasattr(Image, "fromPath"):
                        image_chain.append(Image.fromPath(img_path))
                    else:
                        image_chain.append(Image(file=img_path))
                try:
                    yield event.chain_result(image_chain)
                except Exception as img_send_err:
                    logger.error(
                        f"[social_parser] 高清图片发送失败: {img_send_err}"
                    )

        except Exception as e:
            logger.error(f"[social_parser] 解析失败: {e}")
            yield event.plain_result(f"解析失败: {str(e)[:150]}")

        finally:
            if msg_id:
                try:
                    await event.bot.delete_msg(message_id=msg_id)
                except Exception as e:
                    logger.debug(f"[social_parser] 撤回消息失败: {e}")

            for file_path in local_files_to_clean:
                if file_path and os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                    except Exception as clean_err:
                        logger.error(
                            f"[social_parser] 清理临时文件失败: {clean_err}"
                        )

            event.stop_event()

    def _extract_images_from_info(self, info: dict, image_urls: list):
        if not info:
            return
        thumbs = info.get("thumbnails") or []
        for t in reversed(thumbs):
            t_url = t.get("url", "")
            if t_url and not any(kw in t_url.lower() for kw in ["profile", "avatar", "logo"]):
                if t_url not in image_urls:
                    image_urls.append(t_url)
                    break
        if info.get("_type") == "playlist" and info.get("entries"):
            for entry in info["entries"]:
                if entry and entry.get("thumbnails"):
                    last_thumb = entry["thumbnails"][-1]
                    t_url = last_thumb.get("url", "")
                    if t_url and t_url not in image_urls:
                        image_urls.append(t_url)
        if not image_urls and info.get("entries"):
            for entry in info["entries"]:
                if entry and entry.get("display_url"):
                    if entry["display_url"] not in image_urls:
                        image_urls.append(entry["display_url"])

    # ========== 抖音专属解析 ==========
    def _parse_douyin_fallback(self, url: str) -> dict:
        """抖音兜底解析：使用第三方 API，返回统一结构"""
        proxies = (
            {"http": self.proxy_url, "https": self.proxy_url}
            if self.proxy_url else None
        )
        api_configs = [
            ("https://api.vvhan.com/api/douyin", {"url": url}),
            ("https://api.oioweb.cn/api/douyin/video", {"url": url}),
        ]
        for api_url, params in api_configs:
            try:
                logger.info(f"[social_parser] 尝试抖音接口: {api_url}")
                resp = requests.get(
                    api_url,
                    params=params,
                    headers={"User-Agent": self.user_agent},
                    proxies=proxies,
                    timeout=15,
                    verify=False,
                )
                if resp.status_code != 200:
                    logger.warning(
                        f"[social_parser] 抖音接口 {api_url} 返回状态码 {resp.status_code}"
                    )
                    continue
                try:
                    data = resp.json()
                except Exception:
                    logger.warning(
                        f"[social_parser] 抖音接口 {api_url} 返回非 JSON，前 200 字符: "
                        f"{resp.text[:200]}"
                    )
                    continue
                if not isinstance(data, dict):
                    continue

                info = (
                    data.get("data")
                    if isinstance(data.get("data"), dict)
                    else data
                )
                if not isinstance(info, dict):
                    continue

                video_url = (
                    info.get("video_url")
                    or info.get("play_addr")
                    or info.get("url")
                    or info.get("video")
                )
                if isinstance(video_url, dict):
                    video_url = (
                        video_url.get("url")
                        or video_url.get("play_addr")
                        or (video_url.get("url_list") or [None])[0]
                    )
                if isinstance(video_url, list):
                    video_url = video_url[0] if video_url else None
                if isinstance(video_url, str) and video_url.startswith("//"):
                    video_url = "https:" + video_url

                cover_url = (
                    info.get("cover")
                    or info.get("cover_url")
                    or info.get("origin_cover")
                )
                if isinstance(cover_url, list):
                    cover_url = cover_url[0] if cover_url else None
                if isinstance(cover_url, str) and cover_url.startswith("//"):
                    cover_url = "https:" + cover_url

                images = []
                raw_images = info.get("images") or []
                if isinstance(raw_images, list):
                    for img in raw_images:
                        if isinstance(img, str):
                            images.append(img)
                        elif isinstance(img, dict):
                            u = img.get("url") or img.get("url_list")
                            if isinstance(u, list):
                                u = u[0] if u else None
                            if u:
                                images.append(u)

                if not video_url and not images:
                    continue

                return {
                    "uploader": (
                        info.get("author")
                        or info.get("nickname")
                        or info.get("author_name")
                        or "抖音用户"
                    ),
                    "text": (
                        info.get("title")
                        or info.get("desc")
                        or info.get("description")
                        or ""
                    ),
                    "video_url": video_url,
                    "cover_url": cover_url,
                    "images": images,
                }
            except Exception as e:
                logger.warning(
                    f"[social_parser] 抖音接口请求失败 ({api_url}): {e}"
                )
                continue
        return None

    # ========== 微博专属解析 ==========
    def _generate_weibo_visitor_cookies(self) -> dict:
        url = "https://passport.weibo.com/visitor/genvisitor2"
        headers = {
            "User-Agent": self.user_agent,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        data = {"cb": "visitor_gray_callback", "tid": "", "from": "weibo"}
        try:
            resp = requests.post(
                url,
                data=data,
                headers=headers,
                timeout=10,
                verify=False,
                proxies=(
                    {"http": self.proxy_url, "https": self.proxy_url}
                    if self.proxy_url
                    else None
                ),
            )
            if resp.status_code != 200:
                return {}
            cookies = {}
            for item in resp.headers.get("Set-Cookie", "").split(";"):
                if "=" in item:
                    k, v = item.split("=", 1)
                    k = k.strip()
                    v = v.strip()
                    if k and v:
                        cookies[k] = v
            return cookies if "SUB" in cookies else {}
        except Exception as e:
            logger.warning(f"获取微博访客 Cookie 失败: {e}")
            return {}

    def _expand_short_url(self, short_url: str) -> str:
        headers = {"User-Agent": self.user_agent}
        try:
            resp = requests.head(
                short_url,
                headers=headers,
                timeout=10,
                allow_redirects=True,
                verify=False,
            )
            return resp.url
        except Exception:
            return short_url

    def _parse_weibo_fallback(self, url: str, cookie_path: str) -> dict:
        mblogid = None
        patterns = [
            r"/(?:detail|status|tv/show)/([A-Za-z0-9]+)",
            r"weibo\.(?:com|cn)/\d+/([A-Za-z0-9]+)",
            r"id=([A-Za-z0-9]+)",
            r"weibo\.(?:com|cn)/([A-Za-z0-9]+)$",
        ]
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                mblogid = match.group(1)
                break
        if not mblogid:
            parts = [p for p in url.split("?")[0].split("/") if p]
            if parts:
                mblogid = parts[-1]
        if not mblogid:
            return None

        user_cookies = self._load_cookies_to_dict(cookie_path) if cookie_path else {}
        request_cookies = user_cookies if user_cookies else self._generate_weibo_visitor_cookies()
        if not request_cookies:
            request_cookies = {}

        proxies = (
            {"http": self.proxy_url, "https": self.proxy_url}
            if self.proxy_url else None
        )

        pc_headers = {
            "User-Agent": self.user_agent,
            "Referer": f"https://weibo.com/detail/{mblogid}",
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
        }

        try:
            resp = requests.get(
                f"https://weibo.com/ajax/statuses/show?id={mblogid}",
                headers=pc_headers,
                cookies=request_cookies,
                timeout=12,
                verify=False,
                proxies=proxies,
            )
            if resp.status_code != 200:
                return None

            data = resp.json()
            if not data or "user" not in data:
                return None

            uploader = data.get("user", {}).get("screen_name", "微博用户")
            mid = data.get("mid") or data.get("idstr")
            is_long_text = data.get("isLongText", False)

            def clean_text(raw: str) -> str:
                if not raw:
                    return ""
                raw = re.sub(r"<br\s*/?>", "\n", raw)
                raw = re.sub(r"<[^>]+>", "", raw)
                raw = raw.replace("\u200b", "").replace("​​​", "")
                return "\n".join(line.strip() for line in raw.splitlines() if line.strip())

            full_text = ""
            for key in ["longTextContent_raw", "longTextContent", "text_raw", "text"]:
                val = data.get(key)
                if not val and data.get("longText"):
                    val = data["longText"].get(key.replace("longText", "longTextContent"))
                if val and isinstance(val, str) and len(val) > 100:
                    full_text = clean_text(val)
                    break

            if (is_long_text or len(full_text) < 50) and mblogid:
                try:
                    lt_resp = requests.get(
                        f"https://weibo.com/ajax/statuses/longtext?id={mblogid}",
                        headers=pc_headers,
                        cookies=request_cookies,
                        timeout=10,
                        verify=False,
                        proxies=proxies,
                    )
                    if lt_resp.status_code == 200:
                        lt_data = lt_resp.json().get("data", {})
                        lt_text = lt_data.get("longTextContent") or lt_data.get("longTextContent_raw")
                        if lt_text:
                            full_text = clean_text(lt_text)
                except Exception as e:
                    logger.debug(f"[social_parser] longtext 接口失败: {e}")

            if (is_long_text or len(full_text) < 50) and mid:
                try:
                    m_headers = {
                        "User-Agent": "Mozilla/5.0 (Linux; Android 10; SM-G960U) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36",
                        "Referer": f"https://m.weibo.cn/detail/{mblogid}",
                        "Accept": "application/json, text/plain, */*",
                        "X-Requested-With": "XMLHttpRequest",
                    }
                    ext_resp = requests.get(
                        f"https://m.weibo.cn/statuses/extend?id={mid}",
                        headers=m_headers,
                        cookies=request_cookies,
                        timeout=10,
                        verify=False,
                        proxies=proxies,
                    )
                    if ext_resp.status_code == 200:
                        ext_data = ext_resp.json().get("data", {})
                        ext_text = ext_data.get("longTextContent") or ext_data.get("text")
                        if ext_text:
                            full_text = clean_text(ext_text)
                except Exception as e:
                    logger.debug(f"[social_parser] 移动端 extend 接口失败: {e}")

            if not full_text:
                full_text = clean_text(data.get("text_raw") or data.get("text", ""))

            retweeted = data.get("retweeted_status")
            target_status = retweeted if retweeted else data

            if retweeted:
                main_text = full_text
                ret_text = clean_text(
                    retweeted.get("longTextContent_raw")
                    or retweeted.get("longTextContent")
                    or retweeted.get("text_raw")
                    or retweeted.get("text", "")
                )
                ret_user = retweeted.get("user", {}).get("screen_name", "未知原作者")
                if main_text and ret_text:
                    final_text = f"{main_text}\n\n--------------------\n转发自 @{ret_user}:\n{ret_text}"
                else:
                    final_text = main_text or ret_text
            else:
                final_text = full_text

            def extract_images_from_status(status_obj: dict) -> list:
                images = []
                if not status_obj:
                    return images
                pic_infos = status_obj.get("pic_infos") or {}
                pic_ids = status_obj.get("pic_ids") or list(pic_infos.keys())
                for pid in pic_ids:
                    info = pic_infos.get(pid) or {}
                    url = (
                        info.get("largest", {}).get("url")
                        or info.get("original", {}).get("url")
                        or info.get("mw2000", {}).get("url")
                        or info.get("large", {}).get("url")
                        or info.get("orj1080", {}).get("url")
                        or info.get("bmiddle", {}).get("url")
                    )
                    if url and url not in images:
                        images.append(url)
                if not images and pic_ids:
                    for pid in pic_ids:
                        if not pid:
                            continue
                        constructed = f"https://wx1.sinaimg.cn/large/{pid}.jpg"
                        if constructed not in images:
                            images.append(constructed)
                if not images:
                    for pic in status_obj.get("pics") or []:
                        if isinstance(pic, dict):
                            url = (
                                pic.get("large", {}).get("url")
                                or pic.get("url")
                                or pic.get("original_pic")
                            )
                            if url and url not in images:
                                images.append(url)
                return images

            images = extract_images_from_status(target_status)

            video_url = None
            page_info = target_status.get("page_info", {}) or {}
            media_info = page_info.get("media_info", {}) or {}
            if media_info:
                play_list = media_info.get("playback_list") or []
                best = None
                best_bitrate = -1
                for item in play_list:
                    pinfo = item.get("play_info") if isinstance(item, dict) else None
                    if pinfo and pinfo.get("url"):
                        br = int(pinfo.get("bitrate") or 0)
                        if br > best_bitrate:
                            best = pinfo["url"]
                            best_bitrate = br
                video_url = best or media_info.get("stream_url_hd") or media_info.get("stream_url")

            return {
                "uploader": uploader,
                "text": final_text,
                "images": images,
                "video_url": video_url,
            }
        except Exception as e:
            logger.warning(f"微博 PC AJAX 解析失败: {e}")

        return None

    # ========== 视频下载 ==========
    def _download_video_manual_merge(self, url: str, cookie_path: str) -> tuple:
        max_retries = 2
        last_error = None
        for attempt in range(max_retries):
            try:
                return self._do_download_video_manual_merge(url, cookie_path)
            except Exception as e:
                last_error = e
                logger.warning(f"[social_parser] 视频下载失败 (尝试 {attempt+1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(5)
                    continue
        raise last_error

    def _do_download_video_manual_merge(self, url: str, cookie_path: str) -> tuple:
        ts = int(time.time())
        temp_dir = tempfile.gettempdir()
        out_path = os.path.join(temp_dir, f"merged_{ts}.mp4")

        ffmpeg_dir = (
            os.path.dirname(self.ffmpeg_exe)
            if (self.ffmpeg_exe and os.path.isabs(self.ffmpeg_exe))
            else None
        )

        opts = {
            "quiet": True,
            "no_warnings": True,
            "proxy": self.proxy_url or None,
            "cookiefile": (
                cookie_path
                if (cookie_path and os.path.exists(cookie_path))
                else None
            ),
            "outtmpl": os.path.join(temp_dir, f"dl_{ts}_%(id)s.%(ext)s"),
            "format": (
                "bestvideo[height<=2160][vcodec^=avc1]+bestaudio[ext=m4a]/bestvideo[height<=2160]+bestaudio/best"
            ),
            "merge_output_format": "mp4",
            "extractor_args": {"youtube": {"player_client": ["android"]}},
            "sleep_interval_requests": 1,
            "max_sleep_interval_requests": 3,
        }
        if ffmpeg_dir:
            opts["ffmpeg_location"] = ffmpeg_dir

        try:
            with YoutubeDL(opts) as ydl:
                v_info = ydl.extract_info(url, download=True)
                downloaded_file = ydl.prepare_filename(v_info)
                base, _ = os.path.splitext(downloaded_file)
                target_file = base + ".mp4"
                if os.path.exists(target_file):
                    return target_file, v_info
                elif os.path.exists(downloaded_file):
                    return downloaded_file, v_info
        except Exception as direct_err:
            logger.warning(
                "[social_parser] yt-dlp 原生下载合并失败，降级切换手动分轨合并:"
                f" {direct_err}"
            )

        v_tmpl = os.path.join(temp_dir, f"v_{ts}_%(id)s.%(ext)s")
        a_tmpl = os.path.join(temp_dir, f"a_{ts}_%(id)s.%(ext)s")

        common_opts = {
            "quiet": True,
            "no_warnings": True,
            "proxy": self.proxy_url or None,
            "cookiefile": (
                cookie_path
                if (cookie_path and os.path.exists(cookie_path))
                else None
            ),
            "extractor_args": {"youtube": {"player_client": ["android"]}},
            "sleep_interval_requests": 1,
            "max_sleep_interval_requests": 3,
        }
        if ffmpeg_dir:
            common_opts["ffmpeg_location"] = ffmpeg_dir

        v_opts = {**common_opts, "outtmpl": v_tmpl, "format": "bestvideo[height<=2160][vcodec^=avc1]/bestvideo[height<=2160]/bestvideo"}
        a_opts = {**common_opts, "outtmpl": a_tmpl, "format": "bestaudio[ext=m4a]/bestaudio"}

        v_file, a_file = "", ""
        with YoutubeDL(v_opts) as ydl:
            v_info = ydl.extract_info(url, download=True)
            v_file = ydl.prepare_filename(v_info)
        with YoutubeDL(a_opts) as ydl:
            v_info_a = ydl.extract_info(url, download=True)
            a_file = ydl.prepare_filename(v_info_a)

        try:
            self._ffmpeg_merge_smart(v_file, a_file, out_path)
        except Exception as e:
            logger.warning(f"[social_parser] 合并失败，回退纯视频流: {e}")
            if os.path.exists(v_file):
                shutil.move(v_file, out_path)
            else:
                raise Exception("无法生成有效视频文件")

        for f in [v_file, a_file]:
            if f and os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

        return out_path, v_info

    def _download_video_direct(self, url: str, cookie_path: str) -> tuple:
        ts = int(time.time())
        temp_dir = tempfile.gettempdir()
        out_tmpl = os.path.join(temp_dir, f"direct_{ts}_%(id)s.%(ext)s")

        opts = {
            "quiet": True,
            "no_warnings": True,
            "proxy": self.proxy_url or None,
            "cookiefile": (
                cookie_path
                if (cookie_path and os.path.exists(cookie_path))
                else None
            ),
            "outtmpl": out_tmpl,
            "format": "best",
            "extractor_args": {"youtube": {"player_client": ["android"]}},
            "sleep_interval_requests": 1,
            "max_sleep_interval_requests": 3,
        }
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            return filename, info

    def _download_direct_file(
        self,
        url: str,
        suffix: str = ".mp4",
        referer: str = "https://weibo.com/",
        use_proxy: bool = False,
    ) -> str:
        """直接下载直链文件（微博 / 抖音 / X / Instagram 视频）"""
        ts = int(time.time())
        temp_dir = tempfile.gettempdir()
        out_path = os.path.join(temp_dir, f"direct_{ts}{suffix}")

        headers = {
            "User-Agent": self.user_agent,
            "Referer": referer,
        }
        proxies = (
            {"http": self.proxy_url, "https": self.proxy_url}
            if (use_proxy and self.proxy_url)
            else None
        )

        resp = requests.get(
            url,
            headers=headers,
            proxies=proxies,
            timeout=60,
            verify=False,
            stream=True,
        )
        resp.raise_for_status()

        with open(out_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

        if not os.path.exists(out_path) or os.path.getsize(out_path) < 1024:
            raise Exception("直链视频下载失败或文件体积异常")

        return out_path

    def _ffmpeg_merge_smart(self, video_file: str, audio_file: str, output: str):
        ffmpeg_cmd = self.ffmpeg_exe or "ffmpeg"
        probe_cmd = [ffmpeg_cmd, "-i", video_file, "-f", "null", "-"]
        res = subprocess.run(probe_cmd, capture_output=True, text=True)
        vcodec = None
        for line in (res.stderr + res.stdout).split("\n"):
            if "Stream #0:0" in line or "Video:" in line:
                vcodec = (
                    line.split("Video:")[1].split(",")[0].split(" ")[0].strip()
                    if "Video:" in line
                    else "unknown"
                )
                break
        need_convert = vcodec and vcodec != "h264"
        if need_convert:
            cmd = [
                ffmpeg_cmd, "-i", video_file, "-i", audio_file,
                "-c:v", "libx264", "-c:a", "aac",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                "-map", "0:v:0", "-map", "1:a:0", "-y", output,
            ]
        else:
            cmd = [
                ffmpeg_cmd, "-i", video_file, "-i", audio_file,
                "-c:v", "copy", "-c:a", "aac",
                "-map", "0:v:0", "-map", "1:a:0", "-y", output,
            ]
        subprocess.run(cmd, capture_output=True, check=True)

    # ========== Twitter 图文深度解密组件 ==========
    def _extract_tweet_id(self, url: str) -> str:
        match = re.search(r"/status/(\d+)", url)
        return match.group(1) if match else None

    def _parse_twitter_syndication(self, tweet_id: str) -> dict:
        url = f"https://cdn.syndication.twimg.com/tweet-result?id={tweet_id}&lang=zh"
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        proxies = (
            {"http": self.proxy_url, "https": self.proxy_url}
            if self.proxy_url else None
        )
        try:
            resp = requests.get(url, headers=headers, proxies=proxies, timeout=10, verify=False)
            if resp.status_code != 200:
                return None
            raw = resp.json()
        except Exception:
            return None

        def _collect_media(node):
            """从一个 tweet 节点收集图片 URL 列表和视频 URL"""
            photos = []
            video_url = None
            if not isinstance(node, dict):
                return photos, video_url

            for key in ("photos", "mediaDetails"):
                arr = node.get(key) or []
                if not isinstance(arr, list):
                    continue
                for m in arr:
                    if not isinstance(m, dict):
                        continue
                    m_type = (m.get("type") or "").lower()
                    if m_type in ("video", "animated_gif") or m.get("video_info"):
                        variants = (m.get("video_info") or {}).get("variants") or []
                        best = None
                        best_br = -1
                        for v in variants:
                            if v.get("content_type") == "video/mp4" and v.get("url"):
                                br = int(v.get("bitrate") or 0)
                                if br > best_br:
                                    best_br = br
                                    best = v["url"]
                        if best and not video_url:
                            video_url = best
                        continue
                    u = m.get("url") or m.get("media_url_https") or m.get("media_url")
                    if u and not any(
                        u.lower().split("?")[0].endswith(ext)
                        for ext in (".mp4", ".webm", ".mov", ".m3u8")
                    ):
                        photos.append({"url": u})

            if not video_url and isinstance(node.get("video"), dict):
                variants = node["video"].get("variants") or []
                best = None
                best_br = -1
                for v in variants:
                    if v.get("content_type") == "video/mp4" and v.get("url"):
                        br = int(v.get("bitrate") or 0)
                        if br > best_br:
                            best_br = br
                            best = v["url"]
                video_url = best

            return photos, video_url

        photos, video_url = _collect_media(raw)

        # 递归处理 quoted_tweet / retweeted_status
        for qkey in ("quoted_tweet", "retweeted_status", "quoted_status"):
            sub = raw.get(qkey)
            if isinstance(sub, dict):
                sub_photos, sub_video = _collect_media(sub)
                existing_urls = {p.get("url") for p in photos if isinstance(p, dict)}
                for sp in sub_photos:
                    if sp.get("url") and sp["url"] not in existing_urls:
                        photos.append(sp)
                        existing_urls.add(sp["url"])
                if not video_url and sub_video:
                    video_url = sub_video

        main_text = raw.get("text") or ""
        sub_text = ""
        for qkey in ("quoted_tweet", "retweeted_status", "quoted_status"):
            sub = raw.get(qkey)
            if isinstance(sub, dict):
                st = sub.get("text") or ""
                if st:
                    sub_user = (
                        ((sub.get("user") or {}).get("name"))
                        or ((sub.get("user") or {}).get("screen_name"))
                        or "原推"
                    )
                    sub_text = f"\n\n--------------------\n引用 @{sub_user}:\n{st}"
                    break

        user = raw.get("user") or {}
        return {
            "user": {
                "name": user.get("name") or user.get("screen_name") or "未知作者",
            },
            "text": (main_text + sub_text).strip(),
            "photos": photos,
            "video_url": video_url,
        }

    def _parse_vxtwitter_fallback(self, tweet_id: str) -> dict:
        url = f"https://api.vxtwitter.com/status/{tweet_id}"
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        proxies = (
            {"http": self.proxy_url, "https": self.proxy_url}
            if self.proxy_url else None
        )
        try:
            resp = requests.get(url, headers=headers, proxies=proxies, timeout=12, verify=False)
            if resp.status_code != 200:
                return None
            data = resp.json()
        except Exception as e:
            logger.warning(f"[social_parser] vxtwitter 深度解密节点请求失败: {e}")
            return None

        media_urls = data.get("mediaURLs") or data.get("media_urls") or []
        photos = []
        video_url = None
        seen = set()
        for u in media_urls:
            if not u or u in seen:
                continue
            seen.add(u)
            low = u.lower().split("?")[0]
            if low.endswith((".mp4", ".webm", ".mov", ".m3u8")):
                if not video_url:
                    video_url = u
            else:
                photos.append({"url": u})

        for m in data.get("media_extended") or []:
            if not isinstance(m, dict):
                continue
            m_type = (m.get("type") or "").lower()
            m_url = m.get("url")
            if not m_url or m_url in seen:
                continue
            seen.add(m_url)
            if m_type in ("video", "gif"):
                if not video_url:
                    video_url = m_url
            else:
                photos.append({"url": m_url})

        return {
            "user": {
                "name": data.get("user_name") or data.get("user_screen_name") or "未知作者",
            },
            "text": data.get("text", ""),
            "photos": photos,
            "video_url": video_url,
        }

    # ========== Instagram 备用解析 ==========
    def _load_cookies_to_dict(self, cookie_path: str) -> dict:
        cookies = {}
        if not cookie_path or not os.path.exists(cookie_path):
            return cookies
        try:
            with open(cookie_path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split("\t")
                    if len(parts) < 7:
                        parts = line.split()
                    if len(parts) >= 7:
                        cookies[parts[5]] = parts[6]
        except Exception:
            pass
        return cookies

    def _shortcode_to_media_id(self, shortcode: str) -> int:
        char_map = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        media_id = 0
        for c in shortcode:
            idx = char_map.find(c)
            if idx == -1:
                raise ValueError(f"Invalid character '{c}' in shortcode")
            media_id = media_id * 64 + idx
        return media_id

    def _parse_instagram_mirror(self, url: str) -> dict | None:
        """走用户配置的第三方解析中转地址（可选通道）。

        Instagram 官方通道对匿名请求一律返回 login_required，而登录态 cookies
        又容易导致账号被封。若配置了 instagram_settings.mirror_url，这里会优先
        走它：地址中可用 {url}（帖子链接）或 {code}（帖子短码）作为占位符，未含
        占位符时自动以 ?url= 形式附加。返回体按 JSON 优先、HTML 兜底解析。
        未配置中转地址时返回 None，不影响原有 cookies 链路。
        """
        mirror = str(getattr(self, "ig_mirror_url", "") or "").strip()
        if not mirror:
            return None
        match = re.search(r"/(?:p|reel|tv|stories|share/p)/([^/?#\s]+)", url)
        shortcode = match.group(1) if match else ""
        target = mirror.replace("{url}", quote(url, safe="")).replace(
            "{code}", shortcode
        )
        if "{url}" not in mirror and "{code}" not in mirror:
            target += ("&" if "?" in target else "?") + "url=" + quote(url, safe="")
        proxies = (
            {"http": self.proxy_url, "https": self.proxy_url}
            if self.proxy_url
            else None
        )
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        try:
            resp = requests.get(
                target, headers=headers, proxies=proxies, timeout=20, verify=False
            )
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or resp.encoding
            body = resp.text
        except Exception as e:
            logger.warning(f"[social_parser] Instagram 中转通道请求失败: {e}")
            return None

        def _walk(node, cb):
            if isinstance(node, dict):
                cb(node)
                for item in node.values():
                    _walk(item, cb)
            elif isinstance(node, list):
                for item in node:
                    _walk(item, cb)

        text_keys = ("caption", "description", "text", "title", "content")
        user_keys = (
            "username",
            "uploader",
            "author",
            "user_name",
            "nickname",
            "full_name",
        )
        media_exts = (
            ".jpg",
            ".jpeg",
            ".png",
            ".webp",
            ".heic",
            ".gif",
            ".mp4",
            ".mov",
            ".m3u8",
        )
        text, uploader, video_url = "", "", ""
        images, urls = [], []

        data = None
        if "json" in (resp.headers.get("Content-Type") or "").lower():
            try:
                data = resp.json()
            except Exception:
                data = None
        if data is None:
            try:
                data = json.loads(body)
            except Exception:
                data = None

        if data is not None:

            def _collect(node):
                nonlocal text, uploader, video_url
                for key, val in node.items():
                    lk = key.lower()
                    if (
                        not uploader
                        and lk in user_keys
                        and isinstance(val, str)
                        and val.strip()
                    ):
                        uploader = val.strip()
                    if not text and lk in text_keys:
                        if isinstance(val, str) and val.strip():
                            text = val.strip()
                        elif isinstance(val, dict):
                            inner = val.get("text") or val.get("value")
                            if isinstance(inner, str) and inner.strip():
                                text = inner.strip()
                    if not video_url and lk in (
                        "video_url",
                        "video",
                        "playback_url",
                        "video_versions",
                    ):
                        cand = val
                        if isinstance(cand, dict):
                            cand = cand.get("url")
                        elif isinstance(cand, list) and cand:
                            first = cand[0]
                            cand = first.get("url") if isinstance(first, dict) else first
                        if isinstance(cand, str) and cand.startswith("http"):
                            video_url = cand

            _walk(data, _collect)

            def _scan(node):
                if isinstance(node, str):
                    if node.startswith("http") and any(
                        ext in node.lower() for ext in media_exts
                    ):
                        urls.append(node)
                elif isinstance(node, dict):
                    for val in node.values():
                        _scan(val)
                elif isinstance(node, list):
                    for val in node:
                        _scan(val)

            _scan(data)
        else:
            desc = re.search(
                r'<meta[^>]+(?:property|name)=["\']og:description["\']\s+'
                r'content=["\']([^"\']+)["\']',
                body,
            )
            if desc:
                text = desc.group(1)
            ogv = re.search(
                r'<meta[^>]+(?:property|name)=["\']og:video["\']\s+'
                r'content=["\']([^"\']+)["\']',
                body,
            )
            if ogv:
                video_url = ogv.group(1)
            title = re.search(r"<title>([^<]+)</title>", body)
            if title and "on Instagram" in title.group(1):
                uploader = (
                    title.group(1).split("on Instagram")[0].replace("•", "").strip()
                )
            if not uploader:
                user_match = re.search(r'"username"\s*:\s*"([^"]+)"', body)
                if user_match:
                    uploader = user_match.group(1)
            for raw in re.findall(r'https?://[^\s"\'\\<>]+', body):
                urls.append(raw.replace("\\u0026", "&").replace("\\/", "/"))

        for item in urls:
            low = item.lower()
            if any(
                junk in low
                for junk in ("profile", "avatar", "logo", "/theme/", "100x100", "150x150")
            ):
                continue
            if any(ext in low for ext in (".mp4", ".mov", ".m3u8")):
                if not video_url:
                    video_url = item
                continue
            if item not in images:
                images.append(item)

        if not (text or images or video_url):
            logger.warning("[social_parser] Instagram 中转通道未解析出有效内容")
            return None
        logger.info(
            "[social_parser] Instagram 中转通道解析成功（图片"
            f" {len(images)} 张{'，含视频直链' if video_url else ''}）"
        )
        return {
            "uploader": uploader or "未知作者",
            "text": text,
            "images": images[:9],
            "video_url": video_url,
        }

    def _parse_instagram_fallback(self, url: str, cookie_path: str) -> dict | None:
        match = re.search(r"/p/([^/?#\s]+)", url)
        if not match:
            match = re.search(r"/(?:reel|stories|share/p)/([^/?#\s]+)", url)
        if not match:
            logger.error("[social_parser] 未能从URL中提取到 Instagram 帖子短代码")
            return None
        shortcode = match.group(1)
        uploader, text, images = "未知作者", "", []
        proxies = (
            {"http": self.proxy_url, "https": self.proxy_url}
            if self.proxy_url else None
        )
        official_cookies = self._load_cookies_to_dict(cookie_path)
        if not official_cookies:
            logger.error(
                "[social_parser] Instagram 未加载到登录态 Cookie，且未配置第三方"
                "中转地址；官方通道对匿名请求一律返回 login_required"
                f"（检查路径: {cookie_path}）"
            )
            return None
        csrftoken = official_cookies.get("csrftoken", "")
        base_headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://www.instagram.com/",
            "X-CSRFToken": csrftoken,
            "X-Requested-With": "XMLHttpRequest",
            "X-IG-App-ID": "936619743392459",
        }
        try:
            json_url = f"https://www.instagram.com/p/{shortcode}/?__a=1"
            resp = requests.get(json_url, headers=base_headers, cookies=official_cookies, proxies=proxies, timeout=12, verify=False)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    media = data.get("graphql", {}).get("shortcode_media") or data.get("items", [{}])[0]
                    if media:
                        uploader = media.get("owner", {}).get("username") or media.get("user", {}).get("username", "未知作者")
                        caption = media.get("edge_media_to_caption", {}).get("edges", [])
                        if caption:
                            text = caption[0].get("node", {}).get("text", "")
                        if "edge_sidecar_to_children" in media:
                            for edge in media["edge_sidecar_to_children"]["edges"]:
                                node = edge["node"]
                                if node.get("is_video"):
                                    continue
                                urls = node.get("display_url")
                                if urls:
                                    images.append(urls)
                        elif media.get("display_url"):
                            images.append(media["display_url"])
                        if images:
                            logger.info("[social_parser] JSON 端点成功获取内容")
                            return {"uploader": uploader, "text": text, "images": images}
                except:
                    pass
            else:
                logger.warning(f"[social_parser] JSON 端点请求失败，状态码: {resp.status_code}")
        except Exception as e:
            logger.warning(f"[social_parser] JSON 端点尝试出错: {e}")
        try:
            media_id = self._shortcode_to_media_id(shortcode)
            logger.info(f"[social_parser] 通过短码计算出 media_id: {media_id}")
            api_headers = {
                "User-Agent": "Instagram 300.0.0.29.110 Android (28/9; 440dpi; 1440x2560; ...)",
                "X-IG-App-ID": "936619743392459",
                "X-Requested-With": "XMLHttpRequest",
            }
            api_url = f"https://i.instagram.com/api/v1/media/{media_id}/info/"
            api_resp = requests.get(api_url, headers=api_headers, cookies=official_cookies, proxies=proxies, timeout=10, verify=False)
            if api_resp.status_code == 200:
                data = api_resp.json()
                if "items" in data and data["items"]:
                    item = data["items"][0]
                    uploader = item.get("user", {}).get("username", "未知作者")
                    caption = item.get("caption")
                    text = caption.get("text", "") if caption else ""
                    image_versions = item.get("image_versions2", {}).get("candidates", [])
                    for img in image_versions:
                        if img.get("url") and img["url"] not in images:
                            images.append(img["url"])
                    if images:
                        logger.info("[social_parser] 移动端 API (计算 media_id) 成功获取内容")
                        return {"uploader": uploader, "text": text, "images": images}
            else:
                logger.warning(f"[social_parser] 移动端 API (计算) 请求失败，状态码: {api_resp.status_code}")
        except Exception as e:
            logger.warning(f"[social_parser] 计算 media_id 请求出错: {e}")
        try:
            page_url = f"https://www.instagram.com/p/{shortcode}/"
            resp = requests.get(page_url, headers=base_headers, cookies=official_cookies, proxies=proxies, timeout=12, verify=False)
            if resp.status_code == 200:
                html = resp.text
                media_id_match = re.search(r'"media_id"\s*:\s*"(\d+)"', html)
                if media_id_match:
                    media_id = media_id_match.group(1)
                    logger.info(f"[social_parser] 从页面提取 media_id: {media_id}，尝试移动端 API...")
                    api_headers = {
                        "User-Agent": "Instagram 300.0.0.29.110 Android (28/9; 440dpi; 1440x2560; ...)",
                        "X-IG-App-ID": "936619743392459",
                        "X-Requested-With": "XMLHttpRequest",
                    }
                    api_url = f"https://i.instagram.com/api/v1/media/{media_id}/info/"
                    api_resp = requests.get(api_url, headers=api_headers, cookies=official_cookies, proxies=proxies, timeout=10, verify=False)
                    if api_resp.status_code == 200:
                        data = api_resp.json()
                        if "items" in data and data["items"]:
                            item = data["items"][0]
                            uploader = item.get("user", {}).get("username", "未知作者")
                            caption = item.get("caption")
                            text = caption.get("text", "") if caption else ""
                            image_versions = item.get("image_versions2", {}).get("candidates", [])
                            for img in image_versions:
                                if img.get("url") and img["url"] not in images:
                                    images.append(img["url"])
                            if images:
                                logger.info("[social_parser] 移动端 API (页面提取 media_id) 成功获取内容")
                                return {"uploader": uploader, "text": text, "images": images}
            else:
                logger.warning(f"[social_parser] 获取帖子页面失败，状态码: {resp.status_code}")
        except Exception as e:
            logger.warning(f"[social_parser] 页面解析/API 请求出错: {e}")
        try:
            if "html" not in locals() or not html:
                resp = requests.get(page_url, headers=base_headers, cookies=official_cookies, proxies=proxies, timeout=12, verify=False)
                if resp.status_code == 200:
                    html = resp.text
                else:
                    return None
            if not html:
                return None
            desc_match = re.search(r'<meta[^>]+(?:\bproperty|\bname)=["\']og:description["\']\s+content=["\']([^"\']+)["\']', html)
            if desc_match:
                text = desc_match.group(1)
            title_match = re.search(r"<title>([^<]+)</title>", html)
            if title_match:
                t_val = title_match.group(1)
                if "on Instagram" in t_val:
                    uploader = t_val.split("on Instagram")[0].replace("•", "").strip()
            raw_cdns = re.findall(r'(https://[^"\s\\>]+?cdninstagram\.com/[^"\s\\>]+)', html)
            for img in raw_cdns:
                img_clean = img.replace("\\u0026", "&").replace("\\/", "/")
                if any(x in img_clean.lower() for x in ["profile", "avatar", "logo", "150x150", "/theme/", "100x100"]):
                    continue
                if img_clean not in images:
                    images.append(img_clean)
            if images:
                logger.info(f"[social_parser] 常规网页解析成功，获取到 {len(images)} 张图片")
                return {"uploader": uploader, "text": text, "images": images}
        except Exception as e:
            logger.error(f"[social_parser] 网页解析发生异常: {e}")
        logger.error("[social_parser] 所有 Instagram 解析节点均失败，请检查 cookies 和代理设置。")
        return None

    def _download_image(self, url: str) -> str:
        referer = (
            "https://weibo.com/"
            if "weibo" in url.lower() or "sinaimg" in url.lower()
            else (
                "https://www.instagram.com/"
                if "instagram" in url.lower() or "cdninstagram" in url.lower()
                else "https://x.com/"
            )
        )
        headers = {"User-Agent": self.user_agent, "Referer": referer}
        proxies = (
            None
            if "sinaimg.cn" in url or "weibo.cn" in url
            else (
                {"http": self.proxy_url, "https": self.proxy_url}
                if self.proxy_url
                else None
            )
        )
        resp = requests.get(url, headers=headers, proxies=proxies, timeout=20, verify=False)
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "").lower()
        if "png" in content_type:
            suffix = ".png"
        elif "gif" in content_type:
            suffix = ".gif"
        elif "webp" in content_type:
            suffix = ".webp"
        else:
            suffix = ".jpg"
        tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=tempfile.gettempdir())
        with open(tmp_file.name, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        if os.path.exists(tmp_file.name) and os.path.getsize(tmp_file.name) < 1024:
            logger.warning(f"[social_parser] 警告：下载的素材体积异常小 ({os.path.getsize(tmp_file.name)} 字节)")
        return tmp_file.name

    def _extract_info(self, url: str, opts: dict) -> dict:
        opts = dict(opts)
        opts["ignore_no_formats_error"] = True
        with YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    def _sp_check_cookie_status(self, cookie_path: str) -> dict:
        status = {"exists": False, "expired": False, "expired_count": 0, "total_count": 0}
        if not cookie_path or not os.path.exists(cookie_path):
            return status
        status["exists"] = True
        now = time.time()
        try:
            with open(cookie_path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split("\t")
                    if len(parts) < 7:
                        parts = line.split()
                    if len(parts) >= 7:
                        status["total_count"] += 1
                        try:
                            if 0 < int(parts[4]) < now:
                                status["expired_count"] += 1
                        except ValueError:
                            pass
            if status["expired_count"] > 0:
                status["expired"] = True
        except Exception:
            pass
        return status

    def _sp_probe_media_file(self, path: str) -> dict:
        """用 ffmpeg 探测本地媒体文件的时长与分辨率。

        部分平台（Instagram 等）的 yt-dlp 元数据缺少 duration / width / height，
        直链下载通道也不会带回这两个字段，这里对实际下载的文件做一次兜底探测。
        """
        ffmpeg = self.ffmpeg_exe or "ffmpeg"
        try:
            res = subprocess.run(
                [ffmpeg, "-i", path],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except Exception as e:
            logger.warning(f"[social_parser] ffmpeg 探测媒体信息失败: {e}")
            return {}
        output = (res.stderr or "") + (res.stdout or "")
        duration = 0.0
        d_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", output)
        if d_match:
            duration = (
                int(d_match.group(1)) * 3600
                + int(d_match.group(2)) * 60
                + float(d_match.group(3))
            )
        width = None
        height = None
        v_match = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", output)
        if v_match:
            width = int(v_match.group(1))
            height = int(v_match.group(2))
        return {"duration": int(round(duration)), "width": width, "height": height}

    def _sp_format_duration(self, seconds: float) -> str:
        if not seconds:
            return "00:00"
        total_seconds = int(round(seconds))
        h, remainder = divmod(total_seconds, 3600)
        m, s = divmod(remainder, 60)
        if h > 0:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"
