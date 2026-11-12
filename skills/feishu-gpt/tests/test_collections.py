import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from test_customer_sessions import RuntimeTests, SessionTests
from bot_runtime.collections import CollectionStore
from bot_runtime.customer_sessions import conversation_scope


class CollectionStorageTests(SessionTests):
    def test_restart_isolation_order_and_recall_last_auto_cancels(self):
        store = CollectionStore(self.store)
        store.start("a", "summarize", "start")
        batch = store.active("a")
        store.add(batch["id"], "m1", "one", [])
        store.add(batch["id"], "m2", "two", ["data:image/png;base64,AA"])
        restored = CollectionStore(self.store)
        self.assertIsNone(restored.active("b"))
        self.assertIn("2 条", restored.pending("a"))
        restored.recall("m1")
        self.assertIn("1 条", restored.pending("a"))
        _, prompt, images = restored.begin("a", "", "run")
        self.assertNotIn('"one"', prompt)
        self.assertIn('"two"', prompt)
        self.assertEqual(len(images), 1)
        self.assertEqual(restored.recall("m2"), ["run"])
        self.assertIsNone(restored.active("a"))
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM collection_items").fetchone()[0], 0)
        session = self.store.current("a")
        self.assertFalse(self.store.complete(session["id"], "run", "stale", [], batch_id=batch["id"], user_content="stale"))

    def test_recall_before_enqueue_and_duplicate_delivery(self):
        store = CollectionStore(self.store)
        store.start("a", "summarize", "start")
        batch = store.active("a")
        store.recall("withdrawn")
        store.add(batch["id"], "withdrawn", "secret", [])
        store.add(batch["id"], "m", "one", [])
        store.add(batch["id"], "m", "duplicate", [])
        self.assertIn("1 条", store.pending("a"))
        store.recall("start")
        self.assertIsNone(store.active("a"))

    def test_limits_and_requirements_are_explicit(self):
        store = CollectionStore(self.store)
        store.start("a", "", "start")
        batch = store.active("a")
        with self.assertRaises(ValueError):
            store.begin("a", "summary", "empty")
        with self.assertRaises(ValueError):
            store.add(batch["id"], "long", "x" * 40001, [])
        with self.assertRaises(ValueError):
            store.add(batch["id"], "images", "images", ["a", "b", "c"])
        store.add(batch["id"], "m", "one", [])
        with self.assertRaises(ValueError):
            store.begin("a", "", "run")
        _, _, _ = store.begin("a", "summary", "run")
        store.release(batch["id"], "run")
        self.assertEqual(store.active("a")["status"], "collecting")


class CollectionRuntimeTests(RuntimeTests):
    def test_collect_does_not_call_model_and_run_combines_once(self):
        self.message("/collect summarize", message_id="start")
        self.message("one", message_id="m1")
        self.message("two", message_id="m2")
        self.runtime.ask_chatgpt.assert_not_called()
        self.runtime.send_reply.assert_called_once()
        self.message("/pending", message_id="pending")
        self.assertIn("2 条", self.runtime.send_reply.call_args.args[1])
        self.message("/run keep order", message_id="run")
        self.runtime.ask_chatgpt.assert_called_once()
        prompt = self.runtime.ask_chatgpt.call_args.args[0]
        self.assertIn("one", prompt)
        self.assertIn("two", prompt)
        self.assertIn("keep order", prompt)
        self.assertFalse(self.runtime.ask_chatgpt.call_args.kwargs["allow_tools"])
        self.assertIn("外部数据", self.runtime.ask_chatgpt.call_args.args[1])
        self.assertIsNone(CollectionStore(self.store).active(conversation_scope("chat", "user")))
        self.message("/run", message_id="again")
        self.runtime.ask_chatgpt.assert_called_once()

    def test_cancel_commands_are_material_and_customer_isolation(self):
        self.message("/collect summarize", message_id="start")
        self.message("/agent-run codex delete", message_id="material")
        self.message("/new", message_id="new")
        self.runtime.handle_command.assert_not_called()
        self.runtime.ask_chatgpt.assert_not_called()
        self.message("/pending", user="other", message_id="other")
        self.assertIn("没有", self.runtime.send_reply.call_args.args[1])
        self.message("/cancel", message_id="cancel")
        self.message("/run", message_id="run")
        self.runtime.ask_chatgpt.assert_not_called()
        self.message("normal", message_id="normal")
        self.runtime.ask_chatgpt.assert_called_once()

    def test_recall_event_removes_material_and_interrupts_running_batch(self):
        self.state.cancel_pending_message = Mock(return_value=False)
        self.message("/collect summary", message_id="start")
        self.message("sensitive", message_id="m1")
        def recall_then_answer(*args, **kwargs):
            self.runtime.on_message_recalled(SimpleNamespace(event=SimpleNamespace(message_id="m1", chat_id="chat")))
            return "stale answer"
        self.runtime.ask_chatgpt.side_effect = recall_then_answer
        self.runtime.send_reply.reset_mock()
        self.message("/run", message_id="run")
        self.state.cancel_pending_message.assert_any_call("run")
        self.runtime.send_reply.assert_not_called()
        self.assertIsNone(CollectionStore(self.store).active(conversation_scope("chat", "user")))
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)

    def test_recalled_queued_material_never_reenters_collection(self):
        self.state.cancel_pending_message = Mock(return_value=False)
        self.message("/collect summary", message_id="start")
        self.message("keep", message_id="keep")
        self.runtime.on_message_recalled(SimpleNamespace(event=SimpleNamespace(message_id="late", chat_id="chat")))
        self.message("withdrawn", message_id="late")
        self.message("/pending", message_id="pending")
        self.assertIn("1 条", self.runtime.send_reply.call_args.args[1])
        self.runtime.ask_chatgpt.assert_not_called()

    def test_image_download_runs_in_worker_and_persists_until_run(self):
        self.message("/collect describe", message_id="start")
        image = "data:image/png;base64,AA"
        self.runtime.fetch_message_image_data_urls.return_value = [image]
        self.runtime.process_and_reply("chat", "[图片消息]", "user", ["user"], "chat", "image",
                                       source_type="image", source_content={"image_key": "test"})
        self.runtime.fetch_message_image_data_urls.assert_called_once_with("image", {"image_key": "test"})
        self.runtime.ask_chatgpt.assert_not_called()
        self.message("/run", message_id="run")
        self.assertEqual(self.runtime.ask_chatgpt.call_args.kwargs["image_data_urls"], [image])

    def test_unreadable_material_is_rejected_and_admin_keeps_tools(self):
        self.message("/collect summarize", user="admin", message_id="start")
        self.runtime.process_and_reply("chat", "[merge_forward 消息]", "admin", ["admin"], "chat", "bad",
                                       material_issue="请逐条转发")
        self.assertIn("未收集", self.runtime.send_reply.call_args.args[1])
        self.message("usable", user="admin", message_id="good")
        self.message("/run", user="admin", message_id="run")
        self.assertTrue(self.runtime.ask_chatgpt.call_args.kwargs["allow_tools"])
        self.assertNotIn("merge_forward", self.runtime.ask_chatgpt.call_args.args[0])

    def test_api_failure_keeps_material_for_explicit_retry(self):
        self.message("/collect summary", message_id="start")
        self.message("one", message_id="m1")
        self.runtime.ask_chatgpt.return_value = "（响应超时，请重试）"
        self.message("/run", message_id="run")
        batch = CollectionStore(self.store).active(conversation_scope("chat", "user"))
        self.assertEqual(batch["status"], "collecting")
        self.assertIn("保留", self.runtime.send_reply.call_args.args[1])


def load_tests(loader, standard_tests, pattern):
    suite = unittest.TestSuite()
    for cls in (CollectionStorageTests, CollectionRuntimeTests):
        suite.addTests(cls(name) for name in cls.__dict__ if name.startswith("test_"))
    return suite
