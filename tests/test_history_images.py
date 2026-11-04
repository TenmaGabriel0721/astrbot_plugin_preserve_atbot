"""Integration coverage against AstrBot 4.29.4's real events and message models."""

import asyncio
import base64
import io
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest
from astrbot.api.message_components import Image, Plain, Reply
from astrbot.builtin_stars.astrbot.group_chat_context import GroupChatContext
from astrbot.core.message.message_event_result import MessageChain, MessageEventResult
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import (
    QQOfficialMessageEvent,
)
from data.plugins.astrbot_plugin_preserve_atbot.history_images import (
    HistoryImages,
    scope_key,
)
from data.plugins.astrbot_plugin_preserve_atbot.image_cache import (
    EVENT_KEY,
    MESSAGE_PREFIX,
    VIEW_MARKER,
)
from data.plugins.astrbot_plugin_preserve_atbot.send_capture import SendCapture
from data.plugins.astrbot_plugin_preserve_atbot.sent_history import SentHistory
from PIL import Image as PILImage


def event(
    platform="aiocqhttp",
    mid="m1",
    chain=(),
    raw=None,
    group="42",
    bot_id=None,
    sender="101",
):
    obj = AstrBotMessage()
    obj.type = MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
    obj.message_id = mid
    obj.message = list(chain)
    obj.message_str = "测试"
    obj.sender = MessageMember(sender, "Alice")
    obj.self_id = "999"
    obj.group_id = group
    obj.session_id = group or sender
    obj.timestamp = 1700000000
    obj.raw_message = NS(raw_data=raw or {})
    meta = PlatformMetadata(platform, platform, bot_id or platform)
    cls = AiocqhttpMessageEvent if platform == "aiocqhttp" else QQOfficialMessageEvent
    return cls(
        "测试", obj, meta, obj.session_id, bot=NS(api=NS(call_action=AsyncMock()))
    )


@pytest.fixture
async def service(tmp_path):
    context = MagicMock()
    context.get_config.return_value = {
        "plugin_set": ["*"],
        "provider_settings": {"image_caption_prompt": "describe"},
        "provider_ltm_settings": {
            "group_icl_enable": True,
            "group_message_max_cnt": 42,
            "image_caption": False,
            "active_reply": {
                "enable": False,
                "method": "possibility_reply",
                "possibility_reply": 0,
            },
        },
    }
    context.conversation_manager.get_curr_conversation_id = AsyncMock(
        return_value="branch1"
    )
    context.conversation_manager.get_conversation = AsyncMock(
        return_value=NS(history="[]")
    )
    context.get_using_provider.return_value = NS(
        provider_config={"modalities": ["text", "image", "tool_use"]}
    )
    context.get_all_stars.return_value = []
    plugin = NS(
        context=context,
        name="astrbot_plugin_preserve_atbot",
        fallback_name="珈宝",
        quote_sender_timeout=0.1,
    )
    cache = HistoryImages(plugin, {}, tmp_path / "cache")
    plugin.images = cache
    await cache.initialize()
    try:
        yield cache
    finally:
        await cache.terminate()


def png(tmp_path, name="image.png", color="red"):
    path = tmp_path / name
    PILImage.new("RGB", (30, 20), color).save(path)
    return str(path)


def note(req):
    return json.loads(
        next(
            p.text[len(MESSAGE_PREFIX) :]
            for p in req.extra_user_content_parts
            if p.text.startswith(MESSAGE_PREFIX)
        )
    )


@pytest.mark.parametrize(
    "platform", ["aiocqhttp", "qq_official", "qq_official_webhook"]
)
async def test_real_events_passive_capture_and_view(service, tmp_path, platform):
    incoming = event(platform, chain=[Image.fromFileSystem(png(tmp_path))])
    incoming.send = AsyncMock()
    await asyncio.gather(service.capture(incoming), service.capture(incoming))
    scope = scope_key(incoming.unified_msg_origin, "branch1")
    rows = service.cache.recent(scope)
    assert len(rows) == 1 and rows[0]["sender_id"] == "101"
    incoming.send.assert_not_awaited()
    req = NS(
        contexts=[],
        extra_user_content_parts=[],
        image_urls=["already-attached"],
        conversation=NS(cid="branch1"),
    )
    await service.prepare_request(incoming, req)
    assert "无需再" in note(req)["说明"]
    result = await service.view_images(incoming, [rows[0]["id"]])
    assert result.content[1].mimeType == "image/jpeg"


async def test_bot_chat_private_and_branch_isolation(service, tmp_path):
    original = event(chain=[Image.fromFileSystem(png(tmp_path))])
    await service.capture(original)
    identifier = original.get_extra(EVENT_KEY)["images"][0]["id"]
    for other in [
        event(bot_id="otherbot"),
        event(group="43"),
        event(group=""),
        event("qq_official"),
    ]:
        await service.capture(other)
        assert (await service.view_images(other, [identifier])).isError
    service.context.conversation_manager.get_curr_conversation_id.return_value = (
        "branch2"
    )
    branch = event(mid="m2")
    await service.capture(branch)
    assert (await service.view_images(branch, [identifier])).isError


async def test_first_conversation_binds_previously_passive_images(service, tmp_path):
    service.context.conversation_manager.get_curr_conversation_id.return_value = None
    incoming = event(chain=[Image.fromFileSystem(png(tmp_path))])
    await service.capture(incoming)
    identifier = incoming.get_extra(EVENT_KEY)["images"][0]["id"]
    req = NS(
        contexts=[],
        extra_user_content_parts=[],
        image_urls=[],
        conversation=NS(cid="new"),
    )
    await service.prepare_request(incoming, req)
    assert service.cache.recent(scope_key(incoming.unified_msg_origin, None)) == []
    assert not (await service.view_images(incoming, [identifier])).isError


async def test_onebot_reply_completion_and_raw_segments_do_not_duplicate(
    service, tmp_path
):
    file = png(tmp_path)
    reply = Reply(id="old", sender_id="202", sender_nickname="Bob", chain=[])
    incoming = event(mid="new", chain=[reply])
    incoming.bot.api.call_action.return_value = {
        "message_id": "old",
        "group_id": 42,
        "time": 1690000000,
        "sender": {"user_id": 202, "nickname": "Bob"},
        "message": [
            {
                "type": "image",
                "data": {
                    "file": "hash-does-not-exist.image",
                    "url": "https://gchat.qpic.cn/x",
                },
            }
        ],
    }
    from data.plugins.astrbot_plugin_preserve_atbot import image_cache

    original = image_cache.read_source

    async def source(session, ref, **kwargs):
        assert ref == "https://gchat.qpic.cn/x"
        return open(file, "rb").read()

    image_cache.read_source = source
    try:
        await service.capture(incoming)
    finally:
        image_cache.read_source = original
    incoming.bot.api.call_action.assert_awaited_once()
    row = incoming.get_extra(EVENT_KEY)["images"][0]
    assert row["origin"] == "引用" and row["sender_id"] == "202"
    assert not incoming.get_extra(EVENT_KEY)["images"][0].get("sender_is_bot")
    req = NS(contexts=[], extra_user_content_parts=[], image_urls=[], conversation=None)
    await service.prepare_request(incoming, req)
    assert req.image_urls == []
    raw = {"message": [{"type": "image", "data": {"url": "https://gchat.qpic.cn/x"}}]}
    own = event(mid="own", chain=[Image.fromFileSystem(file)], raw=raw)
    await service.capture(own)
    assert len(own.get_extra(EVENT_KEY)["images"]) == 1


@pytest.mark.parametrize("webhook", [False, True])
async def test_official_quote_author_index_and_unknown_author(
    service, tmp_path, webhook
):
    platform = "qq_official_webhook" if webhook else "qq_official"
    raw = {"id": "alice-old", "message_scene": {"ext": ["msg_idx=alice-index"]}}
    original = event(platform, mid="adapter-old", raw={"d": raw} if webhook else raw)
    await service.capture(original)
    quote = Reply(id="opaque-adapter-id", chain=[Image.fromFileSystem(png(tmp_path))])
    incoming = event(
        platform,
        mid="new",
        sender="202",
        chain=[quote],
        raw={"message_scene": {"ext": ["ref_msg_idx=alice-index"]}},
    )
    await service.capture(incoming)
    row = incoming.get_extra(EVENT_KEY)["images"][0]
    assert row["sender_id"] == "101" and row["origin"] == "引用"
    listed = json.loads(await service.list_images(incoming))["images"][0]
    assert listed["message_id"] == "alice-old"
    unknown = event(
        platform,
        mid="unknown",
        chain=[quote],
        raw={"message_scene": {"ext": ["ref_msg_idx=missing"]}},
    )
    await service.capture(unknown)
    assert unknown.get_extra(EVENT_KEY)["images"][0]["sender_id"] == ""


async def test_official_successful_bot_receipt_indexes_quoted_author(service, tmp_path):
    http = object()
    platform = NS(
        meta=lambda: PlatformMetadata("qq_official", "QQ", "official"),
        client=NS(http=http),
        appid="12345",
    )
    service.context.platform_manager.get_insts.return_value = [platform]
    route = NS(
        path="/v2/groups/{group_openid}/messages", parameters={"group_openid": "42"}
    )
    await service._qq_receipt(
        http, route, {}, {"id": "bot-message", "ext_info": {"ref_idx": "bot-index"}}
    )
    incoming = event(
        "qq_official",
        bot_id="official",
        chain=[Reply(id="x", chain=[Image.fromFileSystem(png(tmp_path))])],
        raw={"message_scene": {"ext": ["ref_msg_idx=bot-index"]}},
    )
    await service.capture(incoming)
    assert incoming.get_extra(EVENT_KEY)["images"][0]["sender_is_bot"]
    assert incoming.get_extra(EVENT_KEY)["quotes_bot"]


async def test_gif_preview_is_jpeg_first_frame_and_history_removes_only_our_preview(
    service, tmp_path
):
    path = tmp_path / "animated.gif"
    PILImage.new("RGB", (20, 20), "red").save(
        path,
        save_all=True,
        append_images=[PILImage.new("RGB", (20, 20), "blue")],
        format="GIF",
    )
    incoming = event(chain=[Image.fromFileSystem(str(path))])
    await service.capture(incoming)
    identifier = incoming.get_extra(EVENT_KEY)["images"][0]["id"]
    result = await service.view_images(incoming, [identifier])
    with PILImage.open(io.BytesIO(base64.b64decode(result.content[1].data))) as preview:
        assert preview.format == "JPEG" and preview.getpixel((0, 0))[0] > 200
    parts = [
        {"type": "text", "text": VIEW_MARKER + "temp.jpg']"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,preview"}},
        {"type": "text", "text": "[Other tool]"},
        {"type": "image_url", "image_url": {"url": "keep-other-image"}},
    ]
    req = NS(
        contexts=[{"role": "user", "content": parts}],
        extra_user_content_parts=[],
        image_urls=[],
        conversation=None,
    )
    await service.prepare_request(incoming, req)
    assert [
        p["image_url"]["url"]
        for p in req.contexts[0]["content"]
        if p["type"] == "image_url"
    ] == ["keep-other-image"]
    assert parts[1]["image_url"]["url"].startswith("data:")


async def test_real_group_row_is_annotated_without_plugin_speech_and_shared_budget(
    service, tmp_path
):
    manager = GroupChatContext(MagicMock(), service.context)
    store = SentHistory(tmp_path / "sent.db", 0)
    capture = SendCapture(service.plugin, store)
    capture.install_group_context()
    try:
        incoming = event(chain=[Plain("旧图"), Image.fromFileSystem(png(tmp_path))])
        await manager.handle_message(incoming)
        text = list(manager.raw_records[incoming.unified_msg_origin])[0]
        assert text.count(MESSAGE_PREFIX) == 1 and "img_" in text
        window = store.group_window(incoming.unified_msg_origin, "branch1", 42)
        assert len(window) == 1 and window[0]["text"] == text
    finally:
        capture.close()
        store.close()


@pytest.mark.parametrize("platform", ["aiocqhttp", "qq_official"])
async def test_acknowledged_plugin_image_is_cached_as_id_not_raw_attachment(
    service, tmp_path, platform
):
    incoming = event(platform, chain=[Plain("抽卡")])
    platform_obj = NS(meta=lambda: PlatformMetadata(platform, platform, platform))
    service.context.platform_manager.platform_insts = [platform_obj]
    store = SentHistory(tmp_path / "sent.db", 10)
    capture = SendCapture(service.plugin, store)
    try:
        chain = MessageChain([Plain("抽卡结果"), Image.fromFileSystem(png(tmp_path))])
        envelope = await capture.prepare(incoming.unified_msg_origin, chain, incoming)
        await capture.finish(envelope)
        assert store.records(incoming.unified_msg_origin, "branch1") == []
        envelope["receipts"].add("successful-send")
        await capture.finish(envelope)
        record = store.records(incoming.unified_msg_origin, "branch1")[0]
        assert isinstance(record["content"], str) and "img_" in record["content"]
        assert "base64" not in record["content"]
        await service.capture(incoming)
        listed = json.loads(await service.list_images(incoming))["images"][0]
        assert listed["sender_is_bot"] and listed["message_id"] == "successful-send"
    finally:
        capture.close()
        store.close()


async def test_disabled_platform_and_session_plugin_selection(service, tmp_path):
    incoming = event(chain=[Image.fromFileSystem(png(tmp_path))])
    service.context.get_config.return_value = {"plugin_set": ["another_plugin"]}
    await service.capture(incoming)
    assert incoming.get_extra(EVENT_KEY) is None
    assert (await service.view_images(incoming, ["img_unknown"])).isError


async def test_lifecycle_restores_wrappers_and_keeps_files_across_reload(
    service, tmp_path
):
    incoming = event(chain=[Image.fromFileSystem(png(tmp_path))])
    await service.capture(incoming)
    identifier = incoming.get_extra(EVENT_KEY)["images"][0]["id"]
    group_class, original, wrapper = service.group_patch
    await service.terminate()
    assert group_class.handle_message is original
    assert service.session.closed and not service.capture_tasks
    reloaded = HistoryImages(service.plugin, {}, service.cache_root)
    await reloaded.initialize()
    try:
        later = event(mid="later")
        await reloaded.capture(later)
        assert not (await reloaded.view_images(later, [identifier])).isError
    finally:
        await reloaded.terminate()


async def test_original_plugin_image_survives_result_decoration(service, tmp_path):
    store = SentHistory(tmp_path / "sent.db", 10)
    capture = SendCapture(service.plugin, store)
    incoming = event()
    service.context.platform_manager.platform_insts = [
        NS(meta=lambda: PlatformMetadata("aiocqhttp", "QQ", "aiocqhttp"))
    ]
    red = png(tmp_path, "original.png", "red")
    blue = png(tmp_path, "decoration.png", "blue")
    result = MessageEventResult(chain=[Image.fromFileSystem(red)])
    handler = AsyncMock(return_value=result)
    try:
        wrapped = capture.wrap_handler(
            handler, {"name": "draw", "display_name": "抽卡", "version": "1"}
        )
        assert await wrapped(incoming) is result
        incoming.set_result(result)
        result.chain = [Image.fromFileSystem(blue)]
        envelope = await capture.prepare(incoming.unified_msg_origin, result, incoming)
        envelope["receipts"].add("delivered")
        await capture.finish(envelope)
        await service.capture(incoming)
        identifier = json.loads(await service.list_images(incoming))["images"][0]["id"]
        preview = await service.view_images(incoming, [identifier])
        with PILImage.open(
            io.BytesIO(base64.b64decode(preview.content[1].data))
        ) as image:
            assert image.getpixel((0, 0))[0] > 200
    finally:
        capture.close()
        store.close()
