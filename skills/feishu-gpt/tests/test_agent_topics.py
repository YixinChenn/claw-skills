import importlib.util
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bot_runtime.customer_sessions import ConversationQueue
import test_agent_notifier as notifier_tests
from test_agent_notifier import load_module


class TopicLookupTests(unittest.TestCase):
    setUp = notifier_tests.AgentTopicTests.setUp
    send = notifier_tests.AgentTopicTests.send

    def test_lookup_matches_owner_and_root_and_restores_cwd(self):
        self.send({**self.data, "cwd": "D:/project"})
        topic = self.notifier.find_agent_topic("om_root_0", "oc_chat", "ou_owner")
        self.assertEqual(topic["conversation_id"], "conversation-a")
        self.assertEqual(topic["cwd"], "D:/project")
        self.assertIsNone(self.notifier.find_agent_topic("om_root_0", "oc_chat", "ou_other"))
        self.assertIsNone(self.notifier.find_agent_topic("om_unknown", "oc_chat", "ou_owner"))

    def test_legacy_index_still_resolves(self):
        key = json.dumps(["open_id", "ou_owner", "codex", ["conversation", "old-session"]])
        (self.root / "agent_notification_threads.json").write_text(json.dumps({key:"om_old"}), encoding="utf-8")
        self.assertEqual(self.notifier.find_agent_topic("om_old", "oc_chat", "ou_owner")["conversation_id"], "old-session")

    def test_claude_aliases_resolve_but_anonymous_topics_do_not(self):
        for index, source in enumerate(("claude", "claude-code", "claude-cli", "claude-desktop")):
            self.send({**self.data, "source": source})
            topic = self.notifier.find_agent_topic(f"om_root_{index}", "oc_chat", "ou_owner")
            self.assertEqual(topic["agent"], "claude")
            self.assertEqual(topic["conversation_id"], "conversation-a")
            self.assertIsNone(self.notifier.find_agent_topic(f"om_root_{index}", "oc_chat", "ou_other"))
        self.send({**self.data, "conversation_id": ""})
        self.assertIsNone(self.notifier.find_agent_topic("om_root_4", "oc_chat", "ou_owner"))


class TopicForwardTests(unittest.TestCase):
    def setUp(self):
        self.notifier, self.runner, self.messaging = Mock(), Mock(), Mock()
        self.topic = {"agent":"codex", "root_id":"om_root", "conversation_id":"session-1", "cwd":"D:/project"}
        self.notifier.find_agent_topic.return_value = self.topic
        self.runner.is_agent_runner_authorized.return_value = True
        self.messaging.send_topic_notification.return_value = True
        self.module = load_module("bot_runtime.agent_topics", {
            "bot_runtime.agent_notifier":self.notifier, "bot_runtime.agent_runner":self.runner,
            "bot_runtime.messaging":self.messaging,
        })
        self.msg = SimpleNamespace(root_id="om_root",thread_id="omt_topic",chat_id="oc_chat",message_id="om_user",message_type="text")
        self.sender = {"sender_open_id":"ou_owner", "sender_type":"user"}

    def test_raw_text_routes_to_exact_session_and_root(self):
        queue = Mock()
        self.module._topic_queue = queue
        self.assertTrue(self.module.forward_agent_topic(self.msg,"continue exactly",self.sender,["ou_owner"]))
        self.messaging.send_topic_notification.assert_not_called()
        self.messaging.add_message_reaction.assert_called_once_with("om_user", "Typing")
        args = queue.submit.call_args.args
        self.assertEqual(args[0], "session-1")
        self.assertEqual(args[3], "continue exactly")
        self.runner.create_agent_job.side_effect = lambda *a, **kw: a[5](a[3], "final result")
        args[1](*args[2:])
        call = self.runner.create_agent_job.call_args
        self.assertEqual(call.args[:5], ("codex","continue exactly","D:/project","om_root","oc_chat"))
        self.assertEqual(call.kwargs, {"conversation_id":"session-1", "capture_result":True})
        self.messaging.send_topic_notification.assert_called_with("om_root","final result","om_user:result")
        self.messaging.remove_reaction.assert_called_once_with("om_user", self.messaging.add_message_reaction.return_value)

    def test_reaction_failure_does_not_block_delivery(self):
        self.module._topic_queue = Mock()
        self.messaging.add_message_reaction.side_effect = RuntimeError("network")
        self.assertTrue(self.module.forward_agent_topic(self.msg, "text", self.sender, ["ou_owner"]))
        self.module._topic_queue.submit.assert_called_once()
        self.messaging.send_topic_notification.assert_not_called()

    def test_non_topic_and_unbound_topic_pass_through(self):
        self.msg.thread_id = ""
        self.assertFalse(self.module.forward_agent_topic(self.msg,"text",self.sender,["ou_owner"]))
        self.msg.thread_id = "omt_topic"
        self.notifier.find_agent_topic.return_value = None
        self.assertFalse(self.module.forward_agent_topic(self.msg,"text",self.sender,["ou_owner"]))
        self.runner.create_agent_job.assert_not_called()

    def test_unauthorized_sender_and_attachments_never_start_jobs(self):
        self.module._topic_queue = Mock()
        self.runner.is_agent_runner_authorized.return_value = False
        self.assertTrue(self.module.forward_agent_topic(self.msg,"text",self.sender,["ou_owner"]))
        self.runner.is_agent_runner_authorized.return_value = True
        self.msg.message_type = "image"
        self.assertTrue(self.module.forward_agent_topic(self.msg,"[image]",self.sender,["ou_owner"]))
        self.module._topic_queue.submit.assert_not_called()

    def test_same_session_waits_for_previous_result(self):
        first, second = threading.Event(), threading.Event()
        callbacks = []
        def create(*args, **kwargs):
            callbacks.append(args[5])
            (first if len(callbacks)==1 else second).set()
        self.runner.create_agent_job.side_effect = create
        queue = ConversationQueue()
        queue.submit("session-1",self.module._resume_topic,self.topic,"first","oc_chat","om_1")
        self.assertTrue(first.wait(2))
        queue.submit("session-1",self.module._resume_topic,self.topic,"second","oc_chat","om_2")
        self.assertFalse(second.wait(.1))
        callbacks[0]("om_root","first result")
        self.assertTrue(second.wait(2))
        callbacks[1]("om_root","second result")

    def test_runner_error_returns_to_topic(self):
        self.runner.create_agent_job.side_effect = RuntimeError("runner unavailable")
        self.module._resume_topic(self.topic,"text","oc_chat","om_1")
        args = self.messaging.send_topic_notification.call_args.args
        self.assertEqual(args[0],"om_root")
        self.assertIn("runner unavailable",args[1])



    def test_claude_uses_inbox_and_never_starts_runner(self):
        self.topic["agent"] = "claude"
        queue = Mock()
        store = Mock()
        store.create.return_value = "request-a"
        self.module._topic_queue = queue
        with patch.object(self.module, "get_reply_store", return_value=store), \
             patch.object(self.module, "send_claude_message") as inbox:
            self.assertTrue(self.module.forward_agent_topic(self.msg, "继续", self.sender, ["ou_owner"]))
            call = queue.submit.call_args.args
            self.assertEqual(call[0], "claude:session-1")
            call[1](*call[2:])
            inbox.assert_called_once()
            self.assertEqual(inbox.call_args.args[0], "session-1")
            self.assertIn("request-a", inbox.call_args.args[1])
            self.assertIn("继续", inbox.call_args.args[1])
            store.set_delivery.assert_called_once_with("request-a", "written")
            self.messaging.send_topic_notification.assert_not_called()
            self.messaging.add_message_reaction.assert_called_once_with("om_user", "Typing")
            store.create.return_value = None
            self.module.forward_agent_topic(self.msg, "继续", self.sender, ["ou_owner"])
            self.assertEqual(queue.submit.call_count, 1)
        self.runner.create_agent_job.assert_not_called()

    def test_claude_failures_report_exact_topic_without_retry(self):
        InboxDeliveryUncertain = self.module.InboxDeliveryUncertain
        for error, status in ((RuntimeError("offline"), "failed"),
                              (InboxDeliveryUncertain("uncertain"), "uncertain")):
            with self.subTest(status=status), \
                 patch.object(self.module, "get_reply_store") as factory, \
                 patch.object(self.module, "send_claude_message", side_effect=error) as inbox:
                self.module._forward_claude(self.topic, "text", "om_user", "request-a")
                factory.return_value.set_delivery.assert_called_once_with("request-a", status)
                self.assertEqual(self.messaging.send_topic_notification.call_args.args[0], "om_root")
                self.assertIn(str(error), self.messaging.send_topic_notification.call_args.args[1])
                inbox.assert_called_once()
        self.runner.create_agent_job.assert_not_called()

    def test_bot_echo_does_not_generate_another_reply(self):
        self.sender["sender_type"] = "app"
        self.module._topic_queue = Mock()
        self.assertTrue(self.module.forward_agent_topic(self.msg, "reply", self.sender, ["ou_owner"]))
        self.module._topic_queue.submit.assert_not_called()
        self.messaging.send_topic_notification.assert_not_called()


class TopicRunnerTests(unittest.TestCase):
    def test_codex_binary_discovery_uses_configured_home_and_explicit_override(self):
        runner = load_module("bot_runtime.agent_runner", {
            "bot_runtime.config_runtime": Mock(), "bot_runtime.paths": Mock(),
        })
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"CODEX_HOME": directory, "CODEX_CLI_PATH": ""}):
            runner.get_agent_workspace.return_value = directory
            binary = Path(directory) / ".sandbox-bin" / "codex.exe"
            binary.parent.mkdir()
            binary.write_bytes(b"")
            self.assertEqual(runner._extract_codex_cli_path_from_config(), str(binary))
            override = Path(directory) / "custom-codex.exe"
            override.write_bytes(b"")
            with patch.dict("os.environ", {"CODEX_CLI_PATH": str(override)}):
                self.assertEqual(runner._extract_codex_cli_path_from_config(), str(override))

    def test_capture_final_reply_and_deliver_without_log_tail(self):
        import bot_runtime
        state = SimpleNamespace(agent_jobs_lock=threading.RLock(),agent_jobs_loaded=True,agent_jobs={})
        with tempfile.TemporaryDirectory() as directory, patch.object(bot_runtime,"state",state,create=True):
            runner = load_module("bot_runtime.agent_runner", {
                "bot_runtime.config_runtime":Mock(), "bot_runtime.paths":Mock(),
            })
            runner.AGENT_RUNNER_ENABLED = True
            runner.AGENT_RUNNER_MAX_CONCURRENT = 2
            with patch.object(runner,"resolve_agent_cwd",return_value=directory), \
                 patch.object(runner,"build_agent_command",return_value=["codex","exec","resume","session-1","prompt"]), \
                 patch.object(runner,"get_agent_job_logs_dir",return_value=directory), \
                 patch.object(runner,"save_agent_jobs"), patch.object(runner.threading,"Thread") as thread:
                job = runner.create_agent_job("codex","prompt",directory,"om_root","oc_chat",Mock(),
                                              conversation_id="session-1",capture_result=True)
            command = thread.call_args.kwargs["args"][1]
            self.assertEqual(command[-3:], ["--output-last-message",job["result_path"],"prompt"])
            Path(job["result_path"]).write_text("FINAL ANSWER",encoding="utf-8")
            with patch.object(runner,"tail_agent_job_log") as tail:
                result = runner.format_agent_job_completion({**job,"status":"succeeded","exit_code":0})
                self.assertIn("FINAL ANSWER",result)
                tail.assert_not_called()
            Path(job["result_path"]).unlink()
            with patch.object(runner,"tail_agent_job_log",return_value="thread already has an active writer"):
                result = runner.format_agent_job_completion({**job,"status":"failed","exit_code":1})
                self.assertIn("本次 CLI 续聊未执行",result)
                self.assertNotIn("最近日志",result)


if __name__ == "__main__":
    unittest.main()
