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


def _load_chatview_modules():
    _ensure_sublime_mocks()
    pkg_name = "_termmate_test_pkg"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [repo_dir]
        sys.modules[pkg_name] = pkg
    return (
        importlib.import_module(f"{pkg_name}.chatview.chatview"),
        importlib.import_module(f"{pkg_name}.chatview.chatthread"),
    )


chatview_mod, chatthread_mod = _load_chatview_modules()
SessionRole = chatthread_mod.SessionRole
CHAT_THREAD_TITLE = chatthread_mod.CHAT_THREAD_TITLE
CHAT_THREAD_ID = chatthread_mod.CHAT_THREAD_ID
THREAD_TITLE_PREFIX = chatthread_mod.THREAD_TITLE_PREFIX
chat_threads = chatthread_mod.chat_threads


class _FakeViewSettings:
    def __init__(self, initial=None):
        self.data = dict(initial or {})

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value

    def has(self, key):
        return key in self.data

    def erase(self, key):
        self.data.pop(key, None)


class TestChatThreadTitles(unittest.TestCase):
    def _make_session(self, role, title="", number=None):
        view = MagicMock()
        view.id.return_value = 100
        settings = _FakeViewSettings()
        view.settings.return_value = settings
        if title:
            settings.set(CHAT_THREAD_TITLE, title)
        if number is not None:
            settings.set(CHAT_THREAD_ID, number)

        session = chatview_mod.ChatSession.__new__(chatview_mod.ChatSession)
        session.window = MagicMock()
        session.chat_view = view
        session.role = role
        session.initial_title = ""
        session._untitled_after_reset = False
        session.agent_thread = None
        return session, view, settings

    def test_primary_without_title_shows_untitled(self):
        session, view, settings = self._make_session(SessionRole.PRIMARY)

        session.set_thread_title("")

        view.set_name.assert_called_with(f"{THREAD_TITLE_PREFIX}untitled")
        self.assertFalse(settings.has(CHAT_THREAD_TITLE))

    def test_primary_with_title_shows_prefix_and_raw_title(self):
        session, view, settings = self._make_session(SessionRole.PRIMARY)

        session.set_thread_title("修复登录")

        view.set_name.assert_called_with(f"{THREAD_TITLE_PREFIX}修复登录")
        self.assertEqual(settings.get(CHAT_THREAD_TITLE), "修复登录")

    def test_prefix_is_not_stripped_from_user_title(self):
        session, view, settings = self._make_session(SessionRole.PRIMARY)

        session.set_thread_title("▣ 修复登录")

        view.set_name.assert_called_with(f"{THREAD_TITLE_PREFIX}▣ 修复登录")
        self.assertEqual(settings.get(CHAT_THREAD_TITLE), "▣ 修复登录")

    def test_placeholder_title_is_treated_as_untitled(self):
        session, view, settings = self._make_session(SessionRole.PRIMARY)

        session.set_thread_title("New session - 2026-11-20T08:15:22.123Z")

        view.set_name.assert_called_with(f"{THREAD_TITLE_PREFIX}untitled")
        self.assertFalse(settings.has(CHAT_THREAD_TITLE))

    def test_task_without_title_uses_primary_title_snapshot(self):
        primary, _, _ = self._make_session(SessionRole.PRIMARY)
        primary.set_thread_title("主标题")
        session, view, settings = self._make_session(SessionRole.TASK, number=2)

        with patch.object(chat_threads, "get_primary_session", return_value=primary):
            session.set_thread_title("")

        view.set_name.assert_called_with(f"{THREAD_TITLE_PREFIX}主标题 #2")

    def test_task_without_title_and_untitled_primary(self):
        primary, _, _ = self._make_session(SessionRole.PRIMARY)
        session, view, _ = self._make_session(SessionRole.TASK, number=3)

        with patch.object(chat_threads, "get_primary_session", return_value=primary):
            session.set_thread_title("")

        view.set_name.assert_called_with(f"{THREAD_TITLE_PREFIX}untitled #3")

    def test_task_with_title_overrides_numbered_fallback(self):
        session, view, settings = self._make_session(SessionRole.TASK, number=2)

        session.set_thread_title("task title")

        view.set_name.assert_called_with(f"{THREAD_TITLE_PREFIX}task title")
        self.assertEqual(settings.get(CHAT_THREAD_TITLE), "task title")

    def test_clear_title_always_shows_untitled(self):
        session, view, settings = self._make_session(SessionRole.TASK, number=2)
        session.set_thread_title("task title")

        session.clear_thread_title()

        view.set_name.assert_called_with(f"{THREAD_TITLE_PREFIX}untitled")
        self.assertFalse(settings.has(CHAT_THREAD_TITLE))

    def test_rename_thread_updates_locally_and_calls_agent(self):
        session, view, _ = self._make_session(SessionRole.PRIMARY)
        session.agent_thread = MagicMock()
        session.agent_thread.rename_session.return_value = True

        session.rename_thread("new title")

        view.set_name.assert_called_with(f"{THREAD_TITLE_PREFIX}new title")
        session.agent_thread.rename_session.assert_called_once()
        args, kwargs = session.agent_thread.rename_session.call_args
        self.assertEqual(args, ("new title",))
        self.assertIn("on_done", kwargs)

    def test_rename_error_is_rendered_in_chat(self):
        session, view, _ = self._make_session(SessionRole.PRIMARY)
        session.agent_thread = MagicMock()

        def fake_rename(title, on_done=None):
            on_done(False, "boom")
            return True

        session.agent_thread.rename_session.side_effect = fake_rename

        session.rename_thread("new title")

        appended = [
            call.args[1].get("text", "")
            for call in view.run_command.call_args_list
            if call.args[0] == "term_chat_output_append" and len(call.args) > 1
        ]
        self.assertTrue(any("⚠️ /rename: boom" in text for text in appended))


if __name__ == "__main__":
    unittest.main()
