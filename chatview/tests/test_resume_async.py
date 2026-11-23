import importlib
import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

repo_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if repo_dir not in sys.path:
    sys.path.insert(0, repo_dir)


def _ensure_sublime_mocks():
    sublime_mod = sys.modules.setdefault("sublime", MagicMock())
    for attr, val in (
        ("DRAW_NO_FILL", 1),
        ("DRAW_NO_OUTLINE", 2),
        ("HIDDEN", 4),
        ("PERSISTENT", 8),
        ("ENCODED_POSITION", 1),
    ):
        if not isinstance(getattr(sublime_mod, attr, None), int):
            setattr(sublime_mod, attr, val)

    plugin_mod = sys.modules.setdefault("sublime_plugin", MagicMock())
    for base in ("WindowCommand", "TextCommand", "EventListener", "ListInputHandler"):
        if not isinstance(getattr(plugin_mod, base, None), type):
            setattr(
                plugin_mod,
                base,
                type(base, (), {"__init__": lambda self, *a, **kw: None}),
            )


_ensure_sublime_mocks()
_pkg_name = "_termmate_test_pkg_resume"
if _pkg_name not in sys.modules:
    _pkg = types.ModuleType(_pkg_name)
    _pkg.__path__ = [repo_dir]
    sys.modules[_pkg_name] = _pkg

chatview_mod = importlib.import_module(f"{_pkg_name}.chatview.chatview")


def _make_command():
    window = MagicMock()
    cmd = chatview_mod.TermChatResumeSessionCommand(window)
    cmd.window = window
    cmd._get_agent = MagicMock(return_value="opencode")
    cmd._get_cwd = MagicMock(return_value="/work")
    return cmd, window


_SESSIONS = [
    {"session_id": "ses_111111", "summary": "first session", "mtime": 1700000000},
    {"session_id": "ses_222222", "summary": "second session", "mtime": 1700000100},
]


class _CapturedThreads:
    """Capture the background thread target without actually running it."""

    def __init__(self):
        self.targets = []

    def __call__(self, *args, **kwargs):
        thread = MagicMock()
        target = kwargs.get("target")
        thread.target = target
        self.targets.append(thread)
        return thread


def _open_panel(cmd, window, threads):
    """Run the command, finish the loader, and return the quick-panel callback."""
    with patch.object(chatview_mod.threading, "Thread", side_effect=threads), \
         patch.object(chatview_mod.sublime, "set_timeout",
                      side_effect=lambda fn, delay=0: fn()):
        cmd.run()
        threads.targets[0].target()
    return window.show_quick_panel.call_args.args[1]


class TestResumeSessionAsyncLoading(unittest.TestCase):
    def test_run_loads_sessions_off_ui_thread(self):
        cmd, window = _make_command()
        cmd._load_sessions = MagicMock(return_value=list(_SESSIONS))
        threads = _CapturedThreads()

        with patch.object(chatview_mod.threading, "Thread", side_effect=threads), \
             patch.object(chatview_mod.sublime, "set_timeout",
                          side_effect=lambda fn, delay=0: fn()):
            cmd.run()

            # Loading has been scheduled but not executed on the UI thread.
            cmd._load_sessions.assert_not_called()
            window.show_quick_panel.assert_not_called()

            # Run the captured background target to simulate completion.
            threads.targets[0].target()

        cmd._load_sessions.assert_called_once_with(
            "opencode", "/work", chatview_mod.sublime.load_settings.return_value
        )
        window.show_quick_panel.assert_called_once()
        items = window.show_quick_panel.call_args.args[0]
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0][0], "first session")
        self.assertIn("ses_1111", items[0][1])

    def test_stale_results_are_discarded(self):
        cmd, window = _make_command()
        threads = _CapturedThreads()
        cmd._load_sessions = MagicMock(side_effect=[list(_SESSIONS), [_SESSIONS[0]]])

        with patch.object(chatview_mod.threading, "Thread", side_effect=threads), \
             patch.object(chatview_mod.sublime, "set_timeout",
                          side_effect=lambda fn, delay=0: fn()):
            cmd.run()
            first_target = threads.targets[0].target
            cmd.run()
            second_target = threads.targets[1].target

            window.show_quick_panel.reset_mock()

            # The first request finished after the second one started.
            first_target()
            window.show_quick_panel.assert_not_called()

            second_target()
            window.show_quick_panel.assert_called_once()
            items = window.show_quick_panel.call_args.args[0]
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0][0], "first session")

    def test_no_sessions_shows_status(self):
        cmd, window = _make_command()
        cmd._load_sessions = MagicMock(return_value=[])
        threads = _CapturedThreads()

        with patch.object(chatview_mod.threading, "Thread", side_effect=threads), \
             patch.object(chatview_mod.sublime, "set_timeout",
                          side_effect=lambda fn, delay=0: fn()), \
             patch.object(chatview_mod.sublime, "status_message") as status:
            cmd.run()
            threads.targets[0].target()

        window.show_quick_panel.assert_not_called()
        status.assert_any_call("No past sessions found for this workspace")


class TestResumeSelectOpensNewTab(unittest.TestCase):
    def setUp(self):
        self.old_session = MagicMock()
        self.old_session.chat_view.id.return_value = 55
        self.old_session.agent_thread.session_id = "ses_111111"

        self.new_view = MagicMock()
        self.new_view.id.return_value = 777
        self.new_session = MagicMock()
        self.new_view_settings = {}
        self.new_view.settings.return_value.get.side_effect = (
            lambda k, d=None: self.new_view_settings.get(k, d)
        )
        self.new_view.settings.return_value.set.side_effect = (
            lambda k, v: self.new_view_settings.__setitem__(k, v)
        )

    def _select(self, cmd, window, index=0):
        cmd._load_sessions = MagicMock(return_value=list(_SESSIONS))
        threads = _CapturedThreads()
        on_select = _open_panel(cmd, window, threads)
        with patch.object(chatview_mod.chat_threads, "get_active_or_primary_session",
                          return_value=self.old_session), \
             patch.object(chatview_mod.TermChatNewThreadCommand,
                          "_next_task_number", return_value=7), \
             patch.object(chatview_mod, "_create_chat_view_and_session",
                          return_value=self.new_view) as create, \
             patch.object(chatview_mod.chat_threads, "sessions",
                          {777: self.new_session}):
            on_select(index)
        return create

    def test_select_opens_new_task_tab_bound_to_chosen_session(self):
        cmd, window = _make_command()
        create = self._select(cmd, window)

        create.assert_called_once()
        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs["role"], chatview_mod.SessionRole.TASK)
        self.assertEqual(kwargs["cwd"], "/work")
        self.assertEqual(kwargs["session_id"], "ses_111111")
        self.assertEqual(kwargs["initial_title"], "first session")

        self.assertEqual(self.new_view_settings[chatview_mod.CHAT_THREAD_ID], 7)
        self.assertEqual(
            self.new_view_settings[chatview_mod.CHAT_THREAD_TITLE], "first session"
        )
        self.new_session.set_thread_title.assert_called_once_with("first session")
        self.new_session._clear_transcript.assert_called_once_with()
        self.assertTrue(self.new_session.has_sent_message)

    def test_select_does_not_touch_current_tab(self):
        cmd, window = _make_command()
        self._select(cmd, window)

        self.old_session.reload_agent.assert_not_called()
        self.old_session._clear_transcript.assert_not_called()
        self.old_session.chat_view.settings.return_value.set.assert_not_called()
        window.run_command.assert_not_called()

    def test_select_fetches_full_history_without_banner(self):
        cmd, window = _make_command()
        self._select(cmd, window)

        self.new_session._fetch_resume_history_async.assert_called_once_with(
            "opencode", "ses_111111", "/work",
            history_limit=0, render_full=True,
        )

    def test_selecting_current_session_still_opens_new_tab(self):
        """The old 'Already on that session' early-return must be gone."""
        cmd, window = _make_command()
        create = self._select(cmd, window, index=0)
        create.assert_called_once()


class TestFetchResumeHistoryAsync(unittest.TestCase):
    def test_worker_fetches_off_ui_thread_and_applies_on_ui_thread(self):
        session = MagicMock()
        info = {"summary": "Fetched title", "mtime": 1700000000, "turns": []}
        session._fetch_session_info = MagicMock(return_value=info)
        threads = _CapturedThreads()

        with patch.object(chatview_mod.threading, "Thread", side_effect=threads), \
             patch.object(chatview_mod.sublime, "set_timeout",
                          side_effect=lambda fn, delay=0: fn()):
            chatview_mod.ChatSession._fetch_resume_history_async(
                session, "opencode", "ses_1", "/work"
            )
            session._fetch_session_info.assert_not_called()

            threads.targets[0].target()

        session._fetch_session_info.assert_called_once_with(
            "opencode", "ses_1", "/work", history_limit=None
        )
        session._apply_resume_history.assert_called_once_with(
            "opencode", "ses_1", info, render_full=False
        )

    def test_worker_error_still_applies_on_ui_thread(self):
        session = MagicMock()
        session._fetch_session_info = MagicMock(side_effect=RuntimeError("boom"))
        threads = _CapturedThreads()

        with patch.object(chatview_mod.threading, "Thread", side_effect=threads), \
             patch.object(chatview_mod.sublime, "set_timeout",
                          side_effect=lambda fn, delay=0: fn()):
            chatview_mod.ChatSession._fetch_resume_history_async(
                session, "opencode", "ses_1", "/work",
                history_limit=0, render_full=True,
            )
            threads.targets[0].target()

        session._fetch_session_info.assert_called_once_with(
            "opencode", "ses_1", "/work", history_limit=0
        )
        session._apply_resume_history.assert_called_once_with(
            "opencode", "ses_1", None, render_full=True
        )

    def test_apply_resume_history_sets_title_and_history(self):
        session = MagicMock()
        info = {"summary": "Fetched title", "mtime": 1700000000, "turns": []}

        chatview_mod.ChatSession._apply_resume_history(
            session, "opencode", "ses_1", info
        )

        session.set_thread_title.assert_called_once_with("Fetched title")
        session._append_resume_history.assert_called_once_with(
            "opencode", "ses_1", info
        )
        session._render_full_history.assert_not_called()

    def test_apply_resume_history_full_renders_directly(self):
        session = MagicMock()
        info = {"summary": "Fetched title", "mtime": 1700000000, "turns": []}

        chatview_mod.ChatSession._apply_resume_history(
            session, "opencode", "ses_1", info, render_full=True
        )

        session.set_thread_title.assert_called_once_with("Fetched title")
        session._render_full_history.assert_called_once_with(info)
        session._append_resume_history.assert_not_called()

    def test_apply_resume_history_ignores_non_string_summary(self):
        session = MagicMock()
        info = {"summary": None, "turns": []}

        chatview_mod.ChatSession._apply_resume_history(
            session, "opencode", "ses_1", info
        )

        session.set_thread_title.assert_not_called()
        session._append_resume_history.assert_called_once_with(
            "opencode", "ses_1", info
        )

    def test_apply_resume_history_skips_closed_view(self):
        session = MagicMock()
        session.chat_view.is_valid.return_value = False
        info = {"summary": "Fetched title", "turns": []}

        chatview_mod.ChatSession._apply_resume_history(
            session, "opencode", "ses_1", info
        )

        session._append_resume_history.assert_not_called()
        session._render_full_history.assert_not_called()


class TestFetchSessionInfoHistoryLimit(unittest.TestCase):
    def test_explicit_zero_is_passed_through(self):
        session = MagicMock()
        with patch.object(chatview_mod, "get_opencode_session_info",
                          return_value={"turns": []}) as get_info:
            result = chatview_mod.ChatSession._fetch_session_info(
                session, "opencode", "ses_1", "/work", history_limit=0
            )
        self.assertEqual(result, {"turns": []})
        get_info.assert_called_once()
        self.assertEqual(get_info.call_args.kwargs["history_limit"], 0)

    def test_none_uses_configured_default(self):
        session = MagicMock()
        settings = MagicMock()
        settings.get.side_effect = lambda key, default=None: (
            12 if key == "session_history_limit" else default
        )
        with patch.object(chatview_mod.sublime, "load_settings",
                          return_value=settings), \
             patch.object(chatview_mod, "get_opencode_session_info",
                          return_value={"turns": []}) as get_info:
            chatview_mod.ChatSession._fetch_session_info(
                session, "opencode", "ses_1", "/work"
            )
        self.assertEqual(get_info.call_args.kwargs["history_limit"], 12)
        settings.get.assert_any_call("session_history_limit", 50)


class TestRenderFullHistory(unittest.TestCase):
    @staticmethod
    def _appended_texts(session):
        texts = []
        for call in session.chat_view.run_command.call_args_list:
            if not call.args or call.args[0] != "term_chat_output_append":
                continue
            payload = call.args[1] if len(call.args) > 1 else call.kwargs
            if isinstance(payload, dict):
                texts.append(payload.get("text", ""))
        return texts

    def test_renders_prompt_and_response_without_resume_banner_or_footer(self):
        session = MagicMock()
        session.markdown_formatter.format.return_value = "rendered response\n"
        session.markdown_formatter.take_html_tables.return_value = []
        info = {"turns": [{"prompt": "hello", "response": "hi there"}]}

        chatview_mod.ChatSession._render_full_history(session, info)

        session._replay_prompt.assert_called_once_with("hello")
        texts = self._appended_texts(session)
        joined = "\n".join(texts)
        self.assertIn("rendered response", joined)
        self.assertNotIn("[Resuming session", joined)
        self.assertNotIn("ResumeConversation", joined)
        self.assertNotIn("Tool calls are not replayed", joined)

    def test_replays_structured_tool_and_shell_events(self):
        session = MagicMock()
        info = {
            "turns": [
                {
                    "events": [
                        {"kind": "user", "text": "fix it"},
                        {"kind": "assistant", "text": "working"},
                        {"kind": "tool", "id": "call_1", "block": {"name": "edit"}},
                        {"kind": "shell", "command": "pytest", "output": "ok"},
                    ]
                }
            ]
        }

        chatview_mod.ChatSession._render_full_history(session, info)

        session._replay_turn_events.assert_called_once_with(info["turns"][0])
        session._replay_prompt.assert_not_called()
        texts = self._appended_texts(session)
        self.assertFalse(any("[Resuming session" in t for t in texts))
        self.assertFalse(any("ResumeConversation" in t for t in texts))

    def test_no_turns_means_no_visible_content(self):
        session = MagicMock()
        chatview_mod.ChatSession._render_full_history(session, None)
        self.assertEqual(self._appended_texts(session), [])


class TestReplayToolResult(unittest.TestCase):
    def test_replayed_tool_call_keeps_result_for_show_link(self):
        session = MagicMock()
        session.get_setting.return_value = None
        processor_cls = chatview_mod.BaseChatMessageProcessor.for_provider("opencode")
        session.message_processor = processor_cls(session)
        block = {
            "name": "command_execution",
            "command": "pytest -q",
            "output": "3 passed\n",
            "status": "completed",
            "exitCode": 0,
        }

        chatview_mod.ChatSession._replay_tool_event(session, block, key="tool_v2_1")

        call = session.chat_view.run_command.call_args
        self.assertEqual(call.args[0], "term_chat_output_append")
        payload = call.args[1]
        self.assertTrue(payload.get("is_tool"))
        phantoms = payload["tool_phantoms"]
        self.assertEqual(len(phantoms), 1)
        self.assertEqual(phantoms[0]["kind"], "tool_call")
        self.assertEqual(phantoms[0]["output"]["detail"], "3 passed")
        self.assertIn("success", phantoms[0]["output"]["summary"])

    def test_replayed_result_renders_show_link(self):
        from chatview.tooloutput import ToolOutputManager

        view = MagicMock()
        view.get_regions.return_value = []
        manager = ToolOutputManager(view, lambda v: 0, lambda v, p: None)
        anchor = chatview_mod.sublime.Region(0, 0)
        entry = manager.add_tool_call(
            anchor,
            {"heading": "⏺ command", "summary": "pytest -q",
             "detail": "pytest -q", "show_command": True},
            {"summary": "1 line success", "detail": "3 passed\n"},
            status_only=True,
        )
        html = manager._row_html(entry)
        self.assertIn("1 line success", html)
        self.assertIn(f"toggle:{entry['key']}", html)
        self.assertIn(">show<", html)


class _Region:
    """Minimal region used to inspect phantom anchors in restore tests."""

    def __init__(self, a, b=None):
        self.a = a
        self.b = a if b is None else b

    def begin(self):
        return min(self.a, self.b)

    def end(self):
        return max(self.a, self.b)

    def size(self):
        return self.end() - self.begin()


def _make_transcript_view(lines):
    """Fake view containing ``lines`` plus the input line after them."""
    view = MagicMock()
    view.is_valid.return_value = True
    offsets = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1
    transcript = "\n".join(lines) + "\n" if lines else ""
    view.size.return_value = pos

    def text_point(row, col=0):
        if row >= len(offsets):
            return pos
        return offsets[row] + col

    def rowcol(point):
        for row, start in enumerate(offsets):
            if point < start + len(lines[row]) + 1:
                return row, point - start
        return len(offsets), 0

    def line_for(point):
        if point >= pos:
            return _Region(pos, pos)
        for row, start in enumerate(offsets):
            end = start + len(lines[row]) + 1
            if point < end:
                return _Region(start, end)
        return _Region(pos, pos)

    def substr(region):
        return transcript[region.begin():region.end()]

    view.text_point.side_effect = text_point
    view.rowcol.side_effect = rowcol
    view.line.side_effect = line_for
    view.substr.side_effect = substr
    return view, pos


class TestRestoreHistoryPhantomsAsync(unittest.TestCase):
    def test_worker_fetches_full_history_off_ui_thread_once(self):
        session = MagicMock()
        info = {"turns": []}
        session._fetch_session_info = MagicMock(return_value=info)
        threads = _CapturedThreads()

        with patch.object(chatview_mod.threading, "Thread", side_effect=threads), \
             patch.object(chatview_mod.sublime, "set_timeout",
                          side_effect=lambda fn, delay=0: fn()):
            chatview_mod.ChatSession._restore_history_phantoms_async(
                session, "opencode", "ses_1", "/work"
            )
            session._fetch_session_info.assert_not_called()

            # A second trigger must not start a duplicate fetch.
            chatview_mod.ChatSession._restore_history_phantoms_async(
                session, "opencode", "ses_1", "/work"
            )
            threads.targets[0].target()

        self.assertEqual(len(threads.targets), 1)
        session._fetch_session_info.assert_called_once_with(
            "opencode", "ses_1", "/work", history_limit=0
        )
        session._restore_history_phantoms.assert_called_once_with(info)

    def test_worker_error_still_applies_on_ui_thread(self):
        session = MagicMock()
        session._fetch_session_info = MagicMock(side_effect=RuntimeError("boom"))
        threads = _CapturedThreads()

        with patch.object(chatview_mod.threading, "Thread", side_effect=threads), \
             patch.object(chatview_mod.sublime, "set_timeout",
                          side_effect=lambda fn, delay=0: fn()):
            chatview_mod.ChatSession._restore_history_phantoms_async(
                session, "opencode", "ses_1", "/work"
            )
            threads.targets[0].target()

        session._restore_history_phantoms.assert_called_once_with(None)
        self.assertFalse(session._history_phantoms_restore_started)

    def test_reconnect_helper_only_fetches_for_opencode(self):
        session = MagicMock()
        session.get_setting.return_value = "claude"
        session._restore_history_phantoms_async = MagicMock()

        chatview_mod.ChatSession._restore_history_phantoms_after_reconnect(
            session, "ses_1", "/work"
        )
        session._restore_history_phantoms_async.assert_not_called()

        session.get_setting.return_value = "opencode"
        chatview_mod.ChatSession._restore_history_phantoms_after_reconnect(
            session, "ses_1", "/work"
        )
        session._restore_history_phantoms_async.assert_called_once_with(
            "opencode", "ses_1", "/work"
        )


class TestRestoreHistoryPhantoms(unittest.TestCase):
    def _make_session(self, lines):
        view, cut = _make_transcript_view(lines)
        session = MagicMock()
        session.chat_view = view
        session.get_setting.return_value = None
        processor_cls = chatview_mod.BaseChatMessageProcessor.for_provider("opencode")
        session.message_processor = processor_cls(session)
        session.tool_outputs = MagicMock()
        return session, view, cut

    @staticmethod
    def _tool_event(event_id, output):
        return {
            "kind": "tool",
            "id": event_id,
            "block": {
                "name": "command_execution",
                "command": "pytest -q",
                "output": output,
                "status": "completed",
                "exitCode": 0,
            },
        }

    def test_reattaches_status_row_without_touching_transcript(self):
        lines = ["⏺ command pytest -q", "assistant answer"]
        session, view, cut = self._make_session(lines)
        info = {"turns": [{"events": [self._tool_event("call_1", "3 passed\n")]}]}

        with patch.object(chatview_mod, "get_input_start", return_value=cut), \
             patch.object(chatview_mod.sublime, "Region", side_effect=_Region):
            chatview_mod.ChatSession._restore_history_phantoms(session, info)

        # Only the phantom manager is touched; the saved transcript is intact.
        view.run_command.assert_not_called()
        add = session.tool_outputs.add_tool_call
        add.assert_called_once()
        args, kwargs = add.call_args
        self.assertEqual(args[1]["detail"], "pytest -q")
        self.assertTrue(args[1]["show_command"])
        self.assertEqual(kwargs["key"], "call_1")
        self.assertEqual(args[2]["detail"], "3 passed")
        self.assertIn("success", args[2]["summary"])
        self.assertTrue(kwargs["status_only"])
        self.assertEqual(args[0].begin(), len(lines[0]) + 1)

        from chatview.tooloutput import ToolOutputManager
        manager = ToolOutputManager(view, lambda v: 0, lambda v, p: None)
        entry = manager.add_tool_call(
            args[0], args[1], args[2],
            key=kwargs["key"], status=kwargs["status"],
            status_only=kwargs["status_only"],
        )
        html = manager._row_html(entry)
        self.assertIn("1 line success", html)
        self.assertIn(f"toggle:{entry['key']}", html)
        self.assertIn(">show<", html)

    def test_repeated_headers_are_matched_in_order(self):
        lines = ["⏺ command pytest -q", "⏺ command pytest -q"]
        session, view, cut = self._make_session(lines)
        info = {"turns": [{"events": [
            self._tool_event("call_1", "first\n"),
            self._tool_event("call_2", "second\n"),
        ]}]}

        with patch.object(chatview_mod, "get_input_start", return_value=cut), \
             patch.object(chatview_mod.sublime, "Region", side_effect=_Region):
            chatview_mod.ChatSession._restore_history_phantoms(session, info)

        calls = session.tool_outputs.add_tool_call.call_args_list
        self.assertEqual([c.kwargs["key"] for c in calls], ["call_1", "call_2"])
        self.assertEqual(calls[0].args[0].begin(), len(lines[0]) + 1)
        self.assertEqual(
            calls[1].args[0].begin(), len(lines[0]) + 1 + len(lines[1]) + 1
        )
        self.assertEqual(calls[0].args[2]["detail"], "first")
        self.assertEqual(calls[1].args[2]["detail"], "second")
        self.assertEqual(calls[0].kwargs["key"], "call_1")
        self.assertEqual(calls[1].kwargs["key"], "call_2")

    def test_unmatched_headers_and_empty_history_are_noops(self):
        session, view, cut = self._make_session(["unrelated text"])
        info = {"turns": [{"events": [self._tool_event("call_1", "out\n")]}]}

        with patch.object(chatview_mod, "get_input_start", return_value=cut), \
             patch.object(chatview_mod.sublime, "Region", side_effect=_Region):
            chatview_mod.ChatSession._restore_history_phantoms(session, info)
            chatview_mod.ChatSession._restore_history_phantoms(session, None)

        session.tool_outputs.add_tool_call.assert_not_called()
        view.run_command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
