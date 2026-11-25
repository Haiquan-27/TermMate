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
CHAT_SESSION_ID = chatview_mod.CHAT_SESSION_ID
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

    @staticmethod
    def _notice_messages(view):
        messages = []
        for call in view.run_command.call_args_list:
            if call.args[0] != "term_chat_output_append" or len(call.args) < 2:
                continue
            for item in call.args[1].get("notice_phantoms") or []:
                messages.append(item.get("message", ""))
        return messages

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

        notices = self._notice_messages(view)
        self.assertTrue(any("/rename: boom" in text for text in notices))

    def test_open_forked_session_creates_bound_task_tab(self):
        session, view, _ = self._make_session(SessionRole.PRIMARY)
        session.cwd = "/work"
        new_view = MagicMock()
        new_view.id.return_value = 777
        new_view.settings.return_value = _FakeViewSettings()
        new_session = MagicMock()

        with patch.object(
            chatview_mod.TermChatNewThreadCommand, "_next_task_number", return_value=7
        ), patch.object(
            chatview_mod, "_create_chat_view_and_session", return_value=new_view
        ) as create, patch.dict(
            chatview_mod.chat_threads.sessions, {777: new_session}
        ):
            session._open_forked_session({
                "session_id": "ses_fork",
                "title": "main title (fork #1)",
            })

        create.assert_called_once()
        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs["role"], SessionRole.TASK)
        self.assertEqual(kwargs["cwd"], "/work")
        self.assertEqual(kwargs["initial_title"], "main title (fork #1)")
        self.assertEqual(kwargs["session_id"], "ses_fork")

        settings = new_view.settings.return_value.data
        self.assertEqual(settings[CHAT_THREAD_ID], 7)
        self.assertEqual(settings[CHAT_SESSION_ID], "ses_fork")
        self.assertTrue(new_session.has_sent_message)
        new_session.set_thread_title.assert_called_once_with("main title (fork #1)")

        notices = self._notice_messages(view)
        self.assertTrue(any("Forked session ses_fork" in text for text in notices))

    def test_run_fork_command_reports_failure(self):
        session, view, _ = self._make_session(SessionRole.PRIMARY)
        session.agent_thread = MagicMock()
        session.start_loading = MagicMock()
        session.stop_loading = MagicMock()

        def fake_fork(on_done=None):
            on_done(None, "boom")
            return True

        session.agent_thread.fork_session.side_effect = fake_fork

        self.assertTrue(session.run_slash_command("fork", "ignored"))

        session.start_loading.assert_called_once_with(text="/fork")
        session.stop_loading.assert_called_once()
        notices = self._notice_messages(view)
        self.assertTrue(any("/fork: failed to create session" in text for text in notices))

    def test_run_new_command_opens_fresh_task_tab(self):
        session, view, _ = self._make_session(SessionRole.PRIMARY)
        session.cwd = "/work"
        new_view = MagicMock()
        new_view.id.return_value = 888
        new_view.settings.return_value = _FakeViewSettings()

        with patch.object(
            chatview_mod.TermChatNewThreadCommand, "_next_task_number", return_value=9
        ), patch.object(
            chatview_mod, "_create_chat_view_and_session", return_value=new_view
        ) as create:
            self.assertTrue(session.run_slash_command("new", "ignored"))

        create.assert_called_once()
        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs["role"], SessionRole.TASK)
        self.assertEqual(kwargs["cwd"], "/work")
        self.assertEqual(kwargs["initial_title"], "New session #9")
        self.assertNotIn("session_id", kwargs)

    def test_loading_spinner_replaces_tab_prefix(self):
        session, view, _ = self._make_session(SessionRole.PRIMARY)
        loading = MagicMock()
        loading.is_loading = True
        loading.current_frame.return_value = "⠋"
        session.loading_animation = loading

        session.set_thread_title("working")

        view.set_name.assert_called_with("⠋ working")

    def test_idle_status_uses_square_prefix(self):
        session, _, _ = self._make_session(SessionRole.PRIMARY)
        session.loading_animation = MagicMock(is_loading=False)
        session.available_models = []

        with patch.object(chat_threads, "get_setting_for_session",
                          side_effect=lambda w, s, key, default=None: (
                              "opencode" if key == chatview_mod.CHAT_AGENT
                              else "gpt-5" if key == "chatview_model_opencode"
                              else default
                          )):
            status = chatview_mod._format_session_status(session.window, session)

        self.assertEqual(status, "▣ opencode(gpt-5)")

    def test_loading_status_uses_spinner_prefix(self):
        session, _, _ = self._make_session(SessionRole.PRIMARY)
        loading = MagicMock()
        loading.is_loading = True
        loading.current_frame.return_value = "⠙"
        session.loading_animation = loading
        session.available_models = []

        with patch.object(chat_threads, "get_setting_for_session",
                          side_effect=lambda w, s, key, default=None: (
                              "opencode" if key == chatview_mod.CHAT_AGENT
                              else "gpt-5" if key == "chatview_model_opencode"
                              else default
                          )):
            status = chatview_mod._format_session_status(session.window, session)

        self.assertEqual(status, "⠙ opencode(gpt-5)")

    def test_loading_tick_refreshes_title_and_status(self):
        session, view, _ = self._make_session(SessionRole.PRIMARY)
        animation = chatview_mod.LoadingAnimation(
            view, on_tick=session._refresh_loading_indicator
        )
        session.loading_animation = animation
        animation.is_loading = True
        animation.frame_index = 3
        session.set_thread_title("working")
        view.set_name.reset_mock()

        with patch.object(chatview_mod, "update_agent_model_status") as update_status, \
             patch.object(chatview_mod.sublime, "set_timeout",
                          side_effect=lambda fn, delay=0: fn()):
            animation._notify_tick(animation.current_frame())

        view.set_name.assert_called_with("⠸ working")
        update_status.assert_called_once_with(session.window)


if __name__ == "__main__":
    unittest.main()
