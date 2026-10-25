"""Capture plugin-owned sends with acknowledged delivery and original content."""

from __future__ import annotations

import asyncio
import base64
import functools
import inspect
import json
import mimetypes
import re
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from astrbot.api import logger
from astrbot.core.message.message_event_result import (
    MessageEventResult,
    ResultContentType,
)
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType
from astrbot.core.star.star_handler import EventType, star_handlers_registry

from .sent_history import fingerprint
from .shared_context import shared_group_limit

OWNER: ContextVar[dict | None] = ContextVar("preserve_atbot_send_owner", default=None)
ENVELOPE: ContextVar[dict | None] = ContextVar(
    "preserve_atbot_send_envelope", default=None
)
RESULT_ORIGIN = "_preserve_atbot_result_origin"
LLM_TYPES = {
    ResultContentType.LLM_RESULT,
    ResultContentType.STREAMING_RESULT,
    ResultContentType.STREAMING_FINISH,
}
MISSING = object()


def serialize_chain(message: Any) -> str | list[dict]:
    """Preserve original text and locally available images without guessing captions.

    Args:
        message: An AstrBot message chain before platform conversion.

    Returns:
        Normal conversation content, with persisted image data when available.
    """
    parts = []
    for component in getattr(message, "chain", []) or []:
        component_type = getattr(component, "type", "")
        kind = str(getattr(component_type, "value", component_type)).lower()
        if kind in {"plain", "text"}:
            parts.append({"type": "text", "text": component.text})
        elif kind == "image":
            reference = getattr(component, "url", None) or getattr(
                component, "file", None
            )
            if reference and isinstance(reference, str):
                if reference.startswith("base64://"):
                    reference = "data:image/png;base64," + reference[len("base64://") :]
                elif not reference.startswith(("http://", "https://", "data:")):
                    path = Path(reference.removeprefix("file:///"))
                    try:
                        mime = mimetypes.guess_type(path.name)[0] or "image/png"
                        reference = (
                            f"data:{mime};base64,"
                            + base64.b64encode(path.read_bytes()).decode()
                        )
                    except OSError:
                        reference = ""
                if reference:
                    parts.append({"type": "image_url", "image_url": {"url": reference}})
                    continue
            parts.append({"type": "text", "text": "[图片消息，原图不可用]"})
        elif kind == "at":
            parts.append(
                {
                    "type": "text",
                    "text": f"@{getattr(component, 'name', '') or getattr(component, 'qq', '')}",
                }
            )
        elif kind == "reply":
            continue
        elif kind in {"node", "nodes"}:
            nodes = getattr(component, "nodes", None) or [component]
            for node in nodes:
                content = getattr(node, "content", []) or []
                text = "".join(getattr(part, "text", "") for part in content)
                parts.append(
                    {
                        "type": "text",
                        "text": f"[转发消息：{getattr(node, 'name', '')}] {text}",
                    }
                )
        else:
            name = getattr(component, "name", None) or ""
            parts.append(
                {
                    "type": "text",
                    "text": f"[{kind or '附件'}{': ' + str(name) if name else ''}]",
                }
            )
    if not any(part["type"] == "image_url" for part in parts):
        return "".join(part.get("text", "") for part in parts)
    return parts


class SendCapture:
    """Install reversible capture wrappers around handlers and platform sends."""

    def __init__(self, plugin: Any, store: Any) -> None:
        self.plugin = plugin
        self.context = plugin.context
        self.store = store
        self.patches: list[tuple[Any, str, Any, Any]] = []
        self.handlers: list[tuple[Any, Any, Any]] = []
        self.closed = False

    def source(self, metadata: Any, handler_name: str = "") -> dict:
        """Construct sanitized, runtime-proven source information.

        Args:
            metadata: The registered plugin metadata.
            handler_name: The producing handler, when known.

        Returns:
            Plugin attribution without credentials, paths, or stack traces.
        """
        return {
            key: re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "")).strip()
            for key, value in {
                "name": metadata.name,
                "display_name": metadata.display_name or metadata.name,
                "version": metadata.version,
                "handler": handler_name,
            }.items()
        }

    def stack_source(self) -> dict | None:
        """Resolve direct/background plugin sends by registered module ownership.

        Returns:
            The inherited handler owner or an exact runtime module match.
        """
        owner = OWNER.get()
        if owner:
            return owner
        stars = [
            star
            for star in self.context.get_all_stars()
            if star.activated and star.module_path and star.name != self.plugin.name
        ]
        frame = inspect.currentframe()
        try:
            for _ in range(50):
                if frame is None:
                    break
                module = frame.f_globals.get("__name__", "")
                for star in stars:
                    root = star.module_path.rsplit(".", 1)[0]
                    if module == star.module_path or module.startswith(root + "."):
                        return {"source": self.source(star, frame.f_code.co_name)}
                frame = frame.f_back
        finally:
            del frame
        return None

    def install_handlers(self) -> None:
        """Tag yielded plugin results and propagate ownership into background tasks."""
        if self.closed:
            return
        for star in self.context.get_all_stars():
            if not star.activated or star.name == self.plugin.name:
                continue
            for metadata in star_handlers_registry.get_handlers_by_module_name(
                star.module_path
            ):
                if metadata.event_type != EventType.AdapterMessageEvent:
                    continue
                original = metadata.handler
                if getattr(original, "_preserve_atbot_wrapped", False):
                    continue
                if not (
                    inspect.isasyncgenfunction(original)
                    or inspect.iscoroutinefunction(original)
                ):
                    continue
                source = self.source(star, metadata.handler_name)
                wrapper = self.wrap_handler(original, source)
                metadata.handler = wrapper
                self.handlers.append((metadata, original, wrapper))

    def wrap_handler(self, original: Any, source: dict) -> Any:
        """Keep framework handler types intact while tagging their produced replies.

        Args:
            original: The already-bound handler after framework registration.
            source: Its registered plugin owner.

        Returns:
            A coroutine or async-generator wrapper matching the original handler.
        """

        def tag(event: Any, result: Any) -> None:
            if event.get_extra("_clean_group_context_session", False):
                self.store.clear(event.unified_msg_origin)
            if not isinstance(result, MessageEventResult):
                result = event.get_result()
            if (
                isinstance(result, MessageEventResult)
                and result.result_content_type not in LLM_TYPES
            ):
                event.set_extra(
                    RESULT_ORIGIN,
                    {
                        "source": source,
                        "content": serialize_chain(result),
                        "result": result,
                    },
                )

        if inspect.isasyncgenfunction(original):

            @functools.wraps(original)
            async def wrapper(event, *args, **kwargs):
                iterator = original(event, *args, **kwargs)
                try:
                    while True:
                        token = OWNER.set(
                            {
                                "source": source,
                                "event": event,
                                "task": asyncio.current_task(),
                            }
                        )
                        try:
                            value = await anext(iterator)
                        except StopAsyncIteration:
                            break
                        finally:
                            OWNER.reset(token)
                        tag(event, value)
                        yield value
                finally:
                    await iterator.aclose()
        else:

            @functools.wraps(original)
            async def wrapper(event, *args, **kwargs):
                token = OWNER.set(
                    {"source": source, "event": event, "task": asyncio.current_task()}
                )
                try:
                    value = await original(event, *args, **kwargs)
                finally:
                    OWNER.reset(token)
                tag(event, value)
                return value

        wrapper._preserve_atbot_wrapped = True
        return wrapper

    async def prepare(self, umo: str, message: Any, event: Any = None) -> dict | None:
        """Snapshot history before a plugin send without mutating core storage.

        Args:
            umo: Exact target bot and conversation origin.
            message: The message chain being sent.
            event: The triggering event when sending a direct reply.

        Returns:
            A send envelope or None for disabled scopes and normal LLM replies.
        """
        if self.closed or not umo:
            return None
        config = self.context.get_config(umo)
        selected = config.get("plugin_set", ["*"])
        if not isinstance(selected, list):
            return None
        if "*" not in selected and self.plugin.name not in selected:
            return None
        owner = self.stack_source()
        origin = event.get_extra(RESULT_ORIGIN) if event else None
        result = event.get_result() if event else None
        if (
            result is not None
            and getattr(result, "result_content_type", None) in LLM_TYPES
        ):
            return None
        if owner:
            source = owner["source"]
        elif origin and origin.get("result") is result:
            source = origin["source"]
        else:
            source = {
                "name": "",
                "display_name": "来源未知",
                "version": "",
                "handler": "",
            }
        # Same-task context sends are replies; inherited background tasks are proactive.
        if event is None and owner and owner.get("task") is asyncio.current_task():
            candidate = owner.get("event")
            if candidate and candidate.unified_msg_origin == umo:
                event = candidate
        conv_mgr = self.context.conversation_manager
        cid = await conv_mgr.get_curr_conversation_id(umo)
        history = []
        if cid:
            conversation = await conv_mgr.get_conversation(umo, cid)
            if conversation:
                value = conversation.history
                history = json.loads(value) if isinstance(value, str) else value
        content = serialize_chain(message)
        if origin and origin.get("result") is result:
            content = origin["content"]
        if not content:
            return None
        event_id = str(
            getattr(getattr(event, "message_obj", None), "message_id", "") or ""
        )
        return {
            "umo": umo,
            "cid": cid,
            "receipts": set(),
            "source": source,
            "content": content,
            "trigger_id": event_id,
            # WakingStage strips prefixes from the event, not the message object.
            "trigger_text": (
                getattr(event.message_obj, "message_str", "") or event.get_message_str()
            )
            if event
            else "",
            "anchors": [
                fingerprint(item)
                for item in (history or [])
                if item.get("role") != "_checkpoint"
            ][-3:],
        }

    def receipt(self, value: Any) -> None:
        """Collect successful API acknowledgements from the current send envelope.

        Args:
            value: An unmodified platform send response.
        """
        envelope = ENVELOPE.get()
        if envelope is None or "receipts" not in envelope or value is None:
            return
        if isinstance(value, dict):
            if value.get("status") == "failed" or value.get("retcode", 0) != 0:
                return
            data = value.get("data", value)
            if not isinstance(data, dict) or not data:
                return
            identifier = data.get("message_id") or data.get("id") or data.get("res_id")
        else:
            identifier = getattr(value, "id", None) or getattr(
                value, "message_id", None
            )
            if not identifier:
                return
        envelope["receipts"].add(str(identifier or "accepted_without_message_id"))

    async def finish(self, envelope: dict | None) -> None:
        """Save only acknowledged messages while preserving original send behavior.

        Args:
            envelope: The completed send operation and its collected acknowledgements.
        """
        if envelope is None or not envelope["receipts"] or self.closed:
            return
        payload = {
            key: value
            for key, value in envelope.items()
            if key not in {"umo", "cid", "receipts"}
        }
        payload["message_ids"] = sorted(envelope["receipts"])
        payload["sent_at"] = datetime.now(timezone.utc).isoformat()
        try:
            await asyncio.to_thread(
                self.store.append,
                envelope["umo"],
                envelope["cid"],
                payload,
                shared_group_limit(self.context, envelope["umo"]),
            )
        except Exception:
            logger.exception("[preserve_atbot] 保存已发送插件消息失败")

    def patch(self, target: Any, name: str, wrapper: Any) -> None:
        """Install a reversible wrapper without changing unrelated attributes.

        Args:
            target: The class or object owning the send entry point.
            name: The attribute to wrap.
            wrapper: The replacement, preserving its original descriptor type.
        """
        original = vars(target).get(name, MISSING)
        setattr(target, name, wrapper)
        self.patches.append(
            (target, name, original, inspect.getattr_static(target, name))
        )

    async def prepare_api(self, bot: Any, params: dict, group: bool) -> dict | None:
        """Capture direct SDK sends when their plugin and target can be verified.

        Args:
            bot: The OneBot client or official BotAPI belonging to a platform instance.
            params: The original API request parameters.
            group: Whether the target is a group rather than a private recipient.

        Returns:
            An envelope for a plugin-owned direct send, or None for framework traffic.
        """
        owner = self.stack_source()
        if not owner:
            return None
        target = (
            (params.get("group_id") or params.get("group_openid"))
            if group
            else (params.get("user_id") or params.get("openid"))
        )
        if target is None:
            return None
        platform_id = None
        for platform in self.context.platform_manager.platform_insts:
            client = getattr(platform, "bot", None)
            if client is bot or getattr(client, "api", None) is bot:
                platform_id = platform.meta().id
                break
        if not platform_id:
            return None
        umo = str(
            MessageSession(
                str(platform_id),
                MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE,
                str(target),
            )
        )
        event = (
            owner.get("event") if owner.get("task") is asyncio.current_task() else None
        )
        if (
            event
            and event.unified_msg_origin.split(":", 1)[0] == str(platform_id)
            and str(event.get_group_id() if group else event.get_sender_id())
            == str(target)
        ):
            umo = event.unified_msg_origin
        else:
            event = None
        segments = params.get("message")
        if isinstance(segments, str):
            from aiocqhttp.message import Message

            segments = list(Message(segments))
        if isinstance(segments, list):
            chain = [
                SimpleNamespace(type=item.get("type", ""), **item.get("data", {}))
                for item in segments
                if isinstance(item, dict)
            ]
        else:
            markdown = params.get("markdown") or {}
            text = (
                params.get("content")
                or (
                    markdown.get("content")
                    if isinstance(markdown, dict)
                    else getattr(markdown, "content", "")
                )
                or ""
            )
            chain = [SimpleNamespace(type="plain", text=text)] if text else []
            if params.get("media"):
                chain.append(
                    SimpleNamespace(type="plain", text="[媒体消息，平台媒体引用]")
                )
        return await self.prepare(umo, SimpleNamespace(chain=chain), event)

    def install_protocols(self) -> None:
        """Cover OneBot plugins calling the API object directly rather than CQHttp."""
        for platform in self.context.platform_manager.platform_insts:
            bot = getattr(platform, "bot", None)
            for api in (getattr(bot, "_api", None), getattr(bot, "api", None)):
                original = getattr(api, "call_action", None)
                if not inspect.iscoroutinefunction(original) or getattr(
                    original, "_preserve_atbot_api_capture", False
                ):
                    continue

                @functools.wraps(original)
                async def call_action(
                    action, *args, _original=original, _bot=bot, **params
                ):
                    envelope = None
                    token = None
                    if ENVELOPE.get() is None and action.startswith("send_"):
                        try:
                            envelope = await self.prepare_api(
                                _bot, params, "group" in action
                            )
                        except Exception:
                            logger.exception(
                                "[preserve_atbot] 直接 OneBot 发送捕获准备失败"
                            )
                        token = ENVELOPE.set(envelope or {"skip": True})
                    try:
                        value = await _original(action, *args, **params)
                        if action.startswith("send_"):
                            self.receipt(value)
                        return value
                    finally:
                        if token is not None:
                            ENVELOPE.reset(token)
                            await self.finish(envelope)

                call_action._preserve_atbot_api_capture = True
                self.patch(api, "call_action", call_action)

    def install_group_context(self) -> None:
        """Observe native group collection without altering core source files."""
        from astrbot.builtin_stars.astrbot.group_chat_context import GroupChatContext

        original = GroupChatContext.handle_message
        if getattr(original, "_preserve_atbot_group_budget", False):
            return

        @functools.wraps(original)
        async def handle_message(manager, event):
            await original(manager, event)
            if self.closed:
                return
            try:
                umo = event.unified_msg_origin
                limit = shared_group_limit(self.context, umo)
                entry_id = event.get_extra("_group_context_record_id", "")
                if not limit or not entry_id:
                    return
                async with manager._get_lock(umo):
                    ids = list(manager._record_ids.get(umo, ()))
                    texts = list(manager.raw_records.get(umo, ()))
                    if entry_id not in ids:
                        return
                    text = texts[ids.index(entry_id)]
                cid = await self.context.conversation_manager.get_curr_conversation_id(
                    umo
                )
                await asyncio.to_thread(
                    self.store.record_group, umo, cid, entry_id, text, limit
                )
            except Exception:
                logger.exception("[preserve_atbot] 记录群聊共同上下文失败")

        handle_message._preserve_atbot_group_budget = True
        self.patch(GroupChatContext, "handle_message", handle_message)

        # Adopt messages already waiting in native buffers during a hot reload.
        for star in self.context.get_all_stars():
            manager = getattr(
                getattr(star, "star_cls", None), "group_chat_context", None
            )
            if not isinstance(manager, GroupChatContext):
                continue
            for umo, texts in list(manager.raw_records.items()):
                limit = shared_group_limit(self.context, umo)
                if not limit:
                    continue
                ids = manager._record_ids.get(umo, ())
                for entry_id, text in zip(list(ids), list(texts), strict=False):
                    self.store.record_group(umo, None, entry_id, text, limit)

    def install(self) -> None:
        """Capture ordinary replies, proactive sends, and platform acknowledgements."""
        from aiocqhttp import CQHttp
        from botpy.api import BotAPI

        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
            AiocqhttpMessageEvent,
        )
        from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import (
            QQOfficialMessageEvent,
        )
        from astrbot.core.star.context import Context

        for event_class in (AiocqhttpMessageEvent, QQOfficialMessageEvent):
            original = event_class.send

            @functools.wraps(original)
            async def send(event, message, _original=original):
                try:
                    envelope = await self.prepare(
                        event.unified_msg_origin, message, event
                    )
                except Exception:
                    logger.exception("[preserve_atbot] 插件消息捕获准备失败")
                    envelope = None
                token = ENVELOPE.set(envelope or {"skip": True})
                try:
                    return await _original(event, message)
                except BaseException:
                    if envelope is not None:
                        envelope["partial_send"] = True
                    raise
                finally:
                    ENVELOPE.reset(token)
                    await self.finish(envelope)

            self.patch(event_class, "send", send)

        original_context_send = Context.send_message

        @functools.wraps(original_context_send)
        async def context_send(context, session, message_chain):
            try:
                umo = (
                    str(MessageSession.from_str(session))
                    if isinstance(session, str)
                    else str(session)
                )
                envelope = await self.prepare(umo, message_chain)
            except Exception:
                logger.exception("[preserve_atbot] 主动消息捕获准备失败")
                envelope = None
            token = ENVELOPE.set(envelope or {"skip": True})
            try:
                return await original_context_send(context, session, message_chain)
            except BaseException:
                if envelope is not None:
                    envelope["partial_send"] = True
                raise
            finally:
                ENVELOPE.reset(token)
                await self.finish(envelope)

        self.patch(Context, "send_message", context_send)

        original_action = CQHttp.call_action

        @functools.wraps(original_action)
        async def call_action(bot, action, **params):
            value = await original_action(bot, action, **params)
            if action.startswith("send_"):
                self.receipt(value)
            return value

        self.patch(CQHttp, "call_action", call_action)

        original_post = QQOfficialMessageEvent._post_send

        @functools.wraps(original_post)
        async def post_send(event, *args, **kwargs):
            value = await original_post(event, *args, **kwargs)
            self.receipt(value)
            return value

        self.patch(QQOfficialMessageEvent, "_post_send", post_send)

        original_c2c = QQOfficialMessageEvent.post_c2c_message

        @functools.wraps(original_c2c)
        async def post_c2c(event, *args, **kwargs):
            value = await original_c2c(event, *args, **kwargs)
            self.receipt(value)
            return value

        self.patch(QQOfficialMessageEvent, "post_c2c_message", post_c2c)

        for method_name, group in (
            ("post_group_message", True),
            ("post_c2c_message", False),
        ):
            original_sdk = getattr(BotAPI, method_name)

            @functools.wraps(original_sdk)
            async def post_sdk(
                api, *args, _original=original_sdk, _group=group, **kwargs
            ):
                envelope = None
                token = None
                if ENVELOPE.get() is None:
                    try:
                        params = dict(
                            inspect.signature(_original)
                            .bind_partial(api, *args, **kwargs)
                            .arguments
                        )
                        envelope = await self.prepare_api(api, params, _group)
                    except Exception:
                        logger.exception(
                            "[preserve_atbot] 直接官方 Bot 发送捕获准备失败"
                        )
                    token = ENVELOPE.set(envelope or {"skip": True})
                try:
                    value = await _original(api, *args, **kwargs)
                    self.receipt(value)
                    return value
                finally:
                    if token is not None:
                        ENVELOPE.reset(token)
                        await self.finish(envelope)

            self.patch(BotAPI, method_name, post_sdk)
        self.install_protocols()
        self.install_handlers()
        self.install_group_context()

    def close(self) -> None:
        """Restore only wrappers still owned by this plugin instance."""
        self.closed = True
        for metadata, original, wrapper in reversed(self.handlers):
            if metadata.handler is wrapper:
                metadata.handler = original
        for target, name, original, wrapper in reversed(self.patches):
            if inspect.getattr_static(target, name) is wrapper:
                if original is MISSING:
                    delattr(target, name)
                else:
                    setattr(target, name, original)
        self.handlers.clear()
        self.patches.clear()
