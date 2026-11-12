import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bot_runtime import claude_inbox
from bot_runtime.claude_replies import ClaudeReplyStore, build_topic_prompt


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.session = {
            "pid": 1234, "sessionId": "session-a", "procStart": "123", "pidDomain": "test",
            "peerProtocol": 1, "messagingSocketPath": r"\\.\pipe\LOCAL\cc-msg-" + "a" * 32,
        }
        self.key = {"peerToken": "b" * 32, "procStartFt": "123", "pidDomain": "test"}
        self.save()

    def save(self):
        self.record = self.root / f"{self.session['pid']}.json"
        self.record.write_text(json.dumps(self.session), encoding="utf-8")
        digest = hashlib.sha256(self.session["messagingSocketPath"].lower().encode()).hexdigest()
        self.key_path = self.root / f"{self.session['pid']}.{digest}.key"
        self.key_path.write_text(json.dumps(self.key), encoding="utf-8")

    def test_exact_session_and_pipe_key_ignores_unrelated_key(self):
        (self.root / "1234.unrelated.key").write_text("not a key", encoding="utf-8")
        with patch.object(claude_inbox, "process_start", return_value="123"):
            session, token = claude_inbox.resolve_session("session-a", self.root)
        self.assertEqual(session, self.session)
        self.assertEqual(token, self.key["peerToken"])
        with self.assertRaisesRegex(RuntimeError, "未运行"):
            claude_inbox.resolve_session("other-session", self.root)

    def test_stale_process_key_domain_and_protocol_are_rejected(self):
        with patch.object(claude_inbox, "process_start", return_value="new"):
            with self.assertRaisesRegex(RuntimeError, "过期"):
                claude_inbox.resolve_session("session-a", self.root)
        with patch.object(claude_inbox, "process_start", return_value="123"):
            for field in ("procStartFt", "pidDomain", "peerToken"):
                old = self.key[field]
                self.key[field] = "invalid"
                self.save()
                with self.assertRaises(RuntimeError):
                    claude_inbox.resolve_session("session-a", self.root)
                self.key[field] = old
            self.session["peerProtocol"] = 99
            self.save()
            with self.assertRaisesRegex(RuntimeError, "协议"):
                claude_inbox.resolve_session("session-a", self.root)

    def test_only_local_pipe_accepted(self):
        self.session["messagingSocketPath"] = r"\\remote\pipe\name"
        self.save()
        with self.assertRaisesRegex(RuntimeError, "本机"):
            claude_inbox.resolve_session("session-a", self.root)

    @unittest.skipUnless(os.name == "nt", "Windows transport")
    def test_two_raw_utf8_frames_and_size_limit(self):
        with patch.object(claude_inbox, "resolve_session", return_value=(self.session, self.key["peerToken"])), \
             patch.object(claude_inbox, "_write_pipe") as write:
            claude_inbox.send_claude_message("session-a", "中文\n第二行")
            frames = write.call_args.args[1].decode("utf-8").splitlines()
            self.assertEqual(len(frames), 2)
            self.assertEqual(json.loads(frames[0]), {"type": "auth", "token": self.key["peerToken"]})
            self.assertEqual(json.loads(frames[1])["message"]["content"], "中文\n第二行")
            with self.assertRaisesRegex(ValueError, "过长"):
                claude_inbox.send_claude_message("session-a", "x" * 210000)

    @unittest.skipUnless(os.name == "nt" and shutil.which("node"), "Windows and Node test receiver")
    def test_real_named_pipe_transport(self):
        pipe = r"\\.\pipe\LOCAL\cc-msg-" + os.urandom(16).hex()
        script = """
const net = require('net');
const timer = setTimeout(() => process.exit(2), 5000);
const server = net.createServer(socket => {
  let buffer = '';
  socket.setEncoding('utf8');
  socket.on('data', chunk => {
    buffer += chunk;
    if (buffer.split('\\n').length >= 3) {
      console.log(JSON.stringify(buffer.trim().split('\\n').map(JSON.parse)));
      socket.end(); server.close(); clearTimeout(timer);
    }
  });
});
server.listen(process.argv[1], () => console.log('READY'));
"""
        server = subprocess.Popen(
            [shutil.which("node"), "-e", script, pipe], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8",
        )
        try:
            self.assertEqual(server.stdout.readline().strip(), "READY")
            self.session.update(pid=server.pid, procStart=claude_inbox.process_start(server.pid),
                                messagingSocketPath=pipe)
            self.key["procStartFt"] = self.session["procStart"]
            self.record.unlink()
            self.save()
            claude_inbox.send_claude_message("session-a", "真实管道测试", self.root)
            output, error = server.communicate(timeout=7)
            self.assertEqual(server.returncode, 0, error)
            frames = json.loads(output)
            self.assertEqual(frames[0]["token"], self.key["peerToken"])
            self.assertEqual(frames[1]["message"]["content"], "真实管道测试")
        finally:
            if server.poll() is None:
                server.kill()
            server.communicate()


class ReplyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "requests.sqlite3"
        self.store = ClaudeReplyStore(self.path)
        self.request_id = self.store.create("om_input", "om_root", "session-a")

    def test_route_survives_restart_and_input_is_deduplicated(self):
        restored = ClaudeReplyStore(self.path)
        self.assertIsNone(restored.create("om_input", "different-root", "other-session"))
        send = Mock(return_value=True)
        restored.reply(self.request_id, "answer", send)
        send.assert_called_once_with("om_root", "answer", "claude:" + self.request_id)
        restored.reply(self.request_id, "answer", send)
        self.assertEqual(send.call_count, 1)
        with self.assertRaisesRegex(ValueError, "相同正文"):
            restored.reply(self.request_id, "changed", send)

    def test_failed_reply_can_retry_only_same_body_with_same_key(self):
        send = Mock(side_effect=[False, True])
        with self.assertRaisesRegex(RuntimeError, "回复失败"):
            self.store.reply(self.request_id, "answer", send)
        with self.assertRaises(ValueError):
            self.store.reply(self.request_id, "changed", send)
        self.store.reply(self.request_id, "answer", send)
        self.assertEqual(send.call_args_list[0], send.call_args_list[1])

    def test_expired_missing_or_failed_requests_never_send(self):
        send = Mock()
        with self.assertRaises(ValueError):
            self.store.reply("missing", "answer", send)
        self.store.set_delivery(self.request_id, "failed")
        with self.assertRaises(ValueError):
            self.store.reply(self.request_id, "answer", send)
        with self.store.connect() as db:
            db.execute("UPDATE requests SET state='written',created_at=?", (time.time() - 90000,))
        with self.assertRaises(ValueError):
            self.store.reply(self.request_id, "answer", send)
        send.assert_not_called()

    def test_fast_reply_is_not_overwritten_by_transport_status(self):
        self.store.reply(self.request_id, "answer", Mock(return_value=True))
        self.store.set_delivery(self.request_id, "written")
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT state FROM requests").fetchone()[0], "replied")

    def test_reaction_is_removed_only_after_successful_reply_and_survives_restart(self):
        remove = Mock(return_value=True)
        self.store.track_reaction(self.request_id, "reaction-a", remove)
        remove.assert_not_called()
        restored = ClaudeReplyStore(self.path)
        with self.assertRaises(RuntimeError):
            restored.reply(self.request_id, "answer", Mock(return_value=False), remove)
        remove.assert_not_called()
        restored.reply(self.request_id, "answer", Mock(return_value=True), remove)
        remove.assert_called_once_with("om_input", "reaction-a")
        with restored.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM topic_reactions").fetchone()[0], 0)

    def test_fast_reply_cleans_late_reaction_and_failed_cleanup_does_not_resend(self):
        send = Mock(return_value=True)
        self.store.reply(self.request_id, "answer", send)
        remove = Mock(side_effect=[False, RuntimeError("network"), True])
        self.store.track_reaction(self.request_id, "late-reaction", remove)
        self.store.clear_replied_reactions(remove)
        self.store.reply(self.request_id, "answer", send, remove)
        self.assertEqual(remove.call_count, 3)
        self.assertEqual(send.call_count, 1)
        self.store.clear_replied_reactions(remove)
        self.assertEqual(remove.call_count, 3)

    def test_compact_prompt_preserves_reply_route_and_user_text(self):
        text = '中文\n"; exit; #'
        prompt = build_topic_prompt(self.request_id, text)
        self.assertIn("feishu-agent-notify", prompt)
        self.assertIn("请求 ID：" + self.request_id, prompt)
        self.assertNotIn("--text-file", prompt)
        self.assertNotIn("python.exe", prompt)
        self.assertLess(len(prompt) - len(json.dumps(text, ensure_ascii=False)), 180)
        self.assertEqual(json.loads(prompt.split("以下 JSON 字符串是用户消息正文：\n")[1]), text)



class StartTopicTests(unittest.TestCase):
    def setUp(self):
        import sys
        from types import SimpleNamespace
        from bot_runtime import claude_replies

        self.replies = claude_replies
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        registry = self.root / "sessions"
        registry.mkdir()
        self.session = {
            "sessionId": "session-a", "name": "Claude 测试", "cwd": "D:/project",
            "messagingSocketPath": r"\\.\pipe\LOCAL\cc-msg-" + "c" * 32,
        }
        (registry / "123.json").write_text(json.dumps(self.session), encoding="utf-8")
        deps = {
            "app_config": SimpleNamespace(settings=SimpleNamespace(agent_notify_enabled=True,
                agents_path=str(self.root), agent_notify_dir=str(self.root / "notify"))),
        }
        self.addCleanup(patch.stopall)
        patch.dict(sys.modules, deps).start()
        patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root),
                               "CLAUDE_CODE_MESSAGING_SOCKET": self.session["messagingSocketPath"]}).start()
        self.resolve = patch.object(claude_inbox, "resolve_session", return_value=(self.session, "private-key")).start()

    def test_start_detects_own_session_and_queues_bound_notification(self):
        result = Path(self.replies.start_topic("首条消息"))
        body = result.read_bytes()
        data = json.loads(body)
        self.assertEqual(data["source"], "claude")
        self.assertEqual(data["conversation_id"], "session-a")
        self.assertEqual(data["title"], "Claude 测试")
        self.assertEqual(data["message"], "首条消息")
        self.assertEqual(body.count(b"\n"), body.count(b"\r\n"))
        self.assertNotIn("private-key", body.decode())
        self.assertEqual(self.resolve.call_args.args[0], "session-a")
        self.assertEqual(str(result), self.replies.start_topic("首条消息"))
        self.assertFalse(list(result.parent.glob("*.tmp")))

    def test_reply_is_queued_and_only_watcher_marks_it_sent(self):
        store = self.replies.get_reply_store()
        request = store.create("om_user", "om_exact_root", "session-a")
        path = Path(self.replies.queue_reply(request, "答复"))
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data, {"kind": "claude_topic_reply", "request_id": request, "message": "答复"})
        with store.connect() as db:
            self.assertNotEqual(db.execute("SELECT state FROM requests").fetchone()[0], "replied")
        with self.assertRaises(ValueError):
            self.replies.queue_reply(request, "不同正文")
        send = Mock(return_value=True)
        store.reply(data["request_id"], data["message"], send)
        send.assert_called_once_with("om_exact_root", "答复", "claude:" + request)
        self.assertIsNone(self.replies.queue_reply(request, "答复"))

    def test_missing_or_mismatched_identity_never_guesses(self):
        with patch.dict(os.environ, {"CLAUDE_CODE_MESSAGING_SOCKET": ""}):
            with self.assertRaisesRegex(ValueError, "显式"):
                self.replies.start_topic("text")
            result = self.replies.start_topic("text", "session-a")
            self.assertTrue(Path(result).exists())
        with patch.dict(os.environ, {"CLAUDE_CODE_MESSAGING_SOCKET": "wrong"}):
            with self.assertRaisesRegex(ValueError, "不一致"):
                self.replies.start_topic("text", "session-a")

    def test_disabled_watcher_or_offline_session_never_queues(self):
        import sys
        config = sys.modules["app_config"].settings
        config.agent_notify_enabled = False
        with self.assertRaisesRegex(RuntimeError, "未启用"):
            self.replies.start_topic("text")
        config.agent_notify_enabled = True
        self.resolve.side_effect = RuntimeError("offline")
        with self.assertRaisesRegex(RuntimeError, "offline"):
            self.replies.start_topic("text")
        self.assertFalse((self.root / "notify").exists())


if __name__ == "__main__":
    unittest.main()
