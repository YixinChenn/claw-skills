
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bot_runtime import skill_loader


def isolated_module(name, dependencies):
    path = Path(__file__).resolve().parents[1] / "bot_runtime" / (name + ".py")
    spec = importlib.util.spec_from_file_location("bot_runtime." + name, path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict("sys.modules", dependencies):
        spec.loader.exec_module(module)
    return module


class SkillDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "work"
        self.user = self.root / "user"
        self.patch_home = patch("bot_runtime.skill_loader.Path.home", return_value=self.user)
        self.patch_home.start()
        self.addCleanup(self.patch_home.stop)

    def skill(self, root, name, content="---\n description unused\ndescription: test skill\n---\ninstructions"):
        path = root / name / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_precedence_catalog_and_slash(self):
        self.skill(self.user / ".agents/skills", "lark-doc", "user copy")
        self.skill(self.user / ".agents/skills", "private-other", "not exposed")
        local = self.skill(self.workspace / "skills", "lark-doc")
        self.assertEqual(skill_loader.discover_skills(str(self.workspace)), {"lark-doc": local.resolve()})
        self.assertIn("test skill", skill_loader.build_skill_catalog_prompt(str(self.workspace)))
        self.assertIn("instructions", skill_loader.build_workspace_skill_prompt(str(self.workspace), "/lark-doc read"))
        self.assertTrue(skill_loader.workspace_skill_exists(str(self.workspace), "lark-doc"))

    def test_reference_and_traversal(self):
        roots = self.user / ".agents/skills"
        doc = self.skill(roots, "lark-doc")
        self.skill(roots, "lark-drive", "drive instructions")
        ref = doc.parent / "references/read.md"
        ref.parent.mkdir()
        ref.write_text("read reference", encoding="utf-8")
        self.assertIn("read reference", skill_loader.read_skill(str(self.workspace), "lark-doc", "references/read.md"))
        self.assertIn("drive instructions", skill_loader.read_skill(str(self.workspace), "lark-doc", "../lark-drive/SKILL.md"))
        for path in ["../../../secret.txt", str(self.root / "secret.txt")]:
            with self.assertRaises(ValueError):
                skill_loader.read_skill(str(self.workspace), "lark-doc", path)
        with self.assertRaises(FileNotFoundError):
            skill_loader.read_skill(str(self.workspace), "lark-missing")


class SkillTaskTests(unittest.TestCase):
    def setUp(self):
        self.paths = Mock()
        self.paths.get_agent_workspace.return_value = "workspace"
        self.paths.build_agent_system_prompt.return_value = "base"
        self.module = isolated_module("skill_tasks", {"bot_runtime.paths": self.paths})
        self.module.read_skill = Mock(return_value="skill instructions")
        self.agent = Mock()

    def run_task(self, response, fields=()):
        self.agent.ask_chatgpt.return_value = response
        with patch.dict("sys.modules", {"bot_runtime.agent": self.agent}):
            return self.module.run_lark_skill_task("task", {"doc_ref": "doc"}, ("lark-doc",), fields)

    def test_required_skill_loaded_before_execution(self):
        self.assertEqual(self.run_task('{"ok":true,"doc_id":"doc"}', ("doc_id",))["doc_id"], "doc")
        self.module.read_skill.assert_called_once_with("workspace", "lark-doc")
        self.assertIn("skill instructions", self.agent.ask_chatgpt.call_args.args[1])

    def test_missing_skill_stops_before_model(self):
        self.module.read_skill.side_effect = FileNotFoundError("missing")
        with self.assertRaises(FileNotFoundError):
            self.run_task('{"ok":true}')
        self.agent.ask_chatgpt.assert_not_called()

    def test_bad_partial_and_missing_results_fail(self):
        for response in ["not json", "[]", '{"ok":false,"doc_id":"partial"}', '{"ok":"true"}', '{"ok":true}']:
            with self.subTest(response=response), self.assertRaises(RuntimeError):
                self.run_task(response, ("doc_id",))


class DocumentSkillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.module = isolated_module("doc_importer", {
            "bot_runtime.config_runtime": Mock(),
            "bot_runtime.messaging": Mock(),
            "bot_runtime.paths": Mock(),
            "bot_runtime.skill_tasks": Mock(),
        })
        for key in ["DOC_IMPORT_WIKI_NODE", "DOC_IMPORT_WIKI_SPACE", "DOC_IMPORT_FOLDER_TOKEN"]:
            setattr(self.module, key, "")
        self.module.DOC_IMPORT_CLI_AS = "user"
        self.module.DOC_IMPORT_BOT_AUTHOR_ENABLED = True
        self.module.APP_ID = "cli_test"
        self.module._remember_doc = Mock()
        self.source = self.root / "input.md"
        self.source.write_text("# Title\n\nBody", encoding="utf-8")
        self.body = self.root / "body.md"
        def temp_file(markdown):
            self.body.write_text(markdown, encoding="utf-8")
            return str(self.body), self.body.name
        self.module._create_temp_markdown_file = temp_file

    def test_update_uses_existing_target_and_cleans_body(self):
        def execute(task, data, skills, fields):
            self.assertEqual(data["doc_ref"], "existing")
            self.assertEqual(Path(data["markdown_path"]).read_text(encoding="utf-8"), "Body")
            self.assertEqual(skills, ("lark-doc", "lark-drive"))
            return {"ok": True, "doc_id": "existing", "doc_url": "https://example.com/docx/existing"}
        self.module.run_lark_skill_task.side_effect = execute
        result = self.module.update_lark_doc_from_markdown(str(self.source), "existing")
        self.assertEqual(result["action"], "updated")
        self.module._remember_doc.assert_called_once_with("Title", result)
        self.assertFalse(self.body.exists())

    def test_failed_creation_does_not_remember_success(self):
        self.module.run_lark_skill_task.side_effect = RuntimeError("partial")
        with self.assertRaisesRegex(RuntimeError, "partial"):
            self.module.create_lark_doc_from_markdown(str(self.source))
        self.module._remember_doc.assert_not_called()
        self.assertFalse(self.body.exists())

    def test_wiki_skill_and_index_sync(self):
        self.module.DOC_IMPORT_WIKI_SPACE = "space"
        self.module.run_lark_skill_task.return_value = {"ok": True, "doc_id": "new", "doc_url": "https://example.com/docx/new"}
        self.module.create_lark_doc_from_markdown(str(self.source))
        self.assertIn("lark-wiki", self.module.run_lark_skill_task.call_args.args[2])
        self.module.DOC_IMPORT_ONLINE_INDEX_DOC = "index"
        self.module._sync_online_document_index("# Heading\n\nIndex body")
        call = self.module.run_lark_skill_task.call_args
        self.assertEqual(call.args[1]["doc_ref"], "index")
        self.assertEqual(call.args[2], ("lark-doc",))
        self.assertFalse(self.body.exists())


if __name__ == "__main__":
    unittest.main()
