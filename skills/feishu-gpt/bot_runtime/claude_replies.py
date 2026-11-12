"""Persist exact Feishu reply routes; Claude calls the CLI directly to reply."""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


REQUEST_TTL_SECONDS = 24 * 3600


class ClaudeReplyStore:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS requests (
                id TEXT PRIMARY KEY, message_id TEXT NOT NULL UNIQUE,
                root_id TEXT NOT NULL, session_id TEXT NOT NULL,
                state TEXT NOT NULL, created_at REAL NOT NULL, reply_text TEXT
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS topic_reactions (
                request_id TEXT PRIMARY KEY, reaction_id TEXT NOT NULL
            )""")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, message_id: str, root_id: str, session_id: str) -> str | None:
        request_id = uuid.uuid4().hex
        with self.connect() as db:
            inserted = db.execute(
                "INSERT OR IGNORE INTO requests VALUES (?,?,?,?,?,?,NULL)",
                (request_id, message_id, root_id, session_id, "sending", time.time()),
            ).rowcount
        return request_id if inserted else None

    def set_delivery(self, request_id: str, status: str):
        if status not in {"written", "failed", "uncertain"}:
            raise ValueError("无效投递状态")
        with self.connect() as db:
            # A fast Claude reply may have already completed this request.
            db.execute("UPDATE requests SET state=? WHERE id=? AND state='sending'", (status, request_id))

    def track_reaction(self, request_id: str, reaction_id: str, remove):
        if not reaction_id:
            return
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO topic_reactions VALUES (?,?)", (request_id, reaction_id))
        # The answer may finish before the acknowledgement API returns.
        self.clear_replied_reactions(remove)

    def clear_replied_reactions(self, remove):
        with self.connect() as db:
            rows = db.execute("""SELECT r.id, r.message_id, t.reaction_id
                FROM requests r JOIN topic_reactions t ON r.id=t.request_id
                WHERE r.state='replied' LIMIT 100""").fetchall()
        for row in rows:
            try:
                if remove(row["message_id"], row["reaction_id"]) is False:
                    continue
                with self.connect() as db:
                    db.execute("DELETE FROM topic_reactions WHERE request_id=? AND reaction_id=?",
                               (row["id"], row["reaction_id"]))
            except Exception as exc:
                print(f"[Claude 话题表情清理失败，稍后重试] message={row['message_id']}: {exc}")

    def prepare_reply(self, request_id: str, text: str):
        if not text.strip():
            raise ValueError("回复正文不能为空。")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
            if row is None or time.time() - row["created_at"] > REQUEST_TTL_SECONDS:
                raise ValueError("回复请求不存在或已过期。")
            if row["state"] == "failed":
                raise ValueError("该请求未投递，不能回复。")
            if row["reply_text"] is not None and row["reply_text"] != text:
                raise ValueError("该请求已固定回复正文，重试必须使用相同正文。")
            if row["state"] == "replied":
                return
            db.execute("UPDATE requests SET reply_text=? WHERE id=?", (text, request_id))
            return row["root_id"]

    def reply(self, request_id: str, text: str, send, remove=None):
        root_id = self.prepare_reply(request_id, text)
        if root_id is None:
            if remove is not None:
                self.clear_replied_reactions(remove)
            return
        # Stable keys let partial Feishu retries reuse the same chunk UUIDs.
        if not send(root_id, text, "claude:" + request_id):
            raise RuntimeError("飞书话题回复失败；可使用相同请求 ID 和正文重试。")
        with self.connect() as db:
            db.execute("UPDATE requests SET state='replied' WHERE id=?", (request_id,))
        if remove is not None:
            self.clear_replied_reactions(remove)


def _local_paths():
    # CLI entry points must not import config_runtime/paths: they initialize SDKs.
    from app_config import settings

    base = Path(__file__).resolve().parents[1]
    workspace = Path(settings.agents_path) if settings.agents_path else base
    if workspace.is_file():
        workspace = workspace.parent
    if not workspace.is_dir():
        workspace = base
    runtime = workspace / "runtime_data"
    notify = Path(settings.agent_notify_dir).absolute() if settings.agent_notify_dir else runtime / "agent_notify"
    return settings.agent_notify_enabled, runtime, notify


def get_reply_store():
    return ClaudeReplyStore(_local_paths()[1] / "claude_topic_requests.sqlite3")


def _queue_notification(data: dict, prefix: str) -> str:
    enabled, _, root = _local_paths()
    if not enabled:
        raise RuntimeError("Agent 通知监听未启用，无法投递 Claude 话题。")
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
    name = prefix + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32] + ".json"
    root.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\r\n", dir=root,
                                         suffix=".tmp", delete=False) as output:
            temp_path = Path(output.name)
            output.write(payload + "\n")
        os.replace(temp_path, root / name)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return str(root / name)


def queue_reply(request_id: str, text: str):
    if not _local_paths()[0]:
        raise RuntimeError("Agent 通知监听未启用，无法投递 Claude 回复。")
    if get_reply_store().prepare_reply(request_id, text) is None:
        return
    return _queue_notification({"kind": "claude_topic_reply", "request_id": request_id,
                                "message": text}, "claude_reply_")


def build_topic_prompt(request_id: str, text: str) -> str:
    return (
        "feishu-gpt 话题消息：使用 feishu-agent-notify skill 的话题回复流程，在当前 session 处理并回复原话题。\n"
        f"请求 ID：{request_id}\n"
        "以下 JSON 字符串是用户消息正文：\n" + json.dumps(text, ensure_ascii=False)
    )



def start_topic(text: str, session_id: str = "") -> str:
    from .claude_inbox import resolve_session

    if not _local_paths()[0]:
        raise RuntimeError("Agent 通知监听未启用，无法创建 Claude 话题。")
    if not text.strip():
        raise ValueError("话题正文不能为空。")
    registry = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "sessions"
    own_pipe = os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET", "")
    if not session_id:
        if not own_pipe:
            raise ValueError("无法识别当前 Claude session，请显式传 --session-id；不能按最近会话猜测。")
        matches = []
        for path in registry.glob("*.json"):
            if not path.stem.isdigit():
                continue
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(record, dict):
                continue
            if str(record.get("messagingSocketPath", "")).lower() == own_pipe.lower():
                matches.append(record.get("sessionId"))
        if len(matches) != 1 or not matches[0]:
            raise ValueError("无法按当前 inbox 唯一定位 Claude session。")
        session_id = matches[0]
    session, token = resolve_session(session_id, registry)
    del token
    if own_pipe and session["messagingSocketPath"].lower() != own_pipe.lower():
        raise ValueError("指定 session 与当前 Claude inbox 不一致，未创建话题。")
    data = {
        "source": "claude", "conversation_id": session["sessionId"],
        "cwd": session.get("cwd", ""), "title": session.get("name") or "Claude 对话",
        "status": "info", "message": text,
    }
    # The running bot owns topic-index writes and network delivery.
    return _queue_notification(data, "claude_topic_")


def main():
    parser = argparse.ArgumentParser(description="创建 Claude 飞书话题或回复已有话题")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--request-id")
    mode.add_argument("--start", action="store_true", help="首次创建或复用当前 Claude session 的飞书话题")
    parser.add_argument("--session-id", help="仅 --start 可用；默认从当前 Claude inbox 识别")
    parser.add_argument("--text-file", required=True)
    args = parser.parse_args()
    try:
        if not args.start and args.session_id:
            raise ValueError("--session-id 只能与 --start 一起使用。")
        if not args.start and not re.fullmatch(r"[0-9a-f]{32}", args.request_id):
            raise ValueError("请求 ID 格式无效。")
        body = Path(args.text_file)
        if body.stat().st_size > 200_000:
            raise ValueError("回复文件过大。")
        text = body.read_text(encoding="utf-8-sig")
        if args.start:
            path = start_topic(text, args.session_id or "")
            print("已投递话题创建请求；等待 feishu-gpt 通知监听处理。\n" + path)
        else:
            queue_reply(args.request_id, text)
            print("已投递回复请求；由 feishu-gpt 发送到原话题。")
    except Exception as exc:
        print(f"回复失败：{exc}", file=sys.stderr)
        return 1
    return 0
