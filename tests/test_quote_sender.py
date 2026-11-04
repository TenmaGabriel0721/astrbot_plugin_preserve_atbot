from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.api.message_components import Image, Reply
from astrbot.core.agent.message import TextPart
from astrbot.core.provider.entities import ProviderRequest
from data.plugins.astrbot_plugin_preserve_atbot.main import PreserveAtBotPlugin


def _make_plugin(**config):
    plugin = object.__new__(PreserveAtBotPlugin)
    plugin.enhance_quote_sender = config.get("enhance_quote_sender", True)
    plugin.quote_sender_fallback_name = config.get(
        "quote_sender_fallback_name", "Unknown User"
    )
    plugin.quote_sender_timeout = config.get("quote_sender_timeout", 1.0)
    plugin.debug_log = config.get("debug_log", False)
    return plugin


def _make_event(reply: Reply, call_action):
    extras = {}
    event = SimpleNamespace(
        bot=SimpleNamespace(call_action=call_action),
        get_messages=lambda: [reply],
        get_platform_name=lambda: "aiocqhttp",
        set_extra=lambda key, value: extras.__setitem__(key, value),
    )
    return event, extras


@pytest.mark.asyncio
async def test_complete_quote_sender_and_image_chain():
    reply = Reply(id="42", chain=None, sender_id=0, sender_nickname="")
    call_action = AsyncMock(
        return_value={
            "data": {
                "message_id": 42,
                "user_id": 10001,
                "sender": {"user_id": 10001, "card": "Alice"},
                "message": [
                    {
                        "type": "image",
                        "data": {"file": "https://example.com/image.jpg"},
                    }
                ],
            }
        }
    )
    event, extras = _make_event(reply, call_action)
    req = ProviderRequest(
        prompt="请描述图片",
        extra_user_content_parts=[
            TextPart(text="<Quoted Message>\n[Empty Text]\n</Quoted Message>")
        ],
    )

    await _make_plugin()._complete_quote_senders(event, req)

    assert reply.sender_id == "10001"
    assert reply.sender_nickname == "Alice"
    assert len(reply.chain) == 1
    assert isinstance(reply.chain[0], Image)
    assert req.extra_user_content_parts[0].text == (
        "<Quoted Message>\n(Alice, QQ: 10001): [Empty Text]\n</Quoted Message>"
    )
    assert extras["_preserved_quote_sender"] is True
    call_action.assert_awaited_once_with("get_msg", message_id="42")


@pytest.mark.asyncio
async def test_complete_quote_sender_rewrites_populated_reply_with_qq():
    reply = Reply(id="42", sender_id="10001", sender_nickname="Alice")
    call_action = AsyncMock()
    event, extras = _make_event(reply, call_action)
    req = ProviderRequest(
        extra_user_content_parts=[
            TextPart(text="<Quoted Message>\n(Alice): quoted text\n</Quoted Message>")
        ]
    )

    await _make_plugin()._complete_quote_senders(event, req)

    call_action.assert_not_awaited()
    assert req.extra_user_content_parts[0].text == (
        "<Quoted Message>\n(Alice, QQ: 10001): quoted text\n</Quoted Message>"
    )
    assert extras["_preserved_quote_sender"] is True


@pytest.mark.asyncio
async def test_complete_quote_sender_uses_existing_id_when_nickname_missing():
    reply = Reply(id="42", sender_id="10001", sender_nickname="")
    call_action = AsyncMock(return_value={"data": {"message_id": 42, "message": []}})
    event, extras = _make_event(reply, call_action)
    req = ProviderRequest(
        extra_user_content_parts=[
            TextPart(text="<Quoted Message>\n[Empty Text]\n</Quoted Message>")
        ]
    )

    await _make_plugin()._complete_quote_senders(event, req)

    assert reply.sender_id == "10001"
    assert reply.sender_nickname == "10001"
    assert req.extra_user_content_parts[0].text == (
        "<Quoted Message>\n(10001): [Empty Text]\n</Quoted Message>"
    )
    assert extras["_preserved_quote_sender"] is True


@pytest.mark.asyncio
async def test_complete_quote_sender_times_out_without_mutation():
    reply = Reply(id="42", sender_id=0, sender_nickname="")

    async def slow_call_action(*args, **kwargs):
        await asyncio.sleep(0.1)
        return {}

    event, extras = _make_event(reply, slow_call_action)
    req = ProviderRequest()

    await _make_plugin(quote_sender_timeout=0.01)._complete_quote_senders(event, req)

    assert reply.sender_id == 0
    assert reply.sender_nickname == ""
    assert extras == {}
