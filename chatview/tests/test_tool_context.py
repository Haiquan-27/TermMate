import sys
import unittest
from unittest.mock import MagicMock, patch

sys.modules.setdefault("sublime", MagicMock())

from chatview.chatprocessor import OpenCodeMessageProcessor, ToolContext


class TestToolContextPipeline(unittest.TestCase):

    def setUp(self):
        self.session = MagicMock()
        self.session.agent_thread.cwd = "/workspace"
        self.session.get_setting.side_effect = (
            lambda key, default=None: {
                "tool_output_inline_lines": 3,
                "tool_output_inline_max_chars": 400,
            }.get(key, default)
        )
        self.processor = OpenCodeMessageProcessor(self.session)

    def test_command_context(self):
        ctx = self.processor._build_tool_context({
            "name": "command_execution",
            "command": "echo hi",
            "status": "success",
        })
        self.assertEqual(ctx.kind, "command")
        self.assertEqual(ctx.text, "⏺ command echo hi")
        self.assertEqual(ctx.detail, "echo hi")
        self.assertTrue(ctx.show_command)
        self.assertFalse(ctx.is_diff)

    def test_diff_context_from_metadata(self):
        diff = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n"
        ctx = self.processor._build_tool_context({
            "name": "edit",
            "input": {"filePath": "/workspace/foo.py"},
            "metadata": {"diff": diff},
        })
        self.assertTrue(ctx.is_diff)
        self.assertEqual(ctx.diff_text, diff)
        self.assertEqual(len(ctx.diffs), 1)
        self.assertEqual(ctx.kind, "diff")
        self.assertTrue(ctx.has_inline_body)

    def test_multifile_diffs_are_explicit(self):
        changes = [
            {"path": "/workspace/a.py", "diff": "@@ -1 +1 @@\n-a\n+b\n"},
            {"path": "/workspace/b.py", "diff": "@@ -1 +1 @@\n-c\n+d\n"},
        ]
        ctx = self.processor._build_tool_context({
            "name": "fileChange",
            "changes": changes,
        })
        self.assertTrue(ctx.is_diff)
        self.assertEqual(len(ctx.diffs), 2)
        self.assertTrue(ctx.is_multi)
        self.assertFalse(ctx.has_phantom)

    def test_format_tool_call_is_facade(self):
        block = {"name": "read", "input": {"filePath": "/workspace/foo.py"}}
        ctx = ToolContext(text="x", has_phantom=False)
        with patch.object(self.processor, "_build_tool_context",
                          return_value=ctx) as build, \
             patch.object(self.processor, "_render_tool",
                          return_value=("rendered", [])) as render:
            result = self.processor.format_tool_call(block, key="k1")
        self.assertEqual(result, ("rendered", []))
        build.assert_called_once_with(block, "k1")
        render.assert_called_once_with(ctx, "k1")

    def test_render_tool_uses_only_context(self):
        ctx = ToolContext(
            text="⏺ read foo.py",
            heading="⏺ read",
            summary="foo.py",
            detail="⏺ read foo.py",
            output_summary="3 lines success",
            output_detail="line1\n",
            has_output=True,
        )
        text, phantoms = self.processor._render_tool(ctx, key="k2")
        self.assertEqual(text, "⏺ read foo.py")
        self.assertEqual(len(phantoms), 1)
        self.assertEqual(phantoms[0]["output"]["summary"], "3 lines success")
        self.assertEqual(phantoms[0]["command"]["detail"], "⏺ read foo.py")

    def test_edit_from_input_strings_keeps_diff_path(self):
        # Provider synthesizes a diff from oldString/newString; the diff is
        # not a literal metadata.diff on the block, so has_inline_body must
        # still route this through the diff branch.
        block = {
            "name": "edit",
            "input": {
                "filePath": "/workspace/foo.py",
                "oldString": "print('a')",
                "newString": "print('b')",
            },
            "status": "success",
        }
        ctx = self.processor._build_tool_context(block)
        self.assertTrue(ctx.is_diff)
        self.assertTrue(ctx.has_inline_body)
        self.assertIn("⏺ edit", ctx.text)
        self.assertTrue(ctx.has_output)

    def test_diff_without_diff_text_not_rendered_as_diff(self):
        # name is diff-like but no diff content exists: must not take diff
        # inline branch; it should be a normal tool row.
        ctx = self.processor._build_tool_context({
            "name": "edit",
            "input": {"filePath": "/workspace/foo.py"},
            "status": "completed",
        })
        self.assertTrue(ctx.is_diff)
        self.assertFalse(ctx.has_inline_body)
        self.assertEqual(ctx.text, "⏺ edit foo.py")

    def test_renderer_ignores_text_heuristics(self):
        # Renderer must not guess from text; context fields drive output.
        ctx = ToolContext(
            text="⏺ fileChange a.py\n@@\n-old\n+new",
            heading="⏺ fileChange",
            summary="a.py",
            detail="⏺ fileChange a.py",
            is_diff=True,
            has_inline_body=True,
            has_phantom=True,
            output_summary="3 lines success",
            output_detail="",
            has_output=True,
        )
        text, phantoms = self.processor._render_tool(ctx, key="k3")
        self.assertEqual(len(phantoms), 1)
        self.assertEqual(phantoms[0]["output"]["summary"], "3 lines success")
        self.assertNotIn("status", phantoms[0])

    def test_multi_file_structure_marks_multi(self):
        ctx = self.processor._build_tool_context({
            "name": "fileChange",
            "changes": [
                {"path": "/workspace/a.py", "diff": "@@ -1 +1 @@\n-a\n+b\n"},
                {"path": "/workspace/b.py", "diff": "@@ -1 +1 @@\n-c\n+d\n"},
            ],
        })
        self.assertTrue(ctx.is_multi)
        self.assertFalse(ctx.has_phantom)
        self.assertGreaterEqual(len(ctx.diffs), 2)


if __name__ == "__main__":
    unittest.main()
