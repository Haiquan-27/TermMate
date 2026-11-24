import asyncio
import json
import os
import signal
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import genfoundry.opencode_agent as opencode_agent
from genfoundry.base_agent import AgentOptions, BaseAgent, SlashCommand
from genfoundry.opencode_agent import (
    OpenCodeAgent,
    OpenCodeHTTPError,
    _shell_command_from_text,
    _shell_synthetic_output,
    detect_opencode_version,
    detect_opencode_version_info,
    find_opencode_cli,
    find_opencode_v2_password,
    find_opencode_v2_service,
    get_opencode_session_info,
    list_opencode_sessions,
    parse_opencode_version_output,
    probe_opencode_service,
)


class TestShellSyntheticOutput(unittest.TestCase):
    def test_extracts_body_and_strips_framing_newlines(self):
        text = (
            '<shell id="sh_1" state="completed" command="npm run dev">\n'
            "server ready\r\n\n</shell>"
        )
        self.assertEqual(_shell_synthetic_output(text), "server ready")

    def test_ignores_redirection_inside_command_attribute(self):
        text = (
            '<shell id="sh_1" command="echo done > out.txt">\n'
            "done\n</shell>"
        )
        self.assertEqual(_shell_synthetic_output(text), "done")

    def test_non_shell_text_is_returned_unchanged(self):
        self.assertEqual(_shell_synthetic_output("plain"), "plain")


class TestShellCommandFromText(unittest.TestCase):
    def test_reads_command_attribute(self):
        text = (
            '<shell id="sh_1" state="completed" command="npm run dev">\n'
            "ready\n</shell>"
        )
        self.assertEqual(_shell_command_from_text(text), "npm run dev")

    def test_reads_command_with_quoted_arguments(self):
        text = (
            '<shell id="sh_1" state="completed" '
            'command="Start-Sleep -Seconds 10; Write-Output "ok"">\n'
            "ok\n</shell>"
        )
        self.assertEqual(
            _shell_command_from_text(text),
            'Start-Sleep -Seconds 10; Write-Output "ok"',
        )

    def test_reads_command_from_notification_prefix(self):
        text = (
            "The following shell command was executed by the user:\n"
            '<shell id="sh_1" state="completed" command="ls -la">\n'
            "a\n</shell>"
        )
        self.assertEqual(_shell_command_from_text(text), "ls -la")

    def test_missing_command_returns_empty(self):
        self.assertEqual(_shell_command_from_text("plain"), "")
        self.assertEqual(_shell_command_from_text(""), "")


class TestFindOpenCodeCLI(unittest.TestCase):
    def test_finds_official_install_location(self):
        expected = "/Users/test/.opencode/bin/opencode"

        with patch("genfoundry.opencode_agent.shutil.which", return_value=None):
            with patch(
                "genfoundry.opencode_agent.os.path.expanduser",
                return_value="/Users/test",
            ):
                with patch(
                    "genfoundry.opencode_agent.os.path.isfile",
                    side_effect=lambda path: path == expected,
                ):
                    with patch(
                        "genfoundry.opencode_agent.os.access", return_value=True
                    ):
                        self.assertEqual(find_opencode_cli(), expected)


class TestOpenCodeAgentEvents(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.agent = OpenCodeAgent(AgentOptions(cwd="."), version=1)
        self.agent._session_id = "ses_test"
        self.agent._turn_active = True

    async def _messages(self):
        result = []
        while not self.agent._message_queue.empty():
            result.append(await self.agent._message_queue.get())
        return result

    async def test_text_delta_and_full_update_are_not_duplicated(self):
        await self.agent._dispatch_event({
            "type": "message.part.delta",
            "properties": {
                "sessionID": "ses_test",
                "messageID": "msg_1",
                "partID": "part_1",
                "field": "text",
                "delta": "hello",
            },
        })
        await self.agent._dispatch_event({
            "type": "message.part.updated",
            "properties": {
                "part": {
                    "id": "part_1",
                    "sessionID": "ses_test",
                    "messageID": "msg_1",
                    "type": "text",
                    "text": "hello world",
                }
            },
        })

        messages = await self._messages()
        self.assertEqual(
            [message.content for message in messages if message.type == "text"],
            ["hello", " world"],
        )

    async def test_user_text_part_is_not_emitted(self):
        await self.agent._dispatch_event({
            "type": "message.updated",
            "properties": {
                "info": {
                    "id": "msg_user",
                    "sessionID": "ses_test",
                    "role": "user",
                },
            },
        })
        await self.agent._dispatch_event({
            "type": "message.part.updated",
            "properties": {
                "part": {
                    "id": "part_user",
                    "sessionID": "ses_test",
                    "messageID": "msg_user",
                    "type": "text",
                    "text": "about you",
                },
            },
        })

        self.assertEqual(await self._messages(), [])

    async def test_submitted_user_delta_is_ignored_before_role_event(self):
        self.agent._user_message_ids.add("msg_user")
        await self.agent._dispatch_event({
            "type": "message.part.delta",
            "properties": {
                "sessionID": "ses_test",
                "messageID": "msg_user",
                "partID": "part_user",
                "field": "text",
                "delta": "about you",
            },
        })

        self.assertEqual(await self._messages(), [])

    async def test_tool_is_emitted_once_at_terminal_state(self):
        base_part = {
            "id": "tool_1",
            "sessionID": "ses_test",
            "messageID": "msg_1",
            "type": "tool",
            "tool": "bash",
        }
        for status in ("pending", "running", "completed", "completed"):
            part = dict(base_part)
            part["state"] = {
                "status": status,
                "input": {"command": "pwd"},
                "title": "pwd",
                "output": "/tmp",
            }
            await self.agent._dispatch_event({
                "type": "message.part.updated",
                "properties": {"part": part},
            })

        messages = await self._messages()
        tools = [message for message in messages if message.type == "tool_use"]
        # Running is surfaced once. Both events share the part id so the view
        # updates one row, and a repeated completed event is ignored.
        self.assertEqual(len(tools), 2)
        self.assertEqual([t.id for t in tools], ["tool_1", "tool_1"])
        self.assertEqual(tools[0].content["status"], "running")
        self.assertEqual(tools[1].content["status"], "completed")
        self.assertEqual(tools[1].content["command"], "pwd")

    async def test_plan_text_is_flushed_when_session_becomes_idle(self):
        self.agent._turn_plan_mode = True
        await self.agent._dispatch_event({
            "type": "message.part.delta",
            "properties": {
                "sessionID": "ses_test",
                "messageID": "msg_1",
                "partID": "part_1",
                "field": "text",
                "delta": "1. inspect\n2. edit",
            },
        })
        await self.agent._dispatch_event({
            "type": "session.idle",
            "properties": {"sessionID": "ses_test"},
        })

        messages = await self._messages()
        self.assertEqual([message.type for message in messages], ["plan_delta", "stop"])
        self.assertEqual(messages[0].content, "1. inspect\n2. edit")

    async def test_permission_event_uses_termmate_control_shape(self):
        await self.agent._dispatch_event({
            "type": "permission.asked",
            "properties": {
                "requestID": "perm_1",
                "sessionID": "ses_test",
                "type": "bash",
                "title": "Run tests",
                "metadata": {"command": "python -m unittest"},
            },
        })

        messages = await self._messages()
        self.assertEqual(len(messages), 1)
        request = messages[0].content["request"]
        self.assertEqual(messages[0].type, "control_request")
        self.assertEqual(request["tool_name"], "Bash")
        self.assertEqual(request["input"]["command"], "python -m unittest")

    async def test_current_permission_event_with_string_kind_is_emitted(self):
        await self.agent._dispatch_event({
            "type": "permission.asked",
            "properties": {
                "id": "perm_current",
                "sessionID": "ses_test",
                "permission": "bash",
                "patterns": ["git status && git diff HEAD"],
                "metadata": {"command": "git status && git diff HEAD"},
                "always": ["git status*"],
                "tool": {
                    "messageID": "msg_1",
                    "callID": "call_1",
                },
            },
        })

        messages = await self._messages()
        self.assertEqual(len(messages), 1)
        request = messages[0].content["request"]
        self.assertEqual(request["tool_name"], "Bash")
        self.assertEqual(
            request["input"]["pattern"],
            ["git status && git diff HEAD"],
        )
        self.assertEqual(
            self.agent._permission_sessions["perm_current"], "ses_test"
        )

    async def test_permission_response_uses_current_reply_route(self):
        self.agent._permission_sessions["perm_1"] = "ses_test"
        self.agent._http = AsyncMock(return_value=True)

        await self.agent.send_approval_response(
            "perm_1", {"behavior": "allow"}
        )

        self.agent._http.assert_awaited_once_with(
            "POST",
            "/permission/perm_1/reply",
            body={"reply": "once"},
        )

    async def test_permission_response_falls_back_to_legacy_route(self):
        self.agent._permission_sessions["perm_1"] = "ses_test"
        missing = OpenCodeHTTPError("POST", "/permission/perm_1/reply", 404, "missing")
        self.agent._http = AsyncMock(side_effect=[missing, None])

        await self.agent.send_approval_response(
            "perm_1", {"behavior": "deny", "message": "no"}
        )

        self.assertEqual(self.agent._http.await_count, 2)
        self.agent._http.assert_awaited_with(
            "POST",
            "/session/ses_test/permissions/perm_1",
            body={"response": "reject"},
        )


class TestOpenCodeManagedServer(unittest.IsolatedAsyncioTestCase):
    async def test_terminates_the_managed_process_group(self):
        process = SimpleNamespace(
            pid=43210,
            returncode=None,
            wait=AsyncMock(return_value=0),
        )
        agent = OpenCodeAgent(AgentOptions(cwd="/workspace"))
        agent._server_process = process

        with patch("genfoundry.opencode_agent.sys.platform", "darwin"), patch(
            "genfoundry.opencode_agent.os.killpg"
        ) as killpg:
            await agent._terminate_server()

        killpg.assert_called_once_with(43210, signal.SIGTERM)
        self.assertIsNone(agent._server_process)

    async def test_requests_an_os_assigned_port(self):
        output = asyncio.StreamReader()
        output.feed_data(
            b"opencode server listening on http://127.0.0.1:53127\n"
        )
        output.feed_eof()
        process = SimpleNamespace(stdout=output, returncode=None)
        agent = OpenCodeAgent(AgentOptions(cwd="/workspace", cli_path="opencode"))
        agent.cli_path = "opencode"

        with patch(
            "genfoundry.opencode_agent.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ) as create_process:
            url = await agent._start_server({"PATH": "/bin"})
            await agent._server_log_task

        self.assertEqual(url, "http://127.0.0.1:53127")
        args = create_process.await_args.args
        if sys.platform == "win32":
            self.assertEqual(
                args[:5],
                ("opencode", "serve", "--hostname=127.0.0.1", "--port=0"),
            )
        else:
            self.assertEqual(args[:2], ("/bin/sh", "-c"))
            self.assertEqual(
                args[-4:],
                ("opencode", "serve", "--hostname=127.0.0.1", "--port=0"),
            )
            self.assertIn("kill -TERM -$$", args[2])
            self.assertEqual(
                create_process.await_args.kwargs["stdin"], asyncio.subprocess.PIPE
            )
            self.assertTrue(create_process.await_args.kwargs["start_new_session"])


class TestOpenCodeTurns(unittest.IsolatedAsyncioTestCase):
    async def test_first_prompt_creates_session_lazily(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=1)
        agent._is_connected = True
        agent._http = AsyncMock(side_effect=[{"id": "ses_new"}, None])

        await agent.send_message("hello")

        self.assertEqual(agent._session_id, "ses_new")
        self.assertEqual(
            [call.args for call in agent._http.await_args_list],
            [
                ("POST", "/session"),
                ("POST", "/session/ses_new/prompt_async"),
            ],
        )
        self.assertEqual(agent._http.await_args_list[0].kwargs, {"body": {}})

        messages = []
        while not agent._message_queue.empty():
            messages.append(await agent._message_queue.get())
        self.assertEqual(
            [message.type for message in messages],
            ["thread_started", "thread_title", "turn_started"],
        )
        self.assertEqual(messages[0].content, {"session_id": "ses_new"})

    async def test_v1_send_message_ignores_think_level(self):
        agent = OpenCodeAgent(
            AgentOptions(
                cwd=".",
                model="anthropic/claude-3-7-sonnet",
                think_level="high",
            ),
            version=1,
        )
        agent._is_connected = True
        agent._session_id = "ses_v1"
        agent._http = AsyncMock(return_value=None)

        await agent.send_message("hello")

        body = agent._http.await_args_list[0].kwargs["body"]
        self.assertEqual(
            body["model"],
            {"providerID": "anthropic", "modelID": "claude-3-7-sonnet"},
        )
        self.assertNotIn("variant", body)

    async def test_connect_without_prompt_does_not_create_session(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=1)
        agent._loop = asyncio.get_running_loop()
        agent._http = AsyncMock(return_value={"healthy": True})
        agent._start_sse_thread = lambda env: agent._sse_ready.set()
        agent._fetch_models = AsyncMock()

        await agent._finish_connect({}, None)
        await asyncio.sleep(0)

        self.assertIsNone(agent._session_id)
        self.assertEqual(agent._http.await_count, 1)
        self.assertEqual(agent._http.await_args.args, ("GET", "/global/health"))
        await agent.disconnect()

    async def test_new_session_switches_session_without_reconnecting(self):
        agent = OpenCodeAgent(
            AgentOptions(cwd=".", session_id="ses_old"), version=1
        )
        agent._is_connected = True
        agent._session_id = "ses_old"
        agent._turn_active = True
        agent._turn_plan_mode = True
        agent._uses_session_status = True
        agent._awaiting_user_message_id = True
        agent._text_cache["part_old"] = "old text"
        agent._part_types["part_old"] = "text"
        agent._message_roles["msg_old"] = "assistant"
        agent._user_message_ids.add("msg_user")
        agent._terminal_tools.add("tool_old")
        agent._permission_sessions["permission_old"] = "ses_old"
        agent._seen_permissions.add("permission_old")
        agent._diff_cache["file.py"] = "old diff"
        agent._plan_text = "old plan"
        agent._http = AsyncMock(side_effect=[None, {"id": "ses_new"}])

        session_id = await agent.new_session()

        self.assertEqual(session_id, "ses_new")
        self.assertEqual(agent._session_id, "ses_new")
        self.assertIsNone(agent.options.session_id)
        self.assertTrue(agent._is_connected)
        self.assertEqual(
            [call.args for call in agent._http.await_args_list],
            [
                ("POST", "/session/ses_old/abort"),
                ("POST", "/session"),
            ],
        )
        self.assertEqual(agent._http.await_args_list[1].kwargs, {"body": {}})
        self.assertFalse(agent._turn_active)
        self.assertFalse(agent._turn_plan_mode)
        self.assertFalse(agent._uses_session_status)
        self.assertFalse(agent._awaiting_user_message_id)
        self.assertTrue(agent._turn_done.is_set())
        self.assertEqual(agent._text_cache, {})
        self.assertEqual(agent._part_types, {})
        self.assertEqual(agent._message_roles, {})
        self.assertEqual(agent._user_message_ids, set())
        self.assertEqual(agent._terminal_tools, set())
        self.assertEqual(agent._permission_sessions, {})
        self.assertEqual(agent._seen_permissions, set())
        self.assertEqual(agent._diff_cache, {})
        self.assertEqual(agent._plan_text, "")

        message = await agent._message_queue.get()
        self.assertEqual(message.type, "thread_started")
        self.assertEqual(message.content, {"session_id": "ses_new"})

    async def test_new_session_rejects_invalid_response(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=1)
        agent._is_connected = True
        agent._session_id = "ses_old"
        agent._http = AsyncMock(return_value={})

        with self.assertRaisesRegex(
            RuntimeError, "OpenCode did not return a valid session"
        ):
            await agent.new_session()

        self.assertEqual(agent._session_id, "ses_old")

    async def test_prompt_uses_server_generated_message_id(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=1)
        agent._is_connected = True
        agent._session_id = "ses_test"
        agent._http = AsyncMock(return_value=None)

        await agent.send_message("hello")

        body = agent._http.await_args.kwargs["body"]
        self.assertNotIn("messageID", body)
        self.assertEqual(body["parts"], [{"type": "text", "text": "hello"}])

        await agent._dispatch_event({
            "type": "message.updated",
            "properties": {
                "info": {
                    "id": "msg_server_generated",
                    "sessionID": "ses_test",
                    "role": "user",
                },
            },
        })

        messages = []
        while not agent._message_queue.empty():
            messages.append(await agent._message_queue.get())
        assigned = [m for m in messages if m.type == "user_message_id"]
        self.assertEqual(len(assigned), 1)
        self.assertEqual(
            assigned[0].content["message_id"], "msg_server_generated"
        )
        self.assertIn("msg_server_generated", agent._user_message_ids)

    async def test_status_idle_releases_next_prompt_and_legacy_idle_is_ignored(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=1)
        agent._is_connected = True
        agent._session_id = "ses_test"
        agent._http = AsyncMock(return_value=None)

        await agent.send_message("first")
        second = asyncio.create_task(agent.send_message("second"))
        await asyncio.sleep(0)

        self.assertEqual(agent._http.await_count, 1)
        self.assertFalse(second.done())

        await agent._dispatch_event({
            "type": "session.status",
            "properties": {
                "sessionID": "ses_test",
                "status": {"type": "idle"},
            },
        })
        await second
        self.assertEqual(agent._http.await_count, 2)
        self.assertTrue(agent._turn_active)

        # The compatibility event for the first turn may arrive after the
        # second prompt starts.  It must not finish that new turn.
        await agent._dispatch_event({
            "type": "session.idle",
            "properties": {"sessionID": "ses_test"},
        })
        self.assertTrue(agent._turn_active)

        await agent._dispatch_event({
            "type": "session.status",
            "properties": {
                "sessionID": "ses_test",
                "status": {"type": "idle"},
            },
        })
        self.assertFalse(agent._turn_active)

    async def test_legacy_idle_releases_prompt_without_status_protocol(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=1)
        agent._is_connected = True
        agent._session_id = "ses_test"
        agent._http = AsyncMock(return_value=None)

        await agent.send_message("first")
        second = asyncio.create_task(agent.send_message("second"))
        await asyncio.sleep(0)

        await agent._dispatch_event({
            "type": "session.idle",
            "properties": {"sessionID": "ses_test"},
        })
        await second

        self.assertEqual(agent._http.await_count, 2)


class TestOpenCodeRuntimeConfig(unittest.TestCase):
    def test_allow_edit_allows_edit_tools(self):
        options = AgentOptions(
            cwd=".",
            approve_mode="allow-edit",
            allowed_tools=["Read", "Glob", "Grep", "TodoWrite"],
            disallowed_tools=["AskUserQuestion"],
        )
        agent = OpenCodeAgent(options)
        config = json.loads(agent._runtime_env()["OPENCODE_CONFIG_CONTENT"])

        self.assertEqual(config["permission"]["edit"], "allow")
        self.assertEqual(config["permission"]["read"], "allow")
        self.assertEqual(config["permission"]["todowrite"], "allow")
        self.assertEqual(config["permission"]["*"], "ask")
        self.assertEqual(config["permission"]["question"], "deny")

    def test_defaults_to_allow_edit(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."))
        config = json.loads(agent._runtime_env()["OPENCODE_CONFIG_CONTENT"])

        self.assertEqual(config["permission"], {"*": "ask", "edit": "allow"})

    def test_default_mode_asks_for_edit(self):
        agent = OpenCodeAgent(AgentOptions(cwd=".", approve_mode="default"))
        config = json.loads(agent._runtime_env()["OPENCODE_CONFIG_CONTENT"])

        self.assertEqual(config["permission"], {"*": "ask"})

    def test_accept_all_allows_edit_and_asks_for_other_tools(self):
        agent = OpenCodeAgent(AgentOptions(cwd=".", approve_mode="accept-all"))
        config = json.loads(agent._runtime_env()["OPENCODE_CONFIG_CONTENT"])

        self.assertEqual(
            config["permission"],
            {"*": "ask", "edit": "allow"},
        )

    def test_set_think_level_updates_options(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=2)

        agent.set_think_level("high")

        self.assertEqual(agent.options.think_level, "high")


class TestOpenCodeVersionDetection(unittest.TestCase):
    def test_parse_version_formats(self):
        self.assertEqual(
            parse_opencode_version_output("opencode v2.0.29"), (2, "2.0.29")
        )
        self.assertEqual(
            parse_opencode_version_output("1.18.25"), (1, "1.18.25")
        )
        self.assertEqual(
            parse_opencode_version_output("v1.18.25"), (1, "1.18.25")
        )

    def test_detect_version_v2(self):
        with patch("genfoundry.opencode_agent.subprocess.run") as mock_run:
            mock_run.return_value = SimpleNamespace(
                stdout="opencode v2.0.29\n", stderr=""
            )
            self.assertEqual(detect_opencode_version("opencode"), 2)

    def test_detect_version_v1(self):
        with patch("genfoundry.opencode_agent.subprocess.run") as mock_run:
            mock_run.return_value = SimpleNamespace(
                stdout="1.18.25\n", stderr=""
            )
            self.assertEqual(detect_opencode_version("opencode"), 1)

    def test_detect_version_failure_returns_none(self):
        with patch(
            "genfoundry.opencode_agent.subprocess.run",
            side_effect=OSError("not found"),
        ):
            self.assertIsNone(detect_opencode_version("opencode"))

    def test_agent_auto_detects_version_from_cli(self):
        with patch(
            "genfoundry.opencode_agent.detect_opencode_version_info",
            return_value=(2, "2.0.29"),
        ):
            agent = OpenCodeAgent(AgentOptions(cwd="."))
            self.assertEqual(agent.version, 2)
            self.assertEqual(agent.version_str, "2.0.29")

        with patch(
            "genfoundry.opencode_agent.detect_opencode_version_info",
            return_value=(1, "1.18.25"),
        ):
            agent = OpenCodeAgent(AgentOptions(cwd="."))
            self.assertEqual(agent.version, 1)
            self.assertEqual(agent.version_str, "1.18.25")

    def test_find_opencode_v2_password(self):
        fake_json = json.dumps({"password": "secret_v2_password"})
        with patch("genfoundry.opencode_agent.os.path.isfile", return_value=True):
            with patch(
                "builtins.open", unittest.mock.mock_open(read_data=fake_json)
            ):
                self.assertEqual(find_opencode_v2_password(), "secret_v2_password")

    def test_finds_windows_user_state_service_json(self):
        """OpenCode on Windows writes service.json under ~\\.local\\state."""
        home = os.path.join("C:" + os.sep, "Users", "tester")
        expected = os.path.join(home, ".local", "state", "opencode", "service.json")
        fake_json = json.dumps({
            "url": "http://127.0.0.1:28894/",
            "password": "user_state_password",
        })
        env = {
            "APPDATA": os.path.join("C:" + os.sep, "AppData", "Roaming"),
            "LOCALAPPDATA": os.path.join("C:" + os.sep, "AppData", "Local"),
        }

        def only_user_state(path):
            return os.path.normcase(path) == os.path.normcase(expected)

        with patch.object(opencode_agent.sys, "platform", "win32"), \
             patch.object(opencode_agent.os.path, "expanduser", return_value=home), \
             patch.object(
                 opencode_agent, "get_termmate_opencode_state_dir",
                 return_value=os.path.join("C:" + os.sep, "Temp", "private"),
             ), \
             patch.object(opencode_agent.os.path, "isfile", side_effect=only_user_state), \
             patch("builtins.open", unittest.mock.mock_open(read_data=fake_json)):
            service = find_opencode_v2_service(env)
            password = find_opencode_v2_password(env)

        self.assertEqual(service["url"], "http://127.0.0.1:28894")
        self.assertEqual(service["password"], "user_state_password")
        self.assertEqual(password, "user_state_password")


class TestOpenCodeV2AgentEvents(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.agent = OpenCodeAgent(AgentOptions(cwd="."), version=2)
        self.agent._session_id = "ses_v2"
        self.agent._turn_active = True

    async def _messages(self):
        result = []
        while not self.agent._message_queue.empty():
            result.append(await self.agent._message_queue.get())
        return result

    async def test_v2_text_delta_streaming(self):
        await self.agent._dispatch_event({
            "type": "session.text.delta",
            "data": {
                "sessionID": "ses_v2",
                "assistantMessageID": "msg_v2_1",
                "ordinal": 0,
                "delta": "Hello from OpenCode V2",
            },
        })

        messages = await self._messages()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].type, "text")
        self.assertEqual(messages[0].content, "Hello from OpenCode V2")
        self.assertEqual(messages[0].id, "msg_v2_1")

    async def test_v2_reasoning_delta_streaming(self):
        await self.agent._dispatch_event({
            "type": "session.reasoning.delta",
            "data": {
                "sessionID": "ses_v2",
                "assistantMessageID": "msg_v2_1",
                "ordinal": 0,
                "delta": "Thinking step 1",
            },
        })

        messages = await self._messages()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].type, "thinking")
        self.assertEqual(messages[0].content, "Thinking step 1")

    async def test_v2_inbox_admission_emits_user_message_id(self):
        self.agent._awaiting_user_message_id = True
        await self.agent._dispatch_event({
            "type": "session.inbox.enqueued",
            "data": {
                "sessionID": "ses_v2",
                "inboxID": "msg_user_v2",
            },
        })

        messages = await self._messages()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].type, "user_message_id")
        self.assertEqual(messages[0].content, {"message_id": "msg_user_v2"})
        self.assertIn("msg_user_v2", self.agent._user_message_ids)

    async def test_v2_tool_called_and_success(self):
        await self.agent._dispatch_event({
            "type": "session.tool.called",
            "data": {
                "sessionID": "ses_v2",
                "id": "call_v2_1",
                "name": "bash",
                "input": {"command": "ls -la"},
            },
        })
        await self.agent._dispatch_event({
            "type": "session.tool.success",
            "data": {
                "sessionID": "ses_v2",
                "id": "call_v2_1",
                "output": "file1.txt\nfile2.txt",
            },
        })

        messages = await self._messages()
        tools = [m for m in messages if m.type == "tool_use"]
        self.assertEqual(len(tools), 2)
        self.assertEqual([t.id for t in tools], ["call_v2_1", "call_v2_1"])
        self.assertEqual(tools[0].content["status"], "running")
        self.assertEqual(tools[0].content["command"], "ls -la")
        self.assertEqual(tools[1].content["name"], "command_execution")
        self.assertEqual(tools[1].content["command"], "ls -la")
        self.assertEqual(tools[1].content["output"], "file1.txt\nfile2.txt")
        self.assertEqual(tools[1].content["status"], "completed")

    async def test_v2_task_running_tool_is_emitted(self):
        await self.agent._dispatch_event({
            "type": "session.tool.called",
            "data": {
                "sessionID": "ses_v2",
                "id": "task_1",
                "name": "task",
                "input": {"description": "Investigate"},
            },
        })
        await self.agent._dispatch_event({
            "type": "session.tool.success",
            "data": {
                "sessionID": "ses_v2",
                "id": "task_1",
                "output": "done",
            },
        })

        tools = [m for m in await self._messages() if m.type == "tool_use"]
        self.assertEqual(len(tools), 2)
        self.assertEqual(tools[0].id, "task_1")
        self.assertEqual(tools[0].content["name"], "task")
        self.assertEqual(tools[0].content["status"], "running")
        self.assertEqual(tools[1].content["status"], "completed")

    async def test_v2_subagent_tool_running_row_is_emitted(self):
        # OpenCode uses "subagent" as the real tool name.
        await self.agent._dispatch_event({
            "type": "session.tool.called",
            "data": {
                "sessionID": "ses_v2",
                "id": "subagent_1",
                "name": "subagent",
                "input": {"description": "Search repo"},
            },
        })

        tools = [m for m in await self._messages() if m.type == "tool_use"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0].id, "subagent_1")
        self.assertEqual(tools[0].content["name"], "subagent")
        self.assertEqual(tools[0].content["status"], "running")

    async def test_v2_subagent_tool_result_does_not_duplicate_child_text(self):
        await self.agent._dispatch_event({
            "type": "session.tool.success",
            "data": {
                "sessionID": "ses_v2",
                "assistantMessageID": "msg_v2_1",
                "id": "subagent_done",
                "name": "subagent",
                "content": [{
                    "type": "text",
                    "text": '<subagent sessionID="ses_child" state="completed">'
                            'full child answer</subagent>',
                }],
            },
        })

        tools = [m for m in await self._messages() if m.type == "tool_use"]
        self.assertEqual(len(tools), 1)
        self.assertIsNone(tools[0].content.get("output"))
        self.assertEqual(tools[0].content["status"], "completed")

    async def test_v2_failed_plan_turn_publishes_buffered_answer(self):
        self.agent._turn_plan_mode = True
        self.agent._plan_text = "这次换个测法：5 个并发"
        await self.agent._dispatch_event({
            "type": "session.execution.failed",
            "data": {
                "sessionID": "ses_v2",
                "error": {"message": "Free usage limit reached"},
            },
        })

        messages = await self._messages()
        self.assertEqual(
            [m.type for m in messages],
            ["plan_delta", "stop", "error"],
        )
        self.assertEqual(messages[0].content, "这次换个测法：5 个并发")
        self.assertEqual(self.agent._plan_text, "")

    async def test_v2_subagent_text_from_child_session_is_visible(self):
        await self.agent._dispatch_event({
            "type": "session.text.delta",
            "data": {
                "sessionID": "ses_child",
                "parentID": "ses_v2",
                "assistantMessageID": "child_msg",
                "delta": "working on it",
            },
        })

        messages = await self._messages()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].type, "subagent_text")
        self.assertEqual(messages[0].content, "working on it")

    async def test_v2_subagent_text_uses_session_created_parent(self):
        # Real session.text.delta events have no parentID.  The parent link is
        # announced earlier by session.created and must be cached.
        await self.agent._dispatch_event({
            "type": "session.created",
            "data": {
                "sessionID": "ses_child_created",
                "info": {
                    "id": "ses_child_created",
                    "parentID": "ses_v2",
                },
            },
        })
        await self.agent._dispatch_event({
            "type": "session.text.delta",
            "data": {
                "sessionID": "ses_child_created",
                "assistantMessageID": "child_msg_created",
                "delta": "from child",
            },
        })

        messages = await self._messages()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].type, "subagent_text")
        self.assertEqual(messages[0].content, "from child")

    async def test_v2_subagent_text_uses_session_updated_info_parent(self):
        await self.agent._dispatch_event({
            "type": "session.updated",
            "data": {
                "sessionID": "ses_child_updated",
                "info": {
                    "id": "ses_child_updated",
                    "parentID": "ses_v2",
                },
            },
        })
        await self.agent._dispatch_event({
            "type": "session.text.delta",
            "data": {
                "sessionID": "ses_child_updated",
                "assistantMessageID": "child_msg_updated",
                "delta": "updated child",
            },
        })

        messages = await self._messages()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].type, "subagent_text")
        self.assertEqual(messages[0].content, "updated child")

    async def test_v2_subagent_text_ended_uses_cached_parent(self):
        await self.agent._dispatch_event({
            "type": "session.created",
            "data": {
                "sessionID": "ses_child_ended",
                "parentID": "ses_v2",
            },
        })
        await self.agent._dispatch_event({
            "type": "session.text.ended",
            "data": {
                "sessionID": "ses_child_ended",
                "assistantMessageID": "child_msg_ended",
                "text": "complete child answer",
            },
        })

        messages = await self._messages()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].type, "subagent_text")
        self.assertEqual(messages[0].content, "complete child answer")

    async def test_v2_child_text_from_other_parent_is_ignored(self):
        await self.agent._dispatch_event({
            "type": "session.created",
            "data": {
                "sessionID": "ses_other_child",
                "parentID": "ses_other_parent",
            },
        })
        await self.agent._dispatch_event({
            "type": "session.text.delta",
            "data": {
                "sessionID": "ses_other_child",
                "assistantMessageID": "other_msg",
                "delta": "not ours",
            },
        })

        self.assertEqual(await self._messages(), [])

    async def test_v2_tool_lifecycle_input_started_and_patch(self):
        # Realistic OpenCode v2 flow: name is in input.started, not in called or success
        await self.agent._dispatch_event({
            "type": "session.tool.input.started",
            "data": {
                "sessionID": "ses_v2",
                "id": "call_v2_edit",
                "name": "edit",
            },
        })
        await self.agent._dispatch_event({
            "type": "session.tool.called",
            "data": {
                "sessionID": "ses_v2",
                "id": "call_v2_edit",
                "input": {
                    "path": "README.md",
                    "oldString": "hello",
                    "newString": "world",
                },
            },
        })
        await self.agent._dispatch_event({
            "type": "session.tool.success",
            "data": {
                "sessionID": "ses_v2",
                "id": "call_v2_edit",
                "content": [{"type": "text", "text": "Edited README.md"}],
                "metadata": {
                    "files": [{
                        "file": "README.md",
                        "patch": "@@ -1 +1 @@\n-hello\n+world\n",
                    }],
                },
            },
        })

        messages = await self._messages()
        tools = [m for m in messages if m.type == "tool_use"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0].content["name"], "edit")
        self.assertEqual(tools[0].content["input"]["path"], "README.md")
        self.assertEqual(
            tools[0].content["metadata"]["diff"],
            "@@ -1 +1 @@\n-hello\n+world\n",
        )
        self.assertEqual(tools[0].content["status"], "completed")

    async def test_v2_tool_shell_lifecycle(self):
        await self.agent._dispatch_event({
            "type": "session.tool.input.started",
            "data": {
                "sessionID": "ses_v2",
                "id": "call_v2_sh",
                "name": "shell",
            },
        })
        await self.agent._dispatch_event({
            "type": "session.tool.called",
            "data": {
                "sessionID": "ses_v2",
                "id": "call_v2_sh",
                "input": {"command": "git diff"},
            },
        })
        await self.agent._dispatch_event({
            "type": "session.tool.success",
            "data": {
                "sessionID": "ses_v2",
                "id": "call_v2_sh",
                "content": [{"type": "text", "text": "diff output"}],
            },
        })

        messages = await self._messages()
        tools = [m for m in messages if m.type == "tool_use"]
        self.assertEqual(len(tools), 2)
        self.assertEqual(tools[0].content["status"], "running")
        self.assertEqual(tools[1].content["name"], "command_execution")
        self.assertEqual(tools[1].content["command"], "git diff")
        self.assertEqual(tools[1].content["output"], "diff output")

    async def test_v2_non_command_tool_has_no_running_row(self):
        await self.agent._dispatch_event({
            "type": "session.tool.called",
            "data": {
                "sessionID": "ses_v2",
                "id": "call_v2_read",
                "name": "read",
                "input": {"path": "a.py"},
            },
        })

        messages = await self._messages()
        self.assertEqual([m.type for m in messages], [])

    async def test_v2_background_shell_resolved_by_shell_exited(self):
        await self.agent._dispatch_event({
            "type": "session.tool.called",
            "data": {
                "sessionID": "ses_v2",
                "id": "call_v2_bg",
                "name": "shell",
                "input": {"command": "npm run dev", "background": True},
            },
        })
        await self.agent._dispatch_event({
            "type": "session.tool.success",
            "data": {
                "sessionID": "ses_v2",
                "id": "call_v2_bg",
                "metadata": {"status": "running", "shellID": "sh_bg"},
            },
        })

        messages = await self._messages()
        tools = [m for m in messages if m.type == "tool_use"]
        self.assertEqual(len(tools), 2)
        self.assertTrue(tools[0].content.get("background"))
        self.assertEqual(tools[1].content["status"], "completed")
        self.assertEqual(
            tools[1].content["metadata"], {"status": "running", "shellID": "sh_bg"})
        # The view turns this into "running in background".
        self.assertEqual(self.agent._background_shells.get("sh_bg"), "call_v2_bg")

        await self.agent._dispatch_event({
            "type": "shell.exited",
            "data": {"id": "sh_bg", "status": "exited", "exit": 0},
        })

        messages = await self._messages()
        exited = [m for m in messages if m.type == "tool_use"]
        self.assertEqual(len(exited), 1)
        self.assertEqual(exited[0].id, "call_v2_bg")
        self.assertEqual(exited[0].content["name"], "command_execution")
        self.assertEqual(exited[0].content["command"], "npm run dev")
        self.assertEqual(exited[0].content["status"], "completed")
        # The mapping is kept so a later synthetic notice can still update it.
        self.assertEqual(self.agent._background_shells.get("sh_bg"), "call_v2_bg")

    async def test_v2_background_shell_killed_is_failed(self):
        await self.agent._dispatch_event({
            "type": "session.tool.called",
            "data": {
                "sessionID": "ses_v2",
                "id": "call_v2_bg",
                "name": "shell",
                "input": {"command": "npm run dev"},
            },
        })
        await self.agent._dispatch_event({
            "type": "session.tool.success",
            "data": {
                "sessionID": "ses_v2",
                "id": "call_v2_bg",
                "metadata": {"status": "running", "shellID": "sh_bg"},
            },
        })
        await self._messages()
        await self.agent._dispatch_event({
            "type": "shell.exited",
            "data": {"id": "sh_bg", "status": "killed", "exit": None},
        })

        messages = await self._messages()
        exited = [m for m in messages if m.type == "tool_use"]
        self.assertEqual(exited[0].content["status"], "failed")

    async def _start_background_shell(self):
        """Feed the events that precede a background shell completion."""
        await self.agent._dispatch_event({
            "type": "session.tool.called",
            "data": {
                "sessionID": "ses_v2",
                "id": "call_v2_bg",
                "name": "shell",
                "input": {"command": "npm run dev", "background": True},
            },
        })
        await self.agent._dispatch_event({
            "type": "session.tool.success",
            "data": {
                "sessionID": "ses_v2",
                "id": "call_v2_bg",
                "metadata": {"status": "running", "shellID": "sh_bg"},
            },
        })
        await self._messages()

    async def test_v2_synthetic_shell_notice_emits_shell_finished(self):
        await self._start_background_shell()

        await self.agent._dispatch_event({
            "type": "session.synthetic",
            "data": {
                "sessionID": "ses_v2",
                "id": "msg_syn",
                "text": (
                    '<shell id="sh_bg" state="completed" '
                    'command="npm run dev">\nserver ready\r\n\n</shell>'
                ),
                "description": "npm run dev",
                "metadata": {
                    "source": "shell",
                    "shellID": "sh_bg",
                    "state": "completed",
                    "exit": 0,
                },
            },
        })

        messages = await self._messages()
        finished = [m for m in messages if m.type == "shell_finished"]
        self.assertEqual(len(finished), 1)
        content = finished[0].content
        self.assertEqual(content["tool_key"], "call_v2_bg")
        self.assertEqual(content["command"], "npm run dev")
        self.assertEqual(content["output"], "server ready")
        self.assertFalse(content["failed"])

    async def test_v2_inbox_synthetic_shell_notice_emits_shell_finished(self):
        await self._start_background_shell()

        await self.agent._dispatch_event({
            "type": "session.inbox.enqueued",
            "data": {
                "sessionID": "ses_v2",
                "inboxID": "msg_inbox",
                "item": {
                    "type": "synthetic",
                    "payload": {
                        "text": (
                            "The following shell command was executed by the "
                            "user:\n"
                            '<shell id="sh_bg" state="completed" '
                            'command="npm run dev">\nserver ready\r\n\n</shell>'
                        ),
                        "metadata": {
                            "source": "shell",
                            "shellID": "sh_bg",
                            "state": "completed",
                            "exit": 0,
                        },
                    },
                    "delivery": "steer",
                },
            },
        })

        messages = await self._messages()
        finished = [m for m in messages if m.type == "shell_finished"]
        self.assertEqual(len(finished), 1)
        content = finished[0].content
        self.assertEqual(content["tool_key"], "call_v2_bg")
        self.assertEqual(content["command"], "npm run dev")
        self.assertEqual(content["output"], "server ready")
        self.assertFalse(content["failed"])

    async def test_v2_inbox_shell_notice_recovers_command_from_text(self):
        await self._start_background_shell()

        await self.agent._dispatch_event({
            "type": "session.inbox.enqueued",
            "data": {
                "sessionID": "ses_v2",
                "inboxID": "msg_inbox",
                "item": {
                    "type": "synthetic",
                    "payload": {
                        "text": (
                            '<shell id="sh_bg" state="completed" '
                            'command="npm run dev">\nserver ready\n</shell>'
                        ),
                        "metadata": {
                            "source": "shell",
                            "shellID": "sh_bg",
                            "state": "completed",
                            "exit": 0,
                        },
                    },
                    "delivery": "steer",
                },
            },
        })

        finished = [
            m for m in await self._messages() if m.type == "shell_finished"]
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0].content["command"], "npm run dev")

    async def test_v2_inbox_user_item_is_not_a_shell_notice(self):
        await self.agent._dispatch_event({
            "type": "session.inbox.enqueued",
            "data": {
                "sessionID": "ses_v2",
                "inboxID": "msg_user",
                "item": {"type": "user", "payload": {"text": "hello"}},
            },
        })

        self.assertEqual(
            [m for m in await self._messages() if m.type == "shell_finished"],
            [])

    async def test_v2_inbox_and_shell_ended_emit_once(self):
        await self._start_background_shell()

        await self.agent._dispatch_event({
            "type": "session.inbox.enqueued",
            "data": {
                "sessionID": "ses_v2",
                "inboxID": "msg_inbox",
                "item": {
                    "type": "synthetic",
                    "payload": {
                        "text": (
                            '<shell id="sh_bg" state="completed" '
                            'command="npm run dev">\nserver ready\n</shell>'
                        ),
                        "metadata": {
                            "source": "shell",
                            "shellID": "sh_bg",
                            "state": "completed",
                            "exit": 0,
                        },
                    },
                },
            },
        })
        await self.agent._dispatch_event({
            "type": "session.shell.ended",
            "data": {
                "sessionID": "ses_v2",
                "shell": {
                    "id": "sh_bg",
                    "command": "npm run dev",
                    "status": "exited",
                    "exit": 0,
                },
                "output": {"output": "server ready", "truncated": False},
            },
        })

        finished = [
            m for m in await self._messages() if m.type == "shell_finished"]
        self.assertEqual(len(finished), 1)

    async def test_v2_synthetic_non_shell_message_is_ignored(self):
        await self.agent._dispatch_event({
            "type": "session.synthetic",
            "data": {
                "sessionID": "ses_v2",
                "id": "msg_syn",
                "text": "embedded context",
                "description": "ACP embedded context",
                "metadata": {"source": "acp"},
            },
        })

        self.assertEqual(
            [m for m in await self._messages() if m.type == "shell_finished"], [])

    async def test_v2_shell_ended_is_a_fallback_and_deduped(self):
        await self._start_background_shell()

        await self.agent._dispatch_event({
            "type": "session.shell.ended",
            "data": {
                "sessionID": "ses_v2",
                "shell": {
                    "id": "sh_bg",
                    "command": "npm run dev",
                    "status": "exited",
                    "exit": 0,
                },
                "output": {"output": "server ready", "truncated": False,
                           "cursor": 12, "size": 12},
            },
        })
        messages = await self._messages()
        finished = [m for m in messages if m.type == "shell_finished"]
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0].content["output"], "server ready")
        self.assertFalse(finished[0].content["failed"])

        # The synthetic notice for the same shell must not add a second line.
        await self.agent._dispatch_event({
            "type": "session.synthetic",
            "data": {
                "sessionID": "ses_v2",
                "id": "msg_syn",
                "text": '<shell id="sh_bg" state="completed">\nserver ready\n</shell>',
                "description": "npm run dev",
                "metadata": {"source": "shell", "shellID": "sh_bg", "exit": 0},
            },
        })
        messages = await self._messages()
        self.assertEqual(
            [m for m in messages if m.type == "shell_finished"], [])

    async def test_v2_shell_ended_ignores_foreground_shells(self):
        await self.agent._dispatch_event({
            "type": "session.tool.called",
            "data": {
                "sessionID": "ses_v2",
                "id": "call_v2_fg",
                "name": "shell",
                "input": {"command": "git status"},
            },
        })
        await self.agent._dispatch_event({
            "type": "session.shell.ended",
            "data": {
                "sessionID": "ses_v2",
                "shell": {"id": "sh_fg", "command": "git status", "status": "exited"},
                "output": "clean",
            },
        })

        self.assertEqual(
            [m for m in await self._messages() if m.type == "shell_finished"], [])

    async def test_v2_shell_ended_failure_is_marked(self):
        await self._start_background_shell()

        await self.agent._dispatch_event({
            "type": "session.shell.ended",
            "data": {
                "sessionID": "ses_v2",
                "shell": {
                    "id": "sh_bg",
                    "command": "npm run dev",
                    "status": "exited",
                    "exit": 1,
                },
                "output": "boom",
            },
        })

        messages = await self._messages()
        finished = [m for m in messages if m.type == "shell_finished"]
        self.assertEqual(len(finished), 1)
        self.assertTrue(finished[0].content["failed"])

    async def test_v2_shell_started_records_command_for_notice(self):
        await self._start_background_shell()
        await self.agent._dispatch_event({
            "type": "session.shell.started",
            "data": {
                "sessionID": "ses_v2",
                "shell": {"id": "sh_bg", "command": "npm run dev"},
            },
        })
        await self.agent._dispatch_event({
            "type": "session.shell.ended",
            "data": {
                "sessionID": "ses_v2",
                "shell": {"id": "sh_bg", "status": "exited", "exit": 0},
            },
        })

        messages = await self._messages()
        finished = [m for m in messages if m.type == "shell_finished"]
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0].content["command"], "npm run dev")

    async def test_v2_text_ended_recovers_text_lost_with_deltas(self):
        await self.agent._dispatch_event({
            "type": "session.text.delta",
            "data": {
                "sessionID": "ses_v2",
                "assistantMessageID": "msg_v2_1",
                "ordinal": 0,
                "delta": "Hello",
            },
        })
        await self.agent._dispatch_event({
            "type": "session.text.ended",
            "data": {
                "sessionID": "ses_v2",
                "assistantMessageID": "msg_v2_1",
                "ordinal": 0,
                "text": "Hello world",
            },
        })

        messages = await self._messages()
        self.assertEqual([m.content for m in messages], ["Hello", " world"])
        self.assertEqual(messages[0].id, "msg_v2_1")
        self.assertEqual(messages[1].id, "msg_v2_1")

    async def test_v2_text_ended_is_ignored_when_deltas_were_complete(self):
        await self.agent._dispatch_event({
            "type": "session.text.delta",
            "data": {
                "sessionID": "ses_v2",
                "assistantMessageID": "msg_v2_1",
                "ordinal": 0,
                "delta": "Hello world",
            },
        })
        await self.agent._dispatch_event({
            "type": "session.text.ended",
            "data": {
                "sessionID": "ses_v2",
                "assistantMessageID": "msg_v2_1",
                "ordinal": 0,
                "text": "Hello world",
            },
        })

        messages = await self._messages()
        self.assertEqual([m.content for m in messages], ["Hello world"])

    async def test_v2_text_ended_without_any_delta_emits_full_text(self):
        await self.agent._dispatch_event({
            "type": "session.text.ended",
            "data": {
                "sessionID": "ses_v2",
                "assistantMessageID": "msg_v2_1",
                "ordinal": 0,
                "text": "后台任务已完成 ✅",
            },
        })

        messages = await self._messages()
        self.assertEqual([m.content for m in messages], ["后台任务已完成 ✅"])

    async def test_v2_text_started_resets_part_accumulator(self):
        await self.agent._dispatch_event({
            "type": "session.text.delta",
            "data": {
                "sessionID": "ses_v2",
                "assistantMessageID": "msg_v2_1",
                "ordinal": 0,
                "delta": "abc",
            },
        })
        await self.agent._dispatch_event({
            "type": "session.text.started",
            "data": {
                "sessionID": "ses_v2",
                "assistantMessageID": "msg_v2_1",
                "ordinal": 1,
            },
        })
        await self.agent._dispatch_event({
            "type": "session.text.ended",
            "data": {
                "sessionID": "ses_v2",
                "assistantMessageID": "msg_v2_1",
                "ordinal": 1,
                "text": "def",
            },
        })

        messages = await self._messages()
        self.assertEqual([m.content for m in messages], ["abc", "def"])

    async def test_v2_background_resume_stops_loading_again(self):
        # The prompt turn already stopped, then a background shell resumed
        # the session with new assistant text.
        self.agent._turn_active = False
        self.agent._idle_announced = True
        await self.agent._dispatch_event({
            "type": "session.execution.started",
            "data": {"sessionID": "ses_v2"},
        })
        await self.agent._dispatch_event({
            "type": "session.execution.succeeded",
            "data": {"sessionID": "ses_v2"},
        })

        messages = await self._messages()
        self.assertEqual([m.type for m in messages], ["stop"])
        self.assertFalse(self.agent._turn_active)

    async def test_v2_duplicate_idle_events_emit_one_stop(self):
        self.agent._turn_active = False
        self.agent._idle_announced = True
        await self.agent._dispatch_event({
            "type": "session.execution.started",
            "data": {"sessionID": "ses_v2"},
        })
        for event_type in (
            "session.execution.succeeded",
            "session.idle",
            "session.execution.succeeded",
        ):
            await self.agent._dispatch_event({
                "type": event_type,
                "data": {"sessionID": "ses_v2"},
            })

        messages = await self._messages()
        self.assertEqual([m.type for m in messages], ["stop"])

    async def test_v2_shell_started_background_enables_ended_fallback(self):
        await self.agent._dispatch_event({
            "type": "session.shell.started",
            "data": {
                "sessionID": "ses_v2",
                "shell": {
                    "id": "sh_bg",
                    "command": "npm run dev",
                    "status": "running",
                    "metadata": {"background": True},
                },
            },
        })
        await self.agent._dispatch_event({
            "type": "session.shell.ended",
            "data": {
                "sessionID": "ses_v2",
                "shell": {"id": "sh_bg", "status": "exited", "exit": 0},
                "output": {"output": "ready", "truncated": False},
            },
        })

        messages = await self._messages()
        finished = [m for m in messages if m.type == "shell_finished"]
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0].content["command"], "npm run dev")
        self.assertEqual(finished[0].content["output"], "ready")
        self.assertFalse(finished[0].content["tool_key"])

    async def test_v2_background_shell_registered_from_input_flag(self):
        await self.agent._dispatch_event({
            "type": "session.tool.called",
            "data": {
                "sessionID": "ses_v2",
                "id": "call_v2_bg",
                "name": "shell",
                "input": {"command": "npm run dev", "background": True},
            },
        })
        await self.agent._dispatch_event({
            "type": "session.tool.success",
            "data": {
                "sessionID": "ses_v2",
                "id": "call_v2_bg",
                "metadata": {"shellID": "sh_bg"},
            },
        })

        self.assertEqual(self.agent._background_shells.get("sh_bg"), "call_v2_bg")

    async def test_v2_shell_finished_recovers_tool_key_from_cache(self):
        await self._start_background_shell()
        # Simulate a lost tool-success registration (or event ordering).
        self.agent._background_shells.clear()

        await self.agent._dispatch_event({
            "type": "session.synthetic",
            "data": {
                "sessionID": "ses_v2",
                "text": '<shell id="sh_bg" state="completed">\nready\n</shell>',
                "description": "npm run dev",
                "metadata": {"source": "shell", "shellID": "sh_bg", "exit": 0},
            },
        })

        messages = await self._messages()
        finished = [m for m in messages if m.type == "shell_finished"]
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0].content["tool_key"], "call_v2_bg")

    async def test_v2_tool_failed(self):
        await self.agent._dispatch_event({
            "type": "session.tool.called",
            "data": {
                "sessionID": "ses_v2",
                "id": "call_v2_2",
                "name": "read",
                "input": {"path": "missing.py"},
            },
        })
        await self.agent._dispatch_event({
            "type": "session.tool.failed",
            "data": {
                "sessionID": "ses_v2",
                "id": "call_v2_2",
                "error": {"message": "File not found"},
            },
        })

        messages = await self._messages()
        tools = [m for m in messages if m.type == "tool_use"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0].content["name"], "read")
        self.assertEqual(tools[0].content["error"], "File not found")
        self.assertEqual(tools[0].content["status"], "error")

    async def test_v2_execution_succeeded_finishes_turn(self):
        await self.agent._dispatch_event({
            "type": "session.execution.succeeded",
            "data": {"sessionID": "ses_v2"},
        })

        self.assertFalse(self.agent._turn_active)
        messages = await self._messages()
        self.assertEqual([m.type for m in messages], ["stop"])

    async def test_v2_execution_failed_emits_error(self):
        await self.agent._dispatch_event({
            "type": "session.execution.failed",
            "data": {
                "sessionID": "ses_v2",
                "error": {"message": "Model execution context exhausted"},
            },
        })

        self.assertFalse(self.agent._turn_active)
        messages = await self._messages()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].type, "error")
        self.assertIn("Model execution context exhausted", messages[0].content)

    async def test_v2_permission_asked_with_action_field(self):
        await self.agent._dispatch_event({
            "type": "permission.asked",
            "data": {
                "id": "per_123",
                "sessionID": "ses_v2",
                "action": "shell",
                "metadata": {"command": "ls -la"},
            },
        })

        messages = await self._messages()
        requests = [m for m in messages if m.type == "control_request"]
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].content["request_id"], "per_123")
        self.assertEqual(
            requests[0].content["request"]["tool_name"], "Bash"
        )


class TestOpenCodeV2Turns(unittest.IsolatedAsyncioTestCase):
    async def test_v2_connect_health_check(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=2)
        agent._loop = asyncio.get_running_loop()
        agent._http = AsyncMock(return_value={"status": "ok"})
        agent._start_sse_thread = lambda env: agent._sse_ready.set()
        agent._fetch_models = AsyncMock()

        await agent._finish_connect({}, None)

        self.assertEqual(agent._http.await_args.args, ("GET", "/api/info"))
        await agent.disconnect()

    async def test_v2_first_prompt_creates_session_with_location(self):
        agent = OpenCodeAgent(AgentOptions(cwd="/test/workspace"), version=2)
        agent._is_connected = True
        agent._http = AsyncMock(side_effect=[
            {"id": "ses_v2_new"},
            None,
            None,
        ])

        await agent.send_message("hello v2")

        self.assertEqual(agent._session_id, "ses_v2_new")
        calls = [call.args for call in agent._http.await_args_list]
        self.assertEqual(
            calls,
            [
                ("POST", "/api/session"),
                ("POST", "/api/session/ses_v2_new/agent"),
                ("POST", "/api/session/ses_v2_new/prompt"),
            ],
        )
        self.assertEqual(
            agent._http.await_args_list[0].kwargs["body"]["location"]["directory"],
            "/test/workspace",
        )
        self.assertEqual(
            agent._http.await_args_list[1].kwargs["body"],
            {"agent": "build"},
        )
        self.assertEqual(
            agent._http.await_args_list[2].kwargs["body"],
            {"text": "hello v2"},
        )

    async def test_v2_session_create_with_disallowed_tools(self):
        agent = OpenCodeAgent(
            AgentOptions(
                cwd="/test/workspace",
                disallowed_tools=["AskUserQuestion", "bash"],
            ),
            version=2,
        )
        agent._is_connected = True
        agent._http = AsyncMock(side_effect=[
            {"id": "ses_v2_perms"},
            None,
            None,
        ])

        await agent.send_message("hello permissions")

        self.assertEqual(agent._session_id, "ses_v2_perms")
        post_session_body = agent._http.await_args_list[0].kwargs["body"]
        self.assertEqual(
            post_session_body["location"]["directory"],
            "/test/workspace",
        )
        self.assertEqual(
            post_session_body["permissions"],
            [
                {"action": "question", "resource": "*", "effect": "deny"},
                {"action": "shell", "resource": "*", "effect": "deny"},
            ],
        )

    async def test_v2_new_session_carries_permissions(self):
        agent = OpenCodeAgent(
            AgentOptions(
                cwd="/test/workspace",
                disallowed_tools=["AskUserQuestion"],
            ),
            version=2,
        )
        agent._is_connected = True
        agent._session_id = "ses_v2_old"
        agent._http = AsyncMock(return_value={"id": "ses_v2_fresh"})

        await agent.new_session()

        self.assertEqual(agent._session_id, "ses_v2_fresh")
        agent._http.assert_awaited_once_with(
            "POST",
            "/api/session",
            body={
                "location": {"directory": "/test/workspace"},
                "permissions": [
                    {"action": "question", "resource": "*", "effect": "deny"}
                ],
            },
        )

    async def test_v2_send_message_with_model_selection(self):
        agent = OpenCodeAgent(
            AgentOptions(cwd=".", model="anthropic/claude-3-7-sonnet"),
            version=2,
        )
        agent._is_connected = True
        agent._session_id = "ses_v2"
        agent._http = AsyncMock(return_value=None)

        await agent.send_message("analyze code")

        calls = [call.args for call in agent._http.await_args_list]
        self.assertEqual(
            calls,
            [
                ("POST", "/api/session/ses_v2/agent"),
                ("POST", "/api/session/ses_v2/model"),
                ("POST", "/api/session/ses_v2/prompt"),
            ],
        )
        self.assertEqual(
            agent._http.await_args_list[1].kwargs["body"],
            {"model": {"providerID": "anthropic", "id": "claude-3-7-sonnet"}},
        )

    async def test_v2_send_message_applies_reasoning_variant(self):
        agent = OpenCodeAgent(
            AgentOptions(
                cwd=".",
                model="opencode/gpt-6.2-astra",
                think_level="high",
            ),
            version=2,
        )
        agent._is_connected = True
        agent._session_id = "ses_v2"
        agent._http = AsyncMock(return_value=None)
        agent.available_models = [
            {
                "value": "opencode/gpt-6.2-astra",
                "supportedReasoningEfforts": ["auto", "low", "medium", "high"],
            }
        ]

        await agent.send_message("analyze code")

        self.assertEqual(
            agent._http.await_args_list[1].kwargs["body"],
            {
                "model": {
                    "providerID": "opencode",
                    "id": "gpt-6.2-astra",
                    "variant": "high",
                }
            },
        )

    async def test_v2_send_message_auto_level_omits_variant(self):
        for level in ("auto", "default", None):
            with self.subTest(level=level):
                agent = OpenCodeAgent(
                    AgentOptions(
                        cwd=".",
                        model="opencode/gpt-6.2-astra",
                        think_level=level,
                    ),
                    version=2,
                )
                agent._is_connected = True
                agent._session_id = "ses_v2"
                agent._http = AsyncMock(return_value=None)

                await agent.send_message("hi")

                self.assertEqual(
                    agent._http.await_args_list[1].kwargs["body"],
                    {
                        "model": {
                            "providerID": "opencode",
                            "id": "gpt-6.2-astra",
                        }
                    },
                )

    async def test_v2_send_message_skips_unsupported_variant(self):
        agent = OpenCodeAgent(
            AgentOptions(
                cwd=".",
                model="opencode/gpt-6.2-astra",
                think_level="max",
            ),
            version=2,
        )
        agent._is_connected = True
        agent._session_id = "ses_v2"
        agent._http = AsyncMock(return_value=None)
        agent.available_models = [
            {
                "value": "opencode/gpt-6.2-astra",
                "supportedReasoningEfforts": ["auto", "low", "high"],
            }
        ]

        await agent.send_message("hi")

        self.assertEqual(
            agent._http.await_args_list[1].kwargs["body"],
            {
                "model": {
                    "providerID": "opencode",
                    "id": "gpt-6.2-astra",
                }
            },
        )

    async def test_v2_interrupt_route(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=2)
        agent._is_connected = True
        agent._session_id = "ses_v2"
        agent._turn_active = True
        agent._http = AsyncMock(return_value=None)

        await agent.interrupt()

        agent._http.assert_awaited_once_with(
            "POST", "/api/session/ses_v2/interrupt", body={}, timeout=5.0
        )
        await agent._finish_turn()
        self.assertFalse(agent._turn_active)

    async def test_v2_rewind_routes(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=2)
        agent._is_connected = True
        agent._session_id = "ses_v2"
        agent._http = AsyncMock(return_value=None)

        await agent.rewind("msg_123")

        calls = [call.args for call in agent._http.await_args_list]
        self.assertEqual(
            calls,
            [
                ("POST", "/api/session/ses_v2/revert/stage"),
                ("POST", "/api/session/ses_v2/revert/commit"),
            ],
        )
        self.assertEqual(
            agent._http.await_args_list[0].kwargs["body"],
            {"messageID": "msg_123"},
        )


class TestOpenCodeV2ModelsAndPermissions(unittest.IsolatedAsyncioTestCase):
    async def test_v2_fetch_models_parses_model_list(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=2)
        raw_models = [
            {
                "id": "gpt-6.1-sol-fast",
                "modelID": "gpt-6.1-sol",
                "providerID": "openai",
                "name": "GPT-6.1 Sol Fast",
            },
            {
                "id": "claude-3-5-sonnet",
                "modelID": "claude-3-5-sonnet",
                "providerID": "anthropic",
                "name": "Claude 3.5 Sonnet",
            },
        ]
        agent._http = AsyncMock(return_value=raw_models)

        await agent._fetch_models()

        agent._http.assert_awaited_once_with("GET", "/api/model")
        self.assertEqual(len(agent.available_models), 2)
        self.assertEqual(
            agent.available_models[0]["value"], "openai/gpt-6.1-sol-fast"
        )
        self.assertEqual(
            agent.available_models[1]["value"], "anthropic/claude-3-5-sonnet"
        )

    async def test_v2_fetch_models_exposes_reasoning_variants(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=2)
        raw_models = [
            {
                "id": "gpt-6.2-astra",
                "modelID": "gpt-6.2-astra",
                "providerID": "opencode",
                "name": "GPT-6.2 Astra",
                "variants": [
                    {"id": "low", "settings": {"reasoningEffort": "low"}},
                    {"id": "high", "settings": {"reasoningEffort": "high"}},
                    {"id": "high", "settings": {"reasoningEffort": "high"}},
                ],
            },
            {
                "id": "hy4-flash-free",
                "modelID": "hy4-flash-free",
                "providerID": "opencode",
                "name": "Hy4 Flash Free",
                "variants": [],
            },
            {
                "id": "qwen4-max",
                "modelID": "qwen4-max",
                "providerID": "opencode",
                "name": "Qwen4 Max",
                "variants": [{"id": "auto"}, {"id": "low"}],
            },
        ]
        agent._http = AsyncMock(return_value=raw_models)

        await agent._fetch_models()

        self.assertEqual(
            agent.available_models[0]["supportedReasoningEfforts"],
            ["auto", "low", "high"],
        )
        self.assertNotIn(
            "supportedReasoningEfforts", agent.available_models[1]
        )
        # A real "auto" variant must not be duplicated by the default entry.
        self.assertEqual(
            agent.available_models[2]["supportedReasoningEfforts"],
            ["auto", "low"],
        )

    async def test_v2_permission_reply_route(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=2)
        agent._session_id = "ses_v2"
        agent._permission_sessions["perm_v2_1"] = "ses_v2"
        agent._http = AsyncMock(return_value=None)

        await agent.send_approval_response("perm_v2_1", {"behavior": "allow"})

        agent._http.assert_awaited_once_with(
            "POST",
            "/api/session/ses_v2/permission/perm_v2_1/reply",
            body={"decision": "once"},
        )


class TestOpenCodeSessionsAndResumption(unittest.TestCase):
    def test_unwrap_v2_envelope(self):
        envelope = {"data": [{"id": "s1"}], "cursor": "c1"}
        self.assertEqual(OpenCodeAgent._unwrap(envelope), [{"id": "s1"}])

        single = {"data": {"id": "s1"}}
        self.assertEqual(OpenCodeAgent._unwrap(single), {"id": "s1"})

        raw_list = [{"id": "s1"}]
        self.assertEqual(OpenCodeAgent._unwrap(raw_list), [{"id": "s1"}])

        with self.assertRaises(RuntimeError):
            OpenCodeAgent._unwrap({"error": {"message": "session failed"}})

    def test_list_opencode_sessions_v2(self):
        mock_server = SimpleNamespace(url="http://127.0.0.1:49374", password="pwd")
        v2_sessions = [
            {"id": "ses_v2_1", "title": "Session 1", "time": {"updated": 1700000000000}},
            {"id": "ses_v2_2", "title": "Session 2", "time": {"updated": 1700000010000}},
        ]

        with patch("genfoundry.opencode_agent._SyncOpenCodeServer") as mock_cls:
            mock_cls.return_value.__enter__.return_value = mock_server
            with patch(
                "genfoundry.opencode_agent._sync_request", return_value=v2_sessions
            ):
                sessions = list_opencode_sessions(cwd="/workspace")

        self.assertEqual(len(sessions), 2)
        # Newest first
        self.assertEqual(sessions[0]["session_id"], "ses_v2_2")
        self.assertEqual(sessions[0]["mtime"], 1700000010.0)
        self.assertEqual(sessions[1]["session_id"], "ses_v2_1")
        self.assertEqual(sessions[1]["mtime"], 1700000000.0)

    def test_list_opencode_sessions_v1(self):
        mock_server = SimpleNamespace(url="http://127.0.0.1:50000", password="", version=1)
        v1_sessions = [
            {"id": "ses_v1", "title": "V1 Session", "time": {"updated": 1600000000000}},
        ]

        with patch("genfoundry.opencode_agent._SyncOpenCodeServer") as mock_cls:
            mock_cls.return_value.__enter__.return_value = mock_server
            with patch(
                "genfoundry.opencode_agent._sync_request", return_value=v1_sessions
            ) as mock_req:
                sessions = list_opencode_sessions(cwd="/workspace")
                mock_req.assert_called_once_with(
                    "http://127.0.0.1:50000", "/session", "/workspace", None, ""
                )

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["session_id"], "ses_v1")
        self.assertEqual(sessions[0]["summary"], "V1 Session")
        self.assertEqual(sessions[0]["mtime"], 1600000000.0)

    def test_list_opencode_sessions_falls_back_to_all_directories(self):
        mock_server = SimpleNamespace(url="http://127.0.0.1:49374", password="pwd")
        other = [{
            "id": "ses_other",
            "title": "Other workspace",
            "time": {"updated": 1700000020000},
            "directory": "/other/workspace",
        }]

        def fake_request(base_url, path, cwd, extra_env, password=None):
            return [] if cwd else other

        with patch("genfoundry.opencode_agent._SyncOpenCodeServer") as mock_cls, \
             patch("genfoundry.opencode_agent._sync_request",
                   side_effect=fake_request) as request:
            mock_cls.return_value.__enter__.return_value = mock_server
            sessions = list_opencode_sessions(
                "/workspace", include_all_on_empty=True
            )

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["session_id"], "ses_other")
        self.assertEqual(sessions[0]["directory"], "/other/workspace")
        self.assertEqual(request.call_args_list[0].args[2], "/workspace")
        self.assertIsNone(request.call_args_list[1].args[2])

    def test_list_opencode_sessions_reports_lookup_errors(self):
        with patch("genfoundry.opencode_agent._SyncOpenCodeServer",
                   side_effect=RuntimeError("OpenCode service is unavailable")):
            sessions = list_opencode_sessions("/workspace")

        self.assertEqual(sessions, [])
        self.assertEqual(sessions.error, "OpenCode service is unavailable")

    def test_get_opencode_session_info_v2_descending_messages(self):
        mock_server = SimpleNamespace(url="http://127.0.0.1:49374", password="pwd")
        v2_session = {
            "id": "ses_v2",
            "title": "V2 Title",
            "time": {"updated": 1700000050000},
        }
        # V2 returns messages descending (newest first)
        v2_messages = [
            {"type": "idle", "time": {"created": 1700000050000}},
            {
                "type": "assistant",
                "content": [{"type": "text", "text": "Turn 2 response"}],
                "time": {"created": 1700000040000},
            },
            {
                "type": "user",
                "text": "Turn 2 prompt",
                "time": {"created": 1700000030000},
            },
            {
                "type": "assistant",
                "content": [{"type": "text", "text": "Turn 1 response"}],
                "time": {"created": 1700000020000},
            },
            {
                "type": "user",
                "text": "Turn 1 prompt",
                "time": {"created": 1700000010000},
            },
        ]

        def fake_request(base_url, path, cwd, extra_env, password=None):
            if path == "/api/session/ses_v2":
                return v2_session
            if path == "/api/session/ses_v2/message":
                return v2_messages
            return None

        with patch("genfoundry.opencode_agent._SyncOpenCodeServer") as mock_cls:
            mock_cls.return_value.__enter__.return_value = mock_server
            with patch(
                "genfoundry.opencode_agent._sync_request", side_effect=fake_request
            ):
                info = get_opencode_session_info("ses_v2", cwd="/workspace")

        self.assertIsNotNone(info)
        self.assertEqual(info["summary"], "V2 Title")
        self.assertEqual(info["mtime"], 1700000050.0)
        self.assertEqual(len(info["turns"]), 2)
        self.assertEqual(info["turns"][0]["prompt"], "Turn 1 prompt")
        self.assertEqual(info["turns"][0]["response"], "Turn 1 response")
        self.assertEqual(info["turns"][1]["prompt"], "Turn 2 prompt")
        self.assertEqual(info["turns"][1]["response"], "Turn 2 response")
        self.assertEqual(info["prompt"], "Turn 2 prompt")
        self.assertEqual(info["response"], "Turn 2 response")

    def test_get_opencode_session_info_v1_parts(self):
        mock_server = SimpleNamespace(url="http://127.0.0.1:50000", password="")
        v1_session = {
            "id": "ses_v1",
            "title": "V1 Title",
            "time": {"updated": 1600000020000},
        }
        v1_messages = [
            {
                "info": {"role": "user"},
                "parts": [{"type": "text", "text": "V1 prompt"}],
            },
            {
                "info": {"role": "assistant"},
                "parts": [{"type": "text", "text": "V1 response"}],
            },
        ]

        def fake_request(base_url, path, cwd, extra_env, password=None):
            if path == "/session/ses_v1":
                return v1_session
            if path == "/session/ses_v1/message":
                return v1_messages
            return None

        with patch("genfoundry.opencode_agent._SyncOpenCodeServer") as mock_cls:
            mock_cls.return_value.__enter__.return_value = mock_server
            with patch(
                "genfoundry.opencode_agent._sync_request", side_effect=fake_request
            ):
                info = get_opencode_session_info("ses_v1", cwd="/workspace")

        self.assertIsNotNone(info)
        self.assertEqual(info["summary"], "V1 Title")
        self.assertEqual(len(info["turns"]), 1)
        self.assertEqual(info["turns"][0]["prompt"], "V1 prompt")
        self.assertEqual(info["turns"][0]["response"], "V1 response")

    def test_get_opencode_session_info_includes_tool_events(self):
        mock_server = SimpleNamespace(url="http://127.0.0.1:50000", password="")
        session = {"id": "ses_tools", "title": "Tools", "time": {"updated": 1600000020000}}
        messages = [
            {
                "info": {"role": "user"},
                "parts": [{"type": "text", "text": "List files"}],
            },
            {
                "info": {"role": "assistant"},
                "parts": [
                    {"type": "reasoning", "text": "thinking..."},
                    {"type": "text", "text": "I'll list them."},
                    {
                        "type": "tool",
                        "tool": "bash",
                        "state": {
                            "status": "completed",
                            "input": {"command": "ls"},
                            "output": "a\nb\n",
                            "title": "ls",
                        },
                    },
                    {
                        "type": "tool",
                        "tool": "read",
                        "state": {"status": "running", "input": {"path": "a.txt"}},
                    },
                    {
                        "type": "tool",
                        "tool": "edit",
                        "state": {
                            "status": "completed",
                            "input": {"path": "a.txt"},
                            "metadata": {
                                "files": [{"patch": "@@ -1 +1 @@\n-old\n+new"}]
                            },
                        },
                    },
                    {"type": "text", "text": "hidden", "ignored": True},
                ],
            },
        ]

        def fake_request(base_url, path, cwd, extra_env, password=None):
            if path == "/session/ses_tools":
                return session
            if path == "/session/ses_tools/message":
                return messages
            return None

        with patch("genfoundry.opencode_agent._SyncOpenCodeServer") as mock_cls:
            mock_cls.return_value.__enter__.return_value = mock_server
            with patch(
                "genfoundry.opencode_agent._sync_request", side_effect=fake_request
            ):
                info = get_opencode_session_info("ses_tools", cwd="/workspace")

        self.assertIsNotNone(info)
        self.assertEqual(info["turns"][0]["response"], "I'll list them.")
        events = info["turns"][0]["events"]
        self.assertEqual([e["kind"] for e in events], ["user", "assistant", "tool", "tool"])
        self.assertEqual(events[0]["text"], "List files")
        self.assertEqual(events[1]["text"], "I'll list them.")

        bash_block = events[2]["block"]
        self.assertEqual(bash_block["name"], "command_execution")
        self.assertEqual(bash_block["command"], "ls")
        self.assertEqual(bash_block["output"], "a\nb\n")
        self.assertEqual(bash_block["status"], "completed")

        edit_block = events[3]["block"]
        self.assertEqual(edit_block["name"], "edit")
        self.assertEqual(edit_block["metadata"]["diff"], "@@ -1 +1 @@\n-old\n+new")
        self.assertNotIn("error", [e["kind"] for e in events])

    def test_session_info_tool_events_respect_history_limit(self):
        mock_server = SimpleNamespace(url="http://127.0.0.1:50000", password="")
        session = {"id": "ses_limit", "title": "Limit", "time": {"updated": 1600000030000}}
        messages = []
        for index in range(3):
            messages.append({
                "info": {"role": "user"},
                "parts": [{"type": "text", "text": f"prompt {index}"}],
            })
            messages.append({
                "info": {"role": "assistant"},
                "parts": [
                    {"type": "text", "text": f"response {index}"},
                    {
                        "type": "tool",
                        "tool": "bash",
                        "state": {
                            "status": "completed",
                            "input": {"command": f"echo {index}"},
                            "output": f"{index}\n",
                        },
                    },
                ],
            })

        def fake_request(base_url, path, cwd, extra_env, password=None):
            if path == "/session/ses_limit":
                return session
            if path == "/session/ses_limit/message":
                return messages
            return None

        with patch("genfoundry.opencode_agent._SyncOpenCodeServer") as mock_cls:
            mock_cls.return_value.__enter__.return_value = mock_server
            with patch(
                "genfoundry.opencode_agent._sync_request", side_effect=fake_request
            ):
                info = get_opencode_session_info(
                    "ses_limit", cwd="/workspace", history_limit=1
                )

        self.assertIsNotNone(info)
        self.assertEqual(len(info["turns"]), 1)
        turn = info["turns"][0]
        self.assertEqual(turn["prompt"], "prompt 2")
        self.assertEqual([e["kind"] for e in turn["events"]], ["user", "assistant", "tool"])
        self.assertEqual(turn["events"][2]["block"]["output"], "2\n")

    def test_tool_content_from_state_normalises_errors(self):
        from genfoundry.opencode_agent import _tool_content_from_state

        block = _tool_content_from_state("read", {
            "status": "error",
            "input": {"path": "missing.txt"},
            "error": {"data": {"message": "file not found"}},
        })
        self.assertEqual(block["name"], "read")
        self.assertEqual(block["error"], "file not found")
        self.assertEqual(block["status"], "error")

        bash = _tool_content_from_state("bash", {
            "status": "error",
            "input": {"command": "false"},
            "error": "boom",
        })
        self.assertEqual(bash["name"], "command_execution")
        self.assertEqual(bash["output"], "boom")

    def test_tool_content_from_state_reads_v2_content(self):
        from genfoundry.opencode_agent import _tool_content_from_state

        block = _tool_content_from_state("bash", {
            "status": "completed",
            "input": {"command": "ls"},
            "content": [
                {"type": "text", "text": "a.txt\nb.txt\n"},
                {"type": "file", "uri": "file:///tmp/a.txt"},
            ],
        })
        self.assertEqual(block["name"], "command_execution")
        self.assertEqual(block["output"], "a.txt\nb.txt\n")

        # v1 ``output`` still wins when both fields are present.
        both = _tool_content_from_state("read", {
            "status": "completed",
            "output": "legacy",
            "content": [{"type": "text", "text": "v2"}],
        })
        self.assertEqual(both["output"], "legacy")

    def test_tool_content_from_state_suppresses_subagent_output(self):
        from genfoundry.opencode_agent import _tool_content_from_state

        block = _tool_content_from_state("subagent", {
            "status": "completed",
            "content": [{
                "type": "text",
                "text": '<subagent sessionID="ses_child" state="completed">'
                        'full child answer</subagent>',
            }],
        })
        self.assertIsNone(block["output"])
        self.assertEqual(block["status"], "completed")

    def test_get_opencode_session_info_includes_v2_tool_result(self):
        mock_server = SimpleNamespace(url="http://127.0.0.1:50000", password="")
        session = {"id": "ses_v2out", "title": "V2 output",
                   "time": {"updated": 1600000040000}}
        messages = [
            {
                "info": {"role": "user"},
                "content": [{"type": "text", "text": "Run tests"}],
            },
            {
                "info": {"role": "assistant"},
                "content": [
                    {"type": "text", "text": "Running."},
                    {
                        "type": "tool",
                        "tool": "bash",
                        "id": "tool_v2_1",
                        "state": {
                            "status": "completed",
                            "input": {"command": "pytest -q"},
                            "content": [
                                {"type": "text", "text": "3 passed\n"},
                            ],
                            "metadata": {"exit": 0},
                        },
                    },
                ],
            },
        ]

        def fake_request(base_url, path, cwd, extra_env, password=None):
            # _sync_request normally unwraps the v2 {"data": ...} envelope.
            if path == "/api/session/ses_v2out":
                return session
            if path == "/api/session/ses_v2out/message":
                return messages
            return None

        with patch("genfoundry.opencode_agent._SyncOpenCodeServer") as mock_cls:
            mock_cls.return_value.__enter__.return_value = mock_server
            with patch(
                "genfoundry.opencode_agent._sync_request", side_effect=fake_request
            ):
                info = get_opencode_session_info("ses_v2out", cwd="/workspace")

        self.assertIsNotNone(info)
        events = info["turns"][0]["events"]
        self.assertEqual([e["kind"] for e in events], ["user", "assistant", "tool"])
        tool_block = events[2]["block"]
        self.assertEqual(tool_block["name"], "command_execution")
        self.assertEqual(tool_block["output"], "3 passed\n")

    def test_probe_opencode_service(self):
        class _MockContext:
            def __enter__(self):
                return SimpleNamespace(status=200)

            def __exit__(self, *args):
                pass

        with patch("genfoundry.opencode_agent.urlopen", return_value=_MockContext()):
            self.assertTrue(probe_opencode_service("http://127.0.0.1:49374", "pwd"))

        with patch(
            "genfoundry.opencode_agent.urlopen", side_effect=Exception("unreachable")
        ):
            self.assertFalse(probe_opencode_service("http://127.0.0.1:9999", "pwd"))


class TestSlashCommandDescriptor(unittest.TestCase):
    def test_base_agents_are_opting_out(self):
        self.assertFalse(BaseAgent.supports_slash_commands)

    def test_descriptor_defaults(self):
        command = SlashCommand("compact", "Compact the session")
        self.assertEqual(command.name, "compact")
        self.assertEqual(command.description, "Compact the session")
        self.assertEqual(command.aliases, ())

        aliased = SlashCommand("variants", aliases=["thinking", "effort"])
        self.assertEqual(aliased.aliases, ("thinking", "effort"))


class TestOpenCodeSlashCommands(unittest.IsolatedAsyncioTestCase):
    async def test_advertises_commands(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=2)

        self.assertTrue(agent.supports_slash_commands)
        commands = await agent.list_slash_commands()
        self.assertEqual(
            {command.name for command in commands},
            {"compact", "rename", "fork", "new", "clear"},
        )

    async def test_chat_session_commands_are_not_forwarded_to_the_model(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=2)

        # /new opens a new tab and /clear resets the current tab; both are
        # handled by ChatSession so the agent never consumes the raw text.
        self.assertFalse(await agent.run_slash_command("new", ""))
        self.assertFalse(await agent.run_slash_command("clear", ""))

    async def test_unknown_command_is_not_handled(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=2)

        self.assertFalse(await agent.run_slash_command("nope", ""))

    async def test_compact_without_session_reports_nothing(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=2)

        with self.assertRaisesRegex(RuntimeError, "Nothing to compact yet"):
            await agent.run_slash_command("compact", "")

    async def test_compact_rejects_a_concurrent_request(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=2)
        agent._session_id = "ses_1"
        agent._compaction_waiting = True

        with self.assertRaisesRegex(RuntimeError, "already running"):
            await agent.run_slash_command("compact", "")

    async def test_compact_v2_posts_to_compact_endpoint(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=2)
        agent._session_id = "ses_1"
        calls = []

        async def fake_http(method, path, body=None, **kwargs):
            calls.append((method, path, body))
            # The server may emit the whole lifecycle before the HTTP response
            # returns; setting the event here models that ordering.
            agent._compaction_event.set()
            return {"id": "cmp_1"}

        agent._http = fake_http

        handled = await agent.run_slash_command("compact", "")

        self.assertTrue(handled)
        self.assertEqual(
            calls,
            [("POST", "/api/session/ses_1/compact", {})],
        )

    async def test_compact_v2_waits_for_lifecycle_events(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=2)
        agent._session_id = "ses_1"
        posted = asyncio.Event()

        async def fake_http(method, path, body=None, **kwargs):
            posted.set()
            return {"id": "cmp_1"}

        agent._http = fake_http

        task = asyncio.create_task(agent.run_slash_command("compact", ""))
        await asyncio.wait_for(posted.wait(), timeout=1.0)
        self.assertFalse(task.done())

        await agent._dispatch_event({
            "type": "session.compaction.ended",
            "data": {
                "sessionID": "ses_1",
                "reason": "manual",
                "text": "summary",
            },
        })

        self.assertTrue(await asyncio.wait_for(task, timeout=1.0))

    async def test_compact_v1_uses_summarize_with_current_model(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=1)
        agent._session_id = "ses_1"
        agent.options.model = "anthropic/claude-sonnet-4"
        calls = []

        async def fake_http(method, path, body=None, **kwargs):
            calls.append((method, path, body))
            return True

        agent._http = fake_http

        handled = await agent.run_slash_command("compact", "")

        self.assertTrue(handled)
        self.assertEqual(
            calls,
            [(
                "POST",
                "/session/ses_1/summarize",
                {"providerID": "anthropic", "modelID": "claude-sonnet-4"},
            )],
        )
        message = agent._message_queue.get_nowait()
        self.assertEqual(message.type, "compaction_ended")
        self.assertTrue(message.content["manual"])

    async def test_compact_v1_false_reports_nothing_to_compact(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=1)
        agent._session_id = "ses_1"

        async def fake_http(method, path, body=None, **kwargs):
            return False

        agent._http = fake_http

        handled = await agent.run_slash_command("compact", "")

        self.assertTrue(handled)
        message = agent._message_queue.get_nowait()
        self.assertEqual(message.type, "compaction_failed")
        self.assertEqual(message.content["error"], "Nothing to compact yet")

    async def test_manual_compaction_events_resolve_the_wait(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=2)
        agent._session_id = "ses_1"
        agent._compaction_waiting = True

        await agent._dispatch_event({
            "type": "session.compaction.started",
            "data": {"sessionID": "ses_1", "reason": "manual", "recent": ""},
        })
        started = agent._message_queue.get_nowait()
        self.assertEqual(started.type, "compaction_started")
        self.assertTrue(started.content["manual"])

        await agent._dispatch_event({
            "type": "session.compaction.ended",
            "data": {
                "sessionID": "ses_1",
                "reason": "manual",
                "text": "a summary",
                "tokens": {
                    "input": 1200,
                    "output": 30,
                    "reasoning": 0,
                    "cache": {"read": 0, "write": 0},
                },
            },
        })

        self.assertTrue(agent._compaction_event.is_set())
        self.assertEqual(agent._compaction_summary, "a summary")
        self.assertEqual(agent._compaction_tokens["input"], 1200)
        ended = agent._message_queue.get_nowait()
        self.assertEqual(ended.type, "compaction_ended")
        self.assertTrue(ended.content["manual"])
        self.assertEqual(ended.content["summary"], "a summary")

    async def test_manual_compaction_failure_resolves_the_wait(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=2)
        agent._session_id = "ses_1"
        agent._compaction_waiting = True

        await agent._dispatch_event({
            "type": "session.compaction.failed",
            "data": {
                "sessionID": "ses_1",
                "reason": "manual",
                "error": {
                    "type": "compaction.unavailable",
                    "message": "Nothing to compact yet",
                },
            },
        })

        self.assertTrue(agent._compaction_event.is_set())
        self.assertEqual(agent._compaction_error, "Nothing to compact yet")
        failed = agent._message_queue.get_nowait()
        self.assertEqual(failed.type, "compaction_failed")
        self.assertEqual(failed.content["error"], "Nothing to compact yet")

    async def test_auto_compaction_does_not_resolve_a_manual_wait(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=2)
        agent._session_id = "ses_1"

        await agent._dispatch_event({
            "type": "session.compaction.started",
            "data": {"sessionID": "ses_1", "reason": "auto", "recent": ""},
        })
        self.assertTrue(agent._message_queue.empty())

        await agent._dispatch_event({
            "type": "session.compaction.ended",
            "data": {"sessionID": "ses_1", "reason": "auto", "text": "s"},
        })
        ended = agent._message_queue.get_nowait()
        self.assertFalse(ended.content["manual"])
        self.assertFalse(agent._compaction_event.is_set())


class TestOpenCodeFork(unittest.IsolatedAsyncioTestCase):
    async def test_fork_v2_copies_full_history_without_arguments(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=2)
        agent._session_id = "ses_1"
        calls = []

        async def fake_http(method, path, body=None, **kwargs):
            calls.append((method, path, body))
            return {"data": {
                "id": "ses_fork",
                "title": "main title (fork #1)",
                "fork": {"sessionID": "ses_1"},
            }}

        agent._http = fake_http

        result = await agent.fork_session()

        self.assertEqual(calls, [("POST", "/api/session/ses_1/fork", {})])
        self.assertEqual(result, {
            "session_id": "ses_fork",
            "title": "main title (fork #1)",
            "fork": {"sessionID": "ses_1"},
        })

    async def test_fork_slash_command_ignores_arguments(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=2)
        agent._session_id = "ses_1"
        calls = []

        async def fake_http(method, path, body=None, **kwargs):
            calls.append(body)
            return {"id": "ses_fork", "title": "forked"}

        agent._http = fake_http

        self.assertTrue(await agent.run_slash_command("fork", "ignored text"))
        self.assertEqual(calls, [{}])

    async def test_fork_without_a_session_is_rejected(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=2)
        agent._http = AsyncMock()

        with self.assertRaisesRegex(RuntimeError, "Nothing to fork yet"):
            await agent.fork_session()
        agent._http.assert_not_awaited()

    async def test_fork_v1_is_rejected(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=1)
        agent._session_id = "ses_1"
        agent._http = AsyncMock()

        with self.assertRaisesRegex(RuntimeError, r"/fork requires OpenCode v2"):
            await agent.fork_session()
        agent._http.assert_not_awaited()

    async def test_fork_rejects_an_invalid_response(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=2)
        agent._session_id = "ses_1"
        agent._http = AsyncMock(return_value={"data": {"title": "no id"}})

        with self.assertRaisesRegex(RuntimeError, "valid fork"):
            await agent.fork_session()


class TestOpenCodeSessionTitles(unittest.IsolatedAsyncioTestCase):
    def _drain(self, agent):
        messages = []
        while not agent._message_queue.empty():
            messages.append(agent._message_queue.get_nowait())
        return messages

    async def test_rename_v2_patches_server_title_verbatim(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=2)
        agent._session_id = "ses_1"
        calls = []

        async def fake_http(method, path, body=None, **kwargs):
            calls.append((method, path, body))
            return None

        agent._http = fake_http

        handled = await agent.run_slash_command("rename", "▣ 修复登录")

        self.assertTrue(handled)
        self.assertEqual(
            calls,
            [("PATCH", "/api/session/ses_1", {"title": "▣ 修复登录"})],
        )
        self.assertEqual(agent._session_title, "▣ 修复登录")
        titles = [m for m in self._drain(agent) if m.type == "thread_title"]
        self.assertEqual(len(titles), 1)
        self.assertEqual(titles[0].content["title"], "▣ 修复登录")

    async def test_rename_rejects_an_empty_title(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=2)
        agent._session_id = "ses_1"
        agent._http = AsyncMock()

        with self.assertRaisesRegex(RuntimeError, "Usage: /rename <title>"):
            await agent.run_slash_command("rename", "   ")
        agent._http.assert_not_awaited()

    async def test_rename_before_first_prompt_becomes_initial_title(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=2)
        agent._http = AsyncMock()

        handled = await agent.rename_session("before-start")

        self.assertTrue(handled)
        self.assertEqual(agent.options.initial_title, "before-start")
        self.assertEqual(agent._session_create_body_v2()["title"], "before-start")
        agent._http.assert_not_awaited()

    async def test_rename_v1_is_best_effort(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=1)
        agent._session_id = "ses_1"
        calls = []

        async def fake_http(method, path, body=None, **kwargs):
            calls.append((method, path, body))
            raise RuntimeError("unsupported")

        agent._http = fake_http

        self.assertTrue(await agent.rename_session("local only"))

        self.assertEqual(
            calls,
            [("PATCH", "/session/ses_1", {"title": "local only"})],
        )
        self.assertEqual(agent._session_title, "local only")

    async def test_renamed_event_emits_thread_title(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=2)
        agent._session_id = "ses_1"

        await agent._dispatch_event({
            "type": "session.renamed",
            "data": {"sessionID": "ses_1", "title": "auto title"},
        })

        self.assertEqual(agent._session_title, "auto title")
        messages = self._drain(agent)
        self.assertEqual([m.type for m in messages], ["thread_title"])
        self.assertEqual(messages[0].content["title"], "auto title")

    async def test_renamed_event_from_another_session_is_ignored(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=2)
        agent._session_id = "ses_1"

        await agent._dispatch_event({
            "type": "session.renamed",
            "data": {"sessionID": "ses_other", "title": "other"},
        })

        self.assertTrue(agent._message_queue.empty())

    async def test_initial_title_is_sent_on_v2_create(self):
        agent = OpenCodeAgent(
            AgentOptions(cwd=".", initial_title="main title #2"), version=2
        )
        self.assertEqual(agent._session_create_body_v2()["title"], "main title #2")

    async def test_initial_title_is_sent_on_v1_create(self):
        agent = OpenCodeAgent(
            AgentOptions(cwd=".", initial_title="main title #2"), version=1
        )
        self.assertEqual(agent._session_create_body_v1(), {"title": "main title #2"})

    async def test_resumed_session_publishes_its_title(self):
        agent = OpenCodeAgent(AgentOptions(cwd=".", session_id="ses_existing"), version=2)
        agent._loop = asyncio.get_running_loop()
        agent._sse_ready.set()

        async def fake_http(method, path, **kwargs):
            if path == "/api/info":
                return {}
            if path == "/api/session/ses_existing":
                return {"id": "ses_existing", "title": "resumed title"}
            raise AssertionError((method, path))

        agent._http = fake_http
        agent._process_events = AsyncMock()
        agent._start_sse_thread = lambda env: None
        agent._fetch_models = AsyncMock()

        await agent._finish_connect({}, None)

        self.assertEqual(agent._session_title, "resumed title")
        messages = self._drain(agent)
        self.assertEqual(
            [m.type for m in messages], ["thread_started", "thread_title"]
        )
        self.assertEqual(messages[1].content["title"], "resumed title")

    async def test_create_without_echoed_title_keeps_initial_title(self):
        agent = OpenCodeAgent(
            AgentOptions(cwd=".", initial_title="local task title"), version=1
        )
        agent._is_connected = True
        agent._http = AsyncMock(side_effect=[{"id": "ses_new"}, None])

        await agent.send_message("hello")

        titles = [m for m in self._drain(agent) if m.type == "thread_title"]
        self.assertEqual(len(titles), 1)
        self.assertEqual(titles[0].content["title"], "local task title")


class TestOpenCodeTokenUsage(unittest.IsolatedAsyncioTestCase):
    """OpenCode token counters feed the chat view's TokenSummaryPhantom."""

    TOKENS = {
        "input": 3100,
        "output": 217,
        "reasoning": 35,
        "cache": {"read": 31488, "write": 12},
    }

    def _make_agent(self, version):
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=version)
        agent._session_id = "ses_tokens"
        agent._turn_active = True
        return agent

    async def _messages(self, agent):
        result = []
        while not agent._message_queue.empty():
            result.append(await agent._message_queue.get())
        return result

    def test_normalize_tokens_maps_opencode_shape(self):
        normalized = OpenCodeAgent._normalize_opencode_tokens(self.TOKENS)

        self.assertEqual(normalized, {
            "inputTokens": 34600,
            "outputTokens": 217,
            "cachedInputTokens": 31488,
            "totalTokens": 34852,
            "reasoningOutputTokens": 35,
            "activeContextTokens": 34852,
        })

    def test_normalize_tokens_handles_strings_missing_cache_and_invalid(self):
        normalized = OpenCodeAgent._normalize_opencode_tokens({
            "input": "100",
            "output": "20",
            "reasoning": None,
            "cache": "not-a-dict",
        })
        self.assertEqual(normalized["inputTokens"], 100)
        self.assertEqual(normalized["outputTokens"], 20)
        self.assertEqual(normalized["cachedInputTokens"], 0)
        self.assertEqual(normalized["totalTokens"], 120)

        self.assertIsNone(OpenCodeAgent._normalize_opencode_tokens(None))
        self.assertIsNone(OpenCodeAgent._normalize_opencode_tokens("nope"))

    def test_context_window_from_model_variants(self):
        self.assertEqual(
            OpenCodeAgent._context_window_from_model({"contextWindow": 258400}),
            258400,
        )
        self.assertEqual(
            OpenCodeAgent._context_window_from_model({"limit": {"context": 200000}}),
            200000,
        )
        self.assertEqual(OpenCodeAgent._context_window_from_model({}), 0)

    async def test_v2_step_and_usage_events_emit_token_usage_before_stop(self):
        agent = self._make_agent(2)
        agent._model_context_windows["anthropic/claude-sonnet"] = 200000

        await agent._dispatch_event({
            "type": "session.step.started",
            "data": {
                "sessionID": "ses_tokens",
                "model": {"providerID": "anthropic", "id": "claude-sonnet"},
            },
        })
        await agent._dispatch_event({
            "type": "session.step.ended",
            "data": {"sessionID": "ses_tokens", "tokens": self.TOKENS},
        })
        await agent._dispatch_event({
            "type": "session.usage.updated",
            "data": {
                "sessionID": "ses_tokens",
                "tokens": {
                    "input": 6200,
                    "output": 434,
                    "reasoning": 70,
                    "cache": {"read": 62976, "write": 24},
                },
            },
        })
        await agent._dispatch_event({
            "type": "session.execution.succeeded",
            "data": {"sessionID": "ses_tokens"},
        })

        messages = await self._messages(agent)
        self.assertEqual([m.type for m in messages], ["token_usage", "stop"])
        usage = messages[0].content
        self.assertEqual(usage["last"]["totalTokens"], 34852)
        self.assertEqual(usage["last"]["reasoningOutputTokens"], 35)
        self.assertEqual(usage["total"]["inputTokens"], 69200)
        self.assertEqual(usage["modelContextWindow"], 200000)

    async def test_v2_later_step_replaces_earlier_snapshot(self):
        agent = self._make_agent(2)

        await agent._dispatch_event({
            "type": "session.step.ended",
            "data": {"sessionID": "ses_tokens", "tokens": self.TOKENS},
        })
        await agent._dispatch_event({
            "type": "session.step.ended",
            "data": {
                "sessionID": "ses_tokens",
                "tokens": {
                    "input": 9000,
                    "output": 800,
                    "reasoning": 100,
                    "cache": {"read": 40000, "write": 0},
                },
            },
        })
        await agent._dispatch_event({
            "type": "session.execution.succeeded",
            "data": {"sessionID": "ses_tokens"},
        })

        messages = await self._messages(agent)
        self.assertEqual(messages[0].content["last"]["totalTokens"], 49900)

    async def test_v1_message_updated_emits_token_usage(self):
        agent = self._make_agent(1)

        await agent._dispatch_event({
            "type": "message.updated",
            "properties": {
                "info": {
                    "id": "msg_assistant",
                    "sessionID": "ses_tokens",
                    "role": "assistant",
                    "providerID": "openai",
                    "modelID": "gpt-test",
                    "tokens": self.TOKENS,
                },
            },
        })
        await agent._dispatch_event({
            "type": "session.status",
            "properties": {
                "sessionID": "ses_tokens",
                "status": {"type": "idle"},
            },
        })

        messages = await self._messages(agent)
        self.assertEqual([m.type for m in messages], ["token_usage", "stop"])
        self.assertEqual(messages[0].content["last"]["cachedInputTokens"], 31488)
        self.assertEqual(messages[0].content["total"], messages[0].content["last"])

    async def test_v1_step_finish_part_is_a_fallback_source(self):
        agent = self._make_agent(1)

        await agent._dispatch_event({
            "type": "message.part.updated",
            "properties": {
                "part": {
                    "id": "part_step",
                    "sessionID": "ses_tokens",
                    "messageID": "msg_assistant",
                    "type": "step-finish",
                    "tokens": self.TOKENS,
                },
            },
        })
        await agent._dispatch_event({
            "type": "session.idle",
            "properties": {"sessionID": "ses_tokens"},
        })

        messages = await self._messages(agent)
        self.assertEqual([m.type for m in messages], ["token_usage", "stop"])

    async def test_no_tokens_means_no_token_usage_message(self):
        agent = self._make_agent(2)

        await agent._dispatch_event({
            "type": "session.execution.succeeded",
            "data": {"sessionID": "ses_tokens"},
        })

        self.assertEqual(
            [m.type for m in await self._messages(agent)], ["stop"]
        )

    async def test_token_state_is_reset_between_turns(self):
        agent = self._make_agent(2)
        await agent._dispatch_event({
            "type": "session.step.ended",
            "data": {"sessionID": "ses_tokens", "tokens": self.TOKENS},
        })
        await agent._dispatch_event({
            "type": "session.execution.succeeded",
            "data": {"sessionID": "ses_tokens"},
        })
        await self._messages(agent)

        agent._turn_active = False
        agent._turn_done.set()
        with patch.object(agent, "_ensure_session", return_value="ses_tokens"), \
             patch.object(agent, "_http", return_value=None), \
             patch.object(agent, "connect", return_value=None):
            agent._is_connected = True
            await agent.send_message("second turn")

        self.assertIsNone(agent._turn_tokens)
        self.assertEqual("", agent._token_model)


if __name__ == "__main__":
    unittest.main()
