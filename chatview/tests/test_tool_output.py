import sys
import unittest
from unittest.mock import MagicMock, patch

# Sublime Text supplies these modules at plugin runtime. Mocks keep this
# test runnable with ordinary Python.
sys.modules.setdefault("sublime", MagicMock())
sys.modules.setdefault("sublime_plugin", MagicMock())
import sublime


class _Region:
    def __init__(self, a, b=None):
        self._a = a
        self._b = a if b is None else b

    def begin(self):
        return min(self._a, self._b)

    def end(self):
        return max(self._a, self._b)

    def size(self):
        return abs(self._b - self._a)

    def __repr__(self):
        return f"Region({self._a}, {self._b})"


sublime.Region = _Region
sublime.HIDDEN = 4
sublime.LAYOUT_BLOCK = 2

from chatview.utils import (
    backtick_fence_for,
    classify_tool_output,
    clean_tool_output,
    format_fenced_output,
    strip_ansi,
)
from chatview.tooloutput import ToolOutputManager


class FakeView:
    """Minimal buffer emulation for ToolOutputManager tests."""

    def __init__(self, text=""):
        self.text = text
        self.regions = {}
        self.commands = []

    def add_regions(self, key, regions, flags=0):
        self.regions[key] = [
            sublime.Region(region.begin(), region.end()) for region in regions
        ]

    def erase_regions(self, key):
        self.regions.pop(key, None)

    def get_regions(self, key):
        return list(self.regions.get(key, []))

    def line(self, point):
        start = self.text.rfind("\n", 0, point) + 1
        end = self.text.find("\n", point)
        if end == -1:
            end = len(self.text)
        return sublime.Region(start, end)

    def full_line(self, region):
        start = self.text.rfind("\n", 0, region.begin()) + 1
        end = self.text.find("\n", region.end())
        if end == -1:
            end = len(self.text)
        else:
            end += 1
        return sublime.Region(start, end)

    def insert(self, edit, point, text):
        self.text = self.text[:point] + text + self.text[point:]
        return len(text)

    def erase(self, edit, region):
        self.text = self.text[:region.begin()] + self.text[region.end():]

    def run_command(self, name, args=None):
        self.commands.append((name, args))

    def is_valid(self):
        return True

    def id(self):
        return 1


class TestStripAnsi(unittest.TestCase):
    def test_removes_sgr_sequences(self):
        self.assertEqual(strip_ansi("\x1b[31merror\x1b[0m: bad"), "error: bad")

    def test_removes_osc_title(self):
        self.assertEqual(strip_ansi("\x1b]0;title\x07ok"), "ok")

    def test_keeps_plain_text(self):
        self.assertEqual(strip_ansi("plain"), "plain")

    def test_handles_none(self):
        self.assertEqual(strip_ansi(None), "")


class TestFenceHelpers(unittest.TestCase):
    def test_plain_text_uses_three_backticks(self):
        self.assertEqual(backtick_fence_for("hello"), "```")

    def test_grows_past_embedded_fence(self):
        self.assertEqual(backtick_fence_for("```"), "````")
        self.assertEqual(backtick_fence_for("````"), "`````")
        self.assertEqual(backtick_fence_for("x `` y ```` z"), "`````")

    def test_format_wraps_full_output(self):
        rendered = format_fenced_output("line 1\nline 2")
        self.assertTrue(rendered.startswith("```text\n"))
        self.assertTrue(rendered.endswith("\n```\n"))
        self.assertIn("line 1\nline 2", rendered)

    def test_format_closes_fence_without_trailing_newline(self):
        rendered = format_fenced_output("output")
        self.assertTrue(rendered.rstrip("\n").endswith("```"))

    def test_format_handles_embedded_fence(self):
        rendered = format_fenced_output("```python\nx = 1\n```")
        self.assertIn("````text", rendered)
        self.assertIn("```python", rendered)

    def test_clean_normalises_crlf_and_trailing_lines(self):
        self.assertEqual(clean_tool_output("a\r\nb\r\n\r\n"), "a\nb")

    def test_empty_output_renders_nothing(self):
        self.assertEqual(format_fenced_output("   \n\n"), "")


class TestClassifyToolOutput(unittest.TestCase):
    def test_short_output_is_inline(self):
        mode, text = classify_tool_output("a\nb\nc")
        self.assertEqual(mode, "inline")
        self.assertEqual(text, "a\nb\nc")

    def test_four_lines_is_collapsed(self):
        mode, _ = classify_tool_output("1\n2\n3\n4")
        self.assertEqual(mode, "collapsed")

    def test_single_huge_line_is_collapsed(self):
        mode, _ = classify_tool_output("x" * 401)
        self.assertEqual(mode, "collapsed", "long single lines must not be inlined")

    def test_blank_output_is_none(self):
        self.assertEqual(classify_tool_output(""), (None, ""))
        self.assertEqual(classify_tool_output("  \n\n"), (None, ""))

    def test_thresholds_are_configurable(self):
        mode, _ = classify_tool_output("1\n2\n3\n4", inline_lines=10)
        self.assertEqual(mode, "inline")


class TestToolOutputManager(unittest.TestCase):
    def setUp(self):
        # Mirrors the live buffer: tool header, then the blank line before the
        # input line. CHAT_INPUT_START points at the final newline.
        self.view = FakeView("⏺ Bash ls\n\n")
        self.input_start = {"pos": 10}
        self.manager = ToolOutputManager(
            self.view,
            lambda view, default=None: self.input_start["pos"],
            lambda view, pos: self.input_start.__setitem__("pos", pos),
        )
        # Anchor on the last character of the header line.
        self.anchor = sublime.Region(8, 8)

    def test_add_registers_anchor_and_phantom(self):
        entry = self.manager.add(self.anchor, "42 lines", "line1\nline2")
        self.assertIsNotNone(entry)
        self.assertIn(entry["region_key"], self.view.regions)
        phantoms = self.manager.phantom_set.update.call_args[0][0]
        self.assertEqual(len(phantoms), 1)

    def test_row_html_has_show_and_copy(self):
        entry = self.manager.add(self.anchor, "42 lines", "out")
        html = self.manager._row_html(entry)
        self.assertIn(f"toggle:{entry['key']}", html)
        self.assertIn(f"copy:{entry['key']}", html)
        self.assertIn(">show<", html)
        self.assertIn(">copy<", html)
        self.assertIn("42 lines", html)

    def test_toggle_expands_then_collapses(self):
        entry = self.manager.add(self.anchor, "42 lines", "line1\nline2")
        original = self.view.text

        self.manager.toggle(None, entry["key"])
        self.assertTrue(entry["expanded"])
        self.assertIn("```text", self.view.text)
        self.assertIn("line1\nline2", self.view.text)
        self.assertGreater(self.input_start["pos"], 10)

        self.manager.toggle(None, entry["key"])
        self.assertFalse(entry["expanded"])
        self.assertEqual(self.view.text, original)
        self.assertEqual(self.input_start["pos"], 10)

    def test_expand_toggles_label_to_hide(self):
        entry = self.manager.add(self.anchor, "42 lines", "out")
        self.manager.toggle(None, entry["key"])
        html = self.manager._row_html(entry)
        self.assertIn(">hide<", html)
        self.assertNotIn(">show<", html)

    def test_copy_uses_raw_output(self):
        entry = self.manager.add(self.anchor, "42 lines", "raw output\nsecond")
        with patch("sublime.set_clipboard") as clipboard, \
                patch("sublime.status_message") as status:
            self.manager.copy(entry["key"])
        clipboard.assert_called_once_with("raw output\nsecond")
        status.assert_called_once()
        self.assertIn("2 lines", status.call_args[0][0])
        self.assertFalse(entry["expanded"])

    def test_copy_missing_entry_is_noop(self):
        with patch("sublime.set_clipboard") as clipboard, \
                patch("sublime.status_message") as status:
            self.manager.copy("missing")
        clipboard.assert_not_called()
        self.assertIn("no longer available", status.call_args[0][0])

    def test_truncate_drops_rows_at_or_after_cut_point(self):
        first = self.manager.add(sublime.Region(2, 2), "1 line", "a")
        second = self.manager.add(sublime.Region(8, 8), "2 lines", "b")
        self.manager.truncate(5)
        keys = [entry["key"] for entry in self.manager.entries]
        self.assertIn(first["key"], keys)
        self.assertNotIn(second["key"], keys)

    def test_clear_removes_everything(self):
        self.manager.add(self.anchor, "1 line", "a")
        self.manager.clear()
        self.assertEqual(self.manager.entries, [])
        self.assertFalse(self.view.regions)
        self.manager.phantom_set.update.assert_called_with([])


class TestFormatToolResult(unittest.TestCase):
    def setUp(self):
        from chatview.chatprocessor import OpenCodeMessageProcessor

        self.session = MagicMock()
        self.session.agent_thread.cwd = "/workspace"
        self.session.get_setting.side_effect = (
            lambda key, default=None: {
                "tool_output_inline_lines": 3,
                "tool_output_inline_max_chars": 400,
            }.get(key, default)
        )
        self.processor = OpenCodeMessageProcessor(self.session)

    def _result(self, **block):
        block.setdefault("name", "bash")
        return self.processor.format_tool_result(block)

    def test_short_output_is_inline(self):
        inline, summary, detail = self._result(
            name="command_execution",
            output="ok\ndone",
            status="completed",
        )
        self.assertIn("```text", inline)
        self.assertIn("ok\ndone", inline)
        self.assertEqual((summary, detail), ("", ""))

    def test_long_output_is_collapsed(self):
        output = "\n".join(f"line {i}" for i in range(10))
        inline, summary, detail = self._result(
            name="command_execution", output=output, status="completed"
        )
        self.assertEqual(inline, "")
        self.assertEqual(summary, "10 lines")
        self.assertEqual(detail, output)

    def test_error_summary_is_marked(self):
        output = "\n".join(f"err {i}" for i in range(6))
        _, summary, _ = self._result(
            name="command_execution", output="", error=output, status="error"
        )
        self.assertTrue(summary.startswith("error · "))

    def test_edit_tool_is_not_rendered_twice(self):
        inline, summary, detail = self._result(
            name="edit",
            input={"path": "a.py"},
            output="Edited a.py",
            metadata={"diff": "@@ -1 +1 @@"},
            status="completed",
        )
        self.assertEqual((inline, summary, detail), ("", "", ""))

    def test_empty_output_renders_nothing(self):
        self.assertEqual(self._result(output="", status="completed"), ("", "", ""))

    def test_ansi_is_stripped(self):
        inline, _, detail = self._result(
            output="\x1b[32mgreen\x1b[0m", status="completed"
        )
        self.assertIn("green", inline)
        self.assertNotIn("\x1b", inline)
        self.assertEqual(detail, "")


if __name__ == "__main__":
    unittest.main()
