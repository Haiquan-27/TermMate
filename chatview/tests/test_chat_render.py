import sys
import unittest
from unittest.mock import MagicMock

sys.modules.setdefault("sublime", MagicMock())

from chatview.chatrender import extract_diff_fold_ranges
from chatview.md_render import MarkdownFormatter


class TestDiffFolding(unittest.TestCase):

    def test_fold_diff_block_over_limit_zero_preview(self):
        text = (
            "```diff\n"
            "+line 1\n"
            "+line 2\n"
            "+line 3\n"
            "+line 4\n"
            "```\n"
        )
        ranges = extract_diff_fold_ranges(text, limit=2, preview_lines=0)
        # fold starts right after ```diff (before the newline), through end of closing fence
        self.assertEqual(len(ranges), 1)
        start, end = ranges[0]
        self.assertEqual(text[start:end], "\n+line 1\n+line 2\n+line 3\n+line 4\n```")

    def test_fold_diff_block_with_preview_lines(self):
        text = (
            "  ```diff\n"
            "  +line 1\n"
            "  +line 2\n"
            "  +line 3\n"
            "  +line 4\n"
            "  ```\n"
        )
        ranges = extract_diff_fold_ranges(text, limit=2, preview_lines=2)
        self.assertEqual(len(ranges), 1)
        start, end = ranges[0]
        # offset + min(opening_indent, line_indent), which starts after the 2-space indent
        self.assertEqual(text[start:end], "+line 3\n  +line 4\n  ```")

    def test_ignores_under_limit(self):
        text = (
            "```diff\n"
            "+line 1\n"
            "+line 2\n"
            "```\n"
        )
        ranges = extract_diff_fold_ranges(text, limit=3, preview_lines=0)
        self.assertEqual(ranges, [])

    def test_ignores_unclosed_fence(self):
        text = (
            "```diff\n"
            "+line 1\n"
            "+line 2\n"
            "+line 3\n"
        )
        ranges = extract_diff_fold_ranges(text, limit=1, preview_lines=0)
        self.assertEqual(ranges, [])


class TestMarkdownFormatterCodeFence(unittest.TestCase):

    def setUp(self):
        mock_settings = MagicMock()
        mock_settings.get.side_effect = lambda k, default=None: (
            "html" if k == "table_style" else default
        )
        sys.modules["sublime"].load_settings.return_value = mock_settings

    def test_nested_code_fence_preserves_table_formatting(self):
        text = (
            "````markdown\n"
            "```python\n"
            "# nested code block\n"
            "```\n"
            "````\n"
            "\n"
            "| Col A | Col B |\n"
            "|---|---|\n"
            "| val 1 | val 2 |\n"
        )
        formatter = MarkdownFormatter()
        output = formatter.format(text, flush=True)
        tables = formatter.take_html_tables()

        self.assertFalse(formatter.in_code_block)
        self.assertEqual(len(tables), 1)
        self.assertIn("≡", output)

    def test_table_inside_code_block_not_formatted(self):
        text = (
            "```text\n"
            "| Col A | Col B |\n"
            "|---|---|\n"
            "| val 1 | val 2 |\n"
            "```\n"
        )
        formatter = MarkdownFormatter()
        output = formatter.format(text, flush=True)
        tables = formatter.take_html_tables()

        self.assertFalse(formatter.in_code_block)
        self.assertEqual(len(tables), 0)
        self.assertNotIn("≡", output)

    def test_reset_clears_code_block_state(self):
        text = (
            "```python\n"
            "# unclosed block\n"
        )
        formatter = MarkdownFormatter()
        formatter.format(text, flush=True)
        self.assertTrue(formatter.in_code_block)

        formatter.reset()
        self.assertFalse(formatter.in_code_block)
        self.assertEqual(formatter.table_buffer, [])


class TestMarkdownFormatterEmptyChunk(unittest.TestCase):

    def setUp(self):
        mock_settings = MagicMock()
        mock_settings.get.side_effect = lambda k, default=None: (
            "html" if k == "table_style" else default
        )
        sys.modules["sublime"].load_settings.return_value = mock_settings

    def test_empty_chunk_is_no_op(self):
        # Phantom-only updates call append_content(""); the formatter must not
        # turn that into a newline, or every tool status refresh adds a blank
        # line to the transcript.
        formatter = MarkdownFormatter()
        self.assertEqual(formatter.format("", flush=False), "")
        self.assertEqual(formatter.format("", flush=True), "")
        self.assertEqual(formatter.format(""), "")

    def test_empty_chunk_does_not_clear_pending_line(self):
        formatter = MarkdownFormatter()
        self.assertEqual(formatter.format("partial", flush=False), "")
        self.assertEqual(formatter.remaining_text, "partial")
        self.assertEqual(formatter.format("", flush=False), "")
        self.assertEqual(formatter.remaining_text, "partial")
        self.assertEqual(formatter.format("\n", flush=False), "partial\n")


class TestMarkdownFormatterLiveStreaming(unittest.TestCase):

    def test_partial_line_is_emitted_without_waiting_for_newline(self):
        formatter = MarkdownFormatter()
        self.assertEqual(formatter.format("Hel", live=True), "Hel")
        self.assertEqual(formatter.format("lo", live=True), "lo")
        self.assertEqual(formatter.format("\nnext", live=True), "\nnext")
        # Everything was already streamed; the end-of-turn flush adds nothing.
        self.assertEqual(formatter.format("", flush=True), "")

    def test_flush_does_not_duplicate_streamed_text(self):
        formatter = MarkdownFormatter()
        self.assertEqual(formatter.format("answer", live=True), "answer")
        self.assertEqual(formatter.format("", flush=True), "")
        self.assertEqual(formatter.format(" tail", live=True), " tail")

    def test_table_candidate_waits_for_complete_line(self):
        formatter = MarkdownFormatter()
        self.assertEqual(formatter.format("| a", live=True), "")
        output = formatter.format(" | b |\n|---|---|\n| 1 | 2 |\n", live=True)
        self.assertIn("a", output)
        self.assertIn("b", output)
        self.assertEqual(output.count("a"), 1)


if __name__ == "__main__":
    unittest.main()
