import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
from datetime import datetime

from .config_runtime import (
    AGENT_NOTIFY_CHAT_ID,
    AGENT_NOTIFY_DIR,
    AGENT_NOTIFY_ENABLED,
    AGENT_NOTIFY_OPEN_ID,
    AGENT_NOTIFY_POLL_SECONDS,
    AGENT_NOTIFY_STABLE_SECONDS,
    NOTIFY_CHAT_ID,
    NOTIFY_OPEN_ID,
)
from .messaging import remove_reaction, send_card_to_chat, send_card_to_open_id, send_topic_notification, send_topic_root
from .paths import get_runtime_data_dir

_topic_lock = threading.Lock()


def get_agent_notify_dir() -> str:
    raw = str(AGENT_NOTIFY_DIR or "").strip()
    if raw:
        return os.path.abspath(raw)
    return os.path.join(get_runtime_data_dir(), "agent_notify")


def _safe_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()
    return cleaned or "agent-notify.txt"


def _archive_path(root: str, folder: str, path: str) -> str:
    target_dir = os.path.join(root, folder)
    os.makedirs(target_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(target_dir, f"{stamp}_{_safe_name(os.path.basename(path))}")


def _is_file_stable(path: str) -> bool:
    stable_seconds = max(1, int(AGENT_NOTIFY_STABLE_SECONDS or 2))
    try:
        stat = os.stat(path)
    except OSError:
        return False
    return stat.st_size > 0 and time.time() - stat.st_mtime >= stable_seconds


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        return f.read().strip()


def _load_notification(path: str) -> dict:
    text = _read_text(path)
    if not text:
        raise ValueError("通知文件为空")
    if path.lower().endswith(".json"):
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("JSON 通知必须是对象")
        return data
    lines = text.splitlines()
    return {
        "title": lines[0].strip() if lines else "Agent 通知",
        "message": "\n".join(lines[1:]).strip() if len(lines) > 1 else text,
    }


def _compact(value) -> str:
    return str(value or "").strip()


def format_agent_notification(data: dict, source_file: str) -> str:
    source = _compact(data.get("source")) or "local-agent"
    status = _compact(data.get("status")) or "info"
    title = _compact(data.get("title")) or _compact(data.get("summary")) or "Agent 任务通知"
    message = _compact(data.get("message")) or _compact(data.get("text")) or _compact(data.get("body"))
    job_id = _compact(data.get("job_id")) or _compact(data.get("task_id"))
    conversation_id = (
        _compact(data.get("conversation_id"))
        or _compact(data.get("conversationId"))
        or _compact(data.get("session_id"))
        or _compact(data.get("thread_id"))
    )
    cwd = _compact(data.get("cwd")) or _compact(data.get("workspace"))
    url = _compact(data.get("url")) or _compact(data.get("link"))

    icon = "✅" if status.lower() in {"ok", "done", "success", "completed"} else "⚠️" if status.lower() in {"fail", "failed", "error", "timeout"} else "ℹ️"
    lines = [f"{icon} **{title}**", "", f"- 来源：{source}", f"- 状态：{status}"]
    if job_id:
        lines.append(f"- 任务：{job_id}")
    if conversation_id:
        lines.append(f"- 对话：{conversation_id}")
    if cwd:
        lines.append(f"- 目录：{cwd}")
    if url:
        lines.append(f"- 链接：{url}")
    if message:
        lines.extend(["", message])
    lines.append(f"\n文件：{os.path.basename(source_file)}")
    return "\n".join(lines)


def _conversation_id(data: dict) -> str:
    for key in ("conversation_id", "conversationId", "session_id", "thread_id"):
        value = _compact(data.get(key))
        if value and value.lower() not in {"unknown", "last", "latest"}:
            return value
    return ""


def _save_topic_roots(path: str, roots: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=os.path.dirname(path), suffix=".tmp", delete=False) as f:
            temp_path = f.name
            json.dump(roots, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(temp_path, path)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


def _notify_topic(receive_id_type: str, receive_id: str, text: str, data: dict, source_file: str):
    source = _compact(data.get("source")).lower() or "local-agent"
    conversation_id = _conversation_id(data)
    notification_id = hashlib.sha256(json.dumps(
        [os.path.basename(source_file), data], ensure_ascii=False, sort_keys=True,
    ).encode("utf-8")).hexdigest()
    # Missing IDs must never merge unrelated conversations into one topic.
    conversation_key = ["conversation", conversation_id] if conversation_id else ["notification", notification_id]
    key = json.dumps([receive_id_type, receive_id, source, conversation_key], ensure_ascii=False)
    delivery_key = key + ":" + notification_id
    path = os.path.join(get_runtime_data_dir(), "agent_notification_threads.json")
    with _topic_lock:
        try:
            with open(path, "r", encoding="utf-8") as f:
                roots = json.load(f)
        except FileNotFoundError:
            roots = {}
        if not isinstance(roots, dict):
            raise ValueError("Agent 话题索引格式错误")
        entry = roots.get(key)
        root_id = entry.get("root_id") if isinstance(entry, dict) else entry
        if root_id is not None and (not isinstance(root_id, str) or not root_id.startswith("om_")):
            raise ValueError("Agent 话题根消息 ID 无效")
        if not root_id:
            title = (_compact(data.get("title")) or _compact(data.get("summary")) or "Agent 对话")[:200]
            root_text = f"{source} · {title}"
            if conversation_id:
                root_text += f"\n对话：{conversation_id}"
            root_id = send_topic_root(receive_id_type, receive_id, root_text, delivery_key)
            if not root_id:
                raise RuntimeError("创建 Agent 话题根消息失败")
        cwd = _compact(data.get("cwd")) or _compact(data.get("workspace"))
        updated = {"root_id": root_id, "cwd": cwd or (entry.get("cwd", "") if isinstance(entry, dict) else "")}
        if entry != updated:
            roots[key] = updated
            # Save before replying so a retry or restart can reuse the root.
            _save_topic_roots(path, roots)
        if not send_topic_notification(root_id, text, delivery_key):
            raise RuntimeError(f"发送 Agent 话题回复失败：{root_id}")



def find_agent_topic(root_id: str, chat_id: str, sender_open_id: str) -> dict | None:
    if not root_id:
        return None
    path = os.path.join(get_runtime_data_dir(), "agent_notification_threads.json")
    with _topic_lock:
        try:
            with open(path, "r", encoding="utf-8") as f:
                roots = json.load(f)
        except FileNotFoundError:
            return None
    if not isinstance(roots, dict):
        raise ValueError("Agent 话题索引格式错误")
    for key, entry in roots.items():
        saved_root = entry.get("root_id") if isinstance(entry, dict) else entry
        if saved_root != root_id:
            continue
        kind, target, source, conversation = json.loads(key)
        if source in {"codex", "codex-cli", "codex-desktop"}:
            agent = "codex"
        elif source in {"claude", "claude-code", "claude-cli", "claude-desktop"}:
            agent = "claude"
        else:
            continue
        if conversation[0] != "conversation":
            continue
        if not ((kind == "open_id" and target == sender_open_id) or (kind == "chat_id" and target == chat_id)):
            continue
        return {"agent": agent, "root_id": root_id, "conversation_id": conversation[1],
                "cwd": entry.get("cwd", "") if isinstance(entry, dict) else ""}
    return None


def _notify(text: str, data: dict | None = None, source_file: str = ""):
    targets = [
        ("chat_id", AGENT_NOTIFY_CHAT_ID or NOTIFY_CHAT_ID),
        ("open_id", AGENT_NOTIFY_OPEN_ID or NOTIFY_OPEN_ID),
    ]
    targets = [(kind, value) for kind, value in targets if value]
    if not targets:
        raise RuntimeError("未配置 Agent 通知目标")
    for kind, value in targets:
        if data is not None:
            _notify_topic(kind, value, text, data, source_file)
        else:
            send = send_card_to_chat if kind == "chat_id" else send_card_to_open_id
            if not send(value, text):
                raise RuntimeError("发送 Agent 通知失败")


def process_agent_notification_file(path: str):
    root = get_agent_notify_dir()
    try:
        data = _load_notification(path)
        if data.get("kind") == "claude_topic_reply":
            from .claude_replies import get_reply_store

            get_reply_store().reply(data["request_id"], data["message"], send_topic_notification, remove_reaction)
        else:
            text = format_agent_notification(data, path)
            _notify(text, data, path)
        archived = _archive_path(root, "processed", path)
        shutil.move(path, archived)
        print(f"[AGENT_NOTIFY] 已通知: {path}")
    except Exception as e:
        failed = _archive_path(root, "failed", path)
        try:
            shutil.move(path, failed)
            with open(failed + ".error.txt", "w", encoding="utf-8", newline="\n") as f:
                f.write(str(e))
        except Exception:
            pass
        try:
            _notify(f"⚠️ **Agent 通知处理失败**\n\n- 文件：{os.path.basename(path)}\n- 错误：{e}")
        except Exception as notify_error:
            print(f"[AGENT_NOTIFY] 失败提醒发送失败: {notify_error}")
        print(f"[AGENT_NOTIFY] 处理失败: {path}: {e}")


def scan_agent_notify_dir():
    root = get_agent_notify_dir()
    os.makedirs(root, exist_ok=True)
    os.makedirs(os.path.join(root, "processed"), exist_ok=True)
    os.makedirs(os.path.join(root, "failed"), exist_ok=True)
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if not os.path.isfile(path):
            continue
        if not name.lower().endswith((".json", ".txt")):
            continue
        if _is_file_stable(path):
            process_agent_notification_file(path)
    # Retry only reaction cleanup; never resend a completed answer.
    if os.path.isfile(os.path.join(get_runtime_data_dir(), "claude_topic_requests.sqlite3")):
        from .claude_replies import get_reply_store

        get_reply_store().clear_replied_reactions(remove_reaction)


def start_agent_notify_watcher():
    if not AGENT_NOTIFY_ENABLED:
        return

    def _loop():
        root = get_agent_notify_dir()
        print(f"[AGENT_NOTIFY] 已启用 Agent 通知投递: {root}")
        while True:
            try:
                scan_agent_notify_dir()
            except Exception as e:
                print(f"[AGENT_NOTIFY] 扫描异常: {e}")
            time.sleep(max(1, int(AGENT_NOTIFY_POLL_SECONDS or 5)))

    threading.Thread(target=_loop, daemon=True).start()
