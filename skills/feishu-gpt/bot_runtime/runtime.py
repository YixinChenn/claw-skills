import atexit
import json
import os
import threading

import lark_oapi as lark
from lark_oapi.api.application.v6 import GetApplicationRequest
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1, P2ImMessageRecalledV1

from . import state
from .agent_notifier import start_agent_notify_watcher
from .agent import ThinkingInterrupted, ask_chatgpt, build_prompt, run_agent_heartbeat_check, update_history
from .commands import format_model_status, handle_command
from .config_runtime import APP_ID, APP_SECRET, OPENAI_MODEL, client
from .doc_importer import start_doc_import_watcher
from .messaging import (
    add_thinking_reaction,
    build_message_meta,
    build_mentions_meta,
    fetch_message_image_data_urls,
    fetch_message_text,
    mention_display_name,
    mention_identifier,
    parse_message_content,
    remove_reaction,
    render_user_message,
    resolve_sender_identity,
    send_admin_notification,
    send_message_reply,
    send_reply,
)
from .paths import (
    build_agent_system_prompt,
    configure_process_workspace,
    ensure_runtime_dirs,
    get_agent_workspace,
    get_agents_file_path,
    get_pid_file_path,
    get_tasks_file_path,
)
from .scheduler import start_heartbeat_loop, start_task_scheduler
from .utils import first_non_empty


def _is_group_chat(chat_type: str | None) -> bool:
    normalized = str(chat_type or "").strip().lower()
    return normalized not in {"", "p2p"}


def _is_bot_mentioned(mentions) -> bool:
    items = list(mentions or [])
    if not items:
        return False
    for mention in items:
        display_name = mention_display_name(mention)
        if mention_identifier(mention) == APP_ID or getattr(mention, "id", None) == APP_ID:
            return True
        if display_name and display_name in state.bot_display_names:
            return True
    return False


def load_bot_profile():
    try:
        request = GetApplicationRequest.builder().lang("zh_cn").app_id(APP_ID).build()
        resp = client.application.v6.application.get(request)
        if not resp.success():
            print(f"[WARN] 获取机器人资料失败: {resp.code} {resp.msg}")
            return
        payload = json.loads(resp.raw.content.decode("utf-8", errors="replace")) if getattr(resp, "raw", None) else {}
        app_info = payload.get("data", {}).get("app", {})
        names = set()
        app_name = str(app_info.get("app_name") or "").strip()
        if app_name:
            names.add(app_name)
        for item in app_info.get("i18n") or []:
            name = str((item or {}).get("name") or "").strip()
            if name:
                names.add(name)
        state.bot_display_names = names
        if names:
            print("[INFO] 已加载机器人显示名: " + ", ".join(sorted(names)))
        else:
            print("[WARN] 应用资料返回成功，但未解析到机器人显示名")
    except Exception as e:
        print(f"[WARN] 获取机器人资料异常: {e}")


def _log_first_bot_mention(mentions):
    if state.bot_mention_logged:
        return
    state.bot_mention_logged = True
    payload = {
        "app_id": APP_ID,
        "bot_display_names": sorted(state.bot_display_names),
        "mentions": build_mentions_meta(mentions),
    }
    print("[首个命中机器人提及] " + json.dumps(payload, ensure_ascii=False))


def process_and_reply(
    chat_id: str,
    text: str,
    sender_id: str,
    sender_ids: list[str],
    reply_id: str,
    message_id: str,
    quoted_text: str | None = None,
    image_data_urls: list[str] | None = None,
    use_message_reply: bool = False,
    sender_at_user_id: str | None = None,
    reply_in_thread: bool = False,
):
    cancel_event = state.register_pending_message(message_id)
    with state.chat_locks[chat_id]:
        reaction_id = add_thinking_reaction(message_id)
        try:
            if cancel_event.is_set():
                print(f"[消息已撤回] {sender_id}: {message_id}")
                return
            prompt = build_prompt(chat_id, text, quoted_text)
            state.request_context.value = {
                "origin": "feishu_message",
                "chat_id": chat_id,
                "reply_id": reply_id,
                "sender_ids": sender_ids,
            }
            reply = ask_chatgpt(prompt, build_agent_system_prompt(), cancel_event=cancel_event, image_data_urls=image_data_urls)
            if cancel_event.is_set():
                print(f"[思考已中断] {sender_id}: {message_id}")
                return
            if not reply.strip():
                print(f"[ChatGPT 空回复] {sender_id}: {message_id}")
                return
            print(f"[ChatGPT 回复] {sender_id}: {reply[:80]}{'...' if len(reply) > 80 else ''}")
            update_history(chat_id, text, reply)
        except ThinkingInterrupted:
            print(f"[思考已中断] {sender_id}: {message_id}")
            return
        except Exception as e:
            reply = f"（处理出错：{e}）"
        finally:
            state.request_context.value = {}
            remove_reaction(message_id, reaction_id)
            state.finish_pending_message(message_id)
    if use_message_reply and send_message_reply(message_id, reply, sender_at_user_id, reply_in_thread=reply_in_thread):
        return
    send_reply(reply_id, reply)


def on_message(data: P2ImMessageReceiveV1) -> None:
    msg = data.event.message
    if state.is_duplicate(msg.message_id):
        return
    mentions = getattr(msg, "mentions", None)
    if _is_group_chat(getattr(msg, "chat_type", None)):
        if not _is_bot_mentioned(mentions):
            print(f"[忽略群消息] message={msg.message_id} chat={msg.chat_id} 原因=未@机器人")
            return
        _log_first_bot_mention(mentions)

    try:
        raw_text, content_data = parse_message_content(msg.message_type, msg.content, mentions)
    except Exception:
        return
    if not raw_text:
        return

    chat_id = msg.chat_id
    sender_meta = resolve_sender_identity(data.event.sender)
    sender_id = (
        first_non_empty(
            sender_meta.get("sender_id"),
            sender_meta.get("sender_open_id"),
            sender_meta.get("sender_union_id"),
        )
        or "unknown"
    )
    sender_ids = [
        value
        for value in [
            sender_meta.get("sender_id"),
            sender_meta.get("sender_open_id"),
            sender_meta.get("sender_union_id"),
        ]
        if value
    ]
    message_meta = build_message_meta(msg, data.event.sender, mentions, content_data)
    text = render_user_message(raw_text, message_meta)
    reply_id = msg.thread_id if (msg.thread_id and msg.thread_id.startswith("ot_")) else chat_id
    print(f"[收到消息] {sender_id}: {raw_text}")

    if raw_text.startswith("/") and handle_command(chat_id, raw_text, reply_id, sender_ids=sender_ids):
        return

    quoted_text = fetch_message_text(msg.parent_id) if msg.parent_id else None
    image_data_urls = fetch_message_image_data_urls(msg.message_id, content_data)
    use_message_reply = _is_group_chat(getattr(msg, "chat_type", None))
    reply_in_thread = bool(msg.thread_id and msg.thread_id.startswith("ot_"))
    threading.Thread(
        target=process_and_reply,
        args=(
            chat_id,
            text,
            sender_id,
            sender_ids,
            reply_id,
            msg.message_id,
            quoted_text,
            image_data_urls,
            use_message_reply,
            sender_id if sender_id != "unknown" else None,
            reply_in_thread,
        ),
        daemon=True,
    ).start()


def on_message_recalled(data: P2ImMessageRecalledV1) -> None:
    event = data.event
    message_id = getattr(event, "message_id", None)
    if not message_id:
        return
    recall_type = getattr(event, "recall_type", None) or "unknown"
    chat_id = getattr(event, "chat_id", None) or "unknown"
    cancelled = state.cancel_pending_message(message_id)
    status = "已中断思考" if cancelled else "未命中运行中思考"
    print(f"[消息撤回] chat={chat_id} message={message_id} recall_type={recall_type} {status}")


def write_pid():
    ensure_runtime_dirs()
    pid_path = get_pid_file_path()
    with open(pid_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(str(os.getpid()))


def remove_pid():
    try:
        os.remove(get_pid_file_path())
    except OSError:
        pass


def main():
    configure_process_workspace()
    write_pid()
    atexit.register(remove_pid)

    print("=" * 50)
    print("  飞书 × ChatGPT 机器人启动中...")
    print(f"  APP_ID: {APP_ID}")
    print(f"  MODEL: {OPENAI_MODEL}")
    print("  SHELL TOOL: enabled")
    print(f"  WORKSPACE: {get_agent_workspace()}")
    print(f"  AGENTS: {get_agents_file_path()}")
    print(f"  TASKS: {get_tasks_file_path()}")
    print("=" * 50)

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .register_p2_im_message_recalled_v1(on_message_recalled)
        .build()
    )
    ws_client = lark.ws.Client(
        APP_ID,
        APP_SECRET,
        event_handler=handler,
        log_level=lark.LogLevel.INFO,
    )
    state.ws_client = ws_client
    load_bot_profile()

    print("正在连接飞书服务器（无需公网 IP）...")

    threading.Thread(target=_delayed_online_notify, daemon=True).start()

    start_heartbeat_loop(send_admin_notification, run_agent_heartbeat_check)
    start_task_scheduler(ask_chatgpt, build_agent_system_prompt, send_reply)
    start_agent_notify_watcher()
    start_doc_import_watcher()
    ws_client.start()


def _delayed_online_notify():
    import time

    time.sleep(3)
    send_admin_notification("✅ **机器人已上线**\n")


def run_bot_and_local_chat():
    configure_process_workspace()
    bot_thread = threading.Thread(target=main, daemon=True)
    state.bot_runtime_thread = bot_thread
    bot_thread.start()
    run_local_chat()


def run_local_chat():
    configure_process_workspace()
    local_chat_id = "__local_chat__"
    print("=" * 50)
    print("  本地对话模式")
    print(f"  MODEL: {OPENAI_MODEL}")
    print("  SHELL TOOL: enabled")
    print(f"  WORKSPACE: {get_agent_workspace()}")
    print(f"  AGENTS: {get_agents_file_path()}")
    print(f"  TASKS: {get_tasks_file_path()}")
    print("  输入 /exit 退出，输入 /clear 清空上下文")
    print("=" * 50)

    while True:
        try:
            user_text = input("你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已退出本地对话。")
            return

        if not user_text:
            continue
        if user_text == "/exit":
            print("已退出本地对话。")
            return
        if user_text == "/clear":
            state.conversations.pop(local_chat_id, None)
            print("上下文已清空。")
            continue
        if user_text == "/history":
            history = state.conversations.get(local_chat_id, [])
            non_summary = [turn for turn in history if turn["role"] != "summary"]
            has_summary = any(turn["role"] == "summary" for turn in history)
            turns = len(non_summary) // 2
            summary_note = "（另有更早对话已压缩为摘要）" if has_summary else ""
            print(f"当前保留 {turns} 轮完整对话{summary_note}")
            print(format_model_status())
            print(f"工作区：{get_agent_workspace()}")
            print(f"AGENTS：{get_agents_file_path()}\n")
            continue
        if user_text == "/model":
            print(format_model_status() + "\n")
            continue

        prompt = build_prompt(local_chat_id, user_text)
        reply = ask_chatgpt(prompt, build_agent_system_prompt())
        if not reply.strip():
            print("Agent> （无回复）\n")
            continue
        update_history(local_chat_id, user_text, reply)
        print(f"Agent> {reply}\n")
