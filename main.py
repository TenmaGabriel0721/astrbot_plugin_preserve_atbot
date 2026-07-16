"""在 LLM 请求前把群聊中的 @bot 恢复成普通文本。

AstrBot 的平台适配器会从 ``message_str`` 中移除唤醒机器人的 At 消息段，
但结构化消息链和平台原始消息仍保留相关信息。本插件在
``on_llm_request`` 阶段识别这些信息，并把 ``@机器人名称`` 注入
``ProviderRequest.prompt``，使模型能够区分是否有人明确提到了机器人。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.platform.message_type import MessageType

PLUGIN_NAME = "astrbot_plugin_preserve_atbot"
QQ_OFFICIAL_PLATFORMS = frozenset({"qq_official", "qq_official_webhook"})
DEFAULT_PLATFORMS = frozenset({"qq_official", "qq_official_webhook", "aiocqhttp"})

CQ_AT_RE = re.compile(r"\[CQ:at,([^\]]+)]", re.IGNORECASE)
CQ_PARAM_RE = re.compile(r"(?:^|,)\s*([A-Za-z_][\w-]*)=([^,]*)")
QQ_OFFICIAL_AT_RE = re.compile(r"<@!?([^>]+)>")


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
    "Codex",
    "在 LLM 请求前保留群聊消息中的 @bot 文本标识",
    "1.0.0",
)
class PreserveAtBotPlugin(Star):
    """检测 QQ/OneBot 的 @bot 消息并向 LLM prompt 注入普通文本。"""

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

    @filter.on_llm_request(priority=-10000)
    async def preserve_at_bot(
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

        marker = self.marker_template.format(name=display_name).strip()
        if not marker:
            marker = f"@{display_name}"

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
