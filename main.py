"""Preserve plugin speech, bot mentions, and quoted messages in conversation."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, ComponentTypes, Plain, Reply
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.platform.message_type import MessageType
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .send_capture import SendCapture
from .sent_history import SentHistory
from .shared_context import apply_group_window, shared_group_limit

PLUGIN_NAME = "astrbot_plugin_preserve_atbot"
QQ_OFFICIAL_PLATFORMS = frozenset({"qq_official", "qq_official_webhook"})
DEFAULT_PLATFORMS = frozenset({"qq_official", "qq_official_webhook", "aiocqhttp"})

CQ_AT_RE = re.compile(r"\[CQ:at,([^\]]+)]", re.IGNORECASE)
CQ_PARAM_RE = re.compile(r"(?:^|,)\s*([A-Za-z_][\w-]*)=([^,]*)")
QQ_OFFICIAL_AT_RE = re.compile(r"<@!?([^>]+)>")

# ---- 引用消息发送人补全相关 ----
SUPPORTED_QUOTE_PLATFORMS = frozenset({"aiocqhttp"})
_REPLY_DATA_ID_KEYS = ("message_id", "id", "msg_id")


@dataclass(frozen=True, slots=True)
class BotMention:
    """一次已确认指向机器人的 @。"""

    target_id: str = ""
    name: str = ""
    source: str = ""


def _value(obj: Any, key: str, default: Any = None) -> Any:
    """同时读取对象属性和字典字段。"""
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _clean_identifier(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"", "none", "null"} else text


def _clean_name(value: Any) -> str:
    name = _clean_identifier(value).lstrip("@").strip()
    # 显示名称只能作为普通文本注入；去掉换行和控制字符，避免破坏提示词结构。
    name = re.sub(r"[\x00-\x1f\x7f]+", " ", name)
    return re.sub(r"\s+", " ", name).strip()[:64]


@register(
    PLUGIN_NAME,
    "TenmaGabriel0721",
    "对话拾遗：按对话顺序保留插件发言，补全 @Bot 与引用上下文",
    "1.3.1",
)
class PreserveAtBotPlugin(Star):
    """Restore missing conversation context from plugin sends, mentions, and replies."""

    def __init__(
        self,
        context: Context,
        config: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(context)
        self.config = dict(config or {})
        self.fallback_name = (
            _clean_name(self.config.get("bot_display_name", "Bot")) or "Bot"
        )
        self.marker_template = str(
            self.config.get("marker_template", "@{name}")
        ).strip()
        try:
            template_valid = "{name}" in self.marker_template and bool(
                self.marker_template.format(name="Bot").strip()
            )
        except (KeyError, ValueError, IndexError):
            template_valid = False
        if not template_valid:
            logger.warning(
                "[%s] marker_template 缺少 {name}，已恢复为默认值。",
                PLUGIN_NAME,
            )
            self.marker_template = "@{name}"

        configured_platforms = self.config.get("platforms", list(DEFAULT_PLATFORMS))
        if isinstance(configured_platforms, list):
            self.platforms = frozenset(
                str(item).strip() for item in configured_platforms if str(item).strip()
            )
        else:
            self.platforms = DEFAULT_PLATFORMS

        self.prefer_detected_name = bool(self.config.get("prefer_detected_name", True))
        self.sync_event_message = bool(self.config.get("sync_event_message", True))
        self.debug_log = bool(self.config.get("debug_log", False))
        self.enhance_quote_sender = bool(self.config.get("enhance_quote_sender", True))
        self.quote_sender_fallback_name = _clean_name(
            self.config.get("quote_sender_fallback_name", "Unknown User")
        )
        try:
            self.quote_sender_timeout = max(
                0.1,
                min(float(self.config.get("quote_sender_timeout", 5.0)), 30.0),
            )
        except (TypeError, ValueError):
            self.quote_sender_timeout = 5.0

        self.record_plugin_messages = bool(
            self.config.get("record_plugin_messages", True)
        )
        try:
            self.recent_plugin_message_count = max(
                0, min(int(self.config.get("recent_plugin_message_count", 10)), 100)
            )
        except (TypeError, ValueError):
            self.recent_plugin_message_count = 10
        self.sent_history: SentHistory | None = None
        self.send_capture: SendCapture | None = None

    async def initialize(self) -> None:
        """Initialize speech storage and reversible send capture when enabled."""
        if not self.record_plugin_messages:
            return
        path = (
            Path(get_astrbot_data_path())
            / "plugin_data"
            / PLUGIN_NAME
            / "sent_history.db"
        )
        self.sent_history = await asyncio.to_thread(
            SentHistory, path, self.recent_plugin_message_count
        )
        self.send_capture = SendCapture(self, self.sent_history)
        try:
            self.send_capture.install()
        except Exception:
            self.send_capture.close()
            self.sent_history.close()
            self.send_capture = None
            self.sent_history = None
            raise
        logger.info(
            "[%s] 已启用插件发言记录，每个 Bot 会话分支%s；启用群聊上下文感知时共享其消息数量上限。",
            PLUGIN_NAME,
            f"保留最近 {self.recent_plugin_message_count} 条"
            if self.recent_plugin_message_count
            else "不限制保留数量",
        )

    @filter.on_plugin_loaded()
    async def capture_loaded_plugin(self, metadata: Any) -> None:
        """Refresh handler ownership after a plugin is loaded or reloaded.

        Args:
            metadata: Metadata supplied by AstrBot's plugin lifecycle hook.
        """
        if self.send_capture:
            self.send_capture.install_handlers()
            self.send_capture.install_protocols()
            self.send_capture.install_group_context()

    @filter.on_platform_loaded()
    async def capture_loaded_platform(self) -> None:
        """Cover the current OneBot API objects after platform initialization."""
        if self.send_capture:
            self.send_capture.install_protocols()

    async def terminate(self) -> None:
        """Remove owned capture wrappers before closing speech storage."""
        if self.send_capture:
            self.send_capture.close()
            self.send_capture = None
        if self.sent_history:
            await asyncio.to_thread(self.sent_history.close)
            self.sent_history = None

    @filter.on_llm_request(priority=-10000)
    async def enhance_context(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        """补全引用与 @bot，并将近期插件发言嵌入普通对话消息。"""
        await self._complete_quote_senders(event, req)
        await self._preserve_at_bot(event, req)
        store = getattr(self, "sent_history", None)
        if store is not None and req.conversation is not None:
            try:
                current_id = str(getattr(event.message_obj, "message_id", "") or "")
                allowed_ids = None
                window = None
                group_limit = shared_group_limit(self.context, event.unified_msg_origin)
                if group_limit:
                    window = await asyncio.to_thread(
                        store.group_window,
                        event.unified_msg_origin,
                        req.conversation.cid,
                        group_limit,
                        event.get_extra("_group_context_record_id", ""),
                    )
                    allowed_ids = {
                        item["entry_id"] for item in window if item["kind"] == "sent"
                    }
                req.contexts = await asyncio.to_thread(
                    store.merge,
                    req.contexts,
                    event.unified_msg_origin,
                    req.conversation.cid,
                    current_id,
                    allowed_record_ids=allowed_ids,
                )
                if window is not None:
                    apply_group_window(req, window)
            except Exception:
                logger.exception(
                    "[%s] 无法合并近期插件发言，保留原始对话上下文。", PLUGIN_NAME
                )

    async def _preserve_at_bot(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        """在所有常规 prompt 装饰结束后，最终写入 @bot 文本。"""
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return

        platform = event.get_platform_name()
        if self.platforms and platform not in self.platforms:
            return

        mention = self._find_bot_mention(event, platform)
        if mention is None:
            return

        detected_name = _clean_name(mention.name)
        if self.prefer_detected_name and detected_name:
            display_name = detected_name
        else:
            display_name = self.fallback_name or detected_name or "Bot"

        marker_name = display_name
        if mention.target_id:
            marker_name = f"{display_name}(QQ: {mention.target_id})"
        marker = self.marker_template.format(name=marker_name).strip()
        if not marker:
            marker = f"@{marker_name}"

        old_prompt = (
            req.prompt
            if isinstance(req.prompt, str)
            else (event.get_message_str() or "")
        )
        new_prompt = self._prepend_marker(old_prompt, marker)
        req.prompt = new_prompt

        if self.sync_event_message:
            current_event_text = event.get_message_str() or ""
            event.message_str = self._prepend_marker(current_event_text, marker)
            if hasattr(event.message_obj, "message_str"):
                event.message_obj.message_str = event.message_str

        event.set_extra("_preserved_at_bot_marker", marker)
        event.set_extra("_preserved_at_bot_source", mention.source)

        if self.debug_log:
            logger.info(
                "[%s] 已注入 %s，platform=%s source=%s target=%s",
                PLUGIN_NAME,
                marker,
                platform,
                mention.source,
                mention.target_id,
            )

    async def _complete_quote_senders(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        """补全 Reply 元数据，并同步修正已构建的 LLM 引用文本。"""
        if not self.enhance_quote_sender:
            return

        platform = event.get_platform_name()
        if platform not in SUPPORTED_QUOTE_PLATFORMS:
            return

        replies = [comp for comp in event.get_messages() if isinstance(comp, Reply)]
        if not replies:
            return

        changed = False
        for reply in replies:
            sender_name = _clean_name(getattr(reply, "sender_nickname", ""))
            sender_id = _clean_identifier(getattr(reply, "sender_id", ""))
            if sender_id == "0":
                sender_id = ""

            payload = None
            if not sender_name or not sender_id:
                payload = await self._fetch_quoted_message(event, reply)
                if payload:
                    fetched_id, fetched_name = self._extract_sender(payload)
                    sender_id = sender_id or fetched_id
                    sender_name = sender_name or fetched_name

            if not sender_name:
                sender_name = sender_id or (
                    self.quote_sender_fallback_name if payload else ""
                )
            if not sender_name and not sender_id:
                continue

            reply.sender_nickname = sender_name
            if sender_id:
                reply.sender_id = sender_id
                reply.qq = int(sender_id) if sender_id.isdigit() else 0
            if payload:
                self._fill_reply_content(reply, payload)
            self._replace_quoted_sender(req, sender_name, sender_id)
            changed = True

            if self.debug_log:
                logger.info(
                    "[%s] 已补全引用发送人，platform=%s reply_id=%s sender=%s(%s)",
                    PLUGIN_NAME,
                    platform,
                    _clean_identifier(reply.id),
                    sender_name,
                    sender_id or "unknown",
                )

        if changed:
            event.set_extra("_preserved_quote_sender", True)

    async def _fetch_quoted_message(
        self,
        event: AstrMessageEvent,
        reply: Reply,
    ) -> Mapping[str, Any] | None:
        reply_id = _clean_identifier(getattr(reply, "id", ""))
        bot = getattr(event, "bot", None)
        call_action = getattr(bot, "call_action", None)
        if not reply_id or not callable(call_action):
            return None

        params: list[str | int] = [reply_id]
        if reply_id.isdigit():
            params.append(int(reply_id))
        last_error: Exception | None = None
        for message_id in params:
            try:
                result = await asyncio.wait_for(
                    call_action("get_msg", message_id=message_id),
                    timeout=self.quote_sender_timeout,
                )
                payload = self._unwrap_action_payload(result)
                if payload:
                    return payload
            except Exception as exc:  # noqa: BLE001
                last_error = exc

        if self.debug_log and last_error is not None:
            logger.warning(
                "[%s] 引用消息补全失败，reply_id=%s: %s",
                PLUGIN_NAME,
                reply_id,
                last_error,
            )
        return None

    @staticmethod
    def _unwrap_action_payload(result: Any) -> Mapping[str, Any] | None:
        if not isinstance(result, Mapping):
            return None
        data = result.get("data")
        return data if isinstance(data, Mapping) else result

    @staticmethod
    def _extract_sender(payload: Mapping[str, Any]) -> tuple[str, str]:
        sender = _value(payload, "sender", None)
        if not isinstance(sender, Mapping):
            sender = {}
        sender_id = _clean_identifier(
            _value(sender, "user_id", None)
            or _value(sender, "id", None)
            or _value(payload, "user_id", None)
        )
        sender_name = _clean_name(
            _value(sender, "card", None)
            or _value(sender, "nickname", None)
            or _value(sender, "nick", None)
            or _value(payload, "sender_name", None)
        )
        return sender_id, sender_name

    @staticmethod
    def _fill_reply_content(reply: Reply, payload: Mapping[str, Any]) -> None:
        segments = _value(payload, "message", None)
        if not getattr(reply, "chain", None) and isinstance(segments, list):
            chain = []
            for segment in segments:
                segment_type = str(_value(segment, "type", "")).lower()
                component_type = ComponentTypes.get(segment_type)
                data = _value(segment, "data", None)
                if component_type is None or not isinstance(data, Mapping):
                    continue
                try:
                    chain.append(component_type(**dict(data)))
                except Exception:  # noqa: BLE001
                    continue
            if chain:
                reply.chain = chain

        if not _clean_identifier(getattr(reply, "message_str", "")):
            plain_texts = [
                comp.text
                for comp in (getattr(reply, "chain", None) or [])
                if isinstance(comp, Plain) and comp.text
            ]
            text = "".join(plain_texts).strip()
            if not text:
                raw_message = _value(payload, "raw_message", "")
                if isinstance(raw_message, str):
                    text = raw_message.strip()
            if text:
                reply.message_str = text
                reply.text = text

        timestamp = _value(payload, "time", None)
        if timestamp is not None and not getattr(reply, "time", 0):
            try:
                reply.time = int(timestamp)
            except (TypeError, ValueError):
                pass

    @staticmethod
    def _replace_quoted_sender(
        req: ProviderRequest,
        sender_name: str,
        sender_id: str,
    ) -> None:
        """LLM 请求此时已构建完毕，原地修正引用发送人昵称与 QQ 号。"""
        if not req.extra_user_content_parts:
            return
        if sender_name and sender_id and sender_name != sender_id:
            sender_label = f"{sender_name}, QQ: {sender_id}"
        elif sender_name:
            sender_label = sender_name
        elif sender_id:
            sender_label = f"QQ: {sender_id}"
        else:
            return

        for part in req.extra_user_content_parts:
            text = getattr(part, "text", None)
            if not isinstance(text, str) or "<Quoted Message>" not in text:
                continue
            prefix, separator, suffix = text.partition("<Quoted Message>\n")
            if not separator:
                continue
            first_line, newline, remainder = suffix.partition("\n")
            if first_line.startswith("(") and "): " in first_line:
                _, _, first_line = first_line.partition("): ")
            part.text = (
                f"{prefix}{separator}({sender_label}): {first_line}{newline}{remainder}"
            )

    @staticmethod
    def _prepend_marker(text: str, marker: str) -> str:
        stripped = (text or "").strip()
        if not stripped:
            return marker
        # 允许其他插件已经完成了同样注入，避免重复生成“@Bot @Bot”。
        if stripped == marker or stripped.startswith(f"{marker} "):
            return stripped
        return f"{marker} {stripped}"

    def _find_bot_mention(
        self,
        event: AstrMessageEvent,
        platform: str,
    ) -> BotMention | None:
        self_ids = self._collect_self_ids(event)

        # 1. AstrBot 标准消息链。QQ 官方和 OneBot 都会保留 At 组件。
        for component in event.get_messages():
            if not isinstance(component, At):
                continue
            target = _clean_identifier(getattr(component, "qq", ""))
            if not target or target == "all":
                continue
            if target in self_ids:
                return BotMention(
                    target_id=target,
                    name=_clean_name(getattr(component, "name", "")),
                    source="message_chain",
                )
            if platform in QQ_OFFICIAL_PLATFORMS and target == "qq_official":
                return BotMention(
                    target_id=target,
                    name=_clean_name(getattr(component, "name", "")),
                    source="qq_official_sentinel",
                )

        raw = getattr(event.message_obj, "raw_message", None)

        # 2. QQ 官方 API/Webhook 的 mentions 与 <@id>/<@!id> 格式。
        mention = self._find_qq_official_raw_mention(raw, self_ids)
        if mention is not None:
            return mention

        # 3. OneBot JSON 消息段。
        mention = self._find_onebot_segments(raw, self_ids)
        if mention is not None:
            return mention

        # 4. CQ 码回退，兼容旧实现或其他 OneBot 适配器。
        return self._find_cq_mention(raw, self_ids)

    @staticmethod
    def _collect_self_ids(event: AstrMessageEvent) -> set[str]:
        raw = getattr(event.message_obj, "raw_message", None)
        candidates = {
            _clean_identifier(event.get_self_id()),
            _clean_identifier(getattr(event.message_obj, "self_id", "")),
            _clean_identifier(_value(raw, "self_id", "")),
        }
        envelope = _value(raw, "d", None)
        if envelope is not None:
            candidates.add(_clean_identifier(_value(envelope, "self_id", "")))
        candidates.discard("")
        return candidates

    @staticmethod
    def _raw_payloads(raw: Any) -> Iterable[Any]:
        """依次返回原始对象、Webhook d 包以及嵌套 data。"""
        if raw is None:
            return ()
        payloads: list[Any] = [raw]
        for key in ("d", "data"):
            value = _value(raw, key, None)
            if value is not None and value is not raw:
                payloads.append(value)
        return payloads

    def _find_qq_official_raw_mention(
        self,
        raw: Any,
        self_ids: set[str],
    ) -> BotMention | None:
        for payload in self._raw_payloads(raw):
            mentions = _value(payload, "mentions", None) or []
            confirmed_ids: set[str] = set()
            if isinstance(mentions, Iterable) and not isinstance(
                mentions, (str, bytes, Mapping)
            ):
                for mention in mentions:
                    mention_id = _clean_identifier(
                        _value(mention, "id", None)
                        or _value(mention, "user_id", None)
                        or _value(mention, "openid", None)
                    )
                    is_you = _value(mention, "is_you", False) is True
                    if is_you or (mention_id and mention_id in self_ids):
                        confirmed_ids.add(mention_id)
                        name = (
                            _value(mention, "username", None)
                            or _value(mention, "name", None)
                            or _value(mention, "nick", None)
                        )
                        return BotMention(
                            target_id=mention_id,
                            name=_clean_name(name),
                            source="qq_official_mentions",
                        )

            content = _value(payload, "content", "")
            if isinstance(content, str):
                for mention_id in QQ_OFFICIAL_AT_RE.findall(content):
                    mention_id = _clean_identifier(mention_id)
                    if mention_id in self_ids or mention_id in confirmed_ids:
                        return BotMention(
                            target_id=mention_id,
                            source="qq_official_content",
                        )
        return None

    def _find_onebot_segments(
        self,
        raw: Any,
        self_ids: set[str],
    ) -> BotMention | None:
        for payload in self._raw_payloads(raw):
            segments = _value(payload, "message", None)
            if not isinstance(segments, list):
                continue
            for segment in segments:
                if str(_value(segment, "type", "")).lower() != "at":
                    continue
                data = _value(segment, "data", None)
                if data is None:
                    data = segment
                target = _clean_identifier(
                    _value(data, "qq", None)
                    or _value(data, "user_id", None)
                    or _value(data, "id", None)
                    or _value(data, "target_id", None)
                    or _value(data, "openid", None)
                )
                if target and target != "all" and target in self_ids:
                    name = (
                        _value(data, "name", None)
                        or _value(data, "nickname", None)
                        or _value(data, "card", None)
                    )
                    return BotMention(
                        target_id=target,
                        name=_clean_name(name),
                        source="onebot_json",
                    )
        return None

    def _find_cq_mention(
        self,
        raw: Any,
        self_ids: set[str],
    ) -> BotMention | None:
        candidates: list[str] = []
        if isinstance(raw, str):
            candidates.append(raw)
        for payload in self._raw_payloads(raw):
            for key in ("raw_message", "message"):
                value = _value(payload, key, None)
                if isinstance(value, str):
                    candidates.append(value)

        for source in candidates:
            for raw_params in CQ_AT_RE.findall(source):
                params = {
                    key.lower(): value.strip()
                    for key, value in CQ_PARAM_RE.findall(raw_params)
                }
                target = _clean_identifier(
                    params.get("qq") or params.get("user_id") or params.get("id")
                )
                if target and target != "all" and target in self_ids:
                    return BotMention(target_id=target, source="cq_code")
        return None
