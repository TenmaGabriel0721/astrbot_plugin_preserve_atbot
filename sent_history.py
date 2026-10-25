"""Plugin speech records with optional limits merged into ordinary messages."""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

CHECKPOINT_PREFIX = "preserve_atbot:sent:"
TRIGGER_PREFIX = "preserve_atbot:trigger:"


def message_text(message: dict) -> str:
    """Read text from either representation of a conversation message.

    Args:
        message: A normal AstrBot conversation message.

    Returns:
        The concatenated text content, excluding media parts.
    """
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def fingerprint(message: dict) -> str:
    """Identify a history anchor without depending on serializer defaults.

    Args:
        message: A conversation message or checkpoint.

    Returns:
        A stable digest of its meaningful content and tool identifiers.
    """
    value = {
        "role": message.get("role"),
        "text": message_text(message),
        "tool_calls": message.get("tool_calls") or None,
        "tool_call_id": message.get("tool_call_id") or None,
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class SentHistory:
    """Store the most recent plugin replies without rewriting core history."""

    def __init__(self, path: Path, limit: int = 10) -> None:
        self.limit = max(0, min(int(limit), 100))
        self.lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS sent_messages ("
            "sequence INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT UNIQUE, "
            "umo TEXT NOT NULL, conversation_id TEXT, payload TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS sent_scope ON sent_messages(umo, conversation_id)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS shared_context ("
            "sequence INTEGER PRIMARY KEY AUTOINCREMENT, entry_id TEXT UNIQUE, "
            "kind TEXT NOT NULL, umo TEXT NOT NULL, conversation_id TEXT, text TEXT)"
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS shared_scope ON shared_context(umo, conversation_id)"
        )
        self.db.execute(
            "INSERT OR IGNORE INTO shared_context(entry_id,kind,umo,conversation_id) "
            "SELECT record_id,'sent',umo,conversation_id FROM sent_messages ORDER BY sequence"
        )
        self.db.commit()

    def append(
        self, umo: str, cid: str | None, payload: dict, group_limit: int = 0
    ) -> str:
        """Append a confirmed send, pruning only when the limit is positive.

        Args:
            umo: The exact bot and conversation origin.
            cid: The conversation branch at send time, or None before its creation.
            payload: Source, original content, trigger, receipt, and history anchors.
            group_limit: Shared group context budget; zero disables the shared budget.

        Returns:
            The internal record identifier, hidden from provider-facing messages.
        """
        record_id = uuid.uuid4().hex
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO sent_messages(record_id,umo,conversation_id,payload) "
                "VALUES (?,?,?,?)",
                (record_id, umo, cid, json.dumps(payload, ensure_ascii=False)),
            )
            self.db.execute(
                "INSERT INTO shared_context(entry_id,kind,umo,conversation_id) "
                "VALUES (?,'sent',?,?)",
                (record_id, umo, cid),
            )
            if self.limit > 0:
                self.db.execute(
                    "DELETE FROM sent_messages WHERE umo=? AND conversation_id IS ? "
                    "AND sequence NOT IN (SELECT sequence FROM sent_messages WHERE "
                    "umo=? AND conversation_id IS ? ORDER BY sequence DESC LIMIT ?)",
                    (umo, cid, umo, cid, self.limit),
                )
            self.db.execute(
                "DELETE FROM shared_context WHERE kind='sent' AND umo=? "
                "AND conversation_id IS ? AND entry_id NOT IN "
                "(SELECT record_id FROM sent_messages WHERE umo=? AND conversation_id IS ?)",
                (umo, cid, umo, cid),
            )
        if group_limit > 0:
            self.group_window(umo, cid, group_limit)
        return record_id

    def record_group(
        self, umo: str, cid: str | None, entry_id: str, text: str, limit: int
    ) -> None:
        """Track an incoming group message in the same sequence as plugin sends.

        Args:
            umo: The exact bot and group origin.
            cid: The active conversation branch, or None before its creation.
            entry_id: The identifier assigned by AstrBot's group context collector.
            text: The complete formatted group message, including embedded newlines.
            limit: The current Bot's shared context budget.
        """
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO shared_context(entry_id,kind,umo,conversation_id,text) "
                "VALUES (?,'group',?,?,?)",
                (entry_id, umo, cid, text),
            )
        self.group_window(umo, cid, limit)

    def group_window(
        self, umo: str, cid: str | None, limit: int, current_group_id: str = ""
    ) -> list[dict]:
        """Keep one chronological budget for group context and plugin speech.

        Args:
            umo: The exact bot and group origin.
            cid: The active conversation branch.
            limit: A positive shared message limit from the current Bot configuration.
            current_group_id: Current user prompt, which is not supplemental context.

        Returns:
            Retained entries in chronological order. Evicted plugin records are removed
            so future requests cannot reinsert them after group buffers are consumed.
        """
        if limit <= 0:
            raise ValueError("Shared group context limit must be positive")
        with self.lock, self.db:
            if cid is not None:
                for table in ("sent_messages", "shared_context"):
                    self.db.execute(
                        f"UPDATE {table} SET conversation_id=? WHERE umo=? "
                        "AND conversation_id IS NULL",
                        (cid, umo),
                    )
            if current_group_id:
                self.db.execute(
                    "DELETE FROM shared_context WHERE umo=? AND conversation_id IS ? "
                    "AND kind='group' AND entry_id=?",
                    (umo, cid, current_group_id),
                )
            self.db.execute(
                "DELETE FROM shared_context WHERE umo=? AND conversation_id IS ? "
                "AND sequence NOT IN (SELECT sequence FROM shared_context WHERE "
                "umo=? AND conversation_id IS ? ORDER BY sequence DESC LIMIT ?)",
                (umo, cid, umo, cid, limit),
            )
            self.db.execute(
                "DELETE FROM sent_messages WHERE umo=? AND conversation_id IS ? "
                "AND record_id NOT IN (SELECT entry_id FROM shared_context "
                "WHERE umo=? AND conversation_id IS ? AND kind='sent')",
                (umo, cid, umo, cid),
            )
            rows = self.db.execute(
                "SELECT entry_id,kind,text FROM shared_context WHERE umo=? "
                "AND conversation_id IS ? ORDER BY sequence",
                (umo, cid),
            ).fetchall()
        return [
            {"entry_id": entry_id, "kind": kind, "text": text}
            for entry_id, kind, text in rows
        ]

    def clear(self, umo: str) -> None:
        """Clear records when AstrBot deletes a conversation scope.

        Args:
            umo: The deleted unified message origin.
        """
        with self.lock, self.db:
            self.db.execute("DELETE FROM sent_messages WHERE umo=?", (umo,))
            self.db.execute("DELETE FROM shared_context WHERE umo=?", (umo,))

    def records(self, umo: str, cid: str) -> list[dict]:
        """Read recent speech for exactly one bot, chat, and conversation branch.

        Args:
            umo: The current message origin.
            cid: The active conversation branch.

        Returns:
            Recent records ordered by accepted-send time.
        """
        with self.lock, self.db:
            # Initial sends bind only when that scope first acquires a conversation.
            self.db.execute(
                "UPDATE sent_messages SET conversation_id=? WHERE umo=? "
                "AND conversation_id IS NULL",
                (cid, umo),
            )
            self.db.execute(
                "UPDATE shared_context SET conversation_id=? WHERE umo=? "
                "AND conversation_id IS NULL",
                (cid, umo),
            )
            rows = self.db.execute(
                "SELECT record_id,payload FROM sent_messages WHERE umo=? "
                "AND conversation_id=? ORDER BY sequence DESC LIMIT ?",
                (umo, cid, self.limit or -1),
            ).fetchall()
        return [
            {"record_id": record_id, **json.loads(payload)}
            for record_id, payload in reversed(rows)
        ]

    def merge(
        self,
        history: list[dict],
        umo: str,
        cid: str,
        current_event_id: str = "",
        *,
        allowed_record_ids: set[str] | None = None,
    ) -> list[dict]:
        """Insert plugin speech at its history boundary as normal assistant messages.

        Args:
            history: Core history containing ordinary messages and internal checkpoints.
            umo: The current bot/chat origin.
            cid: The current conversation branch.
            current_event_id: The user message currently supplied through req.prompt.
            allowed_record_ids: A shared-window snapshot, preventing concurrent sends
                from exceeding the combined group and plugin message budget.

        Returns:
            A copy of history with deduplicated chronological plugin speech.
        """
        records = self.records(umo, cid)
        if allowed_record_ids is not None:
            records = [
                record
                for record in records
                if record["record_id"] in allowed_record_ids
            ]
        active_ids = {record["record_id"] for record in records}
        result = copy.deepcopy(history)
        present = set()
        trigger_ids = set()
        index = len(result) - 1
        while index >= 0:
            item = result[index]
            if item.get("role") != "_checkpoint":
                index -= 1
                continue
            identifier = (item.get("content") or {}).get("id", "")
            if identifier.startswith(TRIGGER_PREFIX):
                trigger_ids.add(identifier[len(TRIGGER_PREFIX) :])
            elif identifier.startswith(CHECKPOINT_PREFIX):
                record_id = identifier[len(CHECKPOINT_PREFIX) :]
                if record_id in active_ids:
                    present.add(record_id)
                elif index and result[index - 1].get("role") == "assistant":
                    del result[index - 1 : index + 1]
                    index -= 2
                    continue
            index -= 1

        # Keep records sharing an anchor in send order rather than reversing insertions.
        last_inserted_by_anchor: dict[str, str] = {}
        for record in records:
            record_id = record["record_id"]
            anchors = record.get("anchors") or []
            anchor_key = json.dumps(anchors)
            trigger_id = record.get("trigger_id") or ""
            if record_id in present:
                last_inserted_by_anchor[anchor_key] = record_id
                if trigger_id:
                    trigger_ids.add(trigger_id)
                continue
            if current_event_id and trigger_id == current_event_id:
                continue
            boundary = 0 if not anchors else len(result)
            previous_id = last_inserted_by_anchor.get(anchor_key)
            if previous_id:
                for index, item in enumerate(result):
                    if (
                        item.get("role") == "_checkpoint"
                        and (item.get("content") or {}).get("id")
                        == CHECKPOINT_PREFIX + previous_id
                    ):
                        boundary = index + 1
                        break
            else:
                for anchor in reversed(anchors):
                    found = [
                        index
                        for index, item in enumerate(result)
                        if item.get("role") != "_checkpoint"
                        and fingerprint(item) == anchor
                    ]
                    if found:
                        boundary = found[-1] + 1
                        while (
                            boundary < len(result)
                            and result[boundary].get("role") == "_checkpoint"
                        ):
                            boundary += 1
                        break

            trigger = record.get("trigger_text") or ""
            insert = []
            if trigger and trigger_id not in trigger_ids:
                # Match only the next user at the recorded boundary, not an old command.
                if (
                    boundary < len(result)
                    and result[boundary].get("role") == "user"
                    and message_text(result[boundary]).rstrip().endswith(trigger)
                ):
                    boundary += 1
                    while (
                        boundary < len(result)
                        and result[boundary].get("role") == "_checkpoint"
                    ):
                        boundary += 1
                else:
                    insert.extend(
                        [
                            {"role": "user", "content": trigger},
                            {
                                "role": "_checkpoint",
                                "content": {"id": TRIGGER_PREFIX + trigger_id},
                            },
                        ]
                    )
                trigger_ids.add(trigger_id)

            # Never separate an assistant tool call from its tool results.
            if boundary < len(result) and result[boundary].get("role") == "tool":
                while boundary < len(result) and result[boundary].get("role") in {
                    "tool",
                    "_checkpoint",
                }:
                    boundary += 1
            source = record.get("source") or {}
            name = source.get("display_name") or source.get("name") or "来源未知"
            version = source.get("version") or "未知"
            sent_at = record.get("sent_at") or datetime.now(timezone.utc).isoformat()
            annotation = f"\n\n【插件发言来源：{name}；插件：{source.get('name') or '未知'}；版本：{version}；发送时间：{sent_at}】"
            if record.get("partial_send"):
                annotation += "\n【发送状态：部分消息获得回执，原始内容可能未全部发送】"
            content = copy.deepcopy(record["content"])
            if isinstance(content, str):
                content += annotation
            else:
                content.append({"type": "text", "text": annotation})
            insert.extend(
                [
                    {"role": "assistant", "content": content},
                    {
                        "role": "_checkpoint",
                        "content": {"id": CHECKPOINT_PREFIX + record_id},
                    },
                ]
            )
            result[boundary:boundary] = insert
            last_inserted_by_anchor[anchor_key] = record_id
        return result

    def close(self) -> None:
        """Close storage after send interceptors have been removed."""
        with self.lock:
            self.db.close()
