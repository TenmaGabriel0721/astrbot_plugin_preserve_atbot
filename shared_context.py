"""Share AstrBot's group context budget with chronological plugin speech."""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from typing import Any

from astrbot.core.agent.message import TextPart
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType

PLUGIN_NAME = "astrbot_plugin_preserve_atbot"
GROUP_HEADER = (
    "<system_reminder>You are in a group chat. "
    "Belows are group chat context after your last reply:\n--- BEGIN CONTEXT---\n"
)
SHARED_HEADER = "<system_reminder>Recent group chat context:\n--- BEGIN CONTEXT---\n"
GROUP_FOOTER = "\n--- END CONTEXT ---\n</system_reminder>"
GROUP_BLOCK = re.compile(
    "(?:"
    + re.escape(GROUP_HEADER)
    + "|"
    + re.escape(SHARED_HEADER)
    + ")"
    + ".*?"
    + re.escape(GROUP_FOOTER),
    re.DOTALL,
)


def shared_group_limit(context: Any, umo: str) -> int:
    """Resolve the shared budget for this Bot and group only.

    Args:
        context: AstrBot's configuration-aware plugin context.
        umo: The target bot and conversation origin.

    Returns:
        The positive native group limit, or zero for private/disabled scopes.
    """
    try:
        session = MessageSession.from_str(umo)
    except (ValueError, TypeError):
        return 0
    if session.message_type != MessageType.GROUP_MESSAGE:
        return 0
    config = context.get_config(umo)
    selected = config.get("plugin_set", ["*"])
    if not isinstance(selected, list) or not (
        "*" in selected or PLUGIN_NAME in selected
    ):
        return 0
    settings = config.get("provider_ltm_settings", {})
    if not isinstance(settings, Mapping) or not settings.get("group_icl_enable", False):
        return 0
    try:
        limit = int(settings.get("group_message_max_cnt", 300))
    except (TypeError, ValueError):
        limit = 300
    return limit if limit > 0 else 300


def apply_group_window(req: Any, window: list[dict]) -> None:
    """Replace supplemental group blocks with the retained shared window.

    Args:
        req: The request after AstrBot's built-in group context hook.
        window: Chronological group and sent entries selected under one shared budget.
    """
    history = copy.deepcopy(req.contexts or [])
    cleaned = []
    removed_user = False
    for message in history:
        role = message.get("role")
        if role == "_checkpoint" and removed_user:
            continue
        removed_user = False
        if role == "user":
            content = message.get("content")
            if isinstance(content, str):
                message["content"] = GROUP_BLOCK.sub("", content)
            elif isinstance(content, list):
                parts = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        part["text"] = GROUP_BLOCK.sub("", part.get("text", ""))
                        if not part["text"]:
                            continue
                    parts.append(part)
                message["content"] = parts
            if content and not message.get("content"):
                removed_user = True
                continue
        cleaned.append(message)
    req.contexts = cleaned
    parts = []
    for part in req.extra_user_content_parts or []:
        if isinstance(part, TextPart):
            text = GROUP_BLOCK.sub("", part.text)
            if text:
                parts.append(TextPart(text=text))
        else:
            parts.append(part)
    groups = [entry["text"] for entry in window if entry["kind"] == "group"]
    if groups:
        parts.append(TextPart(text=SHARED_HEADER + "\n".join(groups) + GROUP_FOOTER))
    req.extra_user_content_parts = parts
