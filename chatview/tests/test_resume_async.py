import importlib
import os
import sys
import threading
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

    def test_select_uses_async_history_fetch(self):
        cmd, window = _make_command()
        cmd._load_sessions = MagicMock(return_value=list(_SESSIONS))
        threads = _CapturedThreads()
        session = MagicMock()
        session.chat_view.id.return_value = 55
        session.agent_thread.session_id = "ses_current"

        with patch.object(chatview_mod.threading, "Thread", side_effect=threads), \
             patch.object(chatview_mod.sublime, "set_timeout",
                          side_effect=lambda fn, delay=0: fn()), \
             patch.object(chatview_mod.chat_threads, "get_active_or_primary_session",
                          return_value=session):
            cmd.run()
            threads.targets[0].target()
            on_select = window.show_quick_panel.call_args.args[1]
            on_select(0)

        session.reload_agent.assert_called_once_with(
            session_id_override="ses_111111", quiet=False,
            fetch_history_async=True,
        )


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
            "opencode", "ses_1", "/work"
        )
        session._apply_resume_history.assert_called_once_with(
            "opencode", "ses_1", info
        )

    def test_worker_error_still_applies_on_ui_thread(self):
        session = MagicMock()
        session._fetch_session_info = MagicMock(side_effect=RuntimeError("boom"))
        threads = _CapturedThreads()

        with patch.object(chatview_mod.threading, "Thread", side_effect=threads), \
             patch.object(chatview_mod.sublime, "set_timeout",
                          side_effect=lambda fn, delay=0: fn()):
            chatview_mod.ChatSession._fetch_resume_history_async(
                session, "opencode", "ses_1", "/work"
            )
            threads.targets[0].target()

        session._apply_resume_history.assert_called_once_with(
            "opencode", "ses_1", None
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


if __name__ == "__main__":
    unittest.main()
