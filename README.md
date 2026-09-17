# B站内容理解插件 (content_understanding_plugin)

识别聊天中的 B 站视频内容（BV/av 号、bilibili.com 链接、b23.tv 短链），获取视频卡片与 **B 站官方 AI 总结**，辅助 bot 更好地参与聊天。

**插件不做任何多余的自动回复**——当群聊或私聊中有人分享 B 站视频、链接或小程序卡片时，插件会自动在入站消息末尾附加上 B 站视频信息与官方 AI 总结。这样当 bot（Maisaka / Planner / Replyer）查阅聊天上下文时，就能直接理解该视频的核心内容并参与讨论，避免在群里发送冗余卡片打扰聊天。同时保留 Tool 供模型按需调用。

基于 MaiBot Plugin SDK v2 开发（`maibot-plugin-sdk>=2.3.0`）。

## 功能

### 核心：辅助AI聊天

- **上下文自动增强（`chat.receive.after_process` Hook）**：自动识别消息中的 B 站链接、BV 号、av 号、`b23.tv` 短链及小程序分享卡片，调用 B 站官方接口获取 AI 视频总结，并无感注入到消息内容末尾（写入上下文历史），不发额外回复。
- **`parse_bilibili_video` Tool**：供模型（Planner）在需要时显式调用解析指定的 B 站视频。

### 登录管理

AI 总结接口需要 B 站登录态；未登录时仍可获取视频基本信息（标题、UP、播放量等）。

```
/cu_login    # 扫码登录（发送二维码图片，3分钟内有效）
/cu_status   # 查看登录状态
/cu_logout   # 登出并清除本地凭证
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
| `parse.enable_ai_summary` | `true` | 获取 B 站官方 AI 总结（需登录；部分视频本身无总结） |
| `parse.enable_in_group` | `true` | 在群聊中检测到B站链接/卡片时，自动附加AI总结到上下文 |
| `parse.enable_in_private` | `true` | 在私聊中检测到B站链接/卡片时，自动附加AI总结到上下文 |
| `parse.cache_ttl_seconds` | `1800` | 视频信息缓存时长（秒） |
| `credential.sessdata` 等 | 空 | 手动填写 Cookie 可代替扫码登录 |

扫码登录的凭证持久化在插件数据目录（`data/plugins/<plugin_id>/credential.json`），SDK 不提供写配置能力，因此**不会**写回 `config.toml`。

## AI 总结的实现方式

读取 **B 站官方服务端生成的 AI 视频总结**，而不是本地用 LLM 生成：

```python
video = Video(bvid="BV...", credential=cred)   # 需要登录态
data = await video.get_ai_conclusion(cid=info["cid"])
summary = data["model_result"]["summary"]
```

由 `bilibili-api-python` 的 `Video.get_ai_conclusion()` 封装官方接口，仅需视频 `cid` + 登录凭证。部分视频 B 站侧没有生成 AI 总结，此时返回空。

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

`config.toml`、`credential.json` 均为运行时生成文件。

## 参考

- [MaiBot 插件文档](https://docs.mai-mai.org/plugin/)
- [bilibili-api-python](https://github.com/Nemo2011/bilibili-api) — B 站 API 封装

## 许可证

MIT License
