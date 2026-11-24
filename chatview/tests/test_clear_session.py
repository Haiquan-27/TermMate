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
_pkg_name = "_termmate_test_pkg_clear"
if _pkg_name not in sys.modules:
    _pkg = types.ModuleType(_pkg_name)
    _pkg.__path__ = [repo_dir]
    sys.modules[_pkg_name] = _pkg

chatview_mod = importlib.import_module(f"{_pkg_name}.chatview.chatview")
CHAT_SESSION_ID = chatview_mod.CHAT_SESSION_ID


def _make_session():
    session = chatview_mod.ChatSession.__new__(chatview_mod.ChatSession)
    session.window = MagicMock()
    session.chat_view = MagicMock()
    session.chat_view.settings.return_value = MagicMock()
    session.agent_thread = MagicMock()

    session._clear_transcript = MagicMock()
    session.clear_thread_title = MagicMock()
    session.append_notice = MagicMock()
    session.notice_phantom = MagicMock()
    session.rewind_confirm_panel = MagicMock()
    session.permission_panel = MagicMock()
    session.implement_plan_phantoms = MagicMock()
    session.message_processor = MagicMock()

    session.permission_requests = {"old": ("bash", {})}
    session.pending_response_separator = True
    session.session_allow_all = True
    session.has_sent_message = True
    return session


class TestClearSessionCommand(unittest.TestCase):
    def test_run_resets_the_session_from_the_context_view(self):
        window = MagicMock()
        cmd = chatview_mod.TermChatClearSessionCommand(window)
        cmd.window = window
        session = MagicMock()
        session.reset_session.return_value = True

        with patch.object(
            chatview_mod.chat_threads,
            "get_active_or_primary_session",
            return_value=session,
        ) as get_session:
            cmd.run(view_id=77)

        get_session.assert_called_once_with(window, view_id=77)
        session.reset_session.assert_called_once_with()

    def test_run_reports_when_no_session_is_available(self):
        window = MagicMock()
        cmd = chatview_mod.TermChatClearSessionCommand(window)
        cmd.window = window

        with patch.object(
            chatview_mod.chat_threads,
            "get_active_or_primary_session",
            return_value=None,
        ), patch.object(chatview_mod.sublime, "status_message") as status:
            cmd.run(view_id=77)

        status.assert_called_once_with("No active ChatView session found")


class TestChatSessionClear(unittest.TestCase):
    def test_reset_clears_transcript_panels_and_state(self):
        session = _make_session()

        with patch.object(chatview_mod, "get_best_dir", return_value="/work"):
            result = session.reset_session()

        self.assertTrue(result)
        session._clear_transcript.assert_called_once_with()
        session.notice_phantom.clear.assert_called_once_with()
        session.rewind_confirm_panel.clear.assert_called_once_with()
        session.permission_panel.clear_all.assert_called_once_with()
        session.implement_plan_phantoms.update.assert_called_once_with([])
        self.assertEqual(session.permission_requests, {})
        self.assertFalse(session.pending_response_separator)
        self.assertFalse(session.session_allow_all)
        self.assertFalse(session.has_sent_message)
        self.assertEqual(session.message_processor._plan_text, "")
        session.chat_view.settings.return_value.set.assert_any_call(
            CHAT_SESSION_ID, None
        )
        session.clear_thread_title.assert_called_once_with()
        session.append_notice.assert_any_call(
            f"{chatview_mod.PACKAGE_NAME} session reset...", kind="info"
        )
        session.append_notice.assert_any_call("cwd: /work", kind="info")
        session.agent_thread.reset.assert_called_once_with()

    def test_reset_without_agent_does_nothing(self):
        session = _make_session()
        session.agent_thread = None

        result = session.reset_session()

        self.assertFalse(result)
        session._clear_transcript.assert_not_called()
        session.clear_thread_title.assert_not_called()

    def test_slash_clear_resets_current_tab_without_calling_agent_command(self):
        session = _make_session()
        session.reset_session = MagicMock(return_value=True)

        result = session.run_slash_command("clear", "ignored arguments")

        self.assertTrue(result)
        session.reset_session.assert_called_once_with()
        session.agent_thread.run_slash_command.assert_not_called()

    def test_slash_clear_without_agent_reports_unavailable(self):
        session = _make_session()
        session.agent_thread = None
        session.reset_session = MagicMock(return_value=True)
        session._append_slash_notice = MagicMock()

        result = session.run_slash_command("clear")

        self.assertFalse(result)
        session.reset_session.assert_not_called()
        session._append_slash_notice.assert_called_once_with(
            "/clear is not available.", kind="warning"
        )


if __name__ == "__main__":
    unittest.main()
