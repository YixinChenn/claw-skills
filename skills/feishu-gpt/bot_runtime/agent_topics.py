import threading

from .agent_notifier import find_agent_topic
from .agent_runner import create_agent_job, is_agent_runner_authorized
from .claude_inbox import InboxDeliveryUncertain, send_claude_message
from .claude_replies import build_topic_prompt, get_reply_store
from .customer_sessions import ConversationQueue
from .messaging import add_message_reaction, remove_reaction, send_topic_notification


_topic_queue = ConversationQueue()


def _acknowledge(message_id: str):
    try:
        return add_message_reaction(message_id, "Typing")
    except Exception as exc:
        print(f"[Agent 话题表情失败，继续处理] message={message_id}: {exc}")


def _clear_acknowledgement(message_id: str, reaction_id: str):
    if reaction_id:
        try:
            remove_reaction(message_id, reaction_id)
        except Exception as exc:
            print(f"[Agent 话题表情清理失败] message={message_id}: {exc}")


def _send_result(root_id: str, text: str, delivery_key: str):
    if not send_topic_notification(root_id, text, delivery_key):
        raise RuntimeError(f"Agent 话题回传失败：{root_id}")


def _resume_topic(topic: dict, text: str, chat_id: str, message_id: str, reaction_id: str = None):
    finished = threading.Event()

    def reply(_reply_id, result):
        try:
            _send_result(topic["root_id"], result, message_id + ":result")
            _clear_acknowledgement(message_id, reaction_id)
        finally:
            finished.set()

    try:
        create_agent_job("codex", text, topic["cwd"] or None, topic["root_id"], chat_id, reply,
                         conversation_id=topic["conversation_id"], capture_result=True)
        # Serialize this session until Runner has delivered its completion callback.
        finished.wait()
    except Exception as exc:
        _send_result(topic["root_id"], f"Codex 续聊失败：{exc}", message_id + ":error")


def _forward_claude(topic: dict, text: str, message_id: str, request_id: str):
    store = get_reply_store()
    try:
        send_claude_message(topic["conversation_id"], build_topic_prompt(request_id, text))
    except Exception as exc:
        status = "uncertain" if isinstance(exc, InboxDeliveryUncertain) else "failed"
        store.set_delivery(request_id, status)
        _send_result(topic["root_id"], f"Claude 话题投递失败：{exc}", message_id + ":error")
        return
    store.set_delivery(request_id, "written")
    reaction_id = _acknowledge(message_id)
    store.track_reaction(request_id, reaction_id, remove_reaction)


def forward_agent_topic(msg, raw_text: str, sender_meta: dict, sender_ids: list[str]) -> bool:
    root_id = getattr(msg, "root_id", None) or ""
    if not root_id or not getattr(msg, "thread_id", None):
        return False
    topic = find_agent_topic(root_id, msg.chat_id, sender_meta.get("sender_open_id", ""))
    if topic is None:
        return False
    if sender_meta.get("sender_type") != "user":
        return True
    if not is_agent_runner_authorized(sender_ids):
        _send_result(root_id, "你没有启动 Agent 续聊的权限。", msg.message_id + ":denied")
        return True
    if msg.message_type not in {"text", "post"}:
        _send_result(root_id, "此话题目前支持文字续聊，请把要求写成文字。", msg.message_id + ":unsupported")
        return True
    if topic["agent"] == "claude":
        request_id = get_reply_store().create(msg.message_id, root_id, topic["conversation_id"])
        if request_id is not None:
            _topic_queue.submit("claude:" + topic["conversation_id"], _forward_claude,
                                topic, raw_text, msg.message_id, request_id)
        return True
    reaction_id = _acknowledge(msg.message_id)
    _topic_queue.submit(topic["conversation_id"], _resume_topic, topic, raw_text, msg.chat_id, msg.message_id, reaction_id)
    return True
