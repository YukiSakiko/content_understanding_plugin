"""视频内容理解插件（B站与抖音） - MaiBot SDK v2

识别聊天中的 B 站与抖音视频内容（BV/av 号、bilibili.com 链接、b23.tv 短链、抖音链接与口令短链），
获取视频信息、章节要点与官方 AI 总结，辅助 bot 更好地参与讨论：

- ``chat.receive.after_process`` Hook (BLOCKING):
  自动检测入站消息中的 B 站或抖音视频链接，将视频信息、章节要点与官方 AI 总结
  直接附加到该消息内容末尾（写入上下文历史），插件**不主动发送任何多余回复**，
  使 bot 在思考和聊天时直接拥有该视频的理解能力。
- ``parse_bilibili_video`` 与 ``parse_douyin_video`` Tool:
  供 planner 按需显式解析指定的 B 站或抖音视频。
- ``/cu_login`` ``/cu_status`` ``/cu_logout`` Command:
  管理 B 站扫码登录态与查看登录状态。
"""

from __future__ import annotations

import asyncio
import base64
import json
import random
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

_DOUYIN_SHORT_PATTERN = re.compile(
    r"(?:https?://)?(?:v|jx)\.douyin\.com/[0-9A-Za-z_\-]+",
    re.IGNORECASE,
)
_DOUYIN_WEB_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.|m\.|iesdouyin\.com/share/|jingxuan\.)?douyin\.com/(?:video|note|share/(?:video|note)|m/(?:video|note))/(\d+)",
    re.IGNORECASE,
)
_DOUYIN_MODAL_PATTERN = re.compile(r"[?&]modal_id=(\d+)", re.IGNORECASE)

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
_IOS_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"

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

    enable_ai_summary: bool = Field(default=True, description="获取官方AI视频总结（B站/抖音，部分视频无总结）")
    enable_in_group: bool = Field(default=True, description="在群聊消息中自动附加视频AI总结到上下文")
    enable_in_private: bool = Field(default=True, description="在私聊消息中自动附加视频AI总结到上下文")
    cache_ttl_seconds: int = Field(default=1800, ge=60, le=86400, description="视频信息缓存时长（秒）")
    enable_douyin: bool = Field(default=True, description="是否启用抖音视频解析与AI总结/章节要点")


class CredentialSectionConfig(PluginConfigBase):
    """登录凭证（B站与抖音）"""

    __ui_label__ = "登录凭证"
    __ui_icon__ = "key"
    __ui_order__ = 2

    sessdata: str = Field(default="", description="B站 Cookie - SESSDATA（手动填写可代替扫码登录）")
    bili_jct: str = Field(default="", description="B站 Cookie - bili_jct")
    buvid3: str = Field(default="", description="B站 Cookie - buvid3")
    dedeuserid: str = Field(default="", description="B站 Cookie - DedeUserID")
    douyin_cookie: str = Field(
        default="",
        description="抖音 Cookie（选填：用于获取抖音AI总结与章节要点）",
    )


class PermissionSectionConfig(PluginConfigBase):
    """命令权限配置"""

    __ui_label__ = "权限控制"
    __ui_icon__ = "shield"
    __ui_order__ = 3

    admin_users: list[str] = Field(
        default_factory=list,
        description='管理员 QQ 白名单列表（如 ["123456789"]）。填入后仅列表内的 QQ 可执行扫码登录/登出；留空表示不限制',
    )


class ContentUnderstandingPluginConfig(PluginConfigBase):
    """插件完整配置"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    parse: ParseSectionConfig = Field(default_factory=ParseSectionConfig)
    credential: CredentialSectionConfig = Field(default_factory=CredentialSectionConfig)
    permission: PermissionSectionConfig = Field(default_factory=PermissionSectionConfig)


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


def _clean_html_tags(text: str) -> str:
    """清理返回文本中的 HTML 标签（如 <mark> 等）"""
    return re.sub(r"<[^>]+>", "", text).strip()


def _parse_cookie_text(text: str) -> dict[str, str]:
    """解析 Cookie 文本，自动兼容 Netscape 制表符格式与分号键值对格式。"""
    res: dict[str, str] = {}
    text = (text or "").strip()
    if not text:
        return res
    if "# Netscape" in text or "\t" in text:
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                res[parts[5].strip()] = parts[6].strip()
            elif len(parts) == 2:
                res[parts[0].strip()] = parts[1].strip()
    else:
        for item in text.split(";"):
            if "=" in item:
                k, v = item.strip().split("=", 1)
                res[k.strip()] = v.strip()
    return res


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


class DouyinCookieManager:
    """抖音 Cookie 与凭据管理：支持配置与本地凭据文件。

    并在缺少 ttwid 时自动请求字节跳动游客注册接口获取匿名 ttwid。
    """

    TTWID_REGISTER_URL = "https://ttwid.bytedance.com/ttwid/union/register/"
    TTWID_REGISTER_BODY = {
        "region": "cn",
        "aid": 1768,
        "needFid": False,
        "service": "www.ixigua.com",
        "migrate_info": {"ticket": "", "source": "node"},
        "cbUrlProtocol": "https",
        "union": True,
    }

    def __init__(self, data_dir: Path, cookie_config: CredentialSectionConfig, client: httpx.AsyncClient):
        self._data_dir = data_dir
        self._cookie_config = cookie_config
        self._client = client
        self._cookie_file = data_dir / "douyin_cookies.txt"
        self._cookie_dict: dict[str, str] = {}
        self._cached_cookie_str = ""
        self.reload()

    def reload(self) -> None:
        """重新加载并更新抖音 Cookie。"""
        merged: dict[str, str] = {}

        # 1. 本地持久化文件加载
        if self._cookie_file.exists():
            try:
                merged.update(_parse_cookie_text(self._cookie_file.read_text(encoding="utf-8")))
            except Exception:
                pass

        # 2. 插件配置覆盖
        if self._cookie_config.douyin_cookie.strip():
            merged.update(_parse_cookie_text(self._cookie_config.douyin_cookie))

        self._cookie_dict = merged
        if self._cookie_dict:
            self._cached_cookie_str = "; ".join(f"{k}={v}" for k, v in self._cookie_dict.items())
            try:
                self._cookie_file.parent.mkdir(parents=True, exist_ok=True)
                self._cookie_file.write_text(self._cached_cookie_str, encoding="utf-8")
            except Exception:
                pass
        else:
            self._cached_cookie_str = ""

    def get_cookie_str(self) -> str:
        """获取当前抖音 Cookie 字符串。"""
        return self._cached_cookie_str

    async def ensure_ttwid(self) -> None:
        """确保持有 ttwid，缺失时向字节注册接口获取一次。"""
        if self._cookie_dict.get("ttwid"):
            return
        try:
            resp = await self._client.post(
                self.TTWID_REGISTER_URL,
                json=self.TTWID_REGISTER_BODY,
                headers={"User-Agent": _IOS_UA, "Content-Type": "application/json"},
            )
            ttwid = resp.cookies.get("ttwid")
            if not ttwid:
                for c_header in resp.headers.get_list("set-cookie"):
                    if "ttwid=" in c_header:
                        m = re.search(r"ttwid=([^;]+)", c_header)
                        if m:
                            ttwid = m.group(1)
                            break
            if ttwid:
                self._cookie_dict["ttwid"] = ttwid
                self._cached_cookie_str = "; ".join(f"{k}={v}" for k, v in self._cookie_dict.items())
                try:
                    self._cookie_file.write_text(self._cached_cookie_str, encoding="utf-8")
                except Exception:
                    pass
        except Exception:
            pass


# ============ 插件主类 ============


class ContentUnderstandingPlugin(MaiBotPlugin):
    """视频内容理解插件（B站与抖音）"""

    config_model = ContentUnderstandingPluginConfig

    def __init__(self) -> None:
        super().__init__()
        self._client: Optional[httpx.AsyncClient] = None
        self._cred_mgr: Optional[CredentialManager] = None
        self._douyin_cred_mgr: Optional[DouyinCookieManager] = None
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
        self._douyin_cred_mgr = DouyinCookieManager(data_dir, self.config.credential, self._client)
        self.ctx.logger.info(
            "content_understanding_plugin 已加载 (ai_summary=%s, douyin=%s, in_group=%s, in_private=%s)",
            self.config.parse.enable_ai_summary,
            self.config.parse.enable_douyin,
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
        if self._douyin_cred_mgr is not None:
            self._douyin_cred_mgr.reload()
        self._video_cache.clear()

    # ------------------------------------------------------------------ #
    # 链接解析与信息获取
    # ------------------------------------------------------------------ #

    async def _resolve_target(self, text: str) -> Optional[tuple[str, Any]]:
        """从文本解析视频目标。

        Returns:
            ("bvid", str) | ("aid", int) | ("douyin", str)；无法识别时返回 None。
        """
        # 1. B站视频识别
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

        # 2. 抖音视频识别
        # modal_id 参数
        m = _DOUYIN_MODAL_PATTERN.search(text)
        if m:
            return "douyin", m.group(1)

        # 直链 (video/xxx, note/xxx, share/xxx)
        m = _DOUYIN_WEB_PATTERN.search(text)
        if m:
            return "douyin", m.group(1)

        # 短链 (v.douyin.com, jx.douyin.com)
        for short in _DOUYIN_SHORT_PATTERN.findall(text):
            if not short.startswith("http"):
                short = f"https://{short}"
            try:
                assert self._client is not None
                resp = await self._client.get(short, headers={"User-Agent": _IOS_UA})
                final_url = str(resp.url)
            except Exception:
                continue
            for pat in (_DOUYIN_MODAL_PATTERN, _DOUYIN_WEB_PATTERN):
                m = pat.search(final_url)
                if m:
                    return "douyin", m.group(1)

        return None

    # ------------------------------------------------------------------ #
    # B站视频处理
    # ------------------------------------------------------------------ #

    async def _fetch_video_info(self, kind: str, vid: Any) -> Optional[dict[str, Any]]:
        """获取 B 站视频信息 + AI 总结（带缓存）。失败返回 None。"""
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
            self.ctx.logger.warning("获取B站视频信息失败 (%s=%s): %s", kind, vid, exc)
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
                    self.ctx.logger.debug("获取 B 站 AI 总结失败 (%s): %s", result["bvid"], exc)

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
        """构建返回给 planner 的 B 站 Tool 内容。"""
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
    # 抖音视频处理 (章节要点 + 官方AI总结)
    # ------------------------------------------------------------------ #

    async def _fetch_douyin_basic_info(self, vid: str, cookie_str: str) -> Optional[dict[str, Any]]:
        """获取抖音视频基础信息（标题、作者、时长）。"""
        assert self._client is not None
        urls = (
            f"https://www.iesdouyin.com/share/video/{vid}",
            f"https://m.douyin.com/share/video/{vid}",
        )
        headers = {
            "User-Agent": _IOS_UA,
            "Referer": "https://www.douyin.com/",
        }
        if cookie_str:
            headers["Cookie"] = cookie_str

        for url in urls:
            try:
                resp = await self._client.get(url, headers=headers, follow_redirects=True)
                if resp.status_code != 200:
                    continue
                m = re.search(r"window\._ROUTER_DATA\s*=\s*(.*?)</script>", resp.text, re.DOTALL)
                if not m:
                    continue
                data = json.loads(m.group(1).strip())
                loader = data.get("loaderData", {})
                page = loader.get("video_(id)/page") or loader.get("note_(id)/page") or {}
                item_list = page.get("videoInfoRes", {}).get("item_list", [])
                if not item_list:
                    continue
                item = item_list[0]
                dur = (item.get("video") or {}).get("duration", 0)
                duration_sec = (dur // 1000) if dur > 1000 else dur
                return {
                    "title": item.get("desc", ""),
                    "author": (item.get("author") or {}).get("nickname", "未知创作者"),
                    "duration": duration_sec,
                    "desc": item.get("desc", ""),
                }
            except Exception as exc:
                self.ctx.logger.debug("解析抖音基本信息失败 (%s): %s", url, exc)
                continue
        return None

    async def _fetch_douyin_chapters(self, vid: str, cookie_str: str) -> Optional[dict[str, Any]]:
        """获取抖音视频章节要点（图1路径：从网页 SSR chapterInfo 提取）。"""
        assert self._client is not None
        url = f"https://www.douyin.com/jingxuan?modal_id={vid}"
        headers = {
            "User-Agent": _UA,
            "Cookie": cookie_str,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        try:
            resp = await self._client.get(url, headers=headers)
            if resp.status_code != 200:
                return None
            text = resp.text
            pos = text.find('"chapterInfo"')
            if pos == -1:
                return None
            start = text.find("{", pos)
            depth, in_str, escape, end = 0, False, False, -1
            for i in range(start, min(len(text), start + 30000)):
                c = text[i]
                if escape:
                    escape = False
                    continue
                if c == "\\":
                    escape = True
                    continue
                if c == '"':
                    in_str = not in_str
                    continue
                if not in_str:
                    if c == "{":
                        depth += 1
                    elif c == "}":
                        depth -= 1
                        if depth == 0:
                            end = i + 1
                            break
            if end == -1:
                return None
            raw = text[start:end]
            cleaned = raw.replace('\\\\\\"', '"').replace('\\\\"', '"').replace('\\"', '"')
            cdata = json.loads(cleaned)
            ch_list = []
            for ch in cdata.get("list", []):
                ms = ch.get("timestamp", 0)
                ch_list.append({
                    "time": _format_duration(ms // 1000),
                    "desc": ch.get("desc", ""),
                    "detail": ch.get("detail", ""),
                })
            return {
                "chapterAbstract": cdata.get("chapterAbstract", ""),
                "list": ch_list,
            }
        except Exception as exc:
            self.ctx.logger.debug("提取抖音章节要点失败 (%s): %s", vid, exc)
            return None

    async def _fetch_douyin_ai_summary(self, vid: str, cookie_str: str) -> str:
        """获取抖音官方AI视频总结（图2/3路径：从 AI 搜索流式接口提取）。"""
        assert self._client is not None
        if not cookie_str:
            return ""

        stream_url = "https://so-landing.douyin.com/douyin/select/v1/ai/stream/"
        device_id = str(random.randint(7000000000000000000, 7999999999999999999))
        params = {
            "count": "5",
            "cursor": "0",
            "token": "search",
            "ai_page_type": "ai_chat",
            "search_channel": "aweme_ai_chat",
            "enable_ai_tab_new_framework": "1",
            "need_integration_card": "1",
            "ai_chat_message_use_lynx": "1",
            "version_code": "32.1.0",
            "enter_method": "click_sug",
            "enter_from": "general_search",
            "search_type": "ai_chat_search",
            "aid": "6383",
            "device_id": device_id,
            "keyword": "视频总结",
            "ai_search_enter_from_group_id": vid,
            "aweme_id": vid,
        }
        headers = {
            "User-Agent": _UA,
            "Cookie": cookie_str,
            "Referer": "https://so-landing.douyin.com/search_ai_mobile/pc",
            "Origin": "https://so-landing.douyin.com",
            "Accept": "text/event-stream",
        }
        try:
            async with self._client.stream("GET", stream_url, params=params, headers=headers, timeout=25.0) as resp:
                if resp.status_code != 200:
                    return ""
                tokens: list[str] = []
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    try:
                        d = json.loads(line[5:].strip())
                        for item in d.get("data", []):
                            display = item.get("display", {})
                            disp_inner = display.get("display", {}) if isinstance(display, dict) else {}
                            for span in disp_inner.get("generation_spans", []):
                                text_obj = span.get("text", {})
                                if isinstance(text_obj, dict) and "content" in text_obj:
                                    tokens.append(text_obj["content"])
                    except Exception:
                        pass
                return _clean_html_tags("".join(tokens))
        except Exception as exc:
            self.ctx.logger.debug("获取抖音AI总结流异常 (%s): %s", vid, exc)
            return ""

    async def _fetch_douyin_video_info(self, vid: str) -> Optional[dict[str, Any]]:
        """获取抖音视频信息、章节要点与AI总结（带缓存）。"""
        cache_key = f"douyin:{vid}"
        now = time.time()
        cached = self._video_cache.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]

        assert self._douyin_cred_mgr is not None
        await self._douyin_cred_mgr.ensure_ttwid()
        cookie_str = self._douyin_cred_mgr.get_cookie_str()

        # 1. 基础信息
        basic_info = await self._fetch_douyin_basic_info(vid, cookie_str)
        if not basic_info:
            return None

        result: dict[str, Any] = {
            "vid": vid,
            "title": basic_info.get("title", ""),
            "author": basic_info.get("author", "未知创作者"),
            "duration": basic_info.get("duration", 0),
            "desc": basic_info.get("desc", "")[:200],
            "chapters": [],
            "chapter_abstract": "",
            "ai_summary": "",
        }

        # 2. 章节要点与 AI 总结
        if self.config.parse.enable_ai_summary:
            chapters_task = asyncio.create_task(self._fetch_douyin_chapters(vid, cookie_str))
            ai_summary_task = asyncio.create_task(self._fetch_douyin_ai_summary(vid, cookie_str))

            try:
                ch_data, summary = await asyncio.gather(chapters_task, ai_summary_task, return_exceptions=True)
                if isinstance(ch_data, dict):
                    result["chapters"] = ch_data.get("list", [])
                    result["chapter_abstract"] = ch_data.get("chapterAbstract", "")
                if isinstance(summary, str) and summary:
                    result["ai_summary"] = summary
            except Exception as exc:
                self.ctx.logger.debug("获取抖音AI总结/章节异常 (%s): %s", vid, exc)

        if len(self._video_cache) > 100:
            self._video_cache.clear()
        self._video_cache[cache_key] = (now + self.config.parse.cache_ttl_seconds, result)
        return result

    def _build_injected_douyin_summary(self, info: dict[str, Any]) -> str:
        """构建附加到用户消息末尾的抖音视频信息、章节要点与AI总结块。"""
        title = (info.get("title") or "").strip()
        author = (info.get("author") or "未知创作者").strip()
        duration = _format_duration(info.get("duration", 0))
        chapters = info.get("chapters") or []
        chapter_abstract = (info.get("chapter_abstract") or "").strip()
        ai_summary = (info.get("ai_summary") or "").strip()

        lines = [f"[抖音视频信息: 《{title}》 | 作者: @{author} | 时长: {duration}]"]
        if chapters:
            lines.append("[抖音章节要点]:")
            if chapter_abstract:
                lines.append(chapter_abstract)
            for ch in chapters:
                time_str = ch.get("time", "")
                desc = ch.get("desc", "").strip()
                detail = ch.get("detail", "").strip()
                if detail:
                    lines.append(f"- {time_str} {desc}: {detail}")
                else:
                    lines.append(f"- {time_str} {desc}")

        if ai_summary:
            lines.append(f"[抖音官方AI总结]:\n{ai_summary}")
        elif not chapters and info.get("desc"):
            lines.append(f"[视频简介]: {info['desc'].strip()}")

        return "\n".join(lines)

    def _build_douyin_tool_content(self, info: dict[str, Any]) -> str:
        """构建返回给 planner 的抖音 Tool 内容。"""
        lines = [
            f"标题: {info.get('title', '')}",
            f"作者: @{info.get('author', '未知创作者')}",
            f"时长: {_format_duration(info.get('duration', 0))}",
        ]
        if info.get("desc") and info["desc"] != info.get("title"):
            lines.append(f"简介: {info['desc']}")
        chapters = info.get("chapters") or []
        chapter_abstract = (info.get("chapter_abstract") or "").strip()
        if chapters:
            lines += ["", "抖音章节要点:"]
            if chapter_abstract:
                lines.append(chapter_abstract)
            for ch in chapters:
                t = ch.get("time", "")
                d = ch.get("desc", "").strip()
                det = ch.get("detail", "").strip()
                lines.append(f"- {t} {d}: {det}" if det else f"- {t} {d}")
        if info.get("ai_summary"):
            lines += ["", "抖音官方AI总结:", info["ai_summary"]]
        elif not chapters:
            lines += ["", "(该视频暂无AI总结或章节，可从标题和简介理解内容)"]
        if info.get("vid"):
            lines.append(f"链接: https://www.douyin.com/video/{info['vid']}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # Hook: 拦截消息并在聊天上下文中附加AI总结（不自动发消息回复）
    # ------------------------------------------------------------------ #

    @HookHandler(
        "chat.receive.after_process",
        name="bilibili_summary_injector",
        description="检测入站消息中的B站/抖音视频链接并将AI总结直接附加到消息内容中",
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

        if not text or "[B站视频" in text or "[抖音视频" in text:
            return {"action": "continue"}

        target = await self._resolve_target(text)
        if target is None:
            return {"action": "continue"}

        summary_block = ""
        platform_kind, arg1 = target[0], target[1]
        if platform_kind in ("bvid", "aid"):
            try:
                info = await self._fetch_video_info(platform_kind, arg1)
                if info:
                    summary_block = self._build_injected_summary(info)
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.debug("获取B站视频总结异常: %s", exc)
                return {"action": "continue"}
        elif platform_kind == "douyin":
            if not cfg.enable_douyin:
                return {"action": "continue"}
            try:
                info = await self._fetch_douyin_video_info(str(arg1))
                if info:
                    summary_block = self._build_injected_douyin_summary(info)
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.debug("获取抖音视频总结异常: %s", exc)
                return {"action": "continue"}

        if not summary_block:
            return {"action": "continue"}

        self.ctx.logger.info(
            "为消息 %s 附加%s视频AI总结 (%s=%s)",
            message.get("message_id"),
            "B站" if platform_kind != "douyin" else "抖音",
            platform_kind,
            arg1,
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
        if target is None or target[0] not in ("bvid", "aid"):
            return {"name": "parse_bilibili_video", "content": f"无法从「{link}」中识别出B站视频。"}
        try:
            info = await self._fetch_video_info(target[0], target[1])
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.error("解析视频失败 (%s): %s", link, exc, exc_info=True)
            return {"name": "parse_bilibili_video", "content": f"解析B站视频失败: {exc}"}
        if info is None:
            return {"name": "parse_bilibili_video", "content": "获取视频信息失败，视频可能不存在或网络异常。"}
        return {"name": "parse_bilibili_video", "content": self._build_tool_content(info)}

    @Tool(
        "parse_douyin_video",
        description=(
            "解析抖音视频并获取抖音官方AI视频总结与章节要点。当聊天中出现抖音视频链接、"
            "v.douyin.com 短链、分享口令文本，或有人提到想了解某个抖音视频内容时，调用此工具获取视频标题、"
            "作者、时长、章节要点及AI总结，帮助你理解视频内容并参与讨论。"
        ),
        parameters=[
            ToolParameterInfo(
                name="link",
                param_type=ToolParamType.STRING,
                description="抖音视频分享链接、短链（如 https://v.douyin.com/xxxx/）、分享文本口令或19位视频ID",
                required=True,
            ),
        ],
    )
    async def tool_parse_douyin_video(self, link: str = "", **kwargs: Any) -> dict[str, str]:
        del kwargs
        link = (link or "").strip()
        if not link:
            return {"name": "parse_douyin_video", "content": "参数 link 为空，无法解析。"}
        target = await self._resolve_target(link)
        if target is None or target[0] != "douyin":
            # 兼容直接传入19位数字ID
            m = re.search(r"\b(7\d{18})\b", link)
            if m:
                target = ("douyin", m.group(1))
            else:
                return {"name": "parse_douyin_video", "content": f"无法从「{link}」中识别出抖音视频。"}
        try:
            info = await self._fetch_douyin_video_info(str(target[1]))
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.error("解析抖音视频失败 (%s): %s", link, exc, exc_info=True)
            return {"name": "parse_douyin_video", "content": f"解析抖音视频失败: {exc}"}
        if info is None:
            return {"name": "parse_douyin_video", "content": "获取抖音视频信息失败，视频可能不存在或网络异常。"}
        return {"name": "parse_douyin_video", "content": self._build_douyin_tool_content(info)}

    # ------------------------------------------------------------------ #
    # Command: 登录管理与权限控制
    # ------------------------------------------------------------------ #

    def _is_admin(self, user_id: str) -> bool:
        """检查用户是否在管理员白名单内。未配置任何白名单时允许所有人操作。"""
        permission_cfg = getattr(self.config, "permission", None)
        raw_admins = getattr(permission_cfg, "admin_users", []) if permission_cfg else []
        if not raw_admins:
            return True  # 列表留空表示不启用白名单限制，所有人可登录

        clean_user = str(user_id or "").strip()
        if clean_user.lower().startswith("qq:"):
            clean_user = clean_user[3:].strip()
        if not clean_user:
            return False

        for admin in raw_admins:
            clean_admin = str(admin or "").strip()
            if clean_admin.lower().startswith("qq:"):
                clean_admin = clean_admin[3:].strip()
            if clean_user == clean_admin:
                return True
        return False

    @staticmethod
    def _resolve_sender_id(user_id: str = "", kwargs: Optional[dict[str, Any]] = None) -> str:
        """从命令调用上下文中提取发送者的 QQ/用户 ID。"""
        if user_id:
            return str(user_id).strip()
        kwargs = kwargs or {}
        if kwargs.get("user_id"):
            return str(kwargs["user_id"]).strip()
        message = kwargs.get("message")
        if isinstance(message, dict):
            message_info = message.get("message_info") or {}
            user_info = message_info.get("user_info") or {}
            return str(user_info.get("user_id") or "").strip()
        return ""

    @Command(
        "cu_login",
        description="B站扫码登录（AI总结功能需要登录态）",
        pattern=r"^/cu[_\s]?login\s*$",
        aliases=["/cu登录", "/cu 登录"],
    )
    async def cmd_login(self, stream_id: str = "", user_id: str = "", **kwargs: Any) -> tuple[bool, str, int]:
        sender_id = self._resolve_sender_id(user_id, kwargs)
        if not self._is_admin(sender_id):
            msg = f"❌ 权限不足：用户 {sender_id or '未知'} 不在管理员白名单中，无法执行登录操作。"
            if stream_id:
                await self.ctx.send.text(msg, stream_id)
            return False, msg, 2

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
        description="查看B站与抖音凭据配置状态",
        pattern=r"^/cu[_\s]?status\s*$",
        aliases=["/cu状态", "/cu 状态"],
    )
    async def cmd_status(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, int]:
        del kwargs
        if self._cred_mgr is None:
            return False, "插件未就绪", 0
        bili_msg = await self._cred_mgr.get_status()
        douyin_status = "未配置 Cookie"
        if self._douyin_cred_mgr and self._douyin_cred_mgr.get_cookie_str():
            douyin_status = "已配置 Cookie（AI总结与章节要点已可用）"
        message = f"【B站状态】{bili_msg}\n【抖音状态】{douyin_status}"
        if stream_id:
            await self.ctx.send.text(message, stream_id)
        return True, message, 2

    @Command(
        "cu_logout",
        description="登出B站账号并清除本地凭证",
        pattern=r"^/cu[_\s]?logout\s*$",
        aliases=["/cu登出", "/cu 登出"],
    )
    async def cmd_logout(self, stream_id: str = "", user_id: str = "", **kwargs: Any) -> tuple[bool, str, int]:
        sender_id = self._resolve_sender_id(user_id, kwargs)
        if not self._is_admin(sender_id):
            msg = f"❌ 权限不足：用户 {sender_id or '未知'} 不在管理员白名单中，无法执行登出操作。"
            if stream_id:
                await self.ctx.send.text(msg, stream_id)
            return False, msg, 2

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
