"""B站内容理解插件 - MaiBot SDK v2

识别聊天中的 B 站视频内容（BV/av 号、bilibili.com 链接、b23.tv 短链），
获取视频信息与 B 站官方 AI 总结，辅助 bot 更好地参与讨论：

- ``chat.receive.after_process`` Hook (BLOCKING):
  自动检测入站消息中的 B 站链接/卡片，将视频信息与 B 站官方 AI 总结
  直接附加到该消息内容末尾（写入上下文历史），插件**不主动发送任何多余回复**，
  使 bot 在思考和聊天时直接拥有该视频的理解能力。
- ``parse_bilibili_video`` Tool:
  供 planner 按需显式解析指定的 B 站视频。
- ``/cu_login`` ``/cu_status`` ``/cu_logout`` Command:
  扫码登录 B 站管理（B 站官方 AI 总结接口需登录态）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from pathlib import Path
from typing import Any, Optional

import httpx
import qrcode
from bilibili_api import Credential
from bilibili_api.user import get_self_info
from bilibili_api.video import Video
from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder, ToolParameterInfo, ToolParamType

# ============ 链接识别 ============

_BV_PATTERN = re.compile(r"\b(BV[0-9A-Za-z]{10})\b")
_AV_PATTERN = re.compile(r"\bav(\d{6,})\b", re.IGNORECASE)
_BILI_VIDEO_URL_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.|m\.)?bilibili\.com/video/(?:BV[0-9A-Za-z]{10}|av\d+)",
    re.IGNORECASE,
)
_B23_SHORT_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?(?:b23\.tv|bili2233\.cn)/[0-9A-Za-z\-_]+",
    re.IGNORECASE,
)

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

# ============ 配置模型 ============


class PluginSectionConfig(PluginConfigBase):
    """插件基本信息"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.0.0", description="配置版本")


class ParseSectionConfig(PluginConfigBase):
    """链接解析设置"""

    __ui_label__ = "链接解析"
    __ui_icon__ = "link"
    __ui_order__ = 1

    enable_ai_summary: bool = Field(default=True, description="获取B站官方AI视频总结（需要扫码登录，部分视频无总结）")
    enable_in_group: bool = Field(default=True, description="在群聊消息中自动附加B站视频AI总结到上下文")
    enable_in_private: bool = Field(default=True, description="在私聊消息中自动附加B站视频AI总结到上下文")
    cache_ttl_seconds: int = Field(default=1800, ge=60, le=86400, description="视频信息缓存时长（秒）")


class CredentialSectionConfig(PluginConfigBase):
    """B站登录凭证（可选：手动填写，或使用 /cu_login 扫码登录）"""

    __ui_label__ = "登录凭证"
    __ui_icon__ = "key"
    __ui_order__ = 2

    sessdata: str = Field(default="", description="B站 Cookie - SESSDATA（手动填写可代替扫码登录）")
    bili_jct: str = Field(default="", description="B站 Cookie - bili_jct")
    buvid3: str = Field(default="", description="B站 Cookie - buvid3")
    dedeuserid: str = Field(default="", description="B站 Cookie - DedeUserID")


class ContentUnderstandingPluginConfig(PluginConfigBase):
    """插件完整配置"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    parse: ParseSectionConfig = Field(default_factory=ParseSectionConfig)
    credential: CredentialSectionConfig = Field(default_factory=CredentialSectionConfig)


# ============ 工具函数 ============


def _format_count(value: Any) -> str:
    """格式化播放/点赞等计数（1.2万）"""
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        return "0"
    if n >= 100_000_000:
        return f"{n / 100_000_000:.1f}亿"
    if n >= 10_000:
        return f"{n / 10_000:.1f}万"
    return str(n)


def _format_duration(seconds: Any) -> str:
    """格式化视频时长（分:秒）"""
    try:
        s = int(seconds or 0)
    except (TypeError, ValueError):
        return "未知"
    return f"{s // 60}:{s % 60:02d}"


def _extract_ai_summary(data: Any) -> str:
    """从 get_ai_conclusion 返回中提取总结文本（bilibili_api 版本间结构略有差异）。"""
    if not isinstance(data, dict):
        return ""
    result = data.get("data") if isinstance(data.get("data"), dict) else data
    if not isinstance(result, dict):
        return ""
    model_result = result.get("model_result") or {}
    summary = model_result.get("summary") if isinstance(model_result, dict) else ""
    return str(summary or "").strip()


# ============ 凭证管理 ============


class CredentialManager:
    """B站凭证管理：扫码登录、持久化、有效性缓存。

    凭证优先从 ``data_dir/credential.json``（扫码登录写入）加载，
    其次从插件配置的 cookie 字段构建。SDK 不提供写配置能力，
    因此扫码结果持久化到数据文件而非 config.toml。
    """

    QR_GENERATE = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
    QR_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"

    CHECK_INTERVAL_SECONDS = 1800  # 凭证有效性缓存时长

    def __init__(self, data_dir: Path, cookie_config: CredentialSectionConfig, client: httpx.AsyncClient):
        self._data_dir = data_dir
        self._cookie_config = cookie_config
        self._client = client
        self._cred_file = data_dir / "credential.json"
        self._qr_key = ""
        self._cached_credential: Optional[Credential] = None
        self._cached_at = 0.0

    def _load_from_file(self) -> Optional[Credential]:
        if not self._cred_file.exists():
            return None
        try:
            cookies = json.loads(self._cred_file.read_text(encoding="utf-8"))
            return Credential.from_cookies(cookies) if cookies.get("SESSDATA") else None
        except Exception:
            return None

    def _load_from_config(self) -> Optional[Credential]:
        cfg = self._cookie_config
        if not cfg.sessdata:
            return None
        return Credential.from_cookies(
            {
                "SESSDATA": cfg.sessdata,
                "bili_jct": cfg.bili_jct,
                "buvid3": cfg.buvid3,
                "DedeUserID": cfg.dedeuserid,
            }
        )

    def _save_cookies(self, cookies: dict[str, str]) -> None:
        keep = ("SESSDATA", "bili_jct", "buvid3", "DedeUserID", "ac_time_value")
        payload = {k: v for k, v in cookies.items() if k in keep and v}
        try:
            self._cred_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    async def get_credential(self) -> Optional[Credential]:
        """获取可用凭证：文件 > 配置；有效性 30 分钟内缓存。"""
        now = time.time()
        if self._cached_credential and now - self._cached_at < self.CHECK_INTERVAL_SECONDS:
            return self._cached_credential
        for builder in (self._load_from_file, self._load_from_config):
            cred = builder()
            if cred is None:
                continue
            try:
                if await cred.check_valid():
                    self._cached_credential = cred
                    self._cached_at = now
                    return cred
            except Exception:
                continue
        self._cached_credential = None
        self._cached_at = now
        return None

    def invalidate_cache(self) -> None:
        """登录态变化后重置缓存。"""
        self._cached_credential = None
        self._cached_at = 0.0

    async def login_qrcode(self) -> bytes:
        """生成登录二维码 PNG 图片字节。"""
        resp = await self._client.get(self.QR_GENERATE)
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"生成二维码失败: {data.get('message', '未知错误')}")
        self._qr_key = data["data"]["qrcode_key"]
        img = qrcode.make(data["data"]["url"])
        import io

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    async def poll_qrcode(self) -> str:
        """轮询扫码状态，返回最终结果消息（成功时已保存凭证）。"""
        if not self._qr_key:
            return "二维码未生成，请先执行 /cu_login"
        for _ in range(90):  # 二维码有效期约 3 分钟
            try:
                resp = await self._client.get(self.QR_POLL, params={"qrcode_key": self._qr_key})
                resp_data = resp.json()
            except Exception as exc:
                return f"检查扫码状态失败: {exc}"

            code = resp_data.get("data", {}).get("code", -1)
            if code == 0:
                cookies = dict(resp.cookies)
                if not cookies.get("SESSDATA"):
                    return "登录成功但未提取到凭证，请重试"
                self._save_cookies(cookies)
                self.invalidate_cache()
                uname = "未知"
                try:
                    profile = await get_self_info(Credential.from_cookies(cookies))
                    uname = profile.get("name", "未知")
                except Exception:
                    pass
                return f"✅ 登录成功！账号: {uname}，AI 总结功能已可用"
            if code == 86090:
                await asyncio.sleep(2)
                continue
            if code == 86038:
                return "⏰ 二维码已过期，请重新执行 /cu_login"
            if code == 86101:
                await asyncio.sleep(2)
                continue
            return f"扫码状态异常(code={code}): {resp_data.get('message', '')}"
        return "⏰ 登录超时，请重新执行 /cu_login"

    async def get_status(self) -> str:
        """获取登录状态描述。"""
        cred = await self.get_credential()
        if cred is None:
            return "❌ 未登录（AI 总结不可用，执行 /cu_login 扫码登录）"
        try:
            profile = await get_self_info(cred)
            return f"✅ 已登录: {profile.get('name', '未知')} (UID: {profile.get('mid', '?')})"
        except Exception as exc:
            return f"⚠️ 凭证可能已失效: {exc}"

    async def clear(self) -> str:
        """登出并清除本地凭证。"""
        self._cred_file.unlink(missing_ok=True)
        self.invalidate_cache()
        return "✅ 已登出并清除本地凭证"


# ============ 插件主类 ============


class ContentUnderstandingPlugin(MaiBotPlugin):
    """B站内容理解插件"""

    config_model = ContentUnderstandingPluginConfig

    def __init__(self) -> None:
        super().__init__()
        self._client: Optional[httpx.AsyncClient] = None
        self._cred_mgr: Optional[CredentialManager] = None
        self._poll_task: Optional[asyncio.Task[None]] = None
        self._video_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def on_load(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=True,
            headers={"User-Agent": _UA},
        )
        data_dir = Path(self.ctx.paths.data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        self._cred_mgr = CredentialManager(data_dir, self.config.credential, self._client)
        self.ctx.logger.info(
            "content_understanding_plugin 已加载 (ai_summary=%s, in_group=%s, in_private=%s)",
            self.config.parse.enable_ai_summary,
            self.config.parse.enable_in_group,
            self.config.parse.enable_in_private,
        )

    async def on_unload(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            self._poll_task = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self.ctx.logger.info("content_understanding_plugin 已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        self.ctx.logger.info("配置已更新 (scope=%s, version=%s)", scope, version)
        if self._cred_mgr is not None:
            self._cred_mgr.invalidate_cache()
        self._video_cache.clear()

    # ------------------------------------------------------------------ #
    # 链接解析与信息获取
    # ------------------------------------------------------------------ #

    async def _resolve_target(self, text: str) -> Optional[tuple[str, Any]]:
        """从文本解析视频目标。

        Returns:
            (\"bvid\", str) 或 (\"aid\", int)；无法识别时返回 None。
        """
        match = _BILI_VIDEO_URL_PATTERN.search(text) or _BV_PATTERN.search(text)
        if match:
            token = match.group(0)
            if "/video/" in token:
                token = token.split("/video/", 1)[1]
            token = token.split("?", 1)[0]
            if token.upper().startswith("BV"):
                return "bvid", token
            if token.lower().startswith("av"):
                return "aid", int(token[2:])
        match = _AV_PATTERN.search(text)
        if match:
            return "aid", int(match.group(1))
        # b23.tv / bili2233.cn 短链：跟随重定向后再提取
        for short in _B23_SHORT_PATTERN.findall(text):
            if not short.startswith("http"):
                short = f"https://{short}"
            try:
                assert self._client is not None
                resp = await self._client.get(short)
                final_url = str(resp.url)
            except Exception:
                continue
            for pattern in (_BV_PATTERN, _AV_PATTERN):
                m = pattern.search(final_url)
                if m:
                    if pattern is _AV_PATTERN:
                        return "aid", int(m.group(1))
                    return "bvid", m.group(1)
        return None

    async def _fetch_video_info(self, kind: str, vid: Any) -> Optional[dict[str, Any]]:
        """获取视频信息 + AI 总结（带缓存）。失败返回 None。"""
        cache_key = f"{kind}:{vid}"
        now = time.time()
        cached = self._video_cache.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]

        assert self._cred_mgr is not None
        video = Video(bvid=vid) if kind == "bvid" else Video(aid=vid)
        try:
            info = await video.get_info()
        except Exception as exc:
            self.ctx.logger.warning("获取视频信息失败 (%s=%s): %s", kind, vid, exc)
            return None

        stat = info.get("stat") or {}
        result: dict[str, Any] = {
            "bvid": info.get("bvid", vid if kind == "bvid" else ""),
            "title": info.get("title", ""),
            "up": (info.get("owner") or {}).get("name", "未知UP"),
            "duration": info.get("duration", 0),
            "view": stat.get("view", 0),
            "like": stat.get("like", 0),
            "desc": (info.get("desc") or "")[:200],
            "ai_summary": "",
        }

        if self.config.parse.enable_ai_summary:
            cred = await self._cred_mgr.get_credential()
            if cred is not None:
                video_with_cred = Video(
                    bvid=result["bvid"] if kind == "bvid" else None,
                    aid=int(info.get("aid", 0)) if kind == "aid" else None,
                    credential=cred,
                )
                try:
                    data = await video_with_cred.get_ai_conclusion(cid=info.get("cid"))
                    result["ai_summary"] = _extract_ai_summary(data)
                except Exception as exc:
                    self.ctx.logger.debug("获取 AI 总结失败 (%s): %s", result["bvid"], exc)

        if len(self._video_cache) > 100:
            self._video_cache.clear()
        self._video_cache[cache_key] = (now + self.config.parse.cache_ttl_seconds, result)
        return result

    def _build_injected_summary(self, info: dict[str, Any]) -> str:
        """构建附加到用户消息末尾的B站视频信息与AI总结块。"""
        title = info.get("title", "").strip()
        up = info.get("up", "未知UP").strip()
        duration = _format_duration(info.get("duration", 0))
        ai_summary = (info.get("ai_summary") or "").strip()

        lines = [f"[B站视频信息: 《{title}》 | UP: {up} | 时长: {duration}]"]
        if ai_summary:
            lines.append(f"[B站官方AI总结]: {ai_summary}")
        elif info.get("desc"):
            lines.append(f"[视频简介]: {info['desc'].strip()}")
        return "\n".join(lines)

    def _build_tool_content(self, info: dict[str, Any]) -> str:
        """构建返回给 planner 的 Tool 内容。"""
        lines = [
            f"标题: {info['title']}",
            f"UP主: {info['up']}",
            f"时长: {_format_duration(info['duration'])}",
            f"播放: {_format_count(info['view'])}  点赞: {_format_count(info['like'])}",
        ]
        if info.get("desc"):
            lines.append(f"简介: {info['desc']}")
        if info.get("ai_summary"):
            lines += ["", "B站官方AI总结:", info["ai_summary"]]
        else:
            lines += ["", "(该视频暂无AI总结，可从标题和简介理解内容)"]
        if info.get("bvid"):
            lines.append(f"链接: https://www.bilibili.com/video/{info['bvid']}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # Hook: 拦截消息并在聊天上下文中附加AI总结（不自动发消息回复）
    # ------------------------------------------------------------------ #

    @HookHandler(
        "chat.receive.after_process",
        name="bilibili_summary_injector",
        description="检测入站消息中的B站视频链接并将AI总结直接附加到消息内容中",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        timeout_ms=10000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def inject_bilibili_summary(self, **kwargs: Any) -> dict[str, Any]:
        message = kwargs.get("message")
        if not isinstance(message, dict):
            return {"action": "continue"}
        if message.get("is_notify"):
            return {"action": "continue"}

        cfg = self.config.parse
        message_info = message.get("message_info") or {}
        is_group = bool(message_info.get("group_info"))
        if is_group and not cfg.enable_in_group:
            return {"action": "continue"}
        if not is_group and not cfg.enable_in_private:
            return {"action": "continue"}

        text = str(message.get("processed_plain_text") or "")
        if not text:
            raw_parts = []
            for seg in message.get("raw_message") or []:
                if isinstance(seg, dict) and seg.get("type") == "text":
                    data = seg.get("data")
                    if isinstance(data, str) and data:
                        raw_parts.append(data)
            text = " ".join(raw_parts)

        if not text or "[B站视频" in text:
            return {"action": "continue"}

        target = await self._resolve_target(text)
        if target is None:
            return {"action": "continue"}

        try:
            info = await self._fetch_video_info(target[0], target[1])
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.debug("获取视频总结异常: %s", exc)
            return {"action": "continue"}

        if not info:
            return {"action": "continue"}

        summary_block = self._build_injected_summary(info)
        if not summary_block:
            return {"action": "continue"}

        self.ctx.logger.info(
            "为消息 %s 附加B站视频AI总结 (%s=%s)",
            message.get("message_id"),
            target[0],
            target[1],
        )

        current_plain = str(message.get("processed_plain_text") or "").strip()
        message["processed_plain_text"] = f"{current_plain}\n{summary_block}".strip()

        raw_msg = message.get("raw_message")
        if isinstance(raw_msg, list):
            raw_msg.append({"type": "text", "data": f"\n{summary_block}"})

        return {
            "action": "continue",
            "modified_kwargs": {
                "message": message,
            },
        }

    # ------------------------------------------------------------------ #
    # Tool: 供 planner 显式调用
    # ------------------------------------------------------------------ #

    @Tool(
        "parse_bilibili_video",
        description=(
            "解析B站视频并获取B站官方AI视频总结。当聊天中出现B站视频链接、BV号、"
            "b23.tv短链，或有人提到想看/讨论某个B站视频时，调用此工具获取视频标题、"
            "UP主、时长、播放数据和AI总结，帮助你理解视频内容并参与讨论。"
        ),
        parameters=[
            ToolParameterInfo(
                name="link",
                param_type=ToolParamType.STRING,
                description="B站视频链接、BV号（如 BV1xx411c7mD）、av号或 b23.tv 短链",
                required=True,
            ),
        ],
    )
    async def tool_parse_bilibili_video(self, link: str = "", **kwargs: Any) -> dict[str, str]:
        del kwargs
        link = (link or "").strip()
        if not link:
            return {"name": "parse_bilibili_video", "content": "参数 link 为空，无法解析。"}
        target = await self._resolve_target(link)
        if target is None:
            return {"name": "parse_bilibili_video", "content": f"无法从「{link}」中识别出B站视频。"}
        try:
            info = await self._fetch_video_info(target[0], target[1])
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.error("解析视频失败 (%s): %s", link, exc, exc_info=True)
            return {"name": "parse_bilibili_video", "content": f"解析B站视频失败: {exc}"}
        if info is None:
            return {"name": "parse_bilibili_video", "content": "获取视频信息失败，视频可能不存在或网络异常。"}
        return {"name": "parse_bilibili_video", "content": self._build_tool_content(info)}

    # ------------------------------------------------------------------ #
    # Command: 登录管理
    # ------------------------------------------------------------------ #

    @Command(
        "cu_login",
        description="B站扫码登录（AI总结功能需要登录态）",
        pattern=r"^/cu[_\s]?login\s*$",
        aliases=["/cu登录", "/cu 登录"],
    )
    async def cmd_login(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, int]:
        del kwargs
        if not stream_id or self._cred_mgr is None:
            return False, "缺少 stream_id 或插件未就绪", 0
        if self._poll_task is not None and not self._poll_task.done():
            return True, "已有登录流程进行中，请先完成或等待超时", 2
        try:
            qr_bytes = await self._cred_mgr.login_qrcode()
        except Exception as exc:  # noqa: BLE001
            return False, f"生成二维码失败: {exc}", 0
        await self.ctx.send.image(base64.b64encode(qr_bytes).decode("utf-8"), stream_id)
        await self.ctx.send.text("请用哔哩哔哩客户端扫码登录（3分钟内有效）", stream_id)
        self._poll_task = asyncio.create_task(self._poll_login_result(stream_id))
        return True, "登录二维码已发送", 2

    async def _poll_login_result(self, stream_id: str) -> None:
        try:
            message = await self._cred_mgr.poll_qrcode() if self._cred_mgr else "凭证管理器未初始化"
            await self.ctx.send.text(message, stream_id)
            if message.startswith("✅"):
                self._video_cache.clear()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.error("扫码登录轮询失败: %s", exc, exc_info=True)
            try:
                await self.ctx.send.text(f"登录流程异常: {exc}", stream_id)
            except Exception:
                pass
        finally:
            self._poll_task = None

    @Command(
        "cu_status",
        description="查看B站登录状态",
        pattern=r"^/cu[_\s]?status\s*$",
        aliases=["/cu状态", "/cu 状态"],
    )
    async def cmd_status(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, int]:
        del kwargs
        if self._cred_mgr is None:
            return False, "插件未就绪", 0
        message = await self._cred_mgr.get_status()
        if stream_id:
            await self.ctx.send.text(message, stream_id)
        return True, message, 2

    @Command(
        "cu_logout",
        description="登出B站账号并清除本地凭证",
        pattern=r"^/cu[_\s]?logout\s*$",
        aliases=["/cu登出", "/cu 登出"],
    )
    async def cmd_logout(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, int]:
        del kwargs
        if self._cred_mgr is None:
            return False, "插件未就绪", 0
        message = await self._cred_mgr.clear()
        self._video_cache.clear()
        if stream_id:
            await self.ctx.send.text(message, stream_id)
        return True, message, 2


def create_plugin() -> ContentUnderstandingPlugin:
    """Runner 通过此工厂函数实例化插件。"""
    return ContentUnderstandingPlugin()
