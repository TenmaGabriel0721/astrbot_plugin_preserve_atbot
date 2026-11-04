"""Integrate on-demand images with AstrBot sessions and chronological records."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import time
from dataclasses import replace
from functools import wraps

from astrbot.api import logger
from astrbot.api.message_components import ComponentTypes, Reply
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType

from .image_cache import EVENT_KEY, ImageCacheService, image_entry, message_note, qq_raw
from .image_extraction import field
from .qq_send_observer import QQSendObserver

SCOPE_KEY = "preserve_atbot.image_scope"
QUOTE_KEY = "preserve_atbot.image_quote"
TASK_KEY = "preserve_atbot.image_task"
PLATFORMS = {"aiocqhttp", "qq_official", "qq_official_webhook"}


def scope_key(umo, cid):
    """Use the same bot/chat/branch boundaries as plugin speech records."""
    return json.dumps(
        [str(umo), str(cid) if cid is not None else None], ensure_ascii=False
    )


def identifiers(event):
    raw = qq_raw(event)
    mid = str(
        field(raw, "id")
        or field(raw, "message_id")
        or event.message_obj.message_id
        or ""
    )
    indices = {}
    for entry in field(field(raw, "message_scene"), "ext", []) or []:
        if isinstance(entry, str):
            key, _, value = entry.partition("=")
            if key in {"msg_idx", "ref_msg_idx"}:
                indices[key] = value
    return mid, indices.get("msg_idx", ""), indices.get("ref_msg_idx", "")


class HistoryImages(ImageCacheService):
    """Keep metadata passive; cache and inspect media within the current branch."""

    def __init__(self, plugin, settings, root):
        self.plugin = plugin
        self.observer = None
        self.group_patch = None
        self.capture_tasks = set()
        configured = settings.get("platforms", list(PLATFORMS))
        self.platforms = set(configured) if isinstance(configured, list) else PLATFORMS
        super().__init__(
            plugin.context,
            settings,
            root,
            scope_of=lambda event: (
                event.get_extra(SCOPE_KEY) or scope_key(event.unified_msg_origin, None)
            ),
            allowed=self._allowed,
            find_quote=lambda event: event.get_extra(QUOTE_KEY),
        )

    def _allowed(self, event):
        if event.get_platform_name() not in PLATFORMS or (
            self.platforms and event.get_platform_name() not in self.platforms
        ):
            return False
        # Wrappers also observe other sessions: respect each session's plugin selection.
        selected = self.context.get_config(event.unified_msg_origin).get(
            "plugin_set", ["*"]
        )
        return isinstance(selected, list) and (
            "*" in selected or self.plugin.name in selected
        )

    async def initialize(self):
        try:
            await super().initialize()
            if self.enabled:
                from botpy.http import BotHttp

                self.observer = QQSendObserver(BotHttp, self._qq_receipt, logger)
                self._install_group_annotations()
        except BaseException:
            await self.terminate()
            raise

    async def resolve_scope(self, umo, cid=None):
        if cid is None:
            cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
        scope = scope_key(umo, cid)
        if cid is not None:
            await self._run_io(self.cache.bind_scope, scope_key(umo, None), scope)
        return scope

    async def capture(self, event):
        if not self.active(event):
            return
        task = event.get_extra(TASK_KEY)
        if task is None:
            task = asyncio.create_task(self._capture_event(event))
            event.set_extra(TASK_KEY, task)
            self.capture_tasks.add(task)
            task.add_done_callback(self.capture_tasks.discard)
        await asyncio.shield(task)

    async def _capture_event(self, event):
        try:
            scope = await self.resolve_scope(event.unified_msg_origin)
            event.set_extra(SCOPE_KEY, scope)
            mid, own_index, quote_index = identifiers(event)
            await self._run_io(
                self.cache.remember_author,
                scope,
                [mid, own_index],
                mid,
                event.get_sender_id(),
                event.get_sender_name() or "",
                event.message_obj.timestamp or time.time(),
            )
            replies = [r for r in event.get_messages() if isinstance(r, Reply)]
            quote = await self._run_io(
                self.cache.find_author,
                scope,
                [quote_index] if quote_index else [field(r, "id") for r in replies],
            )
            event.set_extra(QUOTE_KEY, quote)
            if event.get_platform_name() == "aiocqhttp":
                await self._complete_onebot_images(event, replies)
            await super().capture(event)
        except Exception as exc:
            logger.warning("[preserve_atbot] 历史图片收录失败 (%s)", type(exc).__name__)
            event.set_extra(EVENT_KEY, {"images": [], "failed": 1})

    async def _complete_onebot_images(self, event, replies):
        # OneBot carries one reply per message; bound malformed component chains too.
        for reply in replies[:1]:
            if reply.chain or not reply.id:
                continue
            try:
                data = await asyncio.wait_for(
                    event.bot.api.call_action(
                        "get_msg",
                        message_id=int(reply.id)
                        if str(reply.id).isdigit()
                        else reply.id,
                    ),
                    timeout=self.plugin.quote_sender_timeout,
                )
                data = field(data, "data", data)
                # Never allow a returned message from a different group/private peer.
                group = str(event.get_group_id() or "")
                returned_group = str(field(data, "group_id") or "")
                sender = field(data, "sender", {})
                returned_sender = str(
                    field(sender, "user_id") or field(data, "user_id") or ""
                )
                if group and returned_group and group != returned_group:
                    continue
                if not group and returned_sender not in {
                    str(event.get_sender_id()),
                    str(event.get_self_id()),
                }:
                    continue
                raw_chain = field(data, "message", [])
                if isinstance(raw_chain, str):
                    from aiocqhttp.message import Message

                    raw_chain = list(Message(raw_chain))
                chain = []
                for segment in raw_chain:
                    if (
                        isinstance(segment, dict)
                        and segment.get("type") in ComponentTypes
                    ):
                        with contextlib.suppress(Exception):
                            chain.append(
                                ComponentTypes[segment["type"]](
                                    **segment.get("data", {})
                                )
                            )
                reply.chain = chain
                event.set_extra("preserve_atbot.late_quote_images", True)
                reply.sender_id = reply.sender_id or returned_sender
                reply.sender_nickname = (
                    reply.sender_nickname
                    or field(sender, "card")
                    or field(sender, "nickname")
                    or ""
                )
                reply.time = reply.time or field(data, "time") or 0
            except Exception as exc:
                logger.debug(
                    "[preserve_atbot] OneBot 引用图片读取失败 (%s)", type(exc).__name__
                )

    async def _qq_receipt(self, http, route, payload, response):
        if not self.enabled or self.closing or self.cache is None:
            return
        for platform in self.context.platform_manager.get_insts():
            meta = platform.meta()
            bot = getattr(platform, "client", None)
            if (
                meta.name in {"qq_official", "qq_official_webhook"}
                and getattr(bot, "http", None) is http
            ):
                break
        else:
            return
        if self.platforms and meta.name not in self.platforms:
            return
        group = route.path == "/v2/groups/{group_openid}/messages"
        session = str(route.parameters["group_openid" if group else "openid"])
        umo = str(
            MessageSession(
                str(meta.id),
                MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE,
                session,
            )
        )
        selected = self.context.get_config(umo).get("plugin_set", ["*"])
        if not isinstance(selected, list) or not (
            "*" in selected or self.plugin.name in selected
        ):
            return
        scope = await self.resolve_scope(umo)
        mid = str(response["id"])
        ref = str(field(response.get("ext_info"), "ref_idx", "") or "")
        await self._run_io(
            self.cache.remember_author,
            scope,
            [mid, ref],
            mid,
            str(getattr(platform, "appid", "")),
            self.plugin.fallback_name,
            time.time(),
            True,
        )

    def group_note(self, event):
        state = event.get_extra(EVENT_KEY) or {}
        if not state.get("images") and not state.get("failed"):
            return ""
        return "\n" + message_note(
            state.get("images", []),
            0,
            0,
            state.get("failed", 0),
            state.get("quotes_bot", False),
        )

    def _install_group_annotations(self):
        """Attach IDs to native group rows even when plugin speech recording is off."""
        from astrbot.builtin_stars.astrbot.group_chat_context import GroupChatContext

        original = GroupChatContext.handle_message

        @wraps(original)
        async def handle_message(manager, event):
            await original(manager, event)
            if not self.active(event):
                return
            try:
                await self.capture(event)
                note = self.group_note(event)
                entry_id = event.get_extra("_group_context_record_id", "")
                if note and entry_id:
                    umo = event.unified_msg_origin
                    async with manager._get_lock(umo):
                        ids = list(manager._record_ids.get(umo, ()))
                        if entry_id in ids:
                            manager.raw_records[umo][ids.index(entry_id)] += note
            except Exception as exc:
                logger.warning(
                    "[preserve_atbot] 群聊图片编号补全失败 (%s)", type(exc).__name__
                )

        self.group_patch = (GroupChatContext, original, handle_message)
        GroupChatContext.handle_message = handle_message

    async def prepare_request(self, event, req):
        if not self.active(event):
            return
        await self.capture(event)
        # A first request creates its conversation after passive message capture.
        cid = getattr(getattr(req, "conversation", None), "cid", None)
        if cid is not None:
            event.set_extra(
                SCOPE_KEY, await self.resolve_scope(event.unified_msg_origin, cid)
            )
        if event.get_extra("preserve_atbot.late_quote_images"):
            state = event.get_extra(EVENT_KEY) or {}
            state["native"] = 0
        await super().prepare_request(event, req)
        # The core skips GIFs. Keep cached/late-fetched quotes on-demand too; no direct attachment.
        req.contexts = self._compact_sent_images(req.contexts)

    @staticmethod
    def _compact_sent_images(history):
        """Drop only legacy plugin-send attachments, identified by their checkpoint."""
        from .sent_history import CHECKPOINT_PREFIX

        result = copy.deepcopy(history or [])
        for index, checkpoint in enumerate(result):
            identifier = (
                (checkpoint.get("content") or {}).get("id", "")
                if checkpoint.get("role") == "_checkpoint"
                else ""
            )
            if index and identifier.startswith(CHECKPOINT_PREFIX):
                message = result[index - 1]
                if message.get("role") == "assistant" and isinstance(
                    message.get("content"), list
                ):
                    message["content"] = [
                        p
                        if p.get("type") != "image_url"
                        else {
                            "type": "text",
                            "text": "[历史插件图片；需要时用 preserve_image_list 查询，旧版未缓存的图片请重新发送。]",
                        }
                        for p in message["content"]
                    ]
        return result

    async def sent_content(self, envelope):
        """Cache snapshotted outgoing images only after an acknowledged send."""
        scope = await self.resolve_scope(envelope["umo"], envelope.get("cid"))
        mid = min(envelope["receipts"])
        candidates = envelope.get("image_candidates", [])
        entries = []
        seen = time.time()
        for position, candidate in enumerate(candidates):
            candidate = replace(
                candidate,
                message_id=mid,
                is_bot=True,
                sender_name=self.plugin.fallback_name,
                timestamp=seen,
            )
            image_id = await self._store_candidate(scope, candidate, seen, position)
            if image_id:
                entries.append(image_entry(image_id, candidate, 0))
        if entries or candidates:
            note = message_note(entries, 0, 0, len(candidates) - len(entries))
            content = envelope["content"]
            if isinstance(content, str):
                return content + "\n" + note
            return content + [{"type": "text", "text": note}]
        return envelope["content"]

    async def terminate(self):
        self.closing = True
        if self.group_patch:
            target, original, wrapper = self.group_patch
            if target.handle_message is wrapper:
                target.handle_message = original
            self.group_patch = None
        if self.observer:
            self.observer.close()
            self.observer = None
        for task in list(self.capture_tasks):
            task.cancel()
        if self.capture_tasks:
            await asyncio.gather(*list(self.capture_tasks), return_exceptions=True)
        await super().terminate()
