import atexit
import json
import os
import threading
import traceback

import lark_oapi as lark
from lark_oapi.api.application.v6 import GetApplicationRequest
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1, P2ImMessageRecalledV1

from . import state
from .agent_notifier import start_agent_notify_watcher
from .agent_runner import is_agent_runner_authorized
from .agent_topics import forward_agent_topic
from .collections import COLLECTION_COMMANDS, COLLECTION_RULES, CollectionStore
from .model_controls import CONTROL_COMMANDS, default_options, parse_control
from .customer_sessions import CUSTOMER_SYSTEM_PROMPT, ConversationQueue, SessionStore, conversation_scope
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
    get_runtime_data_dir,
    get_agents_file_path,
    get_pid_file_path,
    get_tasks_file_path,
)
from .scheduler import start_heartbeat_loop, start_task_scheduler
from .skill_loader import get_workspace_skills_dir, list_workspace_skills
from .utils import first_non_empty


conversation_queue = ConversationQueue()


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
    scope: str | None = None,
    raw_text: str | None = None,
    cancel_event=None,
    material_issue: str = "",
    source_content=None,
    source_parent_id=None,
    source_type: str = "",
):
    cancel_event = cancel_event or state.register_pending_message(message_id)
    scope = scope or conversation_scope(chat_id, sender_id, admin=is_agent_runner_authorized(sender_ids))
    customer = not is_agent_runner_authorized(sender_ids)
    raw_text = text if raw_text is None else raw_text
    batch_id = None
    with state.chat_locks[scope]:
        reaction_id = None
        try:
            if cancel_event.is_set():
                print(f"[消息已撤回] {sender_id}: {message_id}")
                return
            store = SessionStore(os.path.join(get_runtime_data_dir(), "customer_sessions.sqlite3"))
            state.request_context.value = {
                "origin": "feishu_message",
                "chat_id": chat_id,
                "reply_id": reply_id,
                "sender_ids": sender_ids,
                "customer": customer,
            }
            session = store.current(scope, create=False)
            command = raw_text.strip()
            command_name = command.split(maxsplit=1)[0] if command else ""
            collection_store = CollectionStore(store)
            if collection_store.is_recalled(message_id):
                return
            # Download only after entering the per-conversation queue: /run must not overtake an image.
            if source_type and command_name not in COLLECTION_COMMANDS:
                quoted_text = fetch_message_text(source_parent_id) if not customer and source_parent_id else None
                image_data_urls = fetch_message_image_data_urls(message_id, source_content)
                if source_type not in {"text", "post", "image"}:
                    material_issue = "暂不能读取此类消息的正文（包括合并转发、文件、音视频），请逐条转发文本或粘贴正文。"
                elif source_type == "image" and not image_data_urls:
                    material_issue = "图片下载失败，请重新发送。"
                elif source_type == "post" and any(tag in json.dumps(source_content) for tag in ('"img"', '"media"')):
                    material_issue = "富文本内嵌图片或视频暂不能完整读取，请将图片单独发送。"
            if cancel_event.is_set() or collection_store.is_recalled(message_id):
                return
            active_batch = collection_store.active(scope)
            collection_reply = None
            if command_name in COLLECTION_COMMANDS or active_batch:
                argument = command.split(maxsplit=1)[1] if len(command.split(maxsplit=1)) > 1 else ""
                try:
                    if command_name == "/collect":
                        collection_store.start(scope, argument, message_id)
                        collection_reply = "已进入收集模式。请发送材料，/run [补充要求] 统一处理，/pending 查看，/cancel 取消。"
                    elif command_name == "/pending":
                        if argument:
                            raise ValueError("/pending 不接受参数。")
                        collection_reply = collection_store.pending(scope)
                    elif command_name == "/cancel":
                        if argument:
                            raise ValueError("/cancel 不接受参数。")
                        collection_store.cancel(scope)
                        collection_reply = "已取消收集批次并清除材料。" if active_batch else "当前没有收集批次。"
                    elif command_name == "/run":
                        batch_id, text, image_data_urls = collection_store.begin(scope, argument, message_id)
                        quoted_text = None
                    elif command in {"/new", "/clear", "/end", "新问题", "结束咨询", "/help"}:
                        collection_reply = "正在收集材料。请先 /run 处理或 /cancel 取消；/pending 查看当前批次。"
                    else:
                        if material_issue:
                            raise ValueError("本条未收集：" + material_issue)
                        material = text + ("\n[引用材料]\n" + quoted_text if quoted_text else "")
                        collection_store.add(active_batch["id"], message_id, material, image_data_urls)
                        return
                except ValueError as exc:
                    collection_reply = str(exc)
                if collection_reply is not None:
                    if use_message_reply and send_message_reply(message_id, collection_reply, sender_at_user_id, reply_in_thread=reply_in_thread):
                        return
                    send_reply(reply_id, collection_reply)
                    return
            one_turn_options = None
            control_reply = None
            options = default_options(OPENAI_MODEL)
            last_response = {}
            if not customer:
                if session:
                    saved_options, last_response = store.model_settings(session["id"])
                    options.update(saved_options)
                if command_name in CONTROL_COMMANDS:
                    try:
                        control_reply, saved_options, one_turn_options, question = parse_control(command, options, OPENAI_MODEL)
                        if saved_options is not None:
                            session = session or store.current(scope)
                            store.save_model_settings(session["id"], options=saved_options)
                        if question is not None:
                            text = text.replace(raw_text, question, 1)
                        if command_name == "/status":
                            control_reply += "\n上次主回答实际返回：" + (json.dumps(last_response, ensure_ascii=False) if last_response else "暂无")
                    except ValueError as exc:
                        control_reply = str(exc)
            if control_reply is not None:
                reply = control_reply
            elif command in {"/new", "/clear", "新问题", "/end", "结束咨询"}:
                store.close(scope)
                if command not in {"/end", "结束咨询"}:
                    store.current(scope)
                reply = "已结束本次咨询。下次提问将开启新会话。" if command in {"/end", "结束咨询"} else "已开启新会话，请描述你的问题。旧记录已归档。"
            elif command == "/history":
                context = json.loads(session["context"]) if session else []
                turns = sum(item["role"] == "user" for item in context)
                reply = f"当前会话：{session['id'] if session else '无'}\n保留 {turns} 轮近期对话" + ("，另有历史摘要。" if any(item["role"] == "summary" for item in context) else "。")
            elif customer and command == "/help":
                reply = "可用指令：/collect [要求]（收集）、/run [补充要求]（处理）、/pending（查看材料）、/cancel（取消）；/new（新问题）、/end（结束咨询）、/history（当前会话）、/clear（归档并新建会话）。"
            elif customer and command.startswith("/") and not batch_id:
                reply = "此指令仅供管理员使用。你可以直接描述问题，或输入 /help 查看客服指令。"
            else:
                reply = None
            if reply is not None:
                if use_message_reply and send_message_reply(message_id, reply, sender_at_user_id, reply_in_thread=reply_in_thread):
                    return
                send_reply(reply_id, reply)
                return
            if not customer and command_name not in CONTROL_COMMANDS | COLLECTION_COMMANDS and command.startswith("/") and handle_command(chat_id, command, reply_id, sender_ids=sender_ids):
                return
            try:
                reaction_id = add_thinking_reaction(message_id)
            except Exception:
                print(f"[添加思考表情失败，继续处理] message={message_id}")
                traceback.print_exc()
            if not customer:
                state.request_context.value["model_options"] = one_turn_options or options
            session = session or store.current(scope)
            history_key = session["id"]
            state.conversations[history_key] = json.loads(session["context"])
            if not batch_id:
                store.append(history_key, message_id, "user", text)
            prompt = build_prompt(history_key, text, None if customer else quoted_text)
            reply = ask_chatgpt(
                prompt,
                (CUSTOMER_SYSTEM_PROMPT if customer else build_agent_system_prompt(raw_text if batch_id else text)) + ("\n" + COLLECTION_RULES if batch_id else ""),
                allow_tools=not customer,
                cancel_event=cancel_event,
                image_data_urls=image_data_urls,
            )
            if cancel_event.is_set() or (batch_id and not collection_store.is_running(batch_id, message_id)):
                print(f"[思考已中断] {sender_id}: {message_id}")
                return
            if not reply.strip():
                print(f"[ChatGPT 空回复] {sender_id}: {message_id}")
                return
            print(f"[ChatGPT 回复] {sender_id}: {reply[:80]}{'...' if len(reply) > 80 else ''}")
            if batch_id and reply.startswith(("（OpenAI", "（调用出错", "（未配置", "（响应超时")):
                send_reply(reply_id, "本批处理失败，材料已保留。可用 /pending 查看；重新 /run 前请核对是否已执行部分操作。")
                return
            if customer and reply.startswith(("（OpenAI", "（调用出错", "（未配置")):
                reply = "服务暂时不可用，请稍后重试。"
            last_response = dict(state.request_context.value.get("last_model_response", {}))
            if last_response:
                store.save_model_settings(history_key, last_response=last_response)
            update_history(history_key, text, reply)
            completed = store.complete(history_key, message_id, reply, state.conversations[history_key], batch_id=batch_id, user_content=text if batch_id else None)
            if batch_id and not completed:
                return
        except ThinkingInterrupted:
            print(f"[思考已中断] {sender_id}: {message_id}")
            return
        except Exception as e:
            print(f"[会话处理失败] message={message_id} {type(e).__name__}")
            traceback.print_exc()
            reply = "服务暂时不可用，请稍后重试。" if customer else f"（处理出错：{e}）"
        finally:
            if batch_id:
                try:
                    collection_store.release(batch_id, message_id)
                except Exception:
                    traceback.print_exc()
            if "history_key" in locals():
                state.conversations.pop(history_key, None)
            state.request_context.value = {}
            try:
                if reaction_id:
                    remove_reaction(message_id, reaction_id)
            except Exception:
                print(f"[移除思考表情失败，继续回复] message={message_id}")
                traceback.print_exc()
            finally:
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
    # Strip only a verified leading bot mention so group commands remain commands.
    if _is_group_chat(getattr(msg, "chat_type", None)):
        for mention in mentions or []:
            display = mention_display_name(mention)
            if display in state.bot_display_names and raw_text.startswith("@" + display):
                raw_text = raw_text[len(display) + 1:].lstrip()
                break
    text = render_user_message(raw_text, message_meta)
    reply_id = msg.thread_id if (msg.thread_id and msg.thread_id.startswith("ot_")) else chat_id
    print(f"[收到消息] {sender_id}: {raw_text}")

    if sender_id == "unknown":
        send_reply(reply_id, "无法确认你的身份，请稍后重试。")
        return
    try:
        if forward_agent_topic(msg, raw_text, sender_meta, sender_ids):
            return
    except Exception as exc:
        print(f"[Agent 话题转发失败] message={msg.message_id}: {exc}")
        send_message_reply(msg.message_id, "话题转发失败，请稍后重试。", reply_in_thread=True)
        return
    admin = is_agent_runner_authorized(sender_ids)
    customer_id = sender_meta.get("sender_open_id") or sender_id
    try:
        store = SessionStore(os.path.join(get_runtime_data_dir(), "customer_sessions.sqlite3"))
        scope = store.resolve_scope(chat_id, customer_id, msg.message_id,
                                    group=_is_group_chat(getattr(msg, "chat_type", None)),
                                    thread_id=getattr(msg, "thread_id", None) or "",
                                    root_id=getattr(msg, "root_id", None) or "", admin=admin)
    except Exception as exc:
        print(f"[会话初始化失败] {type(exc).__name__}")
        send_reply(reply_id, "服务暂时不可用，请稍后重试。")
        return
    use_message_reply = _is_group_chat(getattr(msg, "chat_type", None))
    reply_in_thread = bool(msg.thread_id and msg.thread_id.startswith("ot_"))
    cancel_event = state.register_pending_message(msg.message_id)
    conversation_queue.submit(
        scope, process_and_reply, *(
            chat_id,
            text,
            sender_id,
            sender_ids,
            reply_id,
            msg.message_id,
            None,
            None,
            use_message_reply,
            sender_id if sender_id != "unknown" else None,
            reply_in_thread,
            scope,
            raw_text,
            cancel_event,
            "",
            content_data,
            getattr(msg, "parent_id", None),
            msg.message_type,
        ),
    )


def on_message_recalled(data: P2ImMessageRecalledV1) -> None:
    event = data.event
    message_id = getattr(event, "message_id", None)
    if not message_id:
        return
    recall_type = getattr(event, "recall_type", None) or "unknown"
    chat_id = getattr(event, "chat_id", None) or "unknown"
    cancelled = state.cancel_pending_message(message_id)
    try:
        collection_store = CollectionStore(SessionStore(os.path.join(get_runtime_data_dir(), "customer_sessions.sqlite3")))
        for run_id in collection_store.recall(message_id):
            state.cancel_pending_message(run_id)
    except Exception:
        print(f"[撤回收集材料失败] message={message_id}")
        traceback.print_exc()
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
        if user_text == "/skills":
            skills = list_workspace_skills(get_agent_workspace())
            print(f"技能目录：{get_workspace_skills_dir(get_agent_workspace())}")
            print(("可用技能：" + " ".join(f"/{name}" for name in skills)) if skills else "当前没有可用技能。")
            print()
            continue

        prompt = build_prompt(local_chat_id, user_text)
        reply = ask_chatgpt(prompt, build_agent_system_prompt(user_text))
        if not reply.strip():
            print("Agent> （无回复）\n")
            continue
        update_history(local_chat_id, user_text, reply)
        print(f"Agent> {reply}\n")
