"""Regression tests for chronological plugin speech and acknowledged sends."""

import asyncio
import inspect
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import botpy.message
from aiocqhttp import CQHttp
from botpy.api import BotAPI

from astrbot.core.agent.message import (
    bind_checkpoint_messages,
    dump_messages_with_checkpoints,
)
from astrbot.core.message.message_event_result import (
    MessageChain,
    MessageEventResult,
    ResultContentType,
)
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import (
    QQOfficialMessageEvent,
)
from astrbot.core.star.context import Context
from data.plugins.astrbot_plugin_preserve_atbot import main as plugin_main
from data.plugins.astrbot_plugin_preserve_atbot.send_capture import (
    ENVELOPE,
    OWNER,
    SendCapture,
)
from data.plugins.astrbot_plugin_preserve_atbot.sent_history import (
    SentHistory,
    fingerprint,
    message_text,
)


class ChronologicalHistoryTests(unittest.TestCase):
    """Exercise actual history merging without any query tool or context block."""

    def setUp(self):
        self.store = SentHistory(Path(":memory:"), 10)
        self.greeting = [
            {"role": "user", "content": "珈宝你好"},
            {"role": "assistant", "content": "你好"},
        ]

    def tearDown(self):
        self.store.close()

    def add(
        self,
        text="抽卡结果原文",
        trigger="~抽卡",
        event_id="draw1",
        history=None,
        umo="onebot:GroupMessage:42",
        cid="branch1",
    ):
        base = self.greeting if history is None else history
        return self.store.append(
            umo,
            cid,
            {
                "content": text,
                "trigger_text": trigger,
                "trigger_id": event_id,
                "source": {
                    "name": "draw_plugin",
                    "display_name": "抽卡",
                    "version": "1.0",
                },
                "sent_at": "2026-10-26T04:30:00+00:00",
                "anchors": [fingerprint(item) for item in base][-3:],
            },
        )

    def visible(self, history):
        return [item for item in history if item["role"] != "_checkpoint"]

    def test_user_example_is_embedded_as_normal_messages(self):
        self.add()
        result = self.visible(
            self.store.merge(
                self.greeting + [{"role": "user", "content": "哈哈"}],
                "onebot:GroupMessage:42",
                "branch1",
                "haha",
            )
        )
        self.assertEqual(
            [row["role"] for row in result],
            ["user", "assistant", "user", "assistant", "user"],
        )
        self.assertEqual(result[2]["content"], "~抽卡")
        self.assertTrue(result[3]["content"].startswith("抽卡结果原文"))
        self.assertIn("draw_plugin", result[3]["content"])
        self.assertEqual(result[-1]["content"], "哈哈")

    def test_existing_trigger_is_not_duplicated(self):
        self.add()
        history = self.greeting + [
            {"role": "user", "content": "~抽卡"},
            {"role": "user", "content": "哈哈"},
        ]
        result = self.visible(
            self.store.merge(history, "onebot:GroupMessage:42", "branch1")
        )
        self.assertEqual(sum(message_text(item) == "~抽卡" for item in result), 1)
        self.assertEqual(result[3]["role"], "assistant")

    def test_deduplication_survives_native_framework_serialization(self):
        self.add()
        merged = self.store.merge(self.greeting, "onebot:GroupMessage:42", "branch1")
        serialized = dump_messages_with_checkpoints(bind_checkpoint_messages(merged))
        second = self.store.merge(serialized, "onebot:GroupMessage:42", "branch1")
        self.assertEqual(len(self.visible(second)), 4)
        self.assertEqual(
            [m.role for m in bind_checkpoint_messages(second)],
            ["user", "assistant", "user", "assistant"],
        )

    def test_multiple_replies_keep_send_order_and_one_trigger(self):
        self.add(text="第一条")
        self.add(text="第二条")
        result = self.visible(
            self.store.merge(self.greeting, "onebot:GroupMessage:42", "branch1")
        )
        self.assertTrue(result[-2]["content"].startswith("第一条"))
        self.assertTrue(result[-1]["content"].startswith("第二条"))
        self.assertEqual(sum(message_text(m) == "~抽卡" for m in result), 1)

    def test_new_reply_is_after_previously_persisted_reply(self):
        self.add(text="第一条")
        first = self.store.merge(self.greeting, "onebot:GroupMessage:42", "branch1")
        self.add(text="第二条")
        result = self.visible(
            self.store.merge(first, "onebot:GroupMessage:42", "branch1")
        )
        self.assertTrue(result[-2]["content"].startswith("第一条"))
        self.assertTrue(result[-1]["content"].startswith("第二条"))
        self.assertEqual(sum(message_text(m) == "~抽卡" for m in result), 1)

    def test_recent_limit_prunes_only_extra_assistant_messages(self):
        self.store.limit = 1
        self.add(text="旧结果")
        first = self.store.merge(self.greeting, "onebot:GroupMessage:42", "branch1")
        self.add(text="新结果", event_id="draw2")
        second = self.visible(
            self.store.merge(first, "onebot:GroupMessage:42", "branch1")
        )
        self.assertFalse(any(message_text(m).startswith("旧结果") for m in second))
        self.assertTrue(any(message_text(m).startswith("新结果") for m in second))
        self.assertEqual(second[:2], self.greeting)

    def test_bot_chat_and_branch_are_isolated(self):
        self.add()
        for umo, cid in [
            ("official:GroupMessage:42", "branch1"),
            ("onebot:GroupMessage:43", "branch1"),
            ("onebot:GroupMessage:42", "branch2"),
        ]:
            self.assertEqual(self.store.merge(self.greeting, umo, cid), self.greeting)

    def test_zero_limit_keeps_all_records_in_order_without_duplicates(self):
        self.store.limit = 0
        expected = [f"result-{index}" for index in range(125)]
        for text in expected:
            self.add(text=text)
        self.add(text="other bot", umo="official:GroupMessage:42")
        self.add(text="other branch", cid="branch2")
        records = self.store.records("onebot:GroupMessage:42", "branch1")
        self.assertEqual([record["content"] for record in records], expected)
        first = self.store.merge(self.greeting, "onebot:GroupMessage:42", "branch1")
        serialized = dump_messages_with_checkpoints(bind_checkpoint_messages(first))
        second = self.store.merge(serialized, "onebot:GroupMessage:42", "branch1")
        visible = self.visible(second)
        self.assertEqual(
            [message_text(item).split("\n\n")[0] for item in visible[3:]], expected
        )
        self.assertEqual(self.visible(first), visible)

    def test_positive_limit_after_unlimited_prunes_only_target_scope(self):
        self.store.limit = 0
        for text in ("first", "second", "third"):
            self.add(text=text)
        self.add(text="other branch", cid="branch2")
        merged = self.store.merge(self.greeting, "onebot:GroupMessage:42", "branch1")
        self.store.limit = 2
        self.add(text="fourth")
        records = self.store.records("onebot:GroupMessage:42", "branch1")
        self.assertEqual([record["content"] for record in records], ["third", "fourth"])
        visible = self.visible(
            self.store.merge(merged, "onebot:GroupMessage:42", "branch1")
        )
        self.assertEqual(
            [message_text(item).split("\n\n")[0] for item in visible[3:]],
            ["third", "fourth"],
        )
        self.assertEqual(visible[:2], self.greeting)
        self.assertEqual(
            self.store.records("onebot:GroupMessage:42", "branch2")[0]["content"],
            "other branch",
        )

    def test_proactive_push_does_not_invent_user_message(self):
        self.add(trigger="", event_id="", text="新闻推送")
        result = self.visible(
            self.store.merge(self.greeting, "onebot:GroupMessage:42", "branch1")
        )
        self.assertEqual(
            [m["role"] for m in result], ["user", "assistant", "assistant"]
        )

    def test_same_current_user_event_is_deferred_until_next_turn(self):
        self.add()
        result = self.store.merge(
            self.greeting, "onebot:GroupMessage:42", "branch1", "draw1"
        )
        self.assertEqual(result, self.greeting)

    def test_tool_call_and_results_remain_contiguous(self):
        tool_call = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call1",
                    "type": "function",
                    "function": {"name": "test", "arguments": "{}"},
                }
            ],
        }
        tools = [
            tool_call,
            {"role": "tool", "content": "result", "tool_call_id": "call1"},
        ]
        self.add(trigger="", event_id="", history=[tool_call])
        result = self.visible(
            self.store.merge(tools, "onebot:GroupMessage:42", "branch1")
        )
        self.assertEqual(result[0], tool_call)
        self.assertEqual(result[1]["role"], "tool")
        self.assertEqual(result[2]["role"], "assistant")

    def test_initial_records_bind_once_to_first_branch(self):
        self.add(cid=None)
        self.assertEqual(
            len(
                self.visible(
                    self.store.merge(self.greeting, "onebot:GroupMessage:42", "branch1")
                )
            ),
            4,
        )
        self.assertEqual(
            self.store.merge(self.greeting, "onebot:GroupMessage:42", "branch2"),
            self.greeting,
        )


class RecordingSwitchTests(unittest.IsolatedAsyncioTestCase):
    """Keep the recording switch independent of finite and unlimited retention."""

    async def test_only_switch_controls_storage_and_capture_initialization(self):
        for enabled in (True, False):
            for limit in (0, 10):
                with self.subTest(enabled=enabled, limit=limit):
                    plugin = object.__new__(plugin_main.PreserveAtBotPlugin)
                    plugin.context = MagicMock()
                    plugin.record_plugin_messages = enabled
                    plugin.recent_plugin_message_count = limit
                    plugin.sent_history = None
                    plugin.send_capture = None
                    with (
                        patch.object(
                            plugin_main,
                            "SentHistory",
                            side_effect=lambda path, count: SentHistory(
                                Path(":memory:"), count
                            ),
                        ) as storage,
                        patch.object(plugin_main, "SendCapture") as capture,
                    ):
                        try:
                            await plugin.initialize()
                            if enabled:
                                storage.assert_called_once()
                                capture.return_value.install.assert_called_once()
                                plugin.sent_history.append(
                                    "bot:chat", "branch", {"content": "saved"}
                                )
                                self.assertEqual(
                                    plugin.sent_history.records("bot:chat", "branch")[
                                        0
                                    ]["content"],
                                    "saved",
                                )
                            else:
                                storage.assert_not_called()
                                capture.assert_not_called()
                                self.assertIsNone(plugin.sent_history)
                        finally:
                            await plugin.terminate()


class SendCaptureTests(unittest.IsolatedAsyncioTestCase):
    """Exercise platform wrappers with real acknowledgements and failure paths."""

    def setUp(self):
        self.store = SentHistory(Path(":memory:"), 10)
        context = MagicMock()
        context.get_config.return_value = {
            "plugin_set": ["astrbot_plugin_preserve_atbot", "draw_plugin"]
        }
        context.get_all_stars.return_value = []
        context.conversation_manager.get_curr_conversation_id = AsyncMock(
            return_value="branch1"
        )
        context.conversation_manager.get_conversation = AsyncMock(
            return_value=SimpleNamespace(history="[]")
        )
        self.plugin = SimpleNamespace(
            context=context, name="astrbot_plugin_preserve_atbot"
        )
        self.capture = SendCapture(self.plugin, self.store)
        self.extras = {}
        self.result = None
        self.event = SimpleNamespace(
            unified_msg_origin="onebot:GroupMessage:42",
            message_obj=SimpleNamespace(message_id="draw1"),
            get_message_str=lambda: "~抽卡",
            get_group_id=lambda: "42",
            get_sender_id=lambda: "101",
            get_result=lambda: self.result,
            set_extra=lambda key, value: self.extras.__setitem__(key, value),
            get_extra=lambda key, default=None: self.extras.get(key, default),
        )

    def tearDown(self):
        self.capture.close()
        self.store.close()

    async def test_zero_limit_still_captures_acknowledged_sends(self):
        self.store.limit = 0
        envelope = await self.capture.prepare(
            self.event.unified_msg_origin,
            MessageChain().message("unlimited result"),
            self.event,
        )
        self.assertIsNotNone(envelope)
        envelope["receipts"].add("confirmed")
        await self.capture.finish(envelope)
        self.assertEqual(
            self.store.records(self.event.unified_msg_origin, "branch1")[0]["content"],
            "unlimited result",
        )

    async def test_native_c2c_method_binds_instance_and_restores_after_reload(self):
        original = inspect.getattr_static(QQOfficialMessageEvent, "post_c2c_message")
        event = object.__new__(QQOfficialMessageEvent)
        request = AsyncMock(return_value={"id": "c2c-receipt"})
        event.bot = SimpleNamespace(
            api=SimpleNamespace(_http=SimpleNamespace(request=request))
        )
        for cycle in range(2):
            with self.subTest(reload=cycle):
                self.capture = SendCapture(self.plugin, self.store)
                self.capture.install()
                envelope = {"receipts": set()}
                token = ENVELOPE.set(envelope)
                try:
                    result = await event.post_c2c_message(
                        openid="recipient",
                        content="reply",
                        msg_type=0,
                        stream={"state": 1, "id": None, "index": 0},
                    )
                    self.assertEqual(result, {"id": "c2c-receipt"})
                    self.assertEqual(envelope["receipts"], {"c2c-receipt"})
                    payload = request.await_args.kwargs["json"]
                    self.assertEqual(payload["content"], "reply")
                    self.assertNotIn("self", payload)
                    self.assertNotIn("id", payload["stream"])
                finally:
                    ENVELOPE.reset(token)
                    self.capture.close()
                self.assertIs(
                    inspect.getattr_static(QQOfficialMessageEvent, "post_c2c_message"),
                    original,
                )

    async def test_native_c2c_send_reaches_http_for_plugin_and_llm_replies(self):
        request = AsyncMock(return_value={"id": "c2c-reply"})
        bot = SimpleNamespace(
            api=SimpleNamespace(_http=SimpleNamespace(request=request))
        )
        source = MagicMock(spec=botpy.message.C2CMessage)
        source.author.user_openid = "recipient"
        message = AstrBotMessage()
        message.type = MessageType.FRIEND_MESSAGE
        message.self_id = "bot-id"
        message.message_id = "user-message"
        message.message_str = "~draw"
        message.message = []
        message.sender = MessageMember("recipient", "Alice")
        message.raw_message = source
        platform = PlatformMetadata("qq_official", "QQ Official", "official")
        self.capture.install()
        for llm_reply in (False, True):
            with self.subTest(llm_reply=llm_reply):
                event = QQOfficialMessageEvent(
                    "draw", message, platform, "recipient", bot
                )
                self.store.clear(event.unified_msg_origin)
                request.reset_mock()
                if llm_reply:
                    event._result = MessageEventResult().message("reply")
                    event._result.result_content_type = ResultContentType.LLM_RESULT
                with patch(
                    "astrbot.core.platform.astr_message_event.Metric.upload",
                    new_callable=AsyncMock,
                ):
                    await event.send(MessageChain().message("reply"))
                    await asyncio.sleep(0)
                request.assert_awaited_once()
                self.assertTrue(event._has_send_oper)
                self.assertIsNone(event.send_buffer)
                payload = request.await_args.kwargs["json"]
                self.assertEqual(payload["markdown"]["content"], "reply")
                records = self.store.records(event.unified_msg_origin, "branch1")
                if llm_reply:
                    self.assertEqual(records, [])
                else:
                    self.assertEqual(len(records), 1)
                    self.assertEqual(records[0]["content"], "reply")
                    self.assertEqual(records[0]["message_ids"], ["c2c-reply"])

    async def test_reset_clears_unbound_records_without_affecting_other_chats(self):
        async def coroutine_handler(event):
            return MessageEventResult().message("reset")

        async def generator_handler(event):
            yield MessageEventResult().message("reset")

        for handler in (coroutine_handler, generator_handler):
            with self.subTest(handler=handler.__name__):
                umo = self.event.unified_msg_origin
                self.store.append(umo, None, {"content": "old unbound reply"})
                self.store.append(umo, "branch1", {"content": "old reply"})
                self.store.append(
                    "other:GroupMessage:42", "branch1", {"content": "other bot"}
                )
                self.extras["_clean_group_context_session"] = True
                wrapped = self.capture.wrap_handler(handler, {"name": "core"})
                if inspect.isasyncgenfunction(wrapped):
                    iterator = wrapped(self.event)
                    await anext(iterator)
                    await iterator.aclose()
                else:
                    await wrapped(self.event)
                self.assertEqual(self.store.records(umo, "new-branch"), [])
                self.assertEqual(self.store.records(umo, "branch1"), [])
                self.assertEqual(
                    self.store.records("other:GroupMessage:42", "branch1")[-1][
                        "content"
                    ],
                    "other bot",
                )

    async def test_original_wake_prefix_survives_capture_and_context_merge(self):
        self.event.get_message_str = lambda: "抽卡"
        for original in ("~抽卡", "/抽卡", "~  抽卡"):
            with self.subTest(original=original):
                self.store.clear(self.event.unified_msg_origin)
                self.event.message_obj.message_str = original
                envelope = await self.capture.prepare(
                    self.event.unified_msg_origin,
                    MessageChain().message("抽卡结果"),
                    self.event,
                )
                envelope["receipts"].add("confirmed")
                await self.capture.finish(envelope)
                history = self.store.merge(
                    [], self.event.unified_msg_origin, "branch1", "next-message"
                )
                visible = [row for row in history if row["role"] != "_checkpoint"]
                self.assertEqual(visible[0], {"role": "user", "content": original})
                self.assertEqual(visible[1]["role"], "assistant")
                self.assertTrue(visible[1]["content"].startswith("抽卡结果"))
                self.assertEqual(self.event.get_message_str(), "抽卡")

    async def test_empty_original_trigger_falls_back_to_event_text(self):
        self.event.message_obj.message_str = ""
        envelope = await self.capture.prepare(
            self.event.unified_msg_origin, MessageChain().message("结果"), self.event
        )
        self.assertEqual(envelope["trigger_text"], "~抽卡")

    async def test_yielded_origin_survives_image_conversion(self):
        async def handler(event):
            yield MessageEventResult().message("原始抽卡结果")

        wrapped = self.capture.wrap_handler(
            handler, {"name": "draw_plugin", "display_name": "抽卡", "version": "1"}
        )
        self.assertTrue(inspect.isasyncgenfunction(wrapped))
        iterator = wrapped(self.event)
        self.result = await anext(iterator)
        self.assertIsNone(OWNER.get())
        self.result.chain = []
        envelope = await self.capture.prepare(
            self.event.unified_msg_origin,
            MessageChain().message("转换后的内容"),
            self.event,
        )
        self.assertEqual(envelope["content"], "原始抽卡结果")
        self.assertEqual(envelope["source"]["name"], "draw_plugin")
        await iterator.aclose()

    async def test_successful_send_records_once_and_preserves_return(self):
        async def action(bot, name, **params):
            return {"message_id": 123}

        async def send(event, chain):
            return await CQHttp.call_action(object(), "send_group_msg", group_id=42)

        with (
            patch.object(CQHttp, "call_action", action),
            patch.object(AiocqhttpMessageEvent, "send", send),
        ):
            self.capture.install()
            result = await AiocqhttpMessageEvent.send(
                self.event, MessageChain().message("结果")
            )
            self.assertEqual(result, {"message_id": 123})
            records = self.store.records(self.event.unified_msg_origin, "branch1")
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["message_ids"], ["123"])
            self.capture.close()
            self.assertIs(AiocqhttpMessageEvent.send, send)

    async def test_failed_send_is_not_recorded_or_replayed(self):
        calls = 0

        async def send(event, chain):
            nonlocal calls
            calls += 1
            raise RuntimeError("send failed")

        with patch.object(AiocqhttpMessageEvent, "send", send):
            self.capture.install()
            with self.assertRaisesRegex(RuntimeError, "send failed"):
                await AiocqhttpMessageEvent.send(
                    self.event, MessageChain().message("结果")
                )
            self.assertEqual(calls, 1)
            self.assertEqual(
                self.store.records(self.event.unified_msg_origin, "branch1"), []
            )
            self.capture.close()

    async def test_no_receipt_does_not_claim_success(self):
        async def send(event, chain):
            return None

        with patch.object(AiocqhttpMessageEvent, "send", send):
            self.capture.install()
            await AiocqhttpMessageEvent.send(self.event, MessageChain().message("结果"))
            self.assertEqual(
                self.store.records(self.event.unified_msg_origin, "branch1"), []
            )
            self.capture.close()

    async def test_llm_and_streaming_replies_are_not_captured(self):
        for kind in (
            ResultContentType.LLM_RESULT,
            ResultContentType.STREAMING_RESULT,
            ResultContentType.STREAMING_FINISH,
        ):
            self.result = (
                MessageEventResult().message("LLM 回复").set_result_content_type(kind)
            )
            self.assertIsNone(
                await self.capture.prepare(
                    self.event.unified_msg_origin, self.result, self.event
                )
            )

    async def test_profile_without_plugin_does_not_capture(self):
        self.plugin.context.get_config.return_value = {"plugin_set": ["draw_plugin"]}
        self.assertIsNone(
            await self.capture.prepare(
                self.event.unified_msg_origin,
                MessageChain().message("结果"),
                self.event,
            )
        )

    async def test_context_send_is_captured_without_inventing_trigger(self):
        async def action(bot, name, **params):
            return {"message_id": 456}

        async def send(context, session, chain):
            await CQHttp.call_action(object(), "send_group_msg", group_id=42)
            return True

        with (
            patch.object(CQHttp, "call_action", action),
            patch.object(Context, "send_message", send),
        ):
            self.capture.install()
            self.assertTrue(
                await Context.send_message(
                    self.plugin.context,
                    self.event.unified_msg_origin,
                    MessageChain().message("新闻"),
                )
            )
            record = self.store.records(self.event.unified_msg_origin, "branch1")[0]
            self.assertEqual(record["trigger_text"], "")
            self.capture.close()

    async def test_background_task_inherits_plugin_but_not_old_user_trigger(self):
        results = []

        async def background():
            results.append(
                await self.capture.prepare(
                    self.event.unified_msg_origin, MessageChain().message("推送")
                )
            )

        async def handler(event):
            await asyncio.create_task(background())

        wrapped = self.capture.wrap_handler(handler, {"name": "draw_plugin"})
        await wrapped(self.event)
        self.assertEqual(results[0]["source"]["name"], "draw_plugin")
        self.assertEqual(results[0]["trigger_text"], "")

    async def test_direct_onebot_api_send_is_captured_and_binding_restored(self):
        class API:
            async def call_action(self, action, **params):
                return {"message_id": 789}

        api = API()
        bot = SimpleNamespace(api=api, _api=api)
        self.plugin.context.platform_manager = SimpleNamespace(
            platform_insts=[
                SimpleNamespace(bot=bot, meta=lambda: SimpleNamespace(id="onebot"))
            ]
        )
        self.capture.install()

        async def handler(event):
            return await api.call_action(
                "send_group_msg", group_id=42, message="完整抽卡原文"
            )

        wrapped = self.capture.wrap_handler(
            handler, {"name": "draw_plugin", "display_name": "抽卡"}
        )
        self.assertEqual(await wrapped(self.event), {"message_id": 789})
        records = self.store.records(self.event.unified_msg_origin, "branch1")
        self.assertEqual(records[0]["content"], "完整抽卡原文")
        self.assertEqual(records[0]["source"]["name"], "draw_plugin")
        self.assertEqual(records[0]["trigger_text"], "~抽卡")
        self.capture.close()
        self.assertNotIn("call_action", vars(api))
        self.assertEqual(await api.call_action("get_msg"), {"message_id": 789})

    async def test_direct_official_api_send_uses_its_bot_configuration(self):
        api = SimpleNamespace()
        platform = SimpleNamespace(
            bot=SimpleNamespace(api=api), meta=lambda: SimpleNamespace(id="official")
        )
        self.plugin.context.platform_manager = SimpleNamespace(
            platform_insts=[platform]
        )

        async def post(api, group_openid, content="", **kwargs):
            return {"id": "official123"}

        with patch.object(BotAPI, "post_group_message", post):
            self.capture.install()

            async def handler(event):
                return await BotAPI.post_group_message(
                    api, group_openid="42", content="官机抽卡结果"
                )

            wrapped = self.capture.wrap_handler(handler, {"name": "official_draw"})
            await wrapped(self.event)
            self.assertEqual(
                self.store.records("onebot:GroupMessage:42", "branch1"), []
            )
            record = self.store.records("official:GroupMessage:42", "branch1")[0]
            self.assertEqual(record["content"], "官机抽卡结果")
            self.assertEqual(record["trigger_text"], "")
            self.capture.close()

    async def test_direct_cq_image_is_preserved_as_multimodal_content(self):
        api = SimpleNamespace()
        self.plugin.context.platform_manager = SimpleNamespace(
            platform_insts=[
                SimpleNamespace(bot=api, meta=lambda: SimpleNamespace(id="onebot"))
            ]
        )
        token = OWNER.set({"source": {"name": "draw_plugin"}})
        try:
            envelope = await self.capture.prepare_api(
                api,
                {"group_id": 42, "message": "[CQ:image,file=base64://aGVsbG8=]"},
                True,
            )
        finally:
            OWNER.reset(token)
        self.assertEqual(envelope["content"][0]["type"], "image_url")
        self.assertTrue(
            envelope["content"][0]["image_url"]["url"].startswith("data:image/")
        )
