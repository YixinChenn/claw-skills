import importlib.util
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


class RichMessageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "bot_runtime.messaging", Path(__file__).resolve().parents[1] / "bot_runtime/messaging.py"
        )
        cls.messaging = importlib.util.module_from_spec(spec)
        with patch.dict("sys.modules", {
            "lark_oapi.api.im.v1": Mock(),
            "bot_runtime.config_runtime": Mock(),
        }):
            spec.loader.exec_module(cls.messaging)

    def parse(self, data, mentions=None):
        text, parsed = self.messaging.parse_message_content("post", json.dumps(data), mentions)
        self.assertEqual(parsed, data)
        return text

    def test_screenshot_path_and_inline_code_are_preserved(self):
        path = r"D:\Projects\ExampleGame\Plugins\Gameplay\Source\Public\RotationSync.h"
        data = {"title": "", "content": [[
            {"tag": "text", "text": path + " 查一下 P4 历史记录，什么时候添加的 "},
            {"tag": "text", "text": "SyncRotationPitch", "style": ["code"]},
            {"tag": "text", "text": " Override"},
        ]]}
        self.assertEqual(self.parse(data), path + " 查一下 P4 历史记录，什么时候添加的 SyncRotationPitch Override")

    def test_localized_title_and_paragraphs_choose_one_language(self):
        post = {"title": "标题", "content": [[{"tag": "text", "text": "第一行"}], [{"tag": "md", "text": "**第二行**"}]]}
        data = {"en_us": {"content": [[{"tag": "text", "text": "duplicate"}]]}, "zh_cn": post}
        self.assertEqual(self.parse(data), "标题\n第一行\n**第二行**")
        self.assertEqual(self.parse({"en_us": post}), "标题\n第一行\n**第二行**")

    def test_link_code_and_mentions(self):
        mention = SimpleNamespace(key="@_user_1", name="测试用户", id=SimpleNamespace(open_id="ou_1", user_id="u_1", union_id="on_1"))
        data = {"content": [
            [{"tag": "at", "user_id": "ou_1"}, {"tag": "text", "text": " 看 "}, {"tag": "a", "text": "文档", "href": "https://example.com/doc"}],
            [{"tag": "code_block", "language": "PYTHON", "text": "if x:\n    run()"}],
            [{"tag": "text", "text": "@_user_1"}],
        ]}
        self.assertEqual(self.parse(data, [mention]), "@测试用户 看 文档 (https://example.com/doc)\nif x:\n    run()\n@测试用户")

    def test_media_and_empty_post_are_explicit(self):
        self.assertEqual(self.parse({"content": [[{"tag": "img", "image_key": "img_1"}]]}), "[图片]")
        self.assertEqual(self.parse({"content": []}), "[富文本消息：无可提取正文]")
        self.assertEqual(self.parse({"content": [None, [None, {"tag": "text", "text": "正文"}]]}), "正文")

    def test_text_and_image_behavior_unchanged(self):
        self.assertEqual(self.messaging.parse_message_content("text", '{"text":"  hello  "}')[0], "hello")
        self.assertEqual(self.messaging.parse_message_content("image", '{"image_key":"img_1"}')[0], "[图片消息]")

    def test_quoted_post_uses_same_parser(self):
        item = SimpleNamespace(msg_type="post", body=SimpleNamespace(content=json.dumps({
            "content": [[{"tag": "text", "text": "被引用的正文"}]]
        })), mentions=[])
        self.messaging.client.im.v1.message.get.return_value = SimpleNamespace(
            success=lambda: True, data=SimpleNamespace(items=[item])
        )
        text = self.messaging.fetch_message_text("om_quoted")
        self.assertTrue(text.startswith("被引用的正文"))
        self.assertNotIn("[post 消息]", text)


if __name__ == "__main__":
    unittest.main()
