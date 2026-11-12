import importlib.util
import json
import tempfile
import ssl
import threading
import unittest
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import bot_runtime
from bot_runtime.customer_sessions import ConversationQueue, SessionStore, conversation_scope


def isolated(name, state):
    dependencies = {"bot_runtime." + module: Mock() for module in (
        "agent", "agent_notifier", "agent_runner", "commands", "config_runtime", "doc_importer",
        "messaging", "paths", "scheduler", "skill_loader", "utils")}
    dependencies.update({name: Mock() for name in (
        "lark_oapi", "lark_oapi.api.application.v6", "lark_oapi.api.im.v1")})
    dependencies["bot_runtime.agent"].ThinkingInterrupted = type("ThinkingInterrupted", (Exception,), {})
    path = Path(__file__).resolve().parents[1] / "bot_runtime" / (name + ".py")
    spec = importlib.util.spec_from_file_location("bot_runtime." + name, path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict("sys.modules", dependencies), patch.object(bot_runtime, "state", state, create=True):
        spec.loader.exec_module(module)
    return module


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "sessions.sqlite3"
        self.store = SessionStore(self.path)

    def test_restart_summary_original_messages_and_close(self):
        scope = conversation_scope("chat", "customer")
        session = self.store.current(scope)
        self.store.append(session["id"], "msg", "user", "original text")
        context = [{"role": "summary", "content": "summary"}]
        self.store.complete(session["id"], "msg", "answer", context)
        restored = SessionStore(self.path).current(scope)
        self.assertEqual(restored["id"], session["id"])
        self.assertEqual(json.loads(restored["context"]), context)
        self.store.close(scope)
        self.assertIsNone(self.store.current(scope, create=False))
        self.assertNotEqual(self.store.current(scope)["id"], session["id"])
        with self.store.connect() as db:
            self.assertEqual([r[0] for r in db.execute("SELECT content FROM messages ORDER BY id")], ["original text", "answer"])

    def test_customers_threads_chats_and_admin_are_isolated(self):
        scopes = [conversation_scope(chat, user, group=True, thread_id=topic, admin=admin)
                  for chat in ("c1", "c2") for user in ("u1", "u2")
                  for topic in ("t1", "t2", "") for admin in (True, False)]
        self.assertEqual(len({self.store.current(scope)["id"] for scope in scopes}), len(scopes))
        with self.assertRaises(ValueError):
            conversation_scope("chat", "unknown")

    def test_reply_chain_keeps_initial_context_across_restart(self):
        initial = self.store.resolve_scope("group", "u1", "m1", group=True)
        continued = SessionStore(self.path).resolve_scope("group", "u1", "m2", group=True, thread_id="t1", root_id="m1")
        self.assertEqual(initial, continued)
        self.assertEqual(initial, self.store.resolve_scope("group", "u1", "m3", group=True, thread_id="t1"))
        self.assertNotEqual(initial, self.store.resolve_scope("group", "u2", "m4", group=True, thread_id="t1"))
        self.assertNotEqual(initial, self.store.resolve_scope("group", "u1", "m5", group=True, thread_id="t2"))

    def test_queue_orders_commands_and_allows_other_customers(self):
        queue = ConversationQueue()
        release, started, other, done = (threading.Event() for _ in range(4))
        self.addCleanup(release.set)
        events = []
        def first():
            started.set()
            release.wait(3)
            events.append("reply")
        queue.submit("a", first)
        self.assertTrue(started.wait(2))
        queue.submit("a", lambda: events.append("new"))
        queue.submit("a", lambda: (events.append("next"), done.set()))
        queue.submit("b", other.set)
        self.assertTrue(other.wait(2))
        self.assertEqual(events, [])
        release.set()
        self.assertTrue(done.wait(2))
        self.assertEqual(events, ["reply", "new", "next"])


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = SimpleNamespace(conversations={}, chat_locks=defaultdict(threading.Lock),
            request_context=threading.local(), register_pending_message=lambda _: threading.Event(),
            finish_pending_message=Mock())
        self.runtime = isolated("runtime", self.state)
        self.runtime.OPENAI_MODEL = "gpt-6-astra"
        self.runtime.get_runtime_data_dir.return_value = self.temp.name
        self.runtime.is_agent_runner_authorized.side_effect = lambda ids: "admin" in ids
        self.runtime.ask_chatgpt.return_value = "answer"
        self.runtime.build_agent_system_prompt.return_value = "admin system prompt"
        self.runtime.build_prompt.side_effect = lambda key, text, quote: json.dumps(self.state.conversations[key]) + text
        def update(key, text, reply):
            self.state.conversations[key].extend([{"role": "user", "content": text}, {"role": "assistant", "content": reply}])
        self.runtime.update_history.side_effect = update
        self.store = SessionStore(Path(self.temp.name) / "customer_sessions.sqlite3")

    def message(self, text, user="user", message_id="m", cancel_event=None):
        self.runtime.process_and_reply("chat", text, user, [user], "chat", message_id,
                                       raw_text=text, cancel_event=cancel_event)

    def test_customer_has_no_tools_or_internal_prompt_and_restores_history(self):
        self.message("first", message_id="m1")
        self.assertFalse(self.runtime.ask_chatgpt.call_args.kwargs["allow_tools"])
        self.runtime.build_agent_system_prompt.assert_not_called()
        self.assertEqual(self.state.conversations, {})
        self.message("second", message_id="m2")
        self.assertIn("first", self.runtime.ask_chatgpt.call_args.args[0])
        self.message("private", user="other", message_id="m3")
        self.assertNotIn("first", self.runtime.ask_chatgpt.call_args.args[0])

    def test_reaction_ssl_failure_does_not_block_answer_or_cleanup(self):
        for stage in ("add_thinking_reaction", "remove_reaction"):
            with self.subTest(stage=stage), patch.object(self.runtime, stage, side_effect=ssl.SSLError("TLS failed")), patch.object(self.runtime.traceback, "print_exc") as log:
                self.runtime.send_reply.reset_mock()
                self.state.finish_pending_message.reset_mock()
                self.message("hello", message_id=stage)
                self.runtime.send_reply.assert_called_once_with("chat", "answer")
                self.state.finish_pending_message.assert_called_once_with(stage)
                self.assertEqual(self.state.request_context.value, {})
                self.assertEqual(self.state.conversations, {})
                log.assert_called_once()

    def test_cancelled_message_does_not_request_reaction(self):
        cancelled = threading.Event()
        cancelled.set()
        self.message("withdrawn", cancel_event=cancelled)
        self.runtime.add_thinking_reaction.assert_not_called()
        self.runtime.ask_chatgpt.assert_not_called()
        self.state.finish_pending_message.assert_called_once_with("m")

    def test_customer_commands_cannot_reach_admin_or_model(self):
        for command in ("/agent-run codex hi", "/task-list", "/lark-doc read", "/toollog", "/help", "/history"):
            self.message(command)
        self.runtime.handle_command.assert_not_called()
        self.runtime.ask_chatgpt.assert_not_called()
        self.message("/task-list", user="admin")
        self.runtime.handle_command.assert_called_once()

    def test_new_end_and_queued_recall(self):
        self.message("first")
        scope = conversation_scope("chat", "user")
        old = self.store.current(scope)["id"]
        self.message("/new")
        self.assertNotEqual(self.store.current(scope)["id"], old)
        self.message("/end")
        self.assertIsNone(self.store.current(scope, create=False))
        cancelled = threading.Event()
        cancelled.set()
        self.message("withdrawn", cancel_event=cancelled)
        self.assertIsNone(self.store.current(scope, create=False))
        self.message("next", message_id="next")
        self.assertNotIn("first", self.runtime.ask_chatgpt.call_args.args[0])

    def test_admin_settings_persist_single_turn_overrides_and_reset(self):
        self.message("/mode deep", user="admin")
        scope = conversation_scope("chat", "admin", admin=True)
        session = self.store.current(scope)
        restored = SessionStore(self.store.path).model_settings(session["id"])[0]
        self.assertEqual(restored, {"model": "gpt-6-astra", "effort": "high"})
        calls = []
        def answer(*args, **kwargs):
            calls.append((dict(self.state.request_context.value["model_options"]), args[0]))
            self.state.request_context.value["last_model_response"] = {"model": "gpt-6.1-sol", "effort": "low", "service_tier": "default"}
            return "answer"
        self.runtime.ask_chatgpt.side_effect = answer
        self.message("/ask --model gpt-6.1-sol --effort low -- only this turn", user="admin", message_id="one")
        self.assertEqual(calls[-1][0], {"model": "gpt-6.1-sol", "effort": "low"})
        self.assertNotIn("/ask", calls[-1][1])
        self.assertEqual(self.store.model_settings(session["id"])[0], restored)
        self.message("/status", user="admin")
        self.assertIn("gpt-6.1-sol", self.runtime.send_reply.call_args.args[1])
        self.message("next", user="admin", message_id="two")
        self.assertEqual(calls[-1][0], restored)
        self.message("/new", user="admin")
        self.message("fresh", user="admin", message_id="three")
        self.assertEqual(calls[-1][0], {"model": "gpt-6.1-sol", "effort": "high"})

    def test_customer_cannot_change_models_or_override_per_turn(self):
        for command in ("/mode deep", "/effort max", "/model gpt-6.1-sol", "/ask --effort low -- hello", "/status", "/models", "/fast on"):
            self.message(command)
        self.runtime.ask_chatgpt.assert_not_called()
        self.runtime.handle_command.assert_not_called()
        self.assertIsNone(self.store.current(conversation_scope("chat", "user"), create=False))

    def test_invalid_admin_control_does_not_mutate_session(self):
        self.message("/effort low", user="admin")
        session = self.store.current(conversation_scope("chat", "admin", admin=True))
        original = self.store.model_settings(session["id"])[0]
        self.message("/model unsupported", user="admin")
        self.message("/fast on", user="admin")
        self.assertEqual(self.store.model_settings(session["id"])[0], original)
        self.runtime.ask_chatgpt.assert_not_called()

    def test_agent_loop_uses_same_options_and_preserves_actual_metadata(self):
        with patch.dict("sys.modules", {"openai": Mock()}):
            agent = isolated("agent", self.state)
        agent.OPENAI_MODEL = "gpt-6-astra"
        agent.create_model_completion = Mock(return_value=SimpleNamespace(model="gpt-6.1-sol", reasoning=SimpleNamespace(effort="high"), service_tier="default"))
        self.state.request_context.value = {"model_options": {"model": "gpt-6.1-sol", "effort": "high"}}
        for messages in ([{"role": "user", "content": "test"}], [{"role": "tool", "content": "result"}]):
            agent._create_chat_completion(messages, [])
            self.assertEqual(agent.create_model_completion.call_args.args[1], "gpt-6.1-sol")
            self.assertEqual(agent.create_model_completion.call_args.kwargs, {"effort": "high"})
        self.assertEqual(self.state.request_context.value["last_model_response"]["service_tier"], "default")
        self.state.request_context.value["customer"] = True
        agent._create_chat_completion([], [])
        self.assertEqual(agent.create_model_completion.call_args.args[1], "gpt-6-astra")
        self.assertIsNone(agent.create_model_completion.call_args.kwargs["effort"])

    def test_heartbeat_model_applies_to_every_tool_round_and_restores_chat(self):
        with patch.dict("sys.modules", {"openai": Mock()}):
            agent = isolated("agent", self.state)
        agent.OPENAI_API_KEY = "test-key"
        agent.OPENAI_MODEL = "gpt-6-astra"
        agent.MAX_CONSECUTIVE_TOOL_REPEATS = 2
        agent._preview_text = Mock(side_effect=str)
        agent.load_heartbeat_text.return_value = ""
        agent.build_agent_system_prompt.return_value = "system"
        agent.get_all_tools.return_value = []
        agent.execute_tool.return_value = "file contents"
        agent._build_tool_budget_prompt = Mock(return_value="")
        agent._log_tool_event = Mock()
        original = {"model_options": {"model": "chat-model", "effort": "high"}}
        self.state.request_context.value = original
        tool_call = SimpleNamespace(id="call_1", function=SimpleNamespace(name="read_file", arguments='{"path":"test"}'))
        for configured, expected in (("gpt-6.1-sol", "gpt-6.1-sol"), ("", "gpt-6-astra")):
            with self.subTest(model=configured):
                agent.HEARTBEAT_MODEL = configured
                agent.create_model_completion = Mock(side_effect=[
                    SimpleNamespace(model=expected, choices=[SimpleNamespace(message=SimpleNamespace(content="", tool_calls=[tool_call]))]),
                    SimpleNamespace(model=expected, choices=[SimpleNamespace(message=SimpleNamespace(content="HEARTBEAT_OK", tool_calls=[]))]),
                ])
                self.assertEqual(agent.run_agent_heartbeat_check(), "HEARTBEAT_OK")
                self.assertEqual(agent.create_model_completion.call_count, 2)
                for call in agent.create_model_completion.call_args_list:
                    self.assertEqual(call.args[1], expected)
                    self.assertIsNone(call.kwargs["effort"])
                self.assertIs(self.state.request_context.value, original)
                self.assertNotIn("last_model_response", original)
                agent.create_model_completion.side_effect = None
                agent.create_model_completion.return_value = SimpleNamespace(model="chat-model")
                agent._create_chat_completion([], [])
                self.assertEqual(agent.create_model_completion.call_args.args[1], "chat-model")
                original.pop("last_model_response")

    def test_heartbeat_restores_context_even_on_exception(self):
        with patch.dict("sys.modules", {"openai": Mock()}):
            agent = isolated("agent", self.state)
        agent.HEARTBEAT_MODEL = "gpt-6.1-sol"
        agent.load_heartbeat_text.return_value = ""
        agent.ask_chatgpt = Mock(side_effect=RuntimeError("failed"))
        original = {"model_options": {"model": "chat-model"}}
        for existing in (True, False):
            with self.subTest(existing=existing):
                if existing:
                    self.state.request_context.value = original
                else:
                    del self.state.request_context.value
                with self.assertRaisesRegex(RuntimeError, "failed"):
                    agent.run_agent_heartbeat_check()
                if existing:
                    self.assertIs(self.state.request_context.value, original)
                else:
                    self.assertFalse(hasattr(self.state.request_context, "value"))

    def test_heartbeat_config_supports_missing_blank_and_explicit_model(self):
        from app_config import loader
        for values, expected in (({}, ""), ({"HEARTBEAT_MODEL": "  "}, ""), ({"HEARTBEAT_MODEL": " gpt-6.1-sol "}, "gpt-6.1-sol")):
            with self.subTest(values=values), patch.object(loader, "_read_module", return_value=SimpleNamespace(**values)):
                self.assertEqual(loader.load_settings().heartbeat_model, expected)

    def test_tool_executor_rejects_customer_even_if_called_directly(self):
        tools = isolated("tools", self.state)
        self.state.request_context.value = {"customer": True}
        self.assertEqual(tools.get_all_tools("full"), [])
        for name in ("run_shell", "read_file", "read_skill", "start_agent_job", "create_scheduled_task"):
            with self.assertRaises(PermissionError):
                tools.execute_tool(name, {})
        self.state.request_context.value = {}
        self.assertTrue(tools.get_all_tools("full"))


if __name__ == "__main__":
    unittest.main()
