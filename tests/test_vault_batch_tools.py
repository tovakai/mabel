"""Regression tests for workspace-scoped bulk Markdown transformations."""
import tempfile
import unittest
from pathlib import Path
from src.agent_tools.vault_batch_tools import run_batch


class BulkTest(unittest.TestCase):
    def test_preview_apply_frontmatter_and_preserve_other_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            a = root / "A Bug_s Life.md"
            a.write_bytes(b'---\r\nName: A Bug\'s Life\r\nYear: 1998\r\n---\r\ninventory: [[DVD.base]]\r\nrelated: [[Disney]]\r\n')
            b = root / "Other.md"
            b.write_text('---\nName: Other\n---\n', encoding='utf-8')
            args = {"glob": "**/*.md", "operation": "frontmatter", "contains_line": "inventory: [[DVD.base]]", "set_properties": {"owned": True, "owned_formats": "DVD", "storage": "living room"}, "remove_lines": ["inventory: [[DVD.base]]"]}
            preview = run_batch(root, args)
            self.assertEqual((preview['scanned'], preview['matched'], preview['to_change']), (2, 1, 1))
            self.assertIn('---', preview['sample_diffs'][0]['diff'])
            applied = run_batch(root, {**args, "mode": "apply", "plan_id": preview['plan_id']})
            self.assertEqual(applied['updated'], 1)
            data = a.read_bytes()
            self.assertIn(b'owned: true\r\n', data)
            self.assertIn(b'storage: living room\r\n---\r\nrelated: [[Disney]]', data)
            self.assertNotIn(b'inventory:', data)
            self.assertEqual(b.read_text(), '---\nName: Other\n---\n')

    def test_rejects_stale_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            p = root / "note.md"
            p.write_text('hello x')
            args = {"operation": "replace", "glob": "*.md", "old_string": "x", "new_string": "y"}
            preview = run_batch(root, args)
            p.write_text('hello x!')
            with self.assertRaisesRegex(ValueError, 'changed since preview'):
                run_batch(root, {**args, "mode": "apply", "plan_id": preview['plan_id']})
            self.assertEqual(p.read_text(), 'hello x!')

    def test_refuses_nested_yaml_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            p = root / "note.md"
            p.write_text('---\nGenre:\n  - Drama\n---\n')
            with self.assertRaisesRegex(ValueError, 'nested YAML value'):
                run_batch(root, {"glob": "*.md", "set_properties": {"Genre": "Comedy"}})
            self.assertEqual(p.read_text(), '---\nGenre:\n  - Drama\n---\n')

    def test_rejects_escape_glob(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'relative Markdown'):
                run_batch(Path(directory), {"glob": "../secrets/*.md", "set_properties": {"owned": True}})


if __name__ == "__main__":
    unittest.main()
