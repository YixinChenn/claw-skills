import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from bot_runtime.model_api import create_model_completion


class ModelApiTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.tools = [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}]

    def response(self, output, status="completed"):
        self.client.responses.create.return_value = SimpleNamespace(
            model="gpt-6.1-sol", status=status, error=None, incomplete_details=None,
            output=[SimpleNamespace(model_dump=Mock(return_value=item)) for item in output],
        )

    def test_tool_results_replay_reasoning_and_parallel_calls(self):
        output = [
            {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "encrypted"},
            {"type": "function_call", "id": "fc_1", "call_id": "call_a", "name": "read_file", "arguments": '{"path":"a"}'},
            {"type": "function_call", "id": "fc_2", "call_id": "call_b", "name": "read_file", "arguments": '{"path":"b"}'},
        ]
        self.response(output)
        messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "read files"}]
        result = create_model_completion(self.client, "gpt-6.1-sol", messages, self.tools)
        message = result.choices[0].message
        self.assertEqual([call.id for call in message.tool_calls], ["call_a", "call_b"])
        self.assertEqual(message.tool_calls[0].function.arguments, '{"path":"a"}')
        kwargs = self.client.responses.create.call_args.kwargs
        self.assertEqual(kwargs["tools"][0]["name"], "read_file")
        self.assertFalse(kwargs["tools"][0]["strict"])
        self.assertFalse(kwargs["store"])
        self.assertIn("reasoning.encrypted_content", kwargs["include"])
        messages.append({"role": "assistant", "content": "", "responses_output": message.responses_output})
        messages.extend([{"role": "tool", "tool_call_id": call.id, "content": "file content"} for call in message.tool_calls])
        self.response([{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]}])
        result = create_model_completion(self.client, "gpt-6.1-sol", messages, self.tools)
        items = self.client.responses.create.call_args.kwargs["input"]
        self.assertEqual(items[2:5], output)
        self.assertEqual([item["call_id"] for item in items[5:]], ["call_a", "call_b"])
        self.assertTrue(all(item["type"] == "function_call_output" for item in items[5:]))
        self.assertEqual(result.choices[0].message.content, "done")
        self.client.chat.completions.create.assert_not_called()

    def test_image_and_tool_free_request(self):
        self.response([])
        messages = [{"role": "user", "content": [{"type": "text", "text": "image"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}]
        create_model_completion(self.client, "gpt-6.1-sol", messages, None)
        kwargs = self.client.responses.create.call_args.kwargs
        self.assertNotIn("tools", kwargs)
        self.assertNotIn("tool_choice", kwargs)
        self.assertEqual(kwargs["input"][0]["content"][1], {"type": "input_image", "image_url": "data:image/png;base64,AAAA", "detail": "auto"})
        self.assertEqual(messages[0]["content"][0]["type"], "text")

    def test_explicit_effort_and_response_metadata(self):
        self.response([])
        response = self.client.responses.create.return_value
        response.reasoning = SimpleNamespace(effort="high")
        response.service_tier = "default"
        result = create_model_completion(self.client, "gpt-6-astra", [], None, effort="high")
        self.assertEqual(self.client.responses.create.call_args.kwargs["reasoning"], {"effort": "high"})
        self.assertEqual(result.reasoning.effort, "high")
        self.assertEqual(result.service_tier, "default")
        create_model_completion(self.client, "legacy", [], None, effort="low")
        self.assertEqual(self.client.chat.completions.create.call_args.kwargs["reasoning_effort"], "low")

    def test_legacy_models_keep_chat_completions(self):
        messages = [{"role": "user", "content": "hello"}]
        expected = self.client.chat.completions.create.return_value
        self.assertIs(create_model_completion(self.client, "gpt-5.4", messages, self.tools), expected)
        self.client.chat.completions.create.assert_called_once_with(model="gpt-5.4", messages=messages, tools=self.tools, tool_choice="auto")
        self.client.responses.create.assert_not_called()
        create_model_completion(self.client, "gpt-5.4", messages, None)
        self.client.chat.completions.create.assert_called_with(model="gpt-5.4", messages=messages)

    def test_snapshot_and_astra_routing(self):
        self.response([])
        for model in ("gpt-6.1-sol-2026-10-01", "gpt-6-astra"):
            create_model_completion(self.client, model, [], None)
            self.assertEqual(self.client.responses.create.call_args.kwargs["model"], model)
        self.client.chat.completions.create.assert_not_called()

    def test_incomplete_response_is_reported(self):
        self.response([], status="incomplete")
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            create_model_completion(self.client, "gpt-6.1-sol", [], None)

    def test_refusal_is_not_an_empty_reply(self):
        self.response([{"type": "message", "role": "assistant", "content": [{"type": "refusal", "refusal": "Cannot comply"}]}])
        result = create_model_completion(self.client, "gpt-6.1-sol", [], None)
        self.assertEqual(result.choices[0].message.content, "Cannot comply")


if __name__ == "__main__":
    unittest.main()
