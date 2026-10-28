"""视频分镜（Video Storyboard）——让 AstrBot 里的角色真的"看"到视频。

灵感与实现来自 dsh-video-storyboard：把一段视频抽成 9~16 帧拼进**一张图**，
再附带逐格时间轴。模型只花一张图的 token（约 384），而不是逐帧喂图。

支持三种来源：
1. QQ 视频消息（Video 组件）→ 本地文件
2. B 站链接 / b23 短链 / QQ 分享卡片 → 调 B 站 playurl 接口下载
3. 任意本地视频文件

拿到文件后统一调 dsh-video-storyboard 的 CLI 出图，再把「图 + 时间轴」塞进
LLM 请求。本插件不重写抽帧算法，只做 AstrBot 侧的桥。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Video
from astrbot.api.star import Context, Star

HERE = Path(__file__).parent
CONFIG_FILE = HERE / "config.json"

DEFAULTS = {
    # dsh-video-storyboard 的 CLI 入口（必填，或设环境变量 VIDEO_STORYBOARD_CLI）
    "cli_path": "",
    # 可选：node.exe / ffmpeg 所在目录 / B 站 cookie 文件（Netscape 或 k=v; k=v 均可）
    "node_path": "",
    "ffmpeg_dir": "",
    "cookie_file": "",
    # 每张分镜图的格子数：9 = 3x3，16 = 4x4
    "cells": 9,
    # 安全阀
    "max_video_mb": 300,
    "max_duration_s": 1800,
    "cli_timeout_s": 240,
    "enable_bilibili": True,
    # 注入的提示词（让她用自己的口吻评论）
    "instruction": (
        "用你自己的口吻评论这个视频（画面里看到的、节奏、你觉得有意思或无聊的点），"
        "一到三句，可以带情绪；不要逐格复述时间轴，别说“这是一张分镜图”。"
    ),
}

BILI_RE = re.compile(r"https?://(?:www\.|m\.)?bilibili\.com/video/(BV[0-9A-Za-z]{10})")
B23_RE = re.compile(r"(?:https?://)?b23\.tv/([A-Za-z0-9]+)")
STORY_RE = re.compile(r"<storyboard[\s\S]*?</storyboard>", re.I)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def _config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        if CONFIG_FILE.is_file():
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig")))
    except Exception as e:
        logger.warning(f"[video_sb] bad config.json: {e}")
    if not cfg.get("cli_path"):
        cfg["cli_path"] = os.environ.get("VIDEO_STORYBOARD_CLI", "")
    if not cfg.get("node_path"):
        cfg["node_path"] = os.environ.get("VIDEO_STORYBOARD_NODE", "")
    if not cfg.get("ffmpeg_dir"):
        cfg["ffmpeg_dir"] = os.environ.get("VIDEO_STORYBOARD_FFMPEG_DIR", "")
    if not cfg.get("cookie_file"):
        cfg["cookie_file"] = os.environ.get("VIDEO_STORYBOARD_COOKIE", "")
    return cfg


def _node(cfg: dict) -> str | None:
    p = cfg.get("node_path")
    if p and Path(p).exists():
        return str(p)
    w = shutil.which("node")
    if w:
        return w
    for cand in (
        Path(os.environ.get("ProgramFiles", "")) / "nodejs" / "node.exe",
        Path.home() / "AppData/Local/Programs/nodejs/node.exe",
    ):
        if cand.exists():
            return str(cand)
    return None


def _ffmpeg_env(cfg: dict) -> dict:
    env = dict(os.environ)
    d = cfg.get("ffmpeg_dir") or ""
    if d:
        env["PATH"] = str(d) + os.pathsep + env.get("PATH", "")
        for k, name in (("DSH_FFMPEG_PATH", "ffmpeg.exe"), ("DSH_FFPROBE_PATH", "ffprobe.exe")):
            p = Path(d) / name
            if p.exists():
                env[k] = str(p)
    return env


def _cookie(cfg: dict) -> str:
    try:
        p = cfg.get("cookie_file") or ""
        if p and Path(p).is_file():
            return Path(p).read_text(encoding="utf-8-sig").strip()
    except Exception:
        pass
    return ""


def _link_blob(event: AstrMessageEvent) -> str:
    """把纯文本、Json 卡片、其它组件的文本拼成大字符串，用于找链接。

    QQ 转发 B 站视频时经常是 structmsg 卡片，URL 藏在 Json 组件的 data 里，
    message_str 是空的——只扫 message_str 会完全漏掉。
    """
    chunks: list[str] = [getattr(event, "message_str", "") or ""]
    try:
        for comp in event.get_messages():
            data = getattr(comp, "data", None)
            if isinstance(data, dict):
                try:
                    chunks.append(json.dumps(data, ensure_ascii=False))
                except Exception:
                    chunks.append(str(data))
            elif isinstance(data, str):
                chunks.append(data)
            text = getattr(comp, "text", None)
            if isinstance(text, str):
                chunks.append(text)
            url = getattr(comp, "url", None)
            if isinstance(url, str):
                chunks.append(url)
    except Exception as e:
        logger.warning(f"[video_sb] scan components failed: {e}")
    return "\n".join(chunks)


def _bili_get(url: str, cookie: str) -> dict:
    import urllib.request

    headers = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


def _download_bilibili(url: str, outdir: Path, cfg: dict) -> str | None:
    """走 B 站自己的 playurl 接口下载。

    为什么不 yt-dlp：B 站 CDN 对 yt-dlp 的视频流一律返回 HTTP 567（防盗链），
    而 playurl 的 html5 通道能直接拿到 mp4。
    """
    import urllib.request

    cookie = _cookie(cfg)
    try:
        if "b23.tv" in url:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=15) as resp:
                url = resp.geturl()
        m = BILI_RE.search(url) or re.search(r"(BV[0-9A-Za-z]{10})", url)
        if not m:
            logger.warning(f"[video_sb] no BV in {url}")
            return None
        bvid = m.group(1) if m.re.groups else m.group(0)

        view = _bili_get(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}", cookie)
        if view.get("code") != 0:
            logger.warning(f"[video_sb] view api: {view.get('code')} {view.get('message')}")
            return None
        data = view.get("data") or {}
        cid = data.get("cid")
        duration = data.get("duration") or 0
        if duration > int(cfg["max_duration_s"]):
            logger.warning(f"[video_sb] too long: {duration}s > {cfg['max_duration_s']}s")
            return None

        pu = _bili_get(
            "https://api.bilibili.com/x/player/playurl"
            f"?bvid={bvid}&cid={cid}&qn=64&platform=html5&high_quality=1",
            cookie,
        )
        durl = (pu.get("data") or {}).get("durl") or []
        if not durl:
            logger.warning(f"[video_sb] playurl empty: {pu.get('code')} {pu.get('message')}")
            return None

        out = outdir / f"{bvid}.mp4"
        with open(out, "wb") as fh:
            for seg in durl:
                seg_url = seg.get("url")
                if not seg_url:
                    continue
                headers = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}
                if cookie:
                    headers["Cookie"] = cookie
                req = urllib.request.Request(seg_url, headers=headers)
                with urllib.request.urlopen(req, timeout=120) as resp:
                    while True:
                        chunk = resp.read(1024 * 256)
                        if not chunk:
                            break
                        fh.write(chunk)
        size_mb = out.stat().st_size / 1048576
        logger.info(f"[video_sb] downloaded {bvid} {size_mb:.1f}MB ({duration}s)")
        return str(out) if size_mb > 0.05 else None
    except Exception as e:
        logger.warning(f"[video_sb] bilibili download failed: {e}")
        return None


class VideoStoryboard(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self.cfg = _config()

    async def _localize(self, event: AstrMessageEvent, comp: Video) -> str | None:
        try:
            path = await comp.convert_to_file_path()
            if path and Path(path).is_file() and Path(path).stat().st_size > 0:
                return path
        except Exception as e:
            logger.warning(f"[video_sb] convert_to_file_path failed: {e}")
        # 退路：问协议端要文件（NapCat 的 get_file 会返回本地缓存路径）
        try:
            bot = getattr(event, "bot", None)
            ref = getattr(comp, "file", None) or getattr(comp, "url", None)
            if bot is not None and ref:
                ret = await bot.call_action("get_file", file=ref)
                data = ret.get("data", ret) if isinstance(ret, dict) else {}
                local = data.get("file") if isinstance(data, dict) else None
                if local and Path(str(local)).is_file():
                    return str(local)
        except Exception as e:
            logger.warning(f"[video_sb] get_file fallback failed: {e}")
        return None

    async def _storyboard(self, video: str, outdir: Path) -> tuple[Path | None, str]:
        node = _node(self.cfg)
        cli = Path(self.cfg["cli_path"] or "")
        if not node or not cli.is_file():
            logger.warning(f"[video_sb] node/CLI missing: node={node} cli={cli}")
            return None, ""
        sheet = outdir / "sheet.png"
        proc = await asyncio.create_subprocess_exec(
            node, str(cli), video,
            "--cells", str(int(self.cfg["cells"])),
            "--text",
            "--out", str(sheet),
            "--manifest", str(outdir / "manifest.json"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_ffmpeg_env(self.cfg),
        )
        try:
            out_b, err_b = await asyncio.wait_for(
                proc.communicate(), timeout=float(self.cfg["cli_timeout_s"])
            )
        except asyncio.TimeoutError:
            proc.kill()
            logger.warning("[video_sb] CLI timeout")
            return None, ""
        out = out_b.decode("utf-8", "ignore")
        err = err_b.decode("utf-8", "ignore")
        if proc.returncode != 0 or not sheet.is_file():
            logger.warning(f"[video_sb] CLI failed rc={proc.returncode}: {err[-300:]}")
            return None, ""
        m = STORY_RE.search(out)
        return sheet, (m.group(0) if m else out[-1500:])

    @filter.on_llm_request(priority=80)
    async def inject(self, event: AstrMessageEvent, req) -> None:
        outdir = Path(tempfile.mkdtemp(prefix="sb_"))
        video_path: str | None = None

        videos = [c for c in event.get_messages() if isinstance(c, Video)]
        if videos:
            video_path = await self._localize(event, videos[0])
            if not video_path:
                logger.warning("[video_sb] video component not localized")
                return
        else:
            if not self.cfg.get("enable_bilibili", True):
                return
            blob = _link_blob(event)
            m = BILI_RE.search(blob) or B23_RE.search(blob)
            if not m:
                return
            url = m.group(0)
            if not url.startswith("http"):
                url = "https://" + url
            logger.info(f"[video_sb] downloading {url}")
            video_path = await asyncio.to_thread(_download_bilibili, url, outdir, self.cfg)
            if not video_path:
                event.message_str = (
                    (getattr(event, "message_str", "") or "") + "\n（这个视频没下载下来，只能看标题）"
                )
                return

        size_mb = Path(video_path).stat().st_size / 1048576
        if size_mb > float(self.cfg["max_video_mb"]):
            logger.warning(f"[video_sb] skip big video {size_mb:.0f}MB")
            return

        sheet, text = await self._storyboard(video_path, outdir)
        if not sheet:
            event.message_str = (
                (getattr(event, "message_str", "") or "") + "\n（视频分析失败，只能当普通消息处理）"
            )
            return

        try:
            req.image_urls.append(str(sheet))
        except Exception as e:
            logger.warning(f"[video_sb] append image failed: {e}")

        note = (
            "\n\n[系统信息：这里有一个视频，下面这张图是它的分镜拼图（每格编号 + 时间轴），"
            "不是主人说的话]\n"
            f"{text}\n"
            f"要求：{self.cfg['instruction']}"
        )
        try:
            req.extra_user_content_parts.append({"type": "text", "text": note})
        except Exception:
            req.system_prompt = (req.system_prompt or "") + note
        logger.info(f"[video_sb] storyboard injected: {sheet} ({sheet.stat().st_size // 1024} KB)")
