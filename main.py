"""表情包感知 — StickerSense for AstrBot.

补上 AstrBot 的一个信息断层：NapCat 在 CQ 码里带了 `sub_type`（1=表情包，7=贴图）
与 `summary`，但 AstrBot 的 Image 组件只保留 file/url/path，这两个字段被丢弃。
结果模型只看到「一张图片」，于是把表情包当真实照片理解，追问拍摄地点、衣着、天气。

本插件从 `message_obj.raw_message` 里把原始 CQ 码捞回来，还原「这是表情包」的语义，
并以临时内容注入本轮请求。不接管上下文、不改 system_prompt、不写会话历史。

设计原则：
  1. 零额外 LLM 调用（默认）；可选开启视觉描述才调用一次
  2. 只读原始事件，不改其他插件的消息链
  3. 注入走 extra_user_content_parts + mark_as_temp，不落历史、不破坏前缀缓存
"""

import hashlib
import json
import os
import re
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.message import TextPart

PLUGIN_NAME = "astrbot_plugin_sticker_sense"
PLUGIN_VERSION = "0.1.0"
DATA_DIR = Path("/AstrBot/data/plugin_data") / PLUGIN_NAME
SEEN_CACHE = DATA_DIR / "sticker_seen.json"
INJECT_LOG = DATA_DIR / "inject.log"
INJECT_LOG_MAX = 512 * 1024

# NapCat / go-cqhttp 的 image.sub_type 语义
#   1 = 表情包（客户端会分类到表情包图片并缩放显示）
#   7 = 贴图 / 商城表情
# 其余（0/缺失）= 普通图片
SUBTYPE_MEANING = {
    1: "表情包",
    7: "贴图",
    2: "表情包",
    3: "表情包",
}

RAW_KEY = "sticker_sense.pending"


def _safe_get(obj, key, default=None):
    """兼容 dict 与对象两种形态。"""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def extract_image_meta(raw) -> list[dict]:
    """从 aiocqhttp 原始事件里提取每张图片的元信息。

    返回 [{"sub_type": int|None, "summary": str, "file": str, "url": str}, ...]
    """
    out: list[dict] = []
    if raw is None:
        return out

    # 形态一：完整的 aiocqhttp event dict，message 是段数组
    msg = _safe_get(raw, "message")
    if isinstance(msg, list):
        for seg in msg:
            if not isinstance(seg, dict):
                continue
            if seg.get("type") not in ("image", "mface"):
                continue
            data = seg.get("data") or {}
            out.append(
                {
                    "sub_type": data.get("sub_type", data.get("subType")),
                    "summary": _safe_get(data, "summary", "") or "",
                    "file": _safe_get(data, "file", "") or "",
                    "url": _safe_get(data, "url", "") or "",
                    "kind": seg.get("type"),
                }
            )
        if out:
            return out

    # 形态二：只有 raw_message 字符串（CQ 码）
    text = _safe_get(raw, "raw_message") or _safe_get(raw, "message_str") or ""
    if isinstance(text, str) and "CQ:image" in text:
        for chunk in re.findall(r"\[CQ:image,([^\]]+)\]", text):
            kv = {}
            for part in chunk.split(","):
                if "=" in part:
                    k, v = part.split("=", 1)
                    kv[k.strip()] = v.strip()
            out.append(
                {
                    "sub_type": _to_int(kv.get("sub_type")),
                    "summary": _unescape_cq(kv.get("summary", "")),
                    "file": kv.get("file", ""),
                    "url": _unescape_cq(kv.get("url", "")),
                    "kind": "image",
                }
            )
    return out


def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _unescape_cq(s: str) -> str:
    return (
        (s or "")
        .replace("&#91;", "[")
        .replace("&#93;", "]")
        .replace("&#44;", ",")
        .replace("&amp;", "&")
    )


def classify(meta: dict) -> tuple[bool, str]:
    """判定是否表情包，并给出人话标签。"""
    st = meta.get("sub_type")
    summary = (meta.get("summary") or "").strip()
    kind = meta.get("kind") or "image"

    if kind == "mface":
        return True, "商城表情"
    if st in SUBTYPE_MEANING:
        return True, SUBTYPE_MEANING[st]
    # 有些实现把表情包放 summary（形如 [xxx]），但普通图也可能有 summary，
    # 所以 summary 只作为弱信号，不单独判定。
    return False, "普通图片"


@register(PLUGIN_NAME, "xiaoli0412", "表情包感知：还原被丢弃的 sub_type，纠正模型把表情包当照片", PLUGIN_VERSION)
class StickerSense(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self._seen: OrderedDict[str, float] = OrderedDict()

    # ---------- config ----------
    def _cfg(self, key, default):
        v = self.config.get(key, default)
        return default if v is None else v

    async def initialize(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if SEEN_CACHE.exists():
            try:
                data = json.loads(SEEN_CACHE.read_text(encoding="utf-8"))
                self._seen = OrderedDict(data.get("seen", {}))
            except Exception:
                self._seen = OrderedDict()
        # PATCHED(local): 注册 WebUI 面板 API
        try:
            reg = getattr(self.context, "register_web_api", None)
            if reg:
                reg("astrbot_plugin_sticker_sense/overview", self.api_overview,
                    ["GET"], "表情包：缓存与开关")
                logger.info("[表情包感知] WebUI API 已注册 1 条")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[表情包感知] WebUI API 注册失败：%s", exc)

        logger.info(
            "[表情包感知] 已就绪 | 自动纠偏=%s | 视觉描述=%s | 已缓存=%d",
            "开" if self._cfg("hint_enabled", True) else "关",
            "开" if self._cfg("vision_enabled", False) else "关",
            len(self._seen),
        )

    # ---------- capture ----------
    @filter.event_message_type(filter.EventMessageType.ALL, priority=250)
    async def capture(self, event: AstrMessageEvent):
        """消息入口先记下原始图片元信息，避免后续插件改写消息链后丢失。"""
        if not self._cfg("enabled", True):
            return
        try:
            raw = getattr(event.message_obj, "raw_message", None)
            metas = extract_image_meta(raw)
            if not metas:
                return
            enriched = []
            for m in metas:
                is_sticker, label = classify(m)
                enriched.append({**m, "is_sticker": is_sticker, "label": label})
            event.set_extra(RAW_KEY, enriched)

            if self._cfg("log_detect", True):
                for e in enriched:
                    logger.debug(
                        "[表情包感知] 图片 sub_type=%s summary=%r -> %s",
                        e.get("sub_type"), e.get("summary"), e["label"],
                    )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[{PLUGIN_NAME}] 捕获失败: {exc}")

    # ---------- inject ----------
    @filter.on_llm_request(priority=250)
    async def inject_hint(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self._cfg("enabled", True):
            return
        metas = event.get_extra(RAW_KEY)
        if not metas:
            return

        stickers = [m for m in metas if m.get("is_sticker")]
        photos = [m for m in metas if not m.get("is_sticker")]

        # 只在「本轮确实有表情包」时注入，避免污染普通图片对话
        if not stickers:
            if self._cfg("log_detect", True) and photos:
                logger.debug("[表情包感知] 本轮 %d 张普通图片，不注入提示", len(photos))
            return

        lines = ["<sticker_sense>"]
        labels = "/".join(sorted({m["label"] for m in stickers}))
        lines.append(
            f"本条消息里含有 {len(stickers)} 张{labels}（QQ 表情/贴图），"
            "不是用户拍摄的真实照片。"
        )
        summaries = [m["summary"] for m in stickers if m.get("summary")]
        if summaries:
            lines.append("表情包附带的文字标签：" + "、".join(f"「{s}」" for s in summaries))

        lines.append(
            "处理要求：\n"
            "1. 按表情包的语义理解它的情绪/态度（如卖萌、吐槽、无语、得意），不要描述图片内容本身；\n"
            "2. 严禁追问拍摄地点、拍摄时间、穿着、气温、器材等只适用于真实照片的问题；\n"
            "3. 不要评价「图片清楚不清楚」或要求用户重拍；\n"
            "4. 可以自然地回应这个表情传递的情绪，也可以只回一句话——保持当前人格的说话方式。"
        )
        lines.append("</sticker_sense>")

        block = "\n".join(lines)

        # 可选：视觉模型描述（默认关闭，省内存与 token）
        if self._cfg("vision_enabled", False):
            desc = await self._describe(event, stickers)
            if desc:
                block += f"\n\n<sticker_vision>\n画面描述（供参考，不必复述）：{desc}\n</sticker_vision>"

        # 落盘取证：实际注入给模型的原文
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            if INJECT_LOG.exists() and INJECT_LOG.stat().st_size > INJECT_LOG_MAX:
                keep = INJECT_LOG.read_text(encoding="utf-8", errors="replace")[-INJECT_LOG_MAX // 2:]
                INJECT_LOG.write_text("=== rotated ===\n" + keep, encoding="utf-8")
            with INJECT_LOG.open("a", encoding="utf-8") as f:
                f.write(f"\n----- {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} -----\n{block}\n")
        except Exception:
            pass

        part = TextPart(text=block).mark_as_temp()
        parts = getattr(req, "extra_user_content_parts", None)
        if parts is None:
            parts = []
            req.extra_user_content_parts = parts
        parts.append(part)

        # 去重记录（同图 10 分钟内不重复描述）
        for m in stickers:
            key = self._fingerprint(m)
            if key:
                self._seen[key] = time.time()
        while len(self._seen) > 500:
            self._seen.popitem(last=False)

        if self._cfg("log_detect", True):
            logger.info(
                "[表情包感知] 已注入提示：%s ×%d（本轮另含普通图片 %d 张）",
                labels, len(stickers), len(photos),
            )

    # ---------- vision (optional) ----------
    async def _describe(self, event: AstrMessageEvent, stickers: list[dict]) -> str:
        """调用当前会话的多模态模型描述表情包。失败静默返回空串。"""
        try:
            provider = await self.context.get_using_provider_async(
                umo=event.unified_msg_origin
            )
            if provider is None:
                return ""
            urls = [m.get("url") for m in stickers if m.get("url")]
            if not urls:
                return ""
            # 同图去重：10 分钟内不重复描述
            fresh = []
            for u in urls:
                key = hashlib.sha256(u.encode()).hexdigest()[:16]
                if time.time() - self._seen.get(key, 0) < 600:
                    continue
                fresh.append(u)
            if not fresh:
                return ""

            prompt = (
                "这是一张聊天表情包。用一句话（不超过 30 字）说明它表达的情绪或态度，"
                "不要描述画面细节，不要猜拍摄信息。"
            )
            resp = await provider.text_chat(
                prompt=prompt, image_urls=fresh[:2], contexts=[]
            )
            text = (getattr(resp, "completion_text", "") or "").strip()
            return text[:120]
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[{PLUGIN_NAME}] 视觉描述失败: {exc}")
            return ""

    @staticmethod
    def _fingerprint(meta: dict) -> str:
        key = meta.get("file") or meta.get("url") or ""
        if not key:
            return ""
        return hashlib.sha256(key.encode("utf-8", "ignore")).hexdigest()[:16]

    # ---------- commands ----------
    @filter.command("表情包")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看表情包感知状态与最近识别结果。"""
        event.stop_event()
        metas = event.get_extra(RAW_KEY) or []
        lines = [f"🙂 表情包感知 v{PLUGIN_VERSION}"]
        lines.append(f"自动纠偏：{'开' if self._cfg('hint_enabled', True) else '关'}")
        lines.append(f"视觉描述：{'开' if self._cfg('vision_enabled', False) else '关'}")
        lines.append(f"已缓存指纹：{len(self._seen)} 条")
        if metas:
            lines.append("本条消息里的图片：")
            for m in metas:
                lines.append(
                    f"  · {m['label']}（sub_type={m.get('sub_type')}"
                    + (f"，标签「{m['summary']}」" if m.get("summary") else "")
                    + "）"
                )
        else:
            lines.append("本条消息内没有图片。发一张表情包再试，即可看到识别结果。")
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------ WebUI（PATCHED local）
    async def api_overview(self):
        """表情包面板：已缓存的指纹 + 开关状态。"""
        seen = getattr(self, "_seen", None) or {}
        items = []
        for fp, meta in list(seen.items())[-40:][::-1]:
            if isinstance(meta, dict):
                items.append({
                    "fp": str(fp)[:16],
                    "sub_type": meta.get("sub_type"),
                    "summary": str(meta.get("summary") or "")[:60],
                    "first_seen": meta.get("first_seen") or meta.get("ts") or "",
                    "count": meta.get("count") or 1,
                })
            else:
                items.append({"fp": str(fp)[:16], "summary": str(meta)[:60]})
        return {
            "ok": True,
            "data": {
                "hint_enabled": bool(self._cfg("hint_enabled", True)),
                "vision_enabled": bool(self._cfg("vision_enabled", False)),
                "cached": len(seen),
                "items": items,
            },
        }

    async def terminate(self):
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            SEEN_CACHE.write_text(
                json.dumps({"seen": dict(self._seen)}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass
