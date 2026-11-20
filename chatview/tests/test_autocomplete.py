import os
import unittest
import tempfile
import shutil
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from chatview import autocomplete as autocomplete_module
from chatview.autocomplete import (
    AutoComplete,
    parse_at_query_text,
    parse_slash_command_text,
    slash_completion_items,
    slash_completion_replacement,
)


class _FakeRegion:
    def __init__(self, a, b):
        self.a = a
        self.b = b

    def begin(self):
        return min(self.a, self.b)

    def end(self):
        return max(self.a, self.b)


class _FakeSublime:
    INHIBIT_WORD_COMPLETIONS = 1
    INHIBIT_EXPLICIT_COMPLETIONS = 2
    DYNAMIC_COMPLETIONS = 4
    KIND_FUNCTION = 5
    Region = _FakeRegion

    @staticmethod
    def CompletionItem(trigger, annotation="", completion="", kind=0):
        return SimpleNamespace(
            trigger=trigger,
            annotation=annotation,
            completion=completion,
            kind=kind,
        )

    @staticmethod
    def CompletionList(items, flags=0):
        return SimpleNamespace(items=items, flags=flags)


class _FakeView:
    def __init__(self, text, line_start=0, line_end=None):
        self.text = text
        self.line_start = line_start
        self.line_end = len(text) if line_end is None else line_end

    def line(self, pos):
        return _FakeRegion(self.line_start, self.line_end)

    def substr(self, region):
        return self.text[region.begin():region.end()]


class TestAutocomplete(unittest.TestCase):

    def test_parse_at_query_simple(self):
        # "@" -> full_query="", dir_part="", file_filter=""
        parsed = parse_at_query_text("Please check @")
        self.assertIsNotNone(parsed)
        full_query, dir_part, file_filter, offset = parsed
        self.assertEqual(full_query, "")
        self.assertEqual(dir_part, "")
        self.assertEqual(file_filter, "")
        self.assertEqual(offset, 13)

    def test_parse_at_query_directory(self):
        # "@src/" -> full_query="src/", dir_part="src/", file_filter=""
        parsed = parse_at_query_text("Please check @src/")
        self.assertIsNotNone(parsed)
        full_query, dir_part, file_filter, offset = parsed
        self.assertEqual(full_query, "src/")
        self.assertEqual(dir_part, "src/")
        self.assertEqual(file_filter, "")

    def test_parse_at_query_nested_with_filter(self):
        # "@src/components/Cha" -> full_query="src/components/Cha", dir_part="src/components/", file_filter="Cha"
        parsed = parse_at_query_text("Check @src/components/Cha")
        self.assertIsNotNone(parsed)
        full_query, dir_part, file_filter, offset = parsed
        self.assertEqual(full_query, "src/components/Cha")
        self.assertEqual(dir_part, "src/components/")
        self.assertEqual(file_filter, "Cha")

    def test_parse_at_query_space_breaks_token(self):
        # Space after @ without escaping -> None
        parsed = parse_at_query_text("Check @src/components something ")
        self.assertIsNone(parsed)

    def test_parse_at_query_escaped_space(self):
        # Escaped space in path -> allowed
        parsed = parse_at_query_text(r"Check @my\ folder/comp")
        self.assertIsNotNone(parsed)
        full_query, dir_part, file_filter, offset = parsed
        self.assertEqual(full_query, r"my\ folder/comp")

    def test_multi_workspace_target_dir_routing(self):
        with tempfile.TemporaryDirectory() as base_tmp:
            cwd_dir = os.path.join(base_tmp, "frontend")
            other_ws_dir = os.path.join(base_tmp, "backend")
            os.makedirs(os.path.join(cwd_dir, "src", "components"), exist_ok=True)
            os.makedirs(os.path.join(other_ws_dir, "cmd", "server"), exist_ok=True)
            other_workspaces = [other_ws_dir]

            resolve = lambda path: AutoComplete.resolve_target_dir(cwd_dir, other_workspaces, path)

            # 1. CWD root and subdirectories (direct and prefixed with self project name)
            self.assertEqual(resolve(""), cwd_dir)
            self.assertEqual(resolve("frontend/"), cwd_dir)
            src_dir = os.path.join(cwd_dir, "src", "components")
            self.assertEqual(resolve("src/components/"), src_dir)
            self.assertEqual(resolve("frontend/src/components/"), src_dir)

            # 2. Other workspace root and subdirectories
            self.assertEqual(resolve("backend/"), other_ws_dir)
            server_dir = os.path.join(other_ws_dir, "cmd", "server")
            self.assertEqual(resolve("backend/cmd/server/"), server_dir)

            # 3. Sandbox boundary protection (traversal outside project)
            for bad_path in ["../../etc/", "backend/../../etc/", "frontend/../../etc/"]:
                self.assertIsNone(resolve(bad_path))

    def test_scan_directory_and_sorting(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            os.makedirs(os.path.join(tmpdir, "components"), exist_ok=True)
            os.makedirs(os.path.join(tmpdir, "hooks"), exist_ok=True)
            os.makedirs(os.path.join(tmpdir, "node_modules"), exist_ok=True)  # ignored
            os.makedirs(os.path.join(tmpdir, ".git"), exist_ok=True)          # ignored

            with open(os.path.join(tmpdir, "index.ts"), "w") as f:
                f.write("")
            with open(os.path.join(tmpdir, "main.ts"), "w") as f:
                f.write("")
            with open(os.path.join(tmpdir, ".DS_Store"), "w") as f:           # ignored
                f.write("")

            sub_dirs, sub_files = AutoComplete.scan_directory(tmpdir, "")
            self.assertEqual(sub_dirs, ["components", "hooks"])
            self.assertEqual(sub_files, ["index.ts", "main.ts"])

            # Filtered scan
            sub_dirs_f, sub_files_f = AutoComplete.scan_directory(tmpdir, "co")
            self.assertEqual(sub_dirs_f, ["components"])
            self.assertEqual(sub_files_f, [])

    def test_get_workspace_info_view_cwd_priority(self):
        from unittest.mock import MagicMock

        mock_window = MagicMock()
        mock_window.folders.return_value = ["/ws/folder1", "/ws/folder2"]
        mock_window.settings().get.return_value = "/ws/custom_window"

        # 1. When view has its own chatview_cwd, it takes priority over window settings and folders
        mock_view = MagicMock()
        mock_view.settings().get.side_effect = (
            lambda key, default=None: "/ws/view_specific_cwd" if key == "chatview_cwd" else default
        )

        with tempfile.TemporaryDirectory() as view_dir, tempfile.TemporaryDirectory() as win_dir:
            mock_view.settings().get.side_effect = (
                lambda key, default=None: view_dir if key == "chatview_cwd" else default
            )
            mock_window.settings().get.side_effect = (
                lambda key, default=None: win_dir if key == "chatview_active_workspace" else default
            )
            mock_window.folders.return_value = [win_dir]

            active_cwd, other = AutoComplete.get_workspace_info(mock_window, view=mock_view)
            self.assertEqual(active_cwd, view_dir)

        # 2. When view has no chatview_cwd, fallback to window settings
        mock_view_no_cwd = MagicMock()
        mock_view_no_cwd.settings().get.return_value = None
        with tempfile.TemporaryDirectory() as win_dir:
            mock_window.settings().get.side_effect = (
                lambda key, default=None: win_dir if key == "chatview_active_workspace" else default
            )
            mock_window.folders.return_value = [win_dir]

            active_cwd, other = AutoComplete.get_workspace_info(mock_window, view=mock_view_no_cwd)
            self.assertEqual(active_cwd, win_dir)


class TestSlashCommandParsing(unittest.TestCase):

    def test_parse_bare_command(self):
        self.assertEqual(parse_slash_command_text("/compact"), ("compact", ""))

    def test_parse_with_arguments(self):
        self.assertEqual(
            parse_slash_command_text("  /rename New title  "),
            ("rename", "New title"),
        )

    def test_parse_nested_command(self):
        self.assertEqual(
            parse_slash_command_text("/team/review --deep"),
            ("team/review", "--deep"),
        )

    def test_non_commands(self):
        self.assertIsNone(parse_slash_command_text("hello /compact"))
        self.assertIsNone(parse_slash_command_text("/"))
        self.assertIsNone(parse_slash_command_text(""))
        self.assertIsNone(parse_slash_command_text("   "))


class TestSlashCompletionReplacement(unittest.TestCase):

    def test_simple_name(self):
        self.assertEqual(slash_completion_replacement("compact", ""), "compact")
        self.assertEqual(slash_completion_replacement("compact", "comp"), "compact")

    def test_nested_name_replaces_only_current_word(self):
        # Sublime treats '/' as a word separator, so only the segment after
        # the last slash is replaced.
        self.assertEqual(slash_completion_replacement("team/review", "team/"), "review")
        self.assertEqual(slash_completion_replacement("team/review", "team/rev"), "review")
        self.assertEqual(
            slash_completion_replacement("team/review", "team"),
            "team/review",
        )

    def test_sublime_prefix_is_authoritative(self):
        self.assertEqual(
            slash_completion_replacement("team/review", "team/rev", "rev"),
            "review",
        )
        self.assertEqual(
            slash_completion_replacement("my-cmd", "my-c", "c"),
            "cmd",
        )


class TestSlashCompletionItems(unittest.TestCase):

    def setUp(self):
        self.commands = [
            SimpleNamespace(
                name="compact",
                description="Compact the session",
                aliases=(),
            ),
            SimpleNamespace(
                name="team/review",
                description="Team review",
                aliases=("tr",),
            ),
        ]

    def test_filters_by_typed_prefix(self):
        items = slash_completion_items(self.commands, "co", "co")
        self.assertEqual([item[0] for item in items], ["compact"])

    def test_nested_trigger_uses_current_word(self):
        items = slash_completion_items(self.commands, "team/rev", "rev")
        self.assertEqual(items[0][0], "review")
        self.assertEqual(items[0][1], "review")
        self.assertEqual(items[0][2], "team/review · Team review")

    def test_alias_is_offered(self):
        items = slash_completion_items(self.commands, "t", "t")
        self.assertEqual([item[0] for item in items], ["team/review", "tr"])

    def test_empty_query_lists_full_names(self):
        items = slash_completion_items(self.commands, "", "")
        self.assertEqual(
            [item[0] for item in items],
            ["compact", "team/review", "tr"],
        )
        self.assertEqual(items[0][2], "Compact the session")


class TestSlashQueryExtraction(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._sublime_patch = patch.object(
            autocomplete_module, "sublime", _FakeSublime
        )
        cls._sublime_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls._sublime_patch.stop()

    def query(self, text, pos, editable_start=0, line_start=0):
        view = _FakeView(text, line_start=line_start)
        return AutoComplete.extract_slash_query(view, pos, editable_start)

    def test_bare_slash_is_a_valid_token(self):
        query = self.query("/", 1)
        self.assertIsNotNone(query)
        self.assertEqual(query.name, "")
        self.assertEqual(query.slash_pos, 0)

    def test_partial_command(self):
        query = self.query("/comp", 5)
        self.assertEqual(query.name, "comp")

    def test_nested_command(self):
        query = self.query("/team/rev", 9)
        self.assertEqual(query.name, "team/rev")

    def test_slash_must_begin_the_input_line(self):
        self.assertIsNone(self.query("hello /comp", 11))
        self.assertIsNone(self.query("  /comp", 7))
        self.assertIsNone(self.query("@src/com", 8))
        # A slash on a later line is not a command token.
        self.assertIsNone(self.query("/comp\n/x", 8, line_start=6))

    def test_arguments_stop_completion(self):
        self.assertIsNone(self.query("/compact now", 12))

    def test_generate_returns_popup_items(self):
        commands = [
            SimpleNamespace(
                name="compact",
                description="Compact the session",
                aliases=(),
            )
        ]
        view = _FakeView("/co")
        result = AutoComplete.generate_slash_completions(
            view, [3], 0, commands, prefix="co"
        )
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].trigger, "compact")
        self.assertEqual(result.items[0].completion, "compact")

    def test_generate_keeps_full_name_for_nested_commands(self):
        commands = [
            SimpleNamespace(
                name="team/review",
                description="Team review",
                aliases=(),
            )
        ]
        view = _FakeView("/team/rev")
        result = AutoComplete.generate_slash_completions(
            view, [9], 0, commands, prefix="rev"
        )
        self.assertEqual(result.items[0].trigger, "review")
        self.assertEqual(result.items[0].completion, "review")
        self.assertEqual(
            result.items[0].annotation, "team/review · Team review"
        )


if __name__ == "__main__":
    unittest.main()
