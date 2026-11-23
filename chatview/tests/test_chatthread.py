import importlib
import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

# Allow importing chatview when running this test file directly
repo_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if repo_dir not in sys.path:
    sys.path.insert(0, repo_dir)


class MockWindowCommand:
    def __init__(self, window):
        self.window = window


class MockTextCommand:
    def __init__(self, view):
        self.view = view


mock_sublime = sys.modules.setdefault("sublime", MagicMock())
mock_sublime.platform.return_value = "osx"
for attr, val in (
    ("DRAW_NO_FILL", 1),
    ("DRAW_NO_OUTLINE", 2),
    ("HIDDEN", 4),
    ("PERSISTENT", 8),
    ("ENCODED_POSITION", 1),
):
    if not isinstance(getattr(mock_sublime, attr, None), int):
        setattr(mock_sublime, attr, val)

mock_sublime_plugin = sys.modules.setdefault("sublime_plugin", MagicMock())
mock_sublime_plugin.WindowCommand = MockWindowCommand
mock_sublime_plugin.TextCommand = MockTextCommand
for base in ("EventListener", "ListInputHandler"):
    if not isinstance(getattr(mock_sublime_plugin, base, None), type):
        setattr(
            mock_sublime_plugin,
            base,
            type(base, (), {"__init__": lambda self, *a, **kw: None}),
        )

# Load under a synthetic package so both modules can use their relative
# ..genfoundry imports.  chatthread._start_task imports chatview lazily.
_pkg_name = "_termmate_thread_test_pkg"
if _pkg_name not in sys.modules:
    _pkg = types.ModuleType(_pkg_name)
    _pkg.__path__ = [repo_dir]
    sys.modules[_pkg_name] = _pkg
chatview_module = importlib.import_module(f"{_pkg_name}.chatview.chatview")
chatthread_module = importlib.import_module(f"{_pkg_name}.chatview.chatthread")

SessionRole = chatthread_module.SessionRole
CHAT_VIEW_NAME = chatthread_module.CHAT_VIEW_NAME
CHAT_THREAD_NAME = chatthread_module.CHAT_THREAD_NAME
CHAT_THREAD_LABEL = chatthread_module.CHAT_THREAD_LABEL
CHAT_THREAD_COUNTER = chatthread_module.CHAT_THREAD_COUNTER
CHAT_THREAD_ID = chatthread_module.CHAT_THREAD_ID
CHAT_THREAD_TITLE = chatthread_module.CHAT_THREAD_TITLE
THREAD_TITLE_PREFIX = chatthread_module.THREAD_TITLE_PREFIX
UNTITLED_LABEL = chatthread_module.UNTITLED_LABEL
ChatThreadSet = chatthread_module.ChatThreadSet
chat_threads = chatthread_module.chat_threads
chatview_sessions = chatthread_module.chatview_sessions
window_primary_chat = chatthread_module.window_primary_chat
window_last_active_chat = chatthread_module.window_last_active_chat
is_chat_view = chatthread_module.is_chat_view
get_chat_view_role = chatthread_module.get_chat_view_role
is_primary_chat_view = chatthread_module.is_primary_chat_view
is_task_chat_view = chatthread_module.is_task_chat_view
TermChatNewThreadCommand = chatthread_module.TermChatNewThreadCommand
TermChatSwitchThreadCommand = chatthread_module.TermChatSwitchThreadCommand
TermChatRenameThreadCommand = chatthread_module.TermChatRenameThreadCommand


class TestChatThread(unittest.TestCase):

    def setUp(self):
        chat_threads.clear()

    def test_session_role_and_view_identification(self):
        primary_view = MagicMock()
        primary_view.settings.return_value.get.side_effect = (
            lambda k, d=None: SessionRole.PRIMARY.value if k == CHAT_THREAD_LABEL else True
        )
        primary_view.name.return_value = CHAT_VIEW_NAME

        self.assertTrue(is_chat_view(primary_view))
        self.assertEqual(get_chat_view_role(primary_view), SessionRole.PRIMARY)
        self.assertTrue(is_primary_chat_view(primary_view))
        self.assertFalse(is_task_chat_view(primary_view))

        task_view = MagicMock()
        task_view.settings.return_value.get.side_effect = (
            lambda k, d=None: SessionRole.TASK.value if k == CHAT_THREAD_LABEL else True
        )
        task_view.name.return_value = f"{CHAT_THREAD_NAME} #1 Bugfix"

        self.assertTrue(is_chat_view(task_view))
        self.assertEqual(get_chat_view_role(task_view), SessionRole.TASK)
        self.assertFalse(is_primary_chat_view(task_view))
        self.assertTrue(is_task_chat_view(task_view))

    def test_register_and_lookup_sessions(self):
        window = MagicMock()
        window.id.return_value = 1

        primary_view = MagicMock()
        primary_view.id.return_value = 101
        primary_view.settings.return_value.get.side_effect = (
            lambda k, d=None: SessionRole.PRIMARY.value if k == CHAT_THREAD_LABEL else True
        )
        primary_view.name.return_value = CHAT_VIEW_NAME

        task_view = MagicMock()
        task_view.id.return_value = 102
        task_view.settings.return_value.get.side_effect = (
            lambda k, d=None: SessionRole.TASK.value if k == CHAT_THREAD_LABEL else True
        )
        task_view.name.return_value = f"{CHAT_THREAD_NAME} #1"

        primary_session = MagicMock()
        primary_session.role = SessionRole.PRIMARY
        primary_session.window = window
        primary_session.chat_view = primary_view

        task_session = MagicMock()
        task_session.role = SessionRole.TASK
        task_session.window = window
        task_session.chat_view = task_view

        chat_threads.register(window, primary_view, primary_session)
        chat_threads.register(window, task_view, task_session)

        self.assertEqual(chat_threads.get_primary_session(window), primary_session)
        self.assertEqual(
            chat_threads.get_active_or_primary_session(window, view_id=102),
            task_session,
        )

        removed = chat_threads.unregister(101)
        self.assertEqual(removed, primary_session)
        self.assertIsNone(chat_threads.get_primary_session(window))

    def test_rename_thread_command(self):
        window = MagicMock()
        cmd = TermChatRenameThreadCommand(window)

        task_view = MagicMock()
        task_settings = {CHAT_THREAD_LABEL: SessionRole.TASK.value, CHAT_THREAD_ID: 2}
        task_view.settings.return_value.get.side_effect = (
            lambda k, d=None: task_settings.get(k, d)
        )
        task_view.settings.return_value.set.side_effect = (
            lambda k, v: task_settings.__setitem__(k, v)
        )
        task_view.name.return_value = f"{CHAT_THREAD_NAME} #2"

        cmd._apply_rename(task_view, "Fix authentication bug")

        task_view.set_name.assert_called_with(
            f"{THREAD_TITLE_PREFIX}Fix authentication bug"
        )
        self.assertEqual(task_settings[CHAT_THREAD_TITLE], "Fix authentication bug")

    def test_start_task_uses_primary_title_snapshot(self):
        window = MagicMock()
        cmd = TermChatNewThreadCommand(window)
        cmd._next_task_number = MagicMock(return_value=2)

        primary_view = MagicMock()
        primary_settings = {CHAT_THREAD_TITLE: "主标题"}
        primary_view.settings.return_value.get.side_effect = (
            lambda k, d=None: primary_settings.get(k, d)
        )
        primary_session = MagicMock()
        primary_session.chat_view = primary_view

        task_view = MagicMock()
        task_view.id.return_value = 202
        task_settings = {}
        task_view.settings.return_value.get.side_effect = (
            lambda k, d=None: task_settings.get(k, d)
        )
        task_view.settings.return_value.set.side_effect = (
            lambda k, v: task_settings.__setitem__(k, v)
        )

        with patch.object(chat_threads, "get_primary_session", return_value=primary_session), \
             patch.object(chatview_module, "_create_chat_view_and_session", return_value=task_view) as create:
            cmd._start_task("", "", False, "", None)

        self.assertEqual(create.call_args.kwargs["initial_title"], "主标题 #2")
        self.assertEqual(task_settings[CHAT_THREAD_TITLE], "主标题 #2")
        self.assertEqual(task_settings[CHAT_THREAD_ID], 2)

    def test_start_task_uses_supplied_title(self):
        window = MagicMock()
        cmd = TermChatNewThreadCommand(window)
        cmd._next_task_number = MagicMock(return_value=4)

        task_view = MagicMock()
        task_view.id.return_value = 204
        task_settings = {}
        task_view.settings.return_value.get.side_effect = (
            lambda k, d=None: task_settings.get(k, d)
        )
        task_view.settings.return_value.set.side_effect = (
            lambda k, v: task_settings.__setitem__(k, v)
        )

        with patch.object(chat_threads, "get_primary_session", return_value=None), \
             patch.object(chatview_module, "_create_chat_view_and_session", return_value=task_view) as create:
            cmd._start_task("修复登录", "", False, "", None)

        self.assertEqual(create.call_args.kwargs["initial_title"], "修复登录")
        self.assertEqual(task_settings[CHAT_THREAD_TITLE], "修复登录")

    def test_switch_panel_shows_raw_title_and_server_id(self):
        window = MagicMock()
        window.id.return_value = 1
        active = MagicMock()
        active.id.return_value = 99
        active.settings.return_value.get.side_effect = lambda k, d=None: d
        window.active_view.return_value = active
        cmd = TermChatSwitchThreadCommand(window)

        session = MagicMock()
        session.window = window
        session.role = SessionRole.PRIMARY
        session.chat_view = MagicMock()
        session.chat_view.id.return_value = 101
        session.chat_view.is_valid.return_value = True
        session.chat_view.settings.return_value.get.side_effect = (
            lambda k, d=None: "修复登录" if k == CHAT_THREAD_TITLE else d
        )
        session.agent_thread.session_id = "ses_abc123"

        with patch.object(chat_threads, "sessions", {101: session}), \
             patch.object(sys.modules["sublime"], "QuickPanelItem",
                          lambda trigger, details, annotation: [trigger, details]):
            cmd.run()

        items = window.show_quick_panel.call_args.args[0]
        self.assertEqual(items[0][0], "修复登录")
        self.assertEqual(items[0][1], "ses_abc123")


if __name__ == "__main__":
    unittest.main()
