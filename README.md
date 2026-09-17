# 视频内容理解插件 (content_understanding_plugin)

识别聊天中的 **B 站** 与 **抖音** 视频内容（BV/av 号、bilibili.com 链接、b23.tv 短链、抖音分享链接/口令、v.douyin.com 短链等），获取视频基础信息、**章节要点** 与 **官方 AI 总结**，辅助 bot 更好地参与聊天。

**插件不做任何多余的自动回复**——当群聊或私聊中有人分享 B 站或抖音视频链接时，插件会自动在入站消息末尾附加上该视频的信息、章节要点与官方 AI 总结。这样当 bot（Maisaka / Planner / Replyer）查阅聊天上下文时，就能直接理解该视频的核心内容并参与讨论，避免在群里发送冗余卡片打扰聊天。同时提供 Tool 供模型按需调用。

基于 MaiBot Plugin SDK v2 开发（`maibot-plugin-sdk>=2.3.0`）。

## 功能

### 核心：辅助AI聊天

- **上下文自动增强（`chat.receive.after_process` Hook）**：
  - 自动识别消息中的 B 站链接、BV 号、av 号、`b23.tv` 短链及小程序分享卡片，获取 B 站官方 AI 总结。
  - 自动识别消息中的抖音短链（`v.douyin.com/...`）、直链（`douyin.com/video/...`、`jingxuan?modal_id=...`）及手机端复制的完整分享口令文本，获取抖音视频信息、章节要点与官方 AI 总结。
  - 无感注入到消息内容末尾（写入上下文历史），不发额外回复。
- **Tool 供 Planner 显式调用**：
  - `parse_bilibili_video`：解析 B 站视频。
  - `parse_douyin_video`：解析抖音视频。

### 凭据与登录管理

- **B 站**：
  - AI 总结接口需要登录态；未登录时仍可获取视频基本信息（标题、UP、播放量等）。
  - 可通过命令扫码登录或在配置中填写 Cookie。
- **抖音**：
  - 支持直接在配置中填写 `credential.douyin_cookie`（或放置在数据目录 `douyin_cookies.txt`）。
  - **自动补全游客标识**：若 Cookie 缺少 `ttwid`，插件会自动请求字节跳动游客注册端点获取匿名 `ttwid`。

```
/cu_login    # B站扫码登录（发送二维码图片，3分钟内有效）
/cu_status   # 查看 B 站与抖音凭据配置状态
/cu_logout   # 登出 B 站账号并清除本地凭证
```

## 安装

```bash
cd MaiBot
uv pip install -r plugins/content_understanding_plugin/requirements.txt
```

依赖同时声明在 `_manifest.json` 中，Host 也会按声明检查。

## 配置

**无需手动创建 `config.toml`**——插件首次启动时 Runner 会根据 `config_model` 自动生成，并可在 WebUI 中可视化编辑；配置模型新增字段后 Runner 会自动补齐默认值。

主要配置项（含默认值）：

| 项 | 默认 | 说明 |
|---|---|---|
| `parse.enable_ai_summary` | `true` | 获取官方 AI 总结（B站/抖音；部分视频本身无总结） |
| `parse.enable_douyin` | `true` | 是否启用抖音视频解析与 AI 总结/章节要点 |
| `parse.enable_in_group` | `true` | 在群聊中检测到视频链接时，自动附加 AI 总结到上下文 |
| `parse.enable_in_private` | `true` | 在私聊中检测到视频链接时，自动附加 AI 总结到上下文 |
| `parse.cache_ttl_seconds` | `1800` | 视频信息缓存时长（秒） |
| `credential.sessdata` 等 | 空 | 手动填写 B 站 Cookie（也可使用 `/cu_login` 扫码登录） |
| `credential.douyin_cookie` | 空 | 抖音 Cookie（选填；用于获取抖音 AI 视频总结与章节要点） |
| `permission.admin_users` | `[]` | 管理员 QQ 白名单（如 `["123456789"]`），填入后仅列表内的 QQ 可执行登录/登出；留空表示不限制 |

## 官方 AI 总结与章节要点实现

### 1. B 站官方 AI 总结
读取 **B 站官方服务端生成的 AI 视频总结**：
```python
video = Video(bvid="BV...", credential=cred)   # 需要登录态
data = await video.get_ai_conclusion(cid=info["cid"])
summary = data["model_result"]["summary"]
```

### 2. 抖音官方 AI 总结与章节要点
1. **章节要点**：通过抖音精选页面 SSR 数据提取 `chapterInfo`，获取带时间戳的章节大纲（如 `00:27 规则介绍`）与要点说明。
2. **AI 视频总结**：通过抖音官方 AI 总结流式接口（`so-landing.douyin.com/douyin/select/v1/ai/stream/`）流式提取针对视频内容的完整结构化解析。

## 文件结构

```
content_understanding_plugin/
├── .gitignore         # Git 忽略配置（忽略 config.toml 等敏感文件）
├── _manifest.json     # 插件清单（manifest v2）
├── requirements.txt   # Python 依赖
├── plugin.py          # 插件实现
├── __init__.py
└── README.md
```

## 许可证

MIT License
