import unittest

from bot_runtime.model_controls import default_options, parse_control


class ModelControlTests(unittest.TestCase):
    def setUp(self):
        self.defaults = default_options("gpt-6-astra")

    def test_default_options_use_balanced(self):
        for model in ("gpt-6-astra", "gpt-6.1-sol"):
            self.assertEqual(default_options(model), {"model": "gpt-6.1-sol", "effort": "high"})

    def test_modes_reset_model_and_set_effort(self):
        for mode, model, effort in (("quick", "gpt-6.1-sol", "medium"), ("balanced", "gpt-6.1-sol", "high"), ("deep", "gpt-6-astra", "high")):
            with self.subTest(mode=mode):
                current = {"model": "gpt-6.1-sol", "effort": "max"}
                result = parse_control("/mode " + mode, current, "gpt-6-astra")
                self.assertEqual(result[1], {"model": model, "effort": effort})
                self.assertEqual(current, {"model": "gpt-6.1-sol", "effort": "max"})

    def test_one_turn_preserves_body_and_current_settings(self):
        result = parse_control('/ask --model gpt-6-astra --effort low -- 检查 "D:\\file.txt"\n保留 --fast 字样', self.defaults, "gpt-6-astra")
        self.assertIsNone(result[1])
        self.assertEqual(result[2], {"model": "gpt-6-astra", "effort": "low"})
        self.assertEqual(result[3], '检查 "D:\\file.txt"\n保留 --fast 字样')
        self.assertEqual(self.defaults, {"model": "gpt-6.1-sol", "effort": "high"})

    def test_invalid_parameters_do_not_silently_fallback(self):
        for command in ("/effort none", "/model unknown", "/mode unknown", "/status extra",
                        "/ask --effort", "/ask --effort high --effort low -- test",
                        "/ask --fast on -- test", "/ask --unknown value -- test", "/ask --effort low -- "):
            with self.subTest(command=command), self.assertRaises(ValueError):
                parse_control(command, self.defaults, "gpt-6-astra")
        self.assertIn("尚未验证", parse_control("/fast on", self.defaults, "gpt-6-astra")[0])
