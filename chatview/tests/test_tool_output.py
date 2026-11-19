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
        count = len(text)
        self.text = self.text[:point] + text + self.text[point:]
        self._shift_regions(point, count)
        return count

    def erase(self, edit, region):
        begin, end = region.begin(), region.end()
        self.text = self.text[:begin] + self.text[end:]
        self._shift_regions(begin, begin - end)

    def _shift_regions(self, point, delta):
        if not delta:
            return
        for key, regions in self.regions.items():
            shifted = []
            for region in regions:
                begin = region.begin() + delta if region.begin() >= point else region.begin()
                end = region.end() + delta if region.end() >= point else region.end()
                shifted.append(sublime.Region(max(0, begin), max(0, end)))
            self.regions[key] = shifted

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
    INPUT_KEY = "test_input_anchor"

    def setUp(self):
        # Mirrors the live buffer: tool header, then the blank line before the
        # input line. CHAT_INPUT_START points at the final newline.
        self.view = FakeView("⏺ Bash ls\n\n")
        self.view.add_regions(self.INPUT_KEY, [sublime.Region(10, 11)])
        self.manager = ToolOutputManager(
            self.view,
            lambda view, default=None: view.get_regions(self.INPUT_KEY)[0].begin(),
            lambda view, pos: view.add_regions(
                self.INPUT_KEY, [sublime.Region(pos, pos + 1)]
            ),
        )
        # Anchor on the last character of the header line.
        self.anchor = sublime.Region(8, 8)

    def input_pos(self):
        return self.view.get_regions(self.INPUT_KEY)[0].begin()

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
        self.assertGreater(self.input_pos(), 10)

        self.manager.toggle(None, entry["key"])
        self.assertFalse(entry["expanded"])
        self.assertEqual(self.view.text, original)
        self.assertEqual(self.input_pos(), 10)

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

    def test_links_are_separated_by_non_breaking_spaces(self):
        entry = self.manager.add(self.anchor, "42 lines", "out")
        html = self.manager._row_html(entry)
        self.assertIn("</span>&nbsp;&nbsp;<a", html)
        self.assertIn("</a>&nbsp;&nbsp;<a", html)
        self.assertNotIn("margin-left", html)

    def test_tool_call_renders_both_rows_in_one_phantom(self):
        entry = self.manager.add_tool_call(
            self.anchor,
            {"heading": "⏺ command", "summary": "npm test", "detail": "npm test"},
            {"summary": "8 lines", "detail": "line\nline"},
        )
        phantoms = self.manager.phantom_set.update.call_args[0][0]
        self.assertEqual(len(phantoms), 1)
        html = self.manager._row_html(entry)
        self.assertIn("⏺ command (npm test)", html)
        self.assertIn("⎿ 8 lines", html)
        self.assertLess(
            html.index("termmate-command-row"), html.index("⎿ 8 lines"))
        self.assertIn(f"toggle:{entry['key']}:command", html)
        self.assertIn(f"toggle:{entry['key']}:output", html)
        self.assertIn(f"copy:{entry['key']}:output", html)

    def test_tool_call_without_output_has_one_row(self):
        entry = self.manager.add_tool_call(
            self.anchor,
            {"heading": "⏺ command", "summary": "npm test", "detail": "npm test"},
        )
        html = self.manager._row_html(entry)
        self.assertIn("npm test", html)
        self.assertNotIn("⎿", html)
        self.assertNotIn(">copy<", html)

    def test_command_toggle_keeps_output_row_in_place(self):
        entry = self.manager.add_tool_call(
            self.anchor,
            {"heading": "⏺ command", "summary": "npm test", "detail": "npm test"},
            {"summary": "8 lines", "detail": "out line"},
        )
        self.manager.toggle(None, entry["key"], "command")
        self.assertTrue(entry["show_command"])
        self.assertFalse(entry["show_output"])
        self.assertIn("```text\nnpm test\n```", self.view.text)
        html = self.manager._row_html(entry)
        self.assertLess(
            html.index("termmate-command-row"), html.index("⎿ 8 lines"))

        self.manager.toggle(None, entry["key"], "command")
        self.assertFalse(entry["show_command"])
        self.assertEqual(self.view.text, "⏺ Bash ls\n\n")

    def test_output_toggle_places_both_fences_in_fixed_order(self):
        entry = self.manager.add_tool_call(
            self.anchor,
            {"heading": "⏺ command", "summary": "npm test", "detail": "npm test"},
            {"summary": "8 lines", "detail": "out line"},
        )
        self.manager.toggle(None, entry["key"], "output")
        self.assertTrue(entry["show_output"])
        self.assertIn("```text\nout line\n```", self.view.text)
        self.assertNotIn("npm test", self.view.text)

        self.manager.toggle(None, entry["key"], "command")
        text = self.view.text
        self.assertLess(text.index("npm test"), text.index("out line"))

        self.manager.toggle(None, entry["key"], "output")
        self.assertFalse(entry["show_output"])
        command_fence = "```text\nnpm test\n```\n"
        self.assertIn(command_fence, self.view.text)
        self.assertNotIn("out line", self.view.text)
        self.assertEqual(self.input_pos(), 10 + len(command_fence))

    def test_tool_call_copy_uses_output_detail(self):
        entry = self.manager.add_tool_call(
            self.anchor,
            {"heading": "⏺ command", "summary": "npm test", "detail": "npm test"},
            {"summary": "2 lines", "detail": "out\nsecond"},
        )
        with patch("sublime.set_clipboard") as clipboard, \
                patch("sublime.status_message") as status:
            self.manager.copy(entry["key"], "output")
        clipboard.assert_called_once_with("out\nsecond")
        self.assertIn("2 lines", status.call_args[0][0])

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
        tool_keys = [k for k in self.view.regions if k.startswith("term_chat_tool_output_")]
        self.assertEqual(tool_keys, [])
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


class TestFormatToolCall(unittest.TestCase):
    """Command tool calls become phantom-only rows."""

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

    def _call(self, **block):
        block.setdefault("name", "command_execution")
        return self.processor.format_tool_call(block)

    def test_command_never_writes_buffer_text(self):
        text, phantoms = self._call(command="python3 -m unittest")
        self.assertEqual(text, "")
        self.assertEqual(len(phantoms), 1)
        call = phantoms[0]
        self.assertEqual(call["kind"], "tool_call")
        self.assertEqual(call["line"], -1)
        self.assertEqual(call["command"]["summary"], "python3 -m unittest")
        self.assertEqual(call["command"]["detail"], "python3 -m unittest")
        self.assertIsNone(call["output"])

    def test_multiline_preview_gets_ellipsis(self):
        _, phantoms = self._call(command="echo first\necho second")
        self.assertEqual(phantoms[0]["command"]["summary"], "echo first …")
        self.assertEqual(
            phantoms[0]["command"]["detail"], "echo first\necho second")

    def test_long_preview_is_truncated(self):
        command = "x" * 500
        _, phantoms = self._call(command=command)
        summary = phantoms[0]["command"]["summary"]
        self.assertLessEqual(len(summary), 120)
        self.assertTrue(summary.endswith("…"))
        self.assertEqual(phantoms[0]["command"]["detail"], command)

    def test_short_output_stays_inline(self):
        text, phantoms = self._call(
            command="git status", output="ok\ndone", status="completed"
        )
        self.assertIn("```text", text)
        self.assertIn("ok\ndone", text)
        self.assertEqual([p["kind"] for p in phantoms], ["tool_call"])
        self.assertIsNone(phantoms[0]["output"])

    def test_long_output_joins_the_command_phantom(self):
        output = "\n".join(f"line {i}" for i in range(10))
        text, phantoms = self._call(
            command="npm test", output=output, status="completed"
        )
        self.assertEqual(text, "")
        self.assertEqual(len(phantoms), 1)
        call = phantoms[0]
        self.assertEqual(call["kind"], "tool_call")
        self.assertEqual(call["line"], -1)
        self.assertEqual(call["output"]["summary"], "10 lines")
        self.assertEqual(call["output"]["detail"], output)

    def test_edit_tool_output_is_not_duplicated(self):
        text, phantoms = self.processor.format_tool_call({
            "name": "edit",
            "input": {"path": "a.py"},
            "output": "Edited a.py",
            "metadata": {"diff": "@@ -1 +1 @@"},
            "status": "completed",
        })
        self.assertTrue(text.startswith("⏺ edit"))
        self.assertEqual(phantoms, [])

    def test_read_with_long_output_keeps_header_row(self):
        output = "\n".join(f"line {i}" for i in range(10))
        text, phantoms = self.processor.format_tool_call({
            "name": "read",
            "input": {"path": "a.py"},
            "output": output,
            "status": "completed",
        })
        self.assertTrue(text.startswith("⏺ read a.py"))
        self.assertEqual(len(phantoms), 1)
        self.assertEqual(phantoms[0]["kind"], "output")
        self.assertEqual(phantoms[0]["line"], text.count("\n"))


if __name__ == "__main__":
    unittest.main()
