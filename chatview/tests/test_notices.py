import importlib
import os
import sys
import types
import unittest
from unittest.mock import MagicMock

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
_pkg_name = "_termmate_test_pkg_notices"
if _pkg_name not in sys.modules:
    _pkg = types.ModuleType(_pkg_name)
    _pkg.__path__ = [repo_dir]
    sys.modules[_pkg_name] = _pkg

chatpanel_mod = importlib.import_module(f"{_pkg_name}.chatview.chatpanel")
chatview_mod = importlib.import_module(f"{_pkg_name}.chatview.chatview")


class TestNoticeRendering(unittest.TestCase):
    def test_kind_colors_are_distinct_and_icon_free(self):
        for kind, css in chatpanel_mod.NOTICE_KINDS.items():
            html = chatpanel_mod.render_notice_html("hello", kind)
            self.assertIn(css["border"], html)
            self.assertIn(css["bg"], html)
            self.assertIn("hello", html)
            self.assertNotIn("ℹ️", html)
            self.assertNotIn("⚠️", html)

    def test_kind_label_is_shown_with_spacing(self):
        for kind in chatpanel_mod.NOTICE_KINDS:
            html = chatpanel_mod.render_notice_html("hello", kind)
            self.assertIn(f'<span class="notice-kind">{kind}</span>&nbsp;&nbsp;', html)

    def test_unknown_kind_falls_back_to_info(self):
        html = chatpanel_mod.render_notice_html("hello", "bogus")
        self.assertIn(chatpanel_mod.NOTICE_KINDS["info"]["border"], html)
        self.assertIn('<span class="notice-kind">info</span>', html)
        self.assertNotIn("bogus", html)

    def test_message_is_html_escaped(self):
        html = chatpanel_mod.render_notice_html("<b>x</b>", "error")
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", html)
        self.assertNotIn("<b>x</b>", html)

    def test_warning_uses_yellowish(self):
        self.assertIn(
            "--yellowish",
            chatpanel_mod.NOTICE_KINDS["warning"]["border"],
        )


class TestNoticeApi(unittest.TestCase):
    def _make_session(self):
        session = chatview_mod.ChatSession.__new__(chatview_mod.ChatSession)
        session.chat_view = MagicMock()
        session.chat_view.is_valid.return_value = True
        session.notice_phantom = MagicMock()
        return session

    def test_append_notice_uses_phantom_payload(self):
        session = self._make_session()

        session.append_notice("hello", kind="warning")

        session.chat_view.run_command.assert_called_once_with(
            "term_chat_output_append",
            {"notice_phantoms": [{"message": "hello", "kind": "warning"}]},
        )

    def test_show_notice_is_transient(self):
        session = self._make_session()

        session.show_notice("hi", kind="success")

        session.notice_phantom.show.assert_called_once_with("hi", kind="success")
        session.chat_view.run_command.assert_not_called()

    def test_notify_dispatches_by_transient_flag(self):
        session = self._make_session()

        session.notify("transient", kind="info", transient=True)
        session.notify("persistent", kind="error")

        session.notice_phantom.show.assert_called_once_with(
            "transient", kind="info")
        self.assertEqual(
            session.chat_view.run_command.call_args.args[1]["notice_phantoms"],
            [{"message": "persistent", "kind": "error"}],
        )

    def test_append_notice_ignores_invalid_view(self):
        session = self._make_session()
        session.chat_view.is_valid.return_value = False

        session.append_notice("hello")

        session.chat_view.run_command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
