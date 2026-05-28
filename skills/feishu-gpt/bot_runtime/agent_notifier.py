import json
import os
import re
import shutil
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
from .messaging import send_card_to_chat, send_card_to_open_id
from .paths import get_runtime_data_dir


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


def _notify(text: str):
    chat_id = AGENT_NOTIFY_CHAT_ID or NOTIFY_CHAT_ID
    open_id = AGENT_NOTIFY_OPEN_ID or NOTIFY_OPEN_ID
    sent = False
    if chat_id:
        send_card_to_chat(chat_id, text)
        sent = True
    if open_id:
        send_card_to_open_id(open_id, text)
        sent = True
    if not sent:
        print("[AGENT_NOTIFY] 未配置通知目标: " + text.replace("\n", " ")[:200])


def process_agent_notification_file(path: str):
    root = get_agent_notify_dir()
    try:
        data = _load_notification(path)
        text = format_agent_notification(data, path)
        _notify(text)
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
        _notify(f"⚠️ **Agent 通知处理失败**\n\n- 文件：{os.path.basename(path)}\n- 错误：{e}")
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
