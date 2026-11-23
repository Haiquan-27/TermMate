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
    """Populate integer flags on mocked sublime modules so chatview imports cleanly."""
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
    """Load chatview.py under a synthetic package so its relative ..genfoundry imports resolve."""
    _ensure_sublime_mocks()
    pkg_name = "_termmate_test_pkg"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [repo_dir]
        sys.modules[pkg_name] = pkg
    chatview_mod = importlib.import_module(f"{pkg_name}.chatview.chatview")
    chatthread_mod = importlib.import_module(f"{pkg_name}.chatview.chatthread")
    return chatview_mod, chatthread_mod


chatview_mod, chatthread_mod = _load_chatview_modules()
SessionRole = chatthread_mod.SessionRole
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


def _make_fake_view(view_id=10, window=None, initial_settings=None):
    view = MagicMock()
    view.id.return_value = view_id
    view.window.return_value = window
    view.size.return_value = 0
    view.get_regions.return_value = []
    settings = _FakeViewSettings(initial_settings)
    view.settings.return_value = settings
    return view, settings


class TestChatViewSettings(unittest.TestCase):
    """Verify default editor settings applied to ChatView tabs."""

    def setUp(self):
        chat_threads.clear()

    def tearDown(self):
        chat_threads.clear()

    def _assert_default_view_settings(self, settings):
        self.assertIs(settings.get("highlight_gutter"), False)
        self.assertIs(settings.get("highlight_line"), False)
        self.assertIs(settings.get("draw_minimap"), False)
        self.assertIs(settings.get("line_numbers"), False)
        self.assertIs(settings.get("word_wrap"), True)
        self.assertIs(settings.get("fold_buttons"), True)
        self.assertEqual(settings.get("draw_unicode_white_space"), "none")

    def test_create_primary_and_task_chat_view_defaults(self):
        for role, name in (
            (SessionRole.PRIMARY, chatview_mod.CHAT_VIEW_NAME),
            (SessionRole.TASK, "TermChat #1"),
        ):
            window = MagicMock()
            window.id.return_value = 1
            window.folders.return_value = []
            window.settings.return_value = _FakeViewSettings()
            chat_view, settings = _make_fake_view(view_id=20 + len(name), window=window)
            window.new_file.return_value = chat_view

            app_settings = _FakeViewSettings({"share_workspace_folders": False})
            with patch.object(chatview_mod.sublime, "load_settings", return_value=app_settings), \
                 patch.object(chatview_mod, "ChatSession") as mock_session_cls, \
                 patch.object(chatview_mod, "update_agent_model_status"):
                mock_session_cls.return_value.role = role
                chatview_mod._create_chat_view_and_session(
                    window, role=role, view_name=name, cwd="/tmp"
                )

            self._assert_default_view_settings(settings)

    def test_reconnect_chat_view_applies_default_settings(self):
        window = MagicMock()
        window.id.return_value = 2
        window.folders.return_value = []
        window.settings.return_value = _FakeViewSettings()

        chat_view, settings = _make_fake_view(
            view_id=42,
            window=window,
            initial_settings={
                chatview_mod.CHAT_VIEW_FLAG: True,
                chatview_mod.CHAT_THREAD_LABEL: SessionRole.PRIMARY.value,
                "highlight_gutter": True,
                "highlight_line": True,
            },
        )

        app_settings = _FakeViewSettings({"share_workspace_folders": False})
        with patch.object(chatview_mod.sublime, "load_settings", return_value=app_settings), \
             patch.object(chatview_mod, "ChatSession") as mock_session_cls, \
             patch.object(chatview_mod, "update_agent_model_status"):
            mock_session_cls.return_value.role = SessionRole.PRIMARY
            chatview_mod._reconnect_chat_view(chat_view)

        self.assertIs(settings.get("highlight_gutter"), False)
        self.assertIs(settings.get("highlight_line"), False)
        self.assertEqual(settings.get("draw_unicode_white_space"), "none")

    def test_reconnect_restores_historical_tool_phantoms(self):
        window = MagicMock()
        window.id.return_value = 3
        window.folders.return_value = []
        window.settings.return_value = _FakeViewSettings()

        chat_view, _ = _make_fake_view(
            view_id=52,
            window=window,
            initial_settings={
                chatview_mod.CHAT_VIEW_FLAG: True,
                chatview_mod.CHAT_THREAD_LABEL: SessionRole.TASK.value,
                chatview_mod.CHAT_SESSION_ID: "ses_reconnect",
                chatview_mod.CHAT_AGENT: "opencode",
            },
        )

        app_settings = _FakeViewSettings({"share_workspace_folders": False})
        with patch.object(chatview_mod.sublime, "load_settings", return_value=app_settings), \
             patch.object(chatview_mod, "ChatSession") as mock_session_cls, \
             patch.object(chatview_mod, "update_agent_model_status"):
            mock_session_cls.return_value.role = SessionRole.TASK
            mock_session_cls.get_view_session_id.return_value = "ses_reconnect"
            chatview_mod._reconnect_chat_view(chat_view)

        mock_session_cls.return_value._restore_history_phantoms_after_reconnect \
            .assert_called_once_with("ses_reconnect", "")


class TestModelPanelThinkTag(unittest.TestCase):
    """The effort level rides inside the model button."""

    def setUp(self):
        chat_threads.clear()

    def tearDown(self):
        chat_threads.clear()

    def _render(self, think_level):
        view, _ = _make_fake_view(view_id=77)
        window = MagicMock()
        window.id.return_value = 3
        panel = chatview_mod.ModelPanel(view, window)

        session = MagicMock()
        sessions = MagicMock()
        sessions.get.return_value = session
        sessions.values.return_value = []

        values = {
            chatview_mod.CHAT_PLAN_MODE: chatview_mod.PlanMode.FAST.value,
            chatview_mod.CHAT_AGENT: "opencode",
            "chatview_model_opencode": "opencode/gpt-6.2-astra",
        }
        if think_level is not None:
            values["chatview_think_level_opencode"] = think_level

        def fake_setting(_window, _session, key, default=None):
            return values.get(key, default)

        with patch.object(chatview_mod, "chatview_sessions", sessions), \
             patch.object(
                 chatview_mod.chat_threads,
                 "get_setting_for_session",
                 side_effect=fake_setting,
             ), \
             patch.object(chatview_mod.chat_threads, "set_setting_for_session"), \
             patch.object(chatview_mod, "get_input_start", return_value=0), \
             patch.object(chatview_mod.sublime, "Phantom") as phantom_cls:
            panel.update()

        return phantom_cls.call_args.args[1]

    def test_think_level_sits_inside_model_button(self):
        panel_html = self._render("high")

        model_pos = panel_html.index('href="set_model"')
        model_close = panel_html.index("</a>", model_pos)
        think_pos = panel_html.index("high", model_pos)
        self.assertLess(think_pos, model_close)
        self.assertIn("·", panel_html[model_pos:model_close])
        self.assertIn('class="think-value"', panel_html)
        self.assertNotIn('href="set_think"', panel_html)

    def test_no_think_value_without_level(self):
        panel_html = self._render(None)

        row = panel_html.split('<div class="model-row">')[1]
        self.assertNotIn("think-value", row)
        self.assertNotIn("·", row)

    def test_think_level_is_escaped(self):
        panel_html = self._render("<high>")

        self.assertIn("&lt;high&gt;", panel_html)
        self.assertNotIn("<high>", panel_html)


class _FakeListInputItem:
    def __init__(self, text=None, value=None, details=None, annotation=None):
        self.text = text
        self.value = value
        self.details = details
        self.annotation = annotation


class TestModelPickerThinkChain(unittest.TestCase):
    """Picking a model must chain into that model's effort levels."""

    def setUp(self):
        chat_threads.clear()

    def tearDown(self):
        chat_threads.clear()

    def _handler(self, current_model="opencode/gpt-6.2-astra"):
        window = MagicMock()
        session = MagicMock()
        session.available_models = [
            {
                "value": "opencode/gpt-6.2-astra",
                "displayName": "gpt-6.2-astra",
                "supportedReasoningEfforts": ["auto", "low", "high"],
            },
            {"value": "opencode/plain", "displayName": "plain"},
        ]
        handler = chatview_mod.TermChatSetModelListHandler(
            current_model=current_model,
            think_level="high",
            window=window,
            view_id=5,
        )

        def fake_setting(_window, _session, key, default=None):
            if key == chatview_mod.CHAT_AGENT:
                return "opencode"
            if key == "chatview_think_level_opencode":
                return "high"
            return default

        patcher = patch.object(
            chatview_mod.chat_threads,
            "get_active_or_primary_session",
            return_value=session,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(
            chatview_mod.chat_threads,
            "get_setting_for_session",
            side_effect=fake_setting,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(
            chatview_mod.sublime, "ListInputItem", _FakeListInputItem
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return handler

    def test_list_holds_only_models_with_the_current_one_first(self):
        handler = self._handler()

        items = handler.list_items()

        self.assertEqual(
            [i.value for i in items],
            ["opencode/gpt-6.2-astra", "opencode/plain"],
        )

    def test_picking_the_current_model_chains_to_its_levels(self):
        handler = self._handler()

        chained = handler.next_input({"model": "opencode/gpt-6.2-astra"})

        self.assertIsInstance(chained, chatview_mod.TermChatSetThinkLevelListHandler)
        self.assertEqual(chained.active_model["value"], "opencode/gpt-6.2-astra")
        self.assertEqual(chained.current_level, "high")
        self.assertEqual(chained.selected_model, "opencode/gpt-6.2-astra")

    def test_model_without_levels_finishes_the_flow(self):
        handler = self._handler()

        self.assertIsNone(handler.next_input({"model": "opencode/plain"}))

    def test_chained_levels_come_from_the_picked_model(self):
        handler = self._handler()

        chained = handler.next_input({"model": "opencode/gpt-6.2-astra"})
        values = [item.value for item in chained.list_items()]

        self.assertEqual(values[0], "high")  # current level moves to the front
        self.assertEqual(set(values), {"auto", "low", "high"})


class TestModelPickerLoading(unittest.TestCase):
    """Opening the picker before the roster loads must still show a panel."""

    def setUp(self):
        chat_threads.clear()

    def tearDown(self):
        chat_threads.clear()

    def _command(self):
        window = MagicMock()
        command = chatview_mod.TermChatSetModelCommand(window)
        command.window = window
        return command, window

    def _session(self, models=None, cached=None):
        session = MagicMock()
        session.available_models = list(models or [])
        agent = MagicMock()
        agent.available_models = list(cached or [])
        session.agent_thread.agent = agent
        session._model_picker_skip_refresh = False
        session._start_model_wait = MagicMock()
        return session

    def test_empty_roster_shows_loading_panel(self):
        command, _ = self._command()
        session = self._session()

        with patch.object(
            chatview_mod.chat_threads,
            "get_active_or_primary_session",
            return_value=session,
        ), patch.object(
            command, "_request_models", return_value=False
        ), patch.object(
            chatview_mod, "known_models", return_value=[]
        ), patch.object(
            chatview_mod.sublime, "status_message"
        ), patch.object(
            chatview_mod.sublime, "ListInputItem", _FakeListInputItem
        ):
            handler = command.input({"view_id": 5})
            items = handler.list_items()

        self.assertTrue(handler.loading)
        self.assertEqual([i.text for i in items], ["Loading models…"])
        self.assertEqual(items[0].value, "")
        session._start_model_wait.assert_called_once_with([])

    def test_agent_cache_roster_skips_the_loading_panel(self):
        command, _ = self._command()
        entry = {"value": "opencode/gpt-6.2-astra", "displayName": "gpt-6.2-astra"}
        session = self._session(cached=[entry])

        def fake_setting(_window, _session, key, default=None):
            if key == chatview_mod.CHAT_AGENT:
                return "opencode"
            if key == "chatview_model_opencode":
                return "opencode/gpt-6.2-astra"
            return default

        with patch.object(
            chatview_mod.chat_threads,
            "get_active_or_primary_session",
            return_value=session,
        ), patch.object(
            command, "_request_models", return_value=True
        ), patch.object(
            chatview_mod.chat_threads,
            "get_setting_for_session",
            side_effect=fake_setting,
        ), patch.object(
            chatview_mod.sublime, "ListInputItem", _FakeListInputItem
        ), patch.object(
            chatview_mod.sublime, "status_message"
        ):
            handler = command.input({"view_id": 5})
            items = handler.list_items()

        self.assertFalse(handler.loading)
        self.assertEqual([i.value for i in items], ["opencode/gpt-6.2-astra"])
        self.assertTrue(session._model_picker_waiting)

    def test_loading_panel_cancel_keeps_the_pending_refresh(self):
        window = MagicMock()
        session = types.SimpleNamespace(_model_picker_waiting=True)
        handler = chatview_mod.TermChatSetModelListHandler(
            window=window, view_id=5, loading=True
        )

        with patch.object(
            chatview_mod.chat_threads,
            "get_active_or_primary_session",
            return_value=session,
        ):
            handler.cancel()

        self.assertTrue(session._model_picker_waiting)

    def test_populated_panel_cancel_stops_the_pending_refresh(self):
        window = MagicMock()
        session = types.SimpleNamespace(_model_picker_waiting=True)
        handler = chatview_mod.TermChatSetModelListHandler(window=window, view_id=5)

        with patch.object(
            chatview_mod.chat_threads,
            "get_active_or_primary_session",
            return_value=session,
        ):
            handler.cancel()

        self.assertFalse(session._model_picker_waiting)

    def test_model_wait_retries_until_the_agent_is_ready(self):
        session = chatview_mod.ChatSession.__new__(chatview_mod.ChatSession)
        session._model_picker_waiting = False
        session._model_picker_snapshot = None
        callbacks = []

        with patch.object(
            chatview_mod.sublime,
            "set_timeout",
            side_effect=lambda cb, ms: callbacks.append(cb),
        ):
            session._start_model_wait([])

            self.assertTrue(session._model_picker_waiting)
            self.assertEqual(session._model_picker_snapshot, [])
            self.assertEqual(len(callbacks), 1)

            with patch.object(chatview_mod, "request_agent_models", return_value=False):
                callbacks.pop()()
            self.assertEqual(len(callbacks), 1)
            self.assertTrue(session._model_picker_waiting)

            with patch.object(chatview_mod, "request_agent_models", return_value=True):
                callbacks.pop()()
            self.assertEqual(callbacks, [])
            self.assertTrue(session._model_picker_waiting)

    def test_models_update_reopens_the_pending_panel(self):
        session = chatview_mod.ChatSession.__new__(chatview_mod.ChatSession)
        session.window = MagicMock()
        session.available_models = [{"value": "opencode/gpt-6.2-astra"}]
        session._model_picker_waiting = True
        session._model_picker_snapshot = []
        session._think_picker_waiting = False

        with patch.object(chatview_mod, "update_agent_model_status"), \
             patch.object(chatview_mod.sublime, "set_timeout") as set_timeout:
            session.on_available_models_updated()

        self.assertFalse(session._model_picker_waiting)
        set_timeout.assert_called_once_with(session._reopen_model_picker, 0)


class TestThinkLevelPicker(unittest.TestCase):
    """The palette command must list the selected model's own levels."""

    def setUp(self):
        chat_threads.clear()

    def tearDown(self):
        chat_threads.clear()

    def _command(self):
        window = MagicMock()
        command = chatview_mod.TermChatSetThinkLevelCommand(window)
        command.window = window
        return command, window

    def _settings(self, values):
        def fake_setting(_window, _session, key, default=None):
            return values.get(key, default)
        return fake_setting

    def test_uses_agent_cached_models_when_session_list_is_empty(self):
        command, _ = self._command()
        entry = {
            "value": "opencode/gpt-6.2-astra",
            "supportedReasoningEfforts": ["auto", "low", "high"],
        }
        session = MagicMock()
        session.available_models = []
        session.agent_thread.agent.available_models = [entry]
        session._think_picker_skip_refresh = False
        values = {
            chatview_mod.CHAT_AGENT: "opencode",
            "chatview_model_opencode": "opencode/gpt-6.2-astra",
            "chatview_think_level_opencode": "high",
        }

        with patch.object(
            chatview_mod.chat_threads,
            "get_active_or_primary_session",
            return_value=session,
        ), patch.object(
            chatview_mod.chat_threads,
            "get_setting_for_session",
            side_effect=self._settings(values),
        ), patch.object(chatview_mod, "request_agent_models") as request:
            handler = command.input({"view_id": 5})

        request.assert_not_called()
        self.assertIsInstance(handler, chatview_mod.TermChatSetThinkLevelListHandler)
        self.assertEqual(handler.active_model["value"], "opencode/gpt-6.2-astra")
        self.assertEqual(handler.current_level, "high")

    def test_fetches_and_reopens_when_model_is_unknown(self):
        command, _ = self._command()
        session = MagicMock()
        session.available_models = []
        session.agent_thread.agent.available_models = []
        session._think_picker_skip_refresh = False
        values = {
            chatview_mod.CHAT_AGENT: "opencode",
            "chatview_model_opencode": "opencode/gpt-6.2-astra",
            "chatview_think_level_opencode": "high",
        }

        with patch.object(
            chatview_mod.chat_threads,
            "get_active_or_primary_session",
            return_value=session,
        ), patch.object(
            chatview_mod.chat_threads,
            "get_setting_for_session",
            side_effect=self._settings(values),
        ), patch.object(
            chatview_mod, "request_agent_models", return_value=True
        ) as request, patch.object(chatview_mod.sublime, "status_message"):
            handler = command.input({"view_id": 5})

        request.assert_called_once_with(session)
        self.assertIsNone(handler)
        self.assertTrue(session._think_picker_waiting)

    def test_request_agent_models_skips_when_fetch_unavailable(self):
        session = MagicMock()
        session.agent_thread = None
        self.assertFalse(chatview_mod.request_agent_models(session))

        session.agent_thread = MagicMock()
        session.agent_thread.agent = MagicMock(spec=[])
        self.assertFalse(chatview_mod.request_agent_models(session))

    def test_request_agent_models_schedules_fetch(self):
        session = MagicMock()
        session.agent_thread.loop.is_closed.return_value = False

        with patch.object(
            chatview_mod.asyncio, "run_coroutine_threadsafe"
        ) as run:
            self.assertTrue(chatview_mod.request_agent_models(session))

        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
