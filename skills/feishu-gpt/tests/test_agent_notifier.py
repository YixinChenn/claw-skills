import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, dependencies):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name.replace(".", "/") + ".py"))
    module = importlib.util.module_from_spec(spec)
    with patch.dict("sys.modules", dependencies):
        spec.loader.exec_module(module)
    return module


class AgentTopicTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.transport = Mock()
        self.transport.send_topic_root.side_effect = [f"om_root_{i}" for i in range(20)]
        self.transport.send_topic_notification.return_value = True
        self.config = SimpleNamespace(
            AGENT_NOTIFY_CHAT_ID="", AGENT_NOTIFY_OPEN_ID="ou_owner", NOTIFY_CHAT_ID="", NOTIFY_OPEN_ID="",
            AGENT_NOTIFY_DIR=str(self.root / "inbox"), AGENT_NOTIFY_ENABLED=True,
            AGENT_NOTIFY_POLL_SECONDS=1, AGENT_NOTIFY_STABLE_SECONDS=1,
        )
        self.dependencies = {
            "bot_runtime.config_runtime": self.config,
            "bot_runtime.messaging": self.transport,
            "bot_runtime.paths": SimpleNamespace(get_runtime_data_dir=lambda: str(self.root)),
        }
        self.notifier = load_module("bot_runtime.agent_notifier", self.dependencies)
        self.data = {"source": "codex", "conversation_id": "conversation-a", "title": "Task", "message": "Done"}

    def send(self, data=None, filename="notify.json"):
        self.notifier._notify("body", self.data if data is None else data, filename)

    def test_claude_reply_spool_uses_persisted_request_route(self):
        from bot_runtime.claude_replies import ClaudeReplyStore
        store = ClaudeReplyStore(self.root / "requests.sqlite3")
        request = store.create("om_user", "om_original", "session-a")
        inbox = Path(self.config.AGENT_NOTIFY_DIR)
        inbox.mkdir()
        path = inbox / "reply.json"
        path.write_text(json.dumps({"kind": "claude_topic_reply", "request_id": request, "message": "answer"}), encoding="utf-8")
        with patch("bot_runtime.claude_replies.get_reply_store", return_value=store):
            self.notifier.process_agent_notification_file(str(path))
        self.transport.send_topic_notification.assert_called_once_with("om_original", "answer", "claude:" + request)
        self.transport.send_topic_root.assert_not_called()
        self.assertEqual(len(list((inbox / "processed").glob("*.json"))), 1)

    def test_same_conversation_survives_reload(self):
        self.send()
        self.notifier = load_module("bot_runtime.agent_notifier", self.dependencies)
        self.send({**self.data, "title": "Next task", "message": "More"}, "notify2.json")
        self.assertEqual(self.transport.send_topic_root.call_count, 1)
        calls = self.transport.send_topic_notification.call_args_list
        self.assertEqual([c.args[0] for c in calls], ["om_root_0", "om_root_0"])
        self.assertNotEqual(calls[0].args[2], calls[1].args[2])
        self.assertEqual(self.transport.send_topic_root.call_args.args[:2], ("open_id", "ou_owner"))

    def test_conversation_source_and_target_are_isolated(self):
        self.send()
        self.send({**self.data, "conversation_id": "conversation-b"})
        self.send({**self.data, "source": "claude"})
        self.notifier.AGENT_NOTIFY_OPEN_ID = "ou_other"
        self.send()
        self.assertEqual(self.transport.send_topic_root.call_count, 4)

    def test_missing_or_unknown_ids_do_not_merge_notifications(self):
        for value in ("", "unknown", "last", "latest"):
            for i in range(2):
                self.send({**self.data, "conversation_id": value}, f"{value}_{i}.json")
        self.assertEqual(self.transport.send_topic_root.call_count, 8)

    def test_id_aliases_reuse_same_root(self):
        for alias in ("conversation_id", "conversationId", "session_id", "thread_id"):
            self.send({"source": "codex", alias: "conversation-a"}, alias + ".json")
        self.assertEqual(self.transport.send_topic_root.call_count, 1)

    def test_reply_failure_keeps_root_for_retry(self):
        self.transport.send_topic_notification.return_value = False
        with self.assertRaisesRegex(RuntimeError, "话题回复失败"):
            self.send()
        self.transport.send_topic_notification.return_value = True
        self.send()
        self.assertEqual(self.transport.send_topic_root.call_count, 1)
        calls = self.transport.send_topic_notification.call_args_list
        self.assertEqual(calls[0], calls[1])

    def test_root_failure_is_not_persisted(self):
        self.transport.send_topic_root.side_effect = None
        self.transport.send_topic_root.return_value = None
        with self.assertRaisesRegex(RuntimeError, "根消息失败"):
            self.send()
        self.transport.send_topic_notification.assert_not_called()
        self.assertFalse((self.root / "agent_notification_threads.json").exists())

    def test_corrupt_index_does_not_start_another_topic(self):
        (self.root / "agent_notification_threads.json").write_text("broken", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            self.send()
        self.transport.send_topic_root.assert_not_called()

    def test_failed_delivery_is_archived_as_failed(self):
        inbox = Path(self.config.AGENT_NOTIFY_DIR)
        inbox.mkdir()
        path = inbox / "notify.json"
        path.write_text(json.dumps(self.data), encoding="utf-8")
        self.transport.send_topic_notification.return_value = False
        self.notifier.process_agent_notification_file(str(path))
        self.assertFalse(path.exists())
        self.assertEqual(len(list((inbox / "failed").glob("*.json"))), 1)
        self.assertEqual(len(list((inbox / "failed").glob("*.error.txt"))), 1)
        self.assertFalse((inbox / "processed").exists())

    def test_successful_delivery_is_archived_as_processed(self):
        inbox = Path(self.config.AGENT_NOTIFY_DIR)
        inbox.mkdir()
        path = inbox / "notify.json"
        path.write_text(json.dumps(self.data), encoding="utf-8")
        self.notifier.process_agent_notification_file(str(path))
        self.assertEqual(len(list((inbox / "processed").glob("*.json"))), 1)
        self.assertFalse((inbox / "failed").exists())

    def test_unconfigured_target_is_failure(self):
        self.notifier.AGENT_NOTIFY_OPEN_ID = ""
        with self.assertRaisesRegex(RuntimeError, "未配置"):
            self.send()


class TopicTransportTests(unittest.TestCase):
    def setUp(self):
        self.messaging = load_module("bot_runtime.messaging", {
            "bot_runtime.config_runtime": SimpleNamespace(MSG_CHUNK_SIZE=10, NOTIFY_CHAT_ID="", NOTIFY_OPEN_ID="", client=Mock()),
            "lark_oapi.api.im.v1": Mock(),
        })

    def test_chunks_stay_in_topic_with_distinct_stable_ids(self):
        with patch.object(self.messaging, "_reply", return_value="om_reply") as reply:
            self.assertTrue(self.messaging.send_topic_notification("om_root", "x" * 25, "key"))
            calls = list(reply.call_args_list)
            self.assertEqual(len(calls), 3)
            self.assertTrue(all(c.args[0] == "om_root" and c.kwargs["reply_in_thread"] for c in calls))
            self.assertEqual(len({c.kwargs["uuid"] for c in calls}), 3)
            self.assertTrue(all(len(c.kwargs["uuid"]) == 32 for c in calls))
            reply.reset_mock()
            self.messaging.send_topic_notification("om_root", "x" * 25, "key")
            self.assertEqual(calls, reply.call_args_list)

    def test_partial_delivery_returns_failure(self):
        with patch.object(self.messaging, "_reply", side_effect=["om_reply", None]) as reply:
            self.assertFalse(self.messaging.send_topic_notification("om_root", "x" * 25, "key"))
            self.assertEqual(reply.call_count, 2)

    def test_sdk_requests_include_thread_flag_and_idempotency(self):
        response = SimpleNamespace(success=lambda: True, data=SimpleNamespace(message_id="om_sent"))
        self.messaging.client.im.v1.message.reply.return_value = response
        self.messaging.client.im.v1.message.create.return_value = response
        self.assertEqual(self.messaging._reply("om_root", "text", "{}", True, "id"), "om_sent")
        builder = self.messaging.ReplyMessageRequestBody.builder.return_value
        builder.msg_type.return_value.content.return_value.reply_in_thread.assert_called_once_with(True)
        builder.msg_type.return_value.content.return_value.reply_in_thread.return_value.uuid.assert_called_once_with("id")
        self.assertEqual(self.messaging.send_topic_root("open_id", "ou_owner", "Title", "key"), "om_sent")


if __name__ == "__main__":
    unittest.main()
