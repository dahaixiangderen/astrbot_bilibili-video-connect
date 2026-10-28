# astrbot_plugin_video_storyboard

让 AstrBot 里的角色**真的"看"到视频**，而不是只读标题。

把一段视频抽成 **9~16 帧拼进一张图**，再附带逐格时间轴，一次性交给多模态模型。
一张图约 **384 tokens** —— 比逐帧喂图便宜一个数量级。

> 抽帧、切点检测、音频条、时间轴全部来自 [dsh-video-storyboard](https://github.com/SAOS-3624/dsh-video-storyboard)，
> 本插件只做 **AstrBot 侧的桥**：拿视频 → 调 CLI → 把「图 + 文本」塞进 LLM 请求。

## 效果

用户发一个视频 / B 站链接，角色能**引用具体格子里的画面**来评论，例如指出
「第 4 格那个人站在光里、整个装置在绕着转」，而不是复述
「标题：…… 简介：……」。

（示例只描述行为，不附带任何具体人格或聊天记录。）

## 成本

| 项 | 实测 |
| --- | --- |
| 分镜图 | 1 张，约 384 tokens |
| 时间轴文本 | 几百 ~ 2000 tokens |
| **单次合计** | **约 ¥0.015**（deepseek-flash 价目） |
| 下载流量 | 不产生 LLM 费用 |

## 依赖

1. **AstrBot** ≥ 4.x
2. **[dsh-video-storyboard](https://github.com/SAOS-3624/dsh-video-storyboard)** 的 CLI
   （抽帧 / 切点 / 拼图 / 时间轴都在这里）
3. **Node.js** ≥ 20（跑上面的 CLI）
4. **ffmpeg + ffprobe**（CLI 内部调用）
5. 可选：**B 站 cookie**（只在处理 B 站链接时需要；下载走 `playurl` 接口）

## 安装

```bash
# 1) 拿 CLI
git clone https://github.com/SAOS-3624/dsh-video-storyboard.git
cd dsh-video-storyboard
npm test          # 可选，需要 ffmpeg 在 PATH 里

# 2) ffmpeg（Windows 可直接下 gyan.dev / BtbN 的 build 解压）
#    确保 ffmpeg.exe / ffprobe.exe 在一个目录里

# 3) 插件放进 AstrBot
git clone <this-repo> AstrBot/data/plugins/astrbot_plugin_video_storyboard

# 4) 配置
cp AstrBot/data/plugins/astrbot_plugin_video_storyboard/config.example.json \
   AstrBot/data/plugins/astrbot_plugin_video_storyboard/config.json
# 编辑 config.json 里的 cli_path / ffmpeg_dir / cookie_file

# 5) 重启 AstrBot
```

## 配置 `config.json`

| 字段 | 说明 | 默认 |
| --- | --- | --- |
| `cli_path` | `dsh-video-storyboard/bin/storyboard.mjs` 的绝对路径 | 必填 |
| `node_path` | node 可执行文件；留空则用 PATH | `""` |
| `ffmpeg_dir` | 放 `ffmpeg.exe` / `ffprobe.exe` 的目录 | `""` |
| `cookie_file` | B 站 cookie 文本文件（`SESSDATA=...; bili_jct=...`） | `""` |
| `cells` | 每张图的格子数，9 = 3×3，16 = 4×4 | `9` |
| `max_video_mb` | 超过就跳过 | `300` |
| `max_duration_s` | 超过就跳过（默认 30 分钟） | `1800` |
| `cli_timeout_s` | 单次 CLI 超时 | `240` |
| `enable_bilibili` | 是否处理 B 站链接 | `true` |
| `instruction` | 注入给模型的要求（可改成任意人格口吻） | 见源码 |

也可以用环境变量：`VIDEO_STORYBOARD_CLI` / `VIDEO_STORYBOARD_NODE` /
`VIDEO_STORYBOARD_FFMPEG_DIR` / `VIDEO_STORYBOARD_COOKIE`。

> **Windows 提示**：如果 AstrBot 是作为服务/桌面应用启动的，它的进程 PATH 里通常
> **没有** `node`（即使你在自己的终端里敲 `node -v` 是通的）。这时插件日志会报
> `node/CLI missing: node=None`——把 `node_path` 填成 node.exe 的绝对路径即可。

## 排查

| 现象 | 原因 / 处理 |
| --- | --- |
| `node/CLI missing: node=None` | `node` 不在 AstrBot 进程的 PATH，填 `node_path` |
| `[video_sb] CLI failed rc=...` | 多半是 ffmpeg 找不到；检查 `ffmpeg_dir` |
| `bilibili download failed: HTTP Error 412` | cookie 失效，更新 `SESSDATA` |
| `playurl empty` | 视频需要大会员 / 已下架 / 地区限制 |
| 只回复标题、没有画面 | 看日志有没有 `downloading`；没有说明链接没被扫描到（贴个例子提 issue） |
| 下载了但没注入 | 看 `storyboard injected` 那行；没有就是 CLI 失败 |

## 支持的消息形式

| 来源 | 处理方式 |
| --- | --- |
| QQ 视频消息 | `Video` 组件 → `convert_to_file_path()`，失败则调 OneBot `get_file` 拿本地缓存 |
| 纯文本 B 站链接 | 正则匹配 BV → `playurl` 下载 |
| `b23.tv` 短链 | 跟随跳转解析 BV |
| **QQ 分享卡片**（Json 组件） | 扫描组件 `data` 里的 URL（`message_str` 是空的，必须扫） |

## 为什么不用 yt-dlp 下 B 站

B 站 CDN 对 yt-dlp 的视频流一律返回 **HTTP 567**（防盗链），但 `platform=html5`
的 `playurl` 通道能直接拿到 mp4。所以 B 站链接走官方接口，不走 yt-dlp。

## 已知限制

- 超过 `max_duration_s` / `max_video_mb` 的视频会跳过
- B 站 cookie 过期后需要更新（`SESSDATA` 失效会下载失败）
- 分镜只覆盖**时间轴**，不保证覆盖每一个镜头（300 个镜头塞进 9 格是不可能的）；
  需要细看某一格时，可以让模型再要一张特写
- 视频里的对白靠 ASR 或字幕轨；没有字幕又没有 ASR 时只有画面
- 群里默认不触发（私聊为主）；要开群聊请自行调整触发条件

## 许可

本插件 MIT。底层 CLI 的许可见
[dsh-video-storyboard](https://github.com/SAOS-3624/dsh-video-storyboard)。
