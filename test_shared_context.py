"""Verify a combined budget using native group collection and plugin history."""

import copy
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from astrbot.api.message_components import Plain
from astrbot.builtin_stars.astrbot.group_chat_context import GroupChatContext
from astrbot.core.agent.message import (
    TextPart,
    bind_checkpoint_messages,
    dump_messages_with_checkpoints,
)
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.message_type import MessageType
from data.plugins.astrbot_plugin_preserve_atbot.main import PreserveAtBotPlugin
from data.plugins.astrbot_plugin_preserve_atbot.send_capture import SendCapture
from data.plugins.astrbot_plugin_preserve_atbot.sent_history import (
    SentHistory,
    message_text,
)
from data.plugins.astrbot_plugin_preserve_atbot.shared_context import (
    GROUP_FOOTER,
    GROUP_HEADER,
    SHARED_HEADER,
    apply_group_window,
    shared_group_limit,
)

UMO = "onebot:GroupMessage:42"
CID = "branch1"


@pytest.fixture
def store(tmp_path):
    history = SentHistory(tmp_path / "sent.db", 0)
    yield history
    history.close()


@pytest.fixture
def context():
    value = MagicMock()
    value.get_config.return_value = {
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
    value.get_all_stars.return_value = []
    value.conversation_manager.get_curr_conversation_id = AsyncMock(return_value=CID)
    value.conversation_manager.get_conversation = AsyncMock(
        return_value=SimpleNamespace(history="[]")
    )
    return value


def event_for(index, text):
    """Build an event accepted by AstrBot's real group collector.

    Args:
        index: Stable event identifier.
        text: Message content, which can include newlines.

    Returns:
        A minimal group event with working per-event extras.
    """
    extras = {}
    return SimpleNamespace(
        unified_msg_origin=UMO,
        message_obj=SimpleNamespace(
            message_id=str(index), sender=SimpleNamespace(nickname="Alice")
        ),
        get_message_type=lambda: MessageType.GROUP_MESSAGE,
        get_messages=lambda: [Plain(text)],
        get_extra=lambda key, default=None: extras.get(key, default),
        set_extra=lambda key, value: extras.__setitem__(key, value),
    )


def test_interleaved_entries_share_exactly_42_slots_and_survive_reload(store):
    expected = []
    for index in range(30):
        group_id = f"group-{index}"
        store.record_group(UMO, CID, group_id, f"group text {index}\nsecond line", 42)
        expected.append(group_id)
        expected.append(store.append(UMO, CID, {"content": f"plugin {index}"}, 42))
    window = store.group_window(UMO, CID, 42)
    assert [entry["entry_id"] for entry in window] == expected[-42:]
    assert len(store.records(UMO, CID)) == 21
    path = store.db.execute("PRAGMA database_list").fetchone()[2]
    reopened = SentHistory(Path(path), 0)
    try:
        assert reopened.group_window(UMO, CID, 42) == window
    finally:
        reopened.close()


def test_new_group_messages_evict_old_plugin_messages_permanently(store):
    store.append(UMO, CID, {"content": "old plugin"}, 42)
    old_history = store.merge([], UMO, CID)
    for index in range(42):
        store.record_group(UMO, CID, f"group-{index}", str(index), 42)
    assert store.records(UMO, CID) == []
    assert store.merge(old_history, UMO, CID) == []
    assert store.merge([], UMO, CID) == []


def test_branch_bot_private_and_reset_isolation(store):
    for umo, cid in [
        (UMO, CID),
        (UMO, "branch2"),
        ("official:GroupMessage:42", CID),
        ("onebot:FriendMessage:42", CID),
    ]:
        store.append(umo, cid, {"content": "isolated"})
    store.record_group(UMO, CID, "group", "new", 1)
    assert store.records(UMO, CID) == []
    assert len(store.records(UMO, "branch2")) == 1
    assert len(store.records("official:GroupMessage:42", CID)) == 1
    assert len(store.records("onebot:FriendMessage:42", CID)) == 1
    store.clear(UMO)
    assert store.group_window(UMO, CID, 42) == []
    assert len(store.records("official:GroupMessage:42", CID)) == 1


def test_personal_plugin_limit_and_shared_limit_both_apply(store):
    store.limit = 2
    for index in range(8):
        store.append(UMO, CID, {"content": str(index)}, 42)
    for index in range(41):
        store.record_group(UMO, CID, f"g-{index}", str(index), 42)
    assert len(store.group_window(UMO, CID, 42)) == 42
    assert [record["content"] for record in store.records(UMO, CID)] == ["7"]


def test_snapshot_excludes_sends_after_budget_selection(store):
    first_id = store.append(UMO, CID, {"content": "first"})
    store.record_group(UMO, CID, "group", "group", 2)
    store.append(UMO, CID, {"content": "concurrent"})
    merged = store.merge([], UMO, CID, allowed_record_ids={first_id})
    assert [
        message_text(item).split("\n\n")[0]
        for item in merged
        if item["role"] == "assistant"
    ] == ["first"]


@pytest.mark.parametrize(
    "limit,expected",
    [(42, 42), (7, 7), ("9", 9), (0, 300), (-1, 300), ("invalid", 300)],
)
def test_native_limit_validation(context, limit, expected):
    context.get_config.return_value["provider_ltm_settings"][
        "group_message_max_cnt"
    ] = limit
    assert shared_group_limit(context, UMO) == expected


def test_disabled_and_unselected_scopes_do_not_share_budget(context):
    assert shared_group_limit(context, "onebot:FriendMessage:42") == 0
    context.get_config.return_value["provider_ltm_settings"]["group_icl_enable"] = False
    assert shared_group_limit(context, UMO) == 0
    context.get_config.return_value["provider_ltm_settings"]["group_icl_enable"] = True
    context.get_config.return_value["plugin_set"] = []
    assert shared_group_limit(context, UMO) == 0


def test_replaces_old_blocks_preserving_user_text_images_and_tool_history():
    old_block = GROUP_HEADER + "old group\nmultiline" + GROUP_FOOTER
    shared_block = SHARED_HEADER + "old shared" + GROUP_FOOTER
    contexts = [
        {"role": "user", "content": "original prompt\n" + old_block},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": shared_block},
                {
                    "type": "image_url",
                    "image_url": {"url": "https://example.com/image.png"},
                },
            ],
        },
        {"role": "assistant", "content": "answer", "tool_calls": [{"id": "call1"}]},
        {"role": "tool", "tool_call_id": "call1", "content": "tool result"},
    ]
    before = copy.deepcopy(contexts)
    req = SimpleNamespace(
        contexts=contexts,
        extra_user_content_parts=[
            TextPart(text="quote kept"),
            TextPart(text=old_block),
        ],
    )
    apply_group_window(req, [{"kind": "group", "text": "new\nmultiline"}])
    assert contexts == before
    assert req.contexts[0]["content"] == "original prompt\n"
    assert req.contexts[1]["content"] == before[1]["content"][1:]
    assert req.contexts[2:] == before[2:]
    assert [part.text for part in req.extra_user_content_parts] == [
        "quote kept",
        SHARED_HEADER + "new\nmultiline" + GROUP_FOOTER,
    ]


@pytest.mark.asyncio
async def test_native_collection_send_capture_and_model_hook_share_one_budget(
    context, store
):
    plugin = object.__new__(PreserveAtBotPlugin)
    plugin.context = context
    plugin.name = "astrbot_plugin_preserve_atbot"
    plugin.sent_history = store
    plugin._complete_quote_senders = AsyncMock()
    plugin._preserve_at_bot = AsyncMock()
    capture = SendCapture(plugin, store)
    manager = GroupChatContext(None, context)
    original = GroupChatContext.handle_message
    capture.install_group_context()
    try:
        for index in range(30):
            event = event_for(index, f"group-{index}\ncontinuation")
            await manager.handle_message(event)
            envelope = await capture.prepare(
                UMO, MessageChain().message(f"plugin-{index}")
            )
            envelope["receipts"].add(str(index))
            await capture.finish(envelope)
        current = event_for("current", "current prompt")
        await manager.handle_message(current)
        req = SimpleNamespace(
            prompt="current prompt",
            conversation=SimpleNamespace(cid=CID),
            contexts=[],
            extra_user_content_parts=[],
        )
        await manager.on_req_llm(current, req)
        assert req.extra_user_content_parts
        await plugin.enhance_context(current, req)
        window = store.group_window(UMO, CID, 42)
        groups = [item for item in window if item["kind"] == "group"]
        plugins = [item for item in req.contexts if item["role"] == "assistant"]
        assert len(groups) + len(plugins) <= 42
        assert len(plugins) == len([item for item in window if item["kind"] == "sent"])
        block = req.extra_user_content_parts[-1].text
        assert (
            block
            == SHARED_HEADER + "\n".join(item["text"] for item in groups) + GROUP_FOOTER
        )
        assert "current prompt" not in block
        assert "group-0\n" not in block
        assert "plugin-0\n" not in "\n".join(message_text(item) for item in plugins)
        persisted = dump_messages_with_checkpoints(
            bind_checkpoint_messages(req.contexts)
        )
        persisted.append({"role": "user", "content": "current prompt\n" + block})
        for index in range(42):
            await manager.handle_message(
                event_for(f"new-{index}", f"new group {index}")
            )
        later = event_for("later", "later prompt")
        await manager.handle_message(later)
        second = SimpleNamespace(
            prompt="later prompt",
            conversation=SimpleNamespace(cid=CID),
            contexts=persisted,
            extra_user_content_parts=[],
        )
        await manager.on_req_llm(later, second)
        await plugin.enhance_context(later, second)
        assert not any(item["role"] == "assistant" for item in second.contexts)
        assert "--- BEGIN CONTEXT---" not in "\n".join(
            message_text(item) for item in second.contexts
        )
        assert second.contexts[-1]["content"] == "current prompt\n"
        assert store.records(UMO, CID) == []
    finally:
        capture.close()
    assert GroupChatContext.handle_message is original


def test_upgrade_backfills_existing_plugin_history_without_duplicates(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE sent_messages (sequence INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT UNIQUE, umo TEXT NOT NULL, conversation_id TEXT, payload TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO sent_messages(record_id,umo,conversation_id,payload) VALUES (?,?,?,?)",
            ("legacy", UMO, CID, '{"content":"legacy reply"}'),
        )
    for _ in range(2):
        history = SentHistory(path, 0)
        try:
            assert history.group_window(UMO, CID, 42) == [
                {"entry_id": "legacy", "kind": "sent", "text": None}
            ]
        finally:
            history.close()
