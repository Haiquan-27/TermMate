import difflib
import json
import logging
import os
import re
import urllib.parse
import xml.etree.ElementTree
from dataclasses import dataclass, field
from typing import Optional

import sublime

from . import utils

LOG = logging.getLogger("TermMate")

_WINDOWS_DRIVE_PATH_RE = re.compile(r'^[a-zA-Z]:[\\/]')
_TOOL_ARGUMENTS_MAX_CHARS = 300
_COMMAND_PREVIEW_MAX_CHARS = 120
_DIFF_TOOL_NAMES = frozenset({
    "apply_patch",
    "edit",
    "filechange",
    "multiedit",
    "patch",
    "write",
})
_SUBAGENT_TOOL_NAMES = frozenset({
    "Agent", "Task", "Subagent", "agent", "task", "subagent",
    "collabAgentToolCall", "spawnAgent",
})


@dataclass
class ToolContext:
    """Canonical semantic context for one tool call.

    Built once from a raw (possibly history-normalized) block, then consumed
    by the unified renderer.  The renderer never inspects the rendered text to
    guess whether the tool is a diff, a multi-file edit, or a plain row: every
    such decision is already recorded here as an explicit field.
    """

    kind: str = "unknown"
    text: str = ""
    heading: str = ""
    summary: str = ""
    detail: str = ""
    phase: Optional[str] = None
    output_summary: str = ""
    output_detail: str = ""
    error_detail: str = ""
    has_output: bool = False
    show_command: bool = False
    is_diff: bool = False
    diff_text: str = ""
    diffs: list = field(default_factory=list)
    has_inline_body: bool = False
    is_multi: bool = False
    has_phantom: bool = True


@dataclass
class ToolStructure:
    """Structured facts captured while a provider renders a tool block.

    ``_format_tool_block`` keeps returning a plain string (many callers rely
    on that), but accepts an optional ``structure`` to record the same facts
    it already knows while building the string.  This lets the context
    pipeline stay text-driven only for providers that have not been migrated
    yet, without changing any externally visible output.
    """

    kind: str = ""
    heading: str = ""
    summary: str = ""
    detail: str = ""
    is_diff: bool = False
    diff_text: str = ""
    diffs: list = field(default_factory=list)
    has_inline_body: bool = False
    is_multi: bool = False

    def set_header(self, heading, summary, detail=None):
        """Record the tool header explicitly (no text re-parsing needed)."""
        self.heading = heading
        self.summary = summary
        self.detail = detail if detail is not None else f"{heading} {summary}".strip()

    def fill_from_text(self, text, split_header):
        """Compatibility fallback: derive structure from rendered text.

        Only fills fields the provider did not set explicitly, so migrated
        providers stop depending on this heuristic entirely.
        """
        text = text or ""
        if not self.detail:
            self.detail = text.partition("\n")[0].strip()
        if not self.heading and not self.summary:
            self.heading, self.summary = split_header(self.detail)
        if not self.has_inline_body:
            self.has_inline_body = "\n" in text
        if not self.is_multi:
            self.is_multi = text.count("⏺") > 1
        return self


def _is_subagent_tool(block):
    """Return True for a tool call that launches a subagent task."""
    if not isinstance(block, dict):
        return False
    return bool(
        block.get("name") in _SUBAGENT_TOOL_NAMES
        or block.get("tool") in _SUBAGENT_TOOL_NAMES
    )


def _is_windows_abs_path(path):
    return bool(_WINDOWS_DRIVE_PATH_RE.match(path)) or path.startswith(
        ("\\\\", "//"))


def _format_tool_value(value):
    """Return a readable single-line representation of a tool argument value."""
    if isinstance(value, str):
        return value.replace("\r", "\\r").replace("\n", "\\n")
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        text = str(value)
    text = text.replace("\r", "\\r").replace("\n", "\\n")
    return text


def _format_tool_arguments(arguments):
    """Return bounded key/value text for a tool invocation."""
    if arguments is None:
        return ""
    if isinstance(arguments, dict):
        text = ", ".join(
            f"{key}: {_format_tool_value(value)}"
            for key, value in arguments.items()
        )
    else:
        text = _format_tool_value(arguments)
    if len(text) > _TOOL_ARGUMENTS_MAX_CHARS:
        text = text[:_TOOL_ARGUMENTS_MAX_CHARS - 1] + "…"
    return text


def format_model_badge(m: dict) -> str:
    """Format model capability badge using default base heuristics."""
    return BaseChatMessageProcessor(None).format_model_badge(m)


def format_model_display(m: dict) -> tuple:
    """Format raw model data for UI presentation using default base heuristics."""
    return BaseChatMessageProcessor(None).format_model_display(m)


def normalize_model(m: dict) -> dict:
    """Normalize model dictionary using default base processor."""
    return BaseChatMessageProcessor(None).normalize_model(m)


def _make_tool_file_re(tool_names):
    alt = "|".join(re.escape(n) for n in tool_names)
    return re.compile(rf'^⏺ (?:{alt}) (.+?)(?:#L(\d+)(?:-L(\d+))?)?(?:,.*)?$')


def _resolve_path(path_part, cwd):
    return (path_part
            if os.path.isabs(path_part) or _is_windows_abs_path(path_part)
            else os.path.normpath(os.path.join(cwd, path_part)))


def _resolve_rel_path(path, cwd):
    """Return (abs_path, rel_path) for a file path against cwd."""
    abs_path = _resolve_path(path, cwd)
    try:
        rel = os.path.relpath(abs_path, cwd)
    except Exception:
        rel = path
    return abs_path, rel


def _parse_tool_file_line(line_text, tool_file_re, cwd):
    m = tool_file_re.match(line_text.strip())
    if not m:
        return None
    line_start = int(m.group(2)) if m.group(2) else None
    return (_resolve_path(m.group(1).strip(), cwd), line_start)


def _find_line_in_file(file_path, old_text, cwd):
    """Return 1-based line number where old_text starts in file, or None."""
    try:
        abs_path = file_path if os.path.isabs(file_path) else os.path.join(cwd, file_path)
        with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        idx = text.find(old_text)
        if idx != -1:
            return text[:idx].count("\n") + 1
    except Exception:
        pass
    return None


def _make_edit_diff(old_str, new_str, rel_path, start_line=None):
    """Return unified diff text (without --- +++ header lines), or None.

    If start_line is given, hunk headers are shifted so line numbers reflect
    the actual position in the file rather than starting at 1.
    """
    if old_str and not old_str.endswith("\n"): old_str += "\n"
    if new_str and not new_str.endswith("\n"): new_str += "\n"
    lines = list(difflib.unified_diff(
        old_str.splitlines(keepends=True),
        new_str.splitlines(keepends=True),
        fromfile=f"a/{rel_path}",
        tofile=f"b/{rel_path}",
    ))
    if len(lines) <= 2:
        return None
    diff = "".join(lines[2:])
    if start_line and start_line > 1:
        offset = start_line - 1
        def _shift_hunk(m):
            old_s = int(m.group(1)) + offset
            old_c = m.group(2)
            new_s = int(m.group(3)) + offset
            new_c = m.group(4)
            old_part = f"{old_s}" if old_c is None else f"{old_s},{old_c}"
            new_part = f"{new_s}" if new_c is None else f"{new_s},{new_c}"
            return f"@@ -{old_part} +{new_part} @@"
        diff = re.sub(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@', _shift_hunk, diff)
    return diff


def _diff_from_structured_patch(patches):
    """Build unified diff hunk text from Claude structuredPatch data, or None."""
    parts = []
    for hunk in patches or []:
        try:
            header = "@@ -{},{} +{},{} @@".format(
                hunk["oldStart"], hunk["oldLines"], hunk["newStart"], hunk["newLines"])
        except (KeyError, TypeError):
            continue
        lines = hunk.get("lines") or []
        parts.append("\n".join([header] + list(lines)))
    return "\n".join(parts) + "\n" if parts else None


def _diff_start_line(diff_text):
    """Return the destination start line from the first @@ hunk, or None."""
    m = re.search(r'^@@ -\d+(?:,\d+)? \+(\d+)', diff_text, re.MULTILINE)
    return int(m.group(1)) if m else None


def _diff_hunks_only(diff_text):
    """Drop redundant file headers while preserving the complete diff hunks."""
    match = re.search(r'^@@ ', diff_text or "", re.MULTILINE)
    return diff_text[match.start():] if match else diff_text


_HUNK_LINE_RE = re.compile(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@')
_MARKDOWN_LINK_RE = re.compile(
    r'(?<!!)\[[^\]\n]*\]\(\s*(?:<([^>\n]+)>|([^\s)\n]+))'
)


def _parse_markdown_file_target(target, cwd):
    """Resolve a Markdown link target to (absolute path, line, column)."""
    target = urllib.parse.unquote(target.strip())
    if target.lower().startswith("file://"):
        parsed = urllib.parse.urlparse(target)
        path = parsed.path
        if parsed.netloc and parsed.netloc.lower() != "localhost":
            path = f"//{parsed.netloc}{path}"
        elif re.match(r'^/[a-zA-Z]:[\\/]', path):
            path = path[1:]
        target = path
        if parsed.fragment:
            target += f"#{parsed.fragment}"
    elif (not _is_windows_abs_path(target)
          and re.match(r'^[a-z][a-z0-9+.-]*:', target, re.IGNORECASE)):
        return None

    line = None
    column = None
    fragment = re.search(r'#L(\d+)(?:C(\d+)|-L\d+)?$', target)
    if fragment:
        line = int(fragment.group(1))
        column = int(fragment.group(2)) if fragment.group(2) else None
        target = target[:fragment.start()]
    else:
        encoded_position = re.match(r'^(.*?):(\d+)(?::(\d+))?$', target)
        if encoded_position:
            target = encoded_position.group(1)
            line = int(encoded_position.group(2))
            column = (int(encoded_position.group(3))
                      if encoded_position.group(3) else None)

    path = _resolve_path(target, cwd)
    if not os.path.isfile(path):
        return None
    return path, line, column


_REASONING_EFFORT_DESCRIPTIONS = {
    "auto": "Use model default reasoning effort",
    "default": "Use model default reasoning effort",
    "none": "Disable reasoning",
    "minimal": "Lowest reasoning effort",
    "low": "Fast responses with light reasoning",
    "medium": "Balanced speed and reasoning depth (standard)",
    "high": "Deep reasoning for complex coding tasks",
    "xhigh": "Maximum reasoning effort",
    "adaptive": "Dynamic thinking managed by model (default)",
}


_TOOL_RUNNING_STATES = frozenset(
    ("running", "in_progress", "inprogress", "pending", "active", "started")
)


def _tool_call_phase(block):
    """Return ``"running"`` / ``"background"`` for unfinished calls, else ``None``.

    OpenCode reports a tool that was moved to the background as ``completed``
    while its shell keeps running: the tool state says ``completed`` but its
    metadata still says ``status: running`` and carries a ``shellID``.
    """
    status = str(block.get("status") or "").strip().lower()
    if block.get("background") and status in _TOOL_RUNNING_STATES:
        # The adapter knows the shell was started in the background, so the
        # row can say so before any completion event arrives.
        return "background"
    metadata = block.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    meta_status = str(metadata.get("status") or "").strip().lower()
    shell_id = metadata.get("shellID") or metadata.get("shell_id")
    if status in _TOOL_RUNNING_STATES:
        return "running"
    if status in ("completed", "success", "succeeded", ""):
        if meta_status in _TOOL_RUNNING_STATES and (shell_id or meta_status == "running"):
            return "background"
    return None


def _tool_result_status(block):
    """Return ``"success"`` or ``"failed"`` for a finished tool result."""
    status = str(block.get("status") or "").strip().lower()
    if block.get("error") or status in ("error", "failed", "failure"):
        return "failed"
    exit_code = block.get("exit_code", block.get("exitCode"))
    if exit_code is None:
        metadata = block.get("metadata")
        if isinstance(metadata, dict):
            exit_code = metadata.get("exit_code", metadata.get("exitCode"))
    if exit_code is not None:
        try:
            return "success" if int(exit_code) == 0 else "failed"
        except (TypeError, ValueError):
            pass
    return "success"


class BaseChatMessageProcessor:
    """
    Handles buffering, formatting, and displaying messages from the agent.
    """
    _TOOL_FILE_NAMES = ()
    _RISKY_TOOLS = ("Bash", "command_execution", "bash")

    def is_risky_tool(self, tool_name: str) -> bool:
        """Return True if tool requires user confirmation under allow-edit mode."""
        return tool_name in self._RISKY_TOOLS

    @classmethod
    def for_provider(cls, agent_provider: str):
        """Return the processor class for the given agent provider."""
        if not agent_provider:
            return cls
        return PROCESSOR_MAP.get(agent_provider.lower(), cls)

    @classmethod
    def get_default_think_presets(cls) -> list:
        """Return fallback reasoning/thinking presets when no model data is loaded."""
        return []

    @classmethod
    def get_think_presets(cls, active_model: dict = None) -> list:
        """
        Return a unified list of thinking/reasoning effort presets for UI presentation:
        [{'value': str, 'text': str, 'description': str}, ...]

        When active_model is missing/empty, return the provider's default presets,
        empty for unsupported providers.
        """
        if active_model:
            model_efforts = (
                active_model.get("supportedReasoningEfforts")
                or active_model.get("supportedEffortLevels")
            )
            if model_efforts:
                return cls.normalize_think_efforts(model_efforts)
            if active_model.get("supportsAdaptiveThinking"):
                return cls.get_default_think_presets()
            return []
        return cls.get_default_think_presets()

    @classmethod
    def normalize_think_efforts(cls, efforts: list) -> list:
        """Normalize raw effort list/presets into uniform [{'value', 'text', 'description'}, ...]."""
        items = []
        for effort in efforts:
            if isinstance(effort, dict):
                val = effort.get("reasoningEffort") or effort.get("value") or effort.get("effort") or ""
                desc = effort.get("description") or _REASONING_EFFORT_DESCRIPTIONS.get(val, "Reasoning effort level")
                text = effort.get("text") or val
            elif isinstance(effort, (tuple, list)) and len(effort) >= 2:
                val, desc = effort[0], effort[1]
                text = val
            elif isinstance(effort, str):
                val = effort
                text = effort
                desc = _REASONING_EFFORT_DESCRIPTIONS.get(val, "Reasoning effort level")
            else:
                continue
            if val:
                items.append({
                    "value": str(val),
                    "text": str(text),
                    "description": str(desc),
                })
        return items

    def _record_file_changes(self, block):
        """Record fileChange diffs for the end-of-turn file changes artifact."""
        changes = block.get("changes", [])
        cwd = (self.session.agent_thread.cwd
               if self.session.agent_thread else self.session.cwd) or ""
        for change in changes:
            path = change.get("path", "")
            if not path:
                continue
            abs_path, rel_path = _resolve_rel_path(path, cwd)
            diff_text = change.get("diff") or ""
            self.session.record_file_change(abs_path, rel_path, diff_text or None)

    def __init__(self, session):
        self.session = session
        self.last_is_tool_call = False
        self._plan_text = ""
        self._plan_text_rendered = False
        self._tool_file_re = _make_tool_file_re(self._TOOL_FILE_NAMES) if self._TOOL_FILE_NAMES else None
        self._ends_with_newline = True
        # Accumulated subagent text keyed by child session / subagent key.
        self._subagent_text_buffers = {}
        self._subagent_session_to_tool_key = {}
        self._subagent_tool_key_to_session = {}
        self._subagent_tool_blocks = {}
        # Tool call keys already rendered in this view.  This avoids a race where
        # the ST main thread hasn't registered the phantom yet but a terminal
        # update arrives and would otherwise append a duplicate row.
        self._rendered_tool_keys = set()

    def format_model_badge(self, m: dict) -> str:
        """Format a compact reasoning/thinking badge based strictly on structured capabilities."""
        if m.get("supportsAdaptiveThinking"):
            return "Thinking"

        reasoning_efforts = m.get("supportedReasoningEfforts") or m.get("supportedEffortLevels") or []
        default_effort = m.get("defaultReasoningEffort")
        if reasoning_efforts or (default_effort and default_effort != "none") or m.get("supportsEffort"):
            return "Reasoning"

        return ""

    def format_model_display(self, m: dict) -> tuple:
        """Format raw model data for UI presentation.
        Returns (display_name, value, description, annotation).
        """
        value = m.get("value") or m.get("model") or m.get("id") or ""
        display_name = m.get("displayName") or m.get("title") or m.get("name") or value
        desc = (m.get("description") or "").replace("`", "")
        desc = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', desc)
        desc = re.sub(r'<([^>]+)>', r'\1', desc)
        desc = " ".join(desc.split())
        if len(desc) > 120:
            desc = desc[:117] + "..."
        annotation = self.format_model_badge(m)
        if annotation:
            # Ensure pure plain text with no HTML markup
            annotation = re.sub(r'<[^>]+>', '', annotation).strip()
        return display_name, value, desc, annotation

    def normalize_model(self, m: dict) -> dict:
        """Enrich raw model dict with standard display fields while retaining all raw keys."""
        item = dict(m)
        display_name, value, desc, annotation = self.format_model_display(m)
        item["displayName"] = display_name
        item["value"] = value
        item["description"] = desc
        item["annotation"] = annotation
        item["supportedReasoningEfforts"] = (
            m.get("supportedReasoningEfforts")
            or m.get("supportedEffortLevels")
            or []
        )
        return item

    def set_available_models(self, models):
        """Normalize and store available models, retaining raw keys while adding standard display fields."""
        if not isinstance(models, list):
            return
        if not self.session:
            return
        self.session.available_models = [
            self.normalize_model(m) for m in models if isinstance(m, dict)
        ]
        if hasattr(self.session, "on_available_models_updated") and callable(self.session.on_available_models_updated):
            self.session.on_available_models_updated()

    def handle_message(self, message):
        """Dispatch agent message to appropriate handler."""
        try:
            utils.loading_log(
                "agent_message",
                view_id=self.session.chat_view.id() if self.session.chat_view else None,
                view_name=self.session.chat_view.name() if self.session.chat_view else None,
                session_id=str(self.session.chat_view.settings().get("chatview_session_id") or "")
                if self.session.chat_view else "",
                message_type=str(getattr(message, "type", "") or "")
                if not isinstance(message, tuple) else f"tuple:{message[0]}",
                loading=self.session.loading_animation.is_loading
                if getattr(self.session, "loading_animation", None) else None,
            )
        except Exception:
            pass
        # Handle error strings passed from thread wrapper
        if message == "error":
            pass

        # Check for error tuple/custom protocol from AgentThread
        if isinstance(message, tuple) and message[0] == "error":
            self.append_error(message[1])
            self.session.stop_loading()
            return

        # Check for reset_complete message
        if isinstance(message, tuple) and message[0] == "reset_complete":
            LOG.info("Session reset completed successfully")
            return

        if getattr(message, "type", None) == "thread_title":
            content = message.content if isinstance(message.content, dict) else {}
            if hasattr(self.session, "set_thread_title"):
                self.session.set_thread_title(content.get("title", ""))
            return

        if getattr(message, "type", None) == "subagent_text":
            self._handle_subagent_text(message)
            return

        if hasattr(message, "type"):
            if message.type == "token_usage":
                if hasattr(self.session, "set_token_usage"):
                    self.session.set_token_usage(message.content)
                return
            if message.type == "turn_started":
                self._ends_with_newline = True
                self._subagent_text_buffers.clear()
                self._subagent_session_to_tool_key.clear()
                self._subagent_tool_key_to_session.clear()
                self._subagent_tool_blocks.clear()
                # Keep _rendered_tool_keys: tool rows persist in the buffer across turns.
            if message.type in ("stop", "result"):
                outputs = getattr(self.session, "tool_outputs", None)
                if outputs is not None:
                    outputs.finish_running()
            self._handle_typed_message(message)

    def _handle_typed_message(self, message):
        raise NotImplementedError

    def _handle_subagent_text(self, message):
        """Accumulate subagent text; show it behind the tool-call phantom on result."""
        content = message.content if isinstance(message.content, str) else ""
        if not content:
            return
        raw = message.raw_data if isinstance(message.raw_data, dict) else {}
        thread_id = (
            raw.get("thread_id") or raw.get("threadId")
            or raw.get("threadID") or ""
        )
        subagent_id = (
            raw.get("subagent_id") or raw.get("subagentId")
            or raw.get("itemId") or ""
        )
        session_key = str(thread_id or subagent_id or getattr(message, "id", None) or "subagent")
        self._subagent_text_buffers[session_key] = (
            self._subagent_text_buffers.get(session_key, "") + content
        )

        # If the tool call is already rendered and terminal, update its output.
        tool_key = self._subagent_session_to_tool_key.get(session_key)
        if not tool_key:
            return
        outputs = getattr(self.session, "tool_outputs", None)
        if outputs is None or not outputs.find(str(tool_key)):
            return
        block = getattr(self, "_subagent_tool_blocks", {}).get(str(tool_key))
        if not isinstance(block, dict):
            return
        status = str(block.get("status") or "").lower()
        if status in ("running", "pending", "in_progress", "inprogress", ""):
            return
        self.append_tool_call(block, key=str(tool_key))

    def _render_diff_block(self, diff_text):
        if not diff_text:
            return ""
        # Keep the diff visually subordinate to its tool-call header without
        # sacrificing much of the chat view's usable line width.
        indented = "\n".join("  " + line for line in diff_text.rstrip().splitlines())
        return f"  ````diff\n{indented}\n  ````"

    def _format_tool_block(self, block, structure=None):
        name = block.get("name")
        text = f"⏺ {name}" if name else ""
        if structure is not None:
            if name:
                structure.set_header(f"⏺ {name}", "")
            structure.fill_from_text(text, self._split_tool_header)
        return text

    def format_tool_result(self, block, force_collapse=False):
        """Render a completed tool result for the chat view.

        Returns ``(inline_text, summary, detail, error_detail)``:

        - ``inline_text``: fenced block to append directly to the transcript
          (short output, stays selectable and searchable)
        - ``summary`` / ``detail``: data for a collapsed phantom row
          (long output is only written to the buffer when expanded)
        - ``error_detail``: error text for a separate Error block when the
          tool failed; empty when no error block should be shown

        ``force_collapse`` keeps even short output behind the phantom row.
        Command tool calls use it so their success/failed row never vanishes.

        Edit-like tools return four empty strings because ``_format_tool_block``
        already renders their diff.
        """
        if not isinstance(block, dict):
            return "", "", "", ""

        name = str(block.get("name") or "").lower()
        metadata = block.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        has_diff = bool(metadata.get("diff")) or name in _DIFF_TOOL_NAMES
        raw_error = block.get("error")
        status = str(block.get("status") or "").lower()
        is_error = bool(raw_error) or status in ("error", "failed", "failure")
        if has_diff and not is_error:
            return "", "", "", ""

        output = block.get("output")
        if isinstance(output, list):
            output = "\n".join(
                str(item.get("text") or "")
                for item in output
                if isinstance(item, dict) and item.get("type") == "text"
            )
        error = block.get("error")
        if isinstance(output, str):
            output = output
        elif output is None:
            output = ""
        else:
            output = str(output)
        if isinstance(error, str):
            error_text = error
        elif isinstance(error, dict):
            error_text = (
                error.get("message")
                or error.get("error")
                or error.get("detail")
                or str(error)
            )
            if not isinstance(error_text, str):
                error_text = str(error_text)
        elif error is None:
            error_text = ""
        else:
            error_text = str(error)

        has_real_output = bool(output.strip())
        has_error = bool(error_text.strip()) or _tool_result_status(block) == "failed"
        text = output if has_real_output else error_text
        if not isinstance(text, str):
            text = "" if text is None else str(text)

        getter = getattr(self.session, "get_setting", None)
        inline_lines, inline_max_chars = 3, 400
        try:
            settings = sublime.load_settings("TermMate.sublime-settings")
            inline_lines = settings.get("tool_output_inline_lines", 3)
            inline_max_chars = settings.get("tool_output_inline_max_chars", 400)
        except Exception:
            pass
        if getter is not None:
            inline_lines = getter("tool_output_inline_lines", inline_lines)
            inline_max_chars = getter(
                "tool_output_inline_max_chars", inline_max_chars
            )

        mode, cleaned = utils.classify_tool_output(
            text, inline_lines, inline_max_chars
        )
        if mode is None:
            if has_error and error_text.strip():
                # Failed tool with no classifiable output - still show Error.
                _, err_cleaned = utils.classify_tool_output(
                    error_text, 0, 0
                )
                if err_cleaned:
                    err_lines = len(err_cleaned.splitlines())
                    err_unit = "line" if err_lines == 1 else "lines"
                    return "", f"{err_lines} {err_unit} failed", "", err_cleaned
            return "", "", "", ""

        line_count = len(cleaned.splitlines())
        unit = "line" if line_count == 1 else "lines"
        summary = f"{line_count} {unit} {_tool_result_status(block)}"

        error_detail = ""
        if has_error and error_text.strip():
            _, err_cleaned = utils.classify_tool_output(
                error_text, 0, 0
            )
            error_detail = err_cleaned or error_text
            # Avoid duplicating the same text in Return and Error blocks.
            if not has_real_output and error_detail.strip() == cleaned.strip():
                cleaned_for_return = ""
            else:
                cleaned_for_return = cleaned if has_real_output else ""
        else:
            cleaned_for_return = cleaned

        if mode == "inline" and not force_collapse and not error_detail:
            return utils.format_fenced_output(cleaned), "", "", ""
        return "", summary, cleaned_for_return, error_detail

    def _command_text(self, block):
        """Return the raw command for command-like tools, else None."""
        name = str(block.get("name") or "").lower()
        if name in ("command_execution", "shell", "command"):
            value = block.get("command")
            if not isinstance(value, str) or not value.strip():
                inp = block.get("input")
                if isinstance(inp, dict):
                    value = inp.get("command") or inp.get("cmd")
        elif name == "bash":
            # Only intercept command_execution-shaped Bash blocks (top-level
            # command). Claude Bash uses input.command and is formatted by
            # ClaudeMessageProcessor._format_tool_block; intercepting it would
            # regress its rendering.
            value = block.get("command")
        elif name in ("run_command", "runcommand"):
            params = block.get("parameters")
            if not isinstance(params, dict):
                params = block.get("input")
            if not isinstance(params, dict):
                params = {}
            value = (
                params.get("CommandLine")
                or params.get("command")
                or params.get("cmd")
                or block.get("command")
            )
        elif name in ("tool", "unknown", ""):
            value = block.get("command")
            if not isinstance(value, str) or not value.strip():
                inp = block.get("input")
                if isinstance(inp, dict):
                    value = inp.get("command") or inp.get("cmd")
                else:
                    value = None
        else:
            return None
        if not isinstance(value, str) or not value.strip():
            return None
        return value.replace("\r\n", "\n").replace("\r", "\n")

    def _command_workdir(self, block):
        """Return workdir label when it differs from session cwd, else ''."""
        if not isinstance(block, dict):
            return ""
        workdir = block.get("workdir")
        if not isinstance(workdir, str) or not workdir.strip():
            inp = block.get("input")
            if isinstance(inp, dict):
                workdir = inp.get("workdir") or inp.get("cwd")
            else:
                workdir = ""
        if not isinstance(workdir, str) or not workdir.strip():
            meta = block.get("metadata")
            if isinstance(meta, dict):
                workdir = meta.get("workdir") or meta.get("cwd")
        if not isinstance(workdir, str) or not workdir.strip():
            return ""
        workdir = workdir.strip()
        cwd = ""
        try:
            cwd = (self.session.agent_thread.cwd
                   if self.session and self.session.agent_thread else getattr(self.session, "cwd", "")) or ""
        except Exception:
            cwd = ""
        if cwd:
            try:
                norm_wd = os.path.normcase(os.path.normpath(workdir))
                norm_cwd = os.path.normcase(os.path.normpath(cwd))
                if norm_wd == norm_cwd:
                    return ""
                # Show relative path when inside cwd for readability
                try:
                    rel = os.path.relpath(workdir, cwd)
                    if not rel.startswith(".."):
                        workdir = rel
                except Exception:
                    pass
            except Exception:
                pass
        return workdir.replace("\\", "/").rstrip("/")

    def _command_heading(self, block):
        name = str(block.get("name") or "").lower()
        label = "RunCommand" if name in ("run_command", "runcommand") else "command"
        workdir = self._command_workdir(block)
        if workdir:
            return f"⏺ {label} [{workdir}]"
        return f"⏺ {label}"

    def _command_preview(self, command, limit=_COMMAND_PREVIEW_MAX_CHARS):
        """One-line command preview, truncated with an ellipsis."""
        lines = command.rstrip().splitlines() or [""]
        first = lines[0].strip()
        has_more = len(lines) > 1
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = _COMMAND_PREVIEW_MAX_CHARS
        if len(first) > limit:
            first = first[: max(1, limit - 1)].rstrip() + "…"
        elif has_more:
            first = f"{first} …"
        return first or "…"

    @staticmethod
    def _split_tool_header(line):
        """Split ``⏺ read path`` into ``("⏺ read", "path")``."""
        line = str(line or "").strip()
        if not line.startswith("⏺"):
            return "⏺ tool", line
        body = line[1:].strip()
        if not body:
            return "⏺ tool", ""
        name, _, rest = body.partition(" ")
        rest = rest.strip()
        # ``_format_tool_block`` renders generic tools as ``⏺ name (args)``;
        # the status row only needs the arguments without the parentheses.
        if rest.startswith("(") and rest.endswith(")"):
            rest = rest[1:-1].strip()
        return f"⏺ {name}", rest

    def _is_diff_tool(self, block):
        """Return True when the tool's body is an inline diff block."""
        name = str(block.get("name") or "").lower()
        if name in _DIFF_TOOL_NAMES:
            return True
        metadata = block.get("metadata")
        return isinstance(metadata, dict) and bool(metadata.get("diff"))

    @staticmethod
    def _extract_diffs(block):
        """Return ``(diff_text, diffs)`` from structured block data.

        ``diffs`` is a list of ``{"header", "diff_text"}`` entries so a
        multi-file edit is represented explicitly instead of being inferred
        from how many ``⏺`` characters a rendered string happens to contain.
        """
        metadata = block.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        diff_text = metadata.get("diff") or ""
        filediff = metadata.get("filediff")
        if not diff_text and isinstance(filediff, dict):
            diff_text = filediff.get("patch") or ""

        diffs = []
        changes = block.get("changes")
        if isinstance(changes, list):
            for change in changes:
                if not isinstance(change, dict):
                    continue
                path = change.get("path") or ""
                body = change.get("diff") or ""
                if path or body:
                    diffs.append({"header": path, "diff_text": body})
        if diffs:
            if not diff_text:
                diff_text = diffs[0]["diff_text"]
        elif diff_text:
            diffs = [{"header": "", "diff_text": diff_text}]
        return diff_text, diffs

    def _build_tool_context(self, block, key=None):
        """Build a canonical :class:`ToolContext` from a raw tool block.

        This is the single place where provider-specific text generation and
        all diff/multi-file decisions happen.  Downstream rendering only reads
        context fields, so live and history-normalized blocks share one path.
        """
        if not isinstance(block, dict):
            return ToolContext()

        # Command-like tools: the command itself is the detail and the header
        # is synthesized from the tool name (plus optional workdir label).
        command = self._command_text(block)
        if command is not None:
            heading = self._command_heading(block)
            summary = self._command_preview(command)
            detail = command.rstrip("\n")
            workdir = self._command_workdir(block)
            if workdir:
                detail = f"[{workdir}] {detail}"
            ctx = ToolContext(
                kind="command",
                heading=heading,
                summary=summary,
                detail=detail,
                text=f"{heading} {summary}".strip(),
                show_command=True,
                phase=_tool_call_phase(block),
            )
            if not ctx.phase:
                self._fill_result_into_context(ctx, block)
            return ctx

        # Every other tool: the provider renders its text and, in the same
        # pass, records the structural facts it already knows.  Migrated
        # providers set these explicitly; the compatibility fallback derives
        # them from the text once, here, instead of in the renderer.
        structure = ToolStructure()
        text = self._format_tool_block(block, structure=structure)
        diff_text, diffs = self._extract_diffs(block)
        if diff_text or diffs:
            structure.diff_text = diff_text
            structure.diffs = diffs
        structure.fill_from_text(text, self._split_tool_header)
        structure.is_diff = self._is_diff_tool(block)
        if structure.is_diff and structure.diff_text:
            structure.kind = "diff"

        ctx = ToolContext(
            kind=("diff" if structure.is_diff and structure.diff_text
                  else structure.kind or str(
                      block.get("name") or "unknown").lower()),
            text=text,
            heading=structure.heading,
            summary=structure.summary,
            detail=structure.detail,
            is_diff=structure.is_diff,
            diff_text=structure.diff_text,
            diffs=structure.diffs,
            has_inline_body=structure.has_inline_body,
            is_multi=structure.is_multi,
            phase=_tool_call_phase(block),
        )
        if not text or structure.is_multi or not text.lstrip().startswith("⏺"):
            ctx.has_phantom = False
            return ctx

        if ctx.is_diff and ctx.has_inline_body:
            # Inline diff body: the result summary is computed from the body
            # itself because format_tool_result suppresses diff results.
            ctx.has_output = True
            _, out_summary, out_detail, out_error = self.format_tool_result(
                block, force_collapse=True)
            ctx.output_detail = out_detail
            ctx.error_detail = out_error
            if not out_summary:
                body_lines = max(0, len(text.splitlines()) - 1)
                unit = "line" if body_lines == 1 else "lines"
                out_summary = f"{body_lines} {unit} {_tool_result_status(block)}"
            ctx.output_summary = out_summary
            return ctx

        self._fill_result_into_context(ctx, block)
        if (not ctx.phase and not ctx.has_output
                and not any(k in block for k in ("output", "error", "exitCode"))):
            # A plain tool with no result yet has nothing to show below its
            # header (running rows above already returned via ctx.phase).
            ctx.has_phantom = False
        return ctx

    def _fill_result_into_context(self, ctx, block):
        """Populate output/error fields on ``ctx`` from a finished result."""
        _, out_summary, out_detail, out_error = self.format_tool_result(
            block, force_collapse=True)
        ctx.output_summary = out_summary
        ctx.output_detail = out_detail
        ctx.error_detail = out_error
        if out_detail or out_error:
            ctx.has_output = True
        elif any(k in block for k in ("output", "error", "exitCode")):
            # Finished but silent: keep a zero-line row so a previous
            # "running…" status does not simply vanish.
            ctx.has_output = True
            ctx.output_summary = f"0 lines {_tool_result_status(block)}"
            ctx.output_detail = ""

    def _render_tool(self, ctx, key=None):
        """Render one tool call from its context into ``(text, phantoms)``."""
        if not ctx.text or not ctx.has_phantom:
            return ctx.text, []

        call = {
            "line": 0,
            "kind": "tool_call",
            "key": key,
            "status_only": True,
            "command": {
                "heading": ctx.heading,
                "summary": ctx.summary,
                "detail": ctx.detail,
                "show_command": ctx.show_command,
            },
            "output": None,
        }
        if ctx.phase:
            call["status"] = ctx.phase
            return ctx.text, [call]

        if ctx.error_detail:
            call["error"] = {"detail": ctx.error_detail}
        if ctx.has_output:
            call["output"] = {
                "summary": ctx.output_summary,
                "detail": ctx.output_detail,
            }
        return ctx.text, [call]

    @staticmethod
    def _subagent_session_id(block):
        """Extract child session id for a subagent tool call, if present."""
        if not isinstance(block, dict):
            return ""
        input_data = block.get("input") or {}
        if not isinstance(input_data, dict):
            input_data = {}
        metadata = block.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        for source in (block, input_data, metadata):
            for key in ("sessionID", "sessionId", "session_id", "thread_id", "threadId"):
                value = source.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ""

    def format_tool_call(self, block, key=None):
        """Return ``(text, phantoms)`` for one tool call.

        The tool header (``⏺ name args``) stays real buffer text so it can be
        selected, searched and double-clicked to open a file.  Only the
        volatile status row (``⎿ running…`` / ``⎿ N lines success [show]``)
        is a phantom, so it can update in place without touching the buffer.
        ``phantoms`` is a list of dicts consumed by
        ``term_chat_output_append``:

            {"line": 0, "kind": "tool_call", "key": str | None,
             "status_only": True,
             "status": "running" | "background" | None,
             "command": {"heading", "summary", "detail", "show_command"},
             "output": {"summary", "detail"} | None}

        ``line`` is a row offset inside ``text``; ``0`` anchors the status row
        on the appended header line so it renders directly below it.

        Rendering is a two-stage pipeline:

            format_tool_call
              -> _build_tool_context   (provider text + all decisions)
              -> _render_tool          (pure context -> text/phantoms)

        Diff detection, multi-file detection and result/phase resolution all
        live in the context, so the renderer never guesses them from the
        rendered string.
        """
        ctx = self._build_tool_context(block, key)
        return self._render_tool(ctx, key)

    def append_content(self, text, flush=False, is_tool=False, tool_phantoms=None,
                       live=False):
        """Append Markdown content through the chat session.

        ``live`` streams an unfinished line immediately; it is only used for
        agent text, never for tool rows (which are already appended whole).
        """
        if text:
            raw = text.replace("\r", "")
            if raw:
                self._ends_with_newline = raw.endswith("\n")
        if not is_tool and text and text.strip().startswith("⏺"):
            is_tool = True
        kwargs = {"flush": flush}
        if live:
            kwargs["live"] = True
        if is_tool:
            if tool_phantoms:
                self.session.append_markdown(
                    text, is_tool=True, tool_phantoms=tool_phantoms, **kwargs
                )
            else:
                self.session.append_markdown(text, is_tool=True, **kwargs)
        else:
            self.session.append_markdown(text, **kwargs)

    def ensure_trailing_newline(self):
        """Ensure the chat content ends with a newline, without adding extra blank lines."""
        if not self._ends_with_newline:
            self.append_content("\n")
            self._ends_with_newline = True

    def has_tool_call(self, key):
        """Return True when a phantom row was already rendered for ``key``."""
        if not key:
            return False
        if key in getattr(self, "_rendered_tool_keys", set()):
            return True
        outputs = getattr(self.session, "tool_outputs", None)
        return bool(outputs and outputs.find(key) is not None)

    @staticmethod
    def message_key(message):
        """Stable id used to update a row in place.

        Agent messages store their id on ``id``; older adapters used
        ``msg_id``.  Accept both so a repeated event can find its row.
        """
        return (getattr(message, "msg_id", None)
                or getattr(message, "id", None))

    def format_shell_finished(self, command, failed=False, output="", key=None):
        """Phantom row shown when a background shell finishes.

        Mirrors the TUI, which reports the completed background job as a
        separate message.  The single ``show`` link reveals the original
        command and the output the shell produced after the tool call had
        already returned.
        """
        command = str(command or "").strip()
        if not command:
            return None
        return {
            "line": -1,
            "kind": "shell_finished",
            "key": key,
            "label": "Shell failed" if failed else "Shell finished",
            "failed": bool(failed),
            "summary": self._command_preview(command),
            "command": {"detail": command.rstrip("\n")},
            "output": {
                "summary": "",
                "detail": output if isinstance(output, str) else "",
            },
        }

    def _attach_subagent_output(self, block, key=None, session_id=""):
        """Attach accumulated subagent text as tool output on terminal status."""
        if not isinstance(block, dict):
            return
        status = str(block.get("status") or "").lower()
        if status in ("running", "pending", "in_progress", "inprogress"):
            return
        session_id = str(session_id or self._subagent_session_id(block) or "")
        if not session_id and key:
            session_id = str(self._subagent_tool_key_to_session.get(str(key)) or "")
        if not session_id:
            return
        text = self._subagent_text_buffers.get(session_id)
        if not isinstance(text, str) or not text.strip():
            return
        # Preserve explicit error; otherwise expose subagent answer as output.
        if not block.get("output") and not block.get("error"):
            block["output"] = text

    def append_tool_call(self, block, key=None):
        """Append one tool call, or update its existing phantom row.

        A repeated ``key`` means the same tool was reported again (for
        example ``running`` followed by ``completed``). Updating keeps the
        row's position and, when the row is expanded, refreshes the visible
        ``Command`` / ``Return`` blocks.
        """
        if _is_subagent_tool(block):
            session_id = self._subagent_session_id(block)
            if session_id and key:
                self._subagent_session_to_tool_key[str(session_id)] = str(key)
                self._subagent_tool_key_to_session[str(key)] = str(session_id)
            if key:
                self._subagent_tool_blocks[str(key)] = block
            self._attach_subagent_output(block, key=key, session_id=session_id)

        is_update = self.has_tool_call(key)
        rendered, phantoms = self.format_tool_call(block, key=key)
        if _is_subagent_tool(block):
            recorder = getattr(self.session, "record_subagent_tool_call", None)
            if recorder:
                recorder(
                    key or rendered,
                    block.get("name") or block.get("tool"),
                    block.get("status"),
                )
        if key:
            self._rendered_tool_keys.add(str(key))
        if is_update and phantoms:
            for item in phantoms:
                item["update"] = True
            self.append_content("", is_tool=True, tool_phantoms=phantoms)
            return
        if not self.last_is_tool_call:
            self.append_content("\n", is_tool=True)
        self.last_is_tool_call = True
        self.append_content(
            (rendered + "\n") if rendered else "",
            is_tool=True,
            tool_phantoms=phantoms or None,
        )

    def append_error(self, error_msg):
        """Append error notice to chat view."""
        sublime.set_timeout(
            lambda: self.session.append_notice(
                f"Error: {error_msg}", kind="error"),
            0
        )

    def open_tool_file(self, line_text, window, view=None, point=None):
        """Parse a tool-call line and open the referenced file. Returns True if handled."""
        cwd = (self.session.agent_thread.cwd
               if self.session.agent_thread else self.session.cwd)
        if not cwd:
            return False
        result = _parse_tool_file_line(line_text, self._tool_file_re, cwd) if self._tool_file_re else None
        if result is None:
            result = self._parse_diff_hunk_line(line_text, view, point, cwd)
        if result is None:
            return False
        abs_path, line_start = result
        if line_start is not None:
            window.open_file(f"{abs_path}:{line_start}:0", sublime.ENCODED_POSITION)
        else:
            window.open_file(abs_path)
        return True

    def open_subagent_tool_call(self, line_text, window, view=None, point=None):
        """Open the subagent chat view for a subagent tool-call line.

        Returns True when the line is a subagent tool header and a matching
        session was found (or opened).  This lets double-click on a subagent
        row behave like double-click on a read/edit file row.
        """
        text = str(line_text or "").strip()
        if not text.startswith("⏺"):
            return False
        name = text[1:].strip().split(None, 1)[0] if len(text) > 1 else ""
        if name not in _SUBAGENT_TOOL_NAMES:
            return False

        session_id = self._extract_subagent_session_id_from_line(text)
        if not session_id:
            return False

        # Focus an already-open subagent view for this session.
        for v in window.views():
            try:
                if (v.settings().get("chatview_subagent", False)
                        and str(v.settings().get("chatview_session_id") or "") == session_id):
                    window.focus_view(v)
                    return True
            except Exception:
                continue

        self._open_subagent_session_view(window, session_id, text)
        return True

    def _extract_subagent_session_id_from_line(self, line_text):
        """Extract a subagent session id from a rendered tool header."""
        known = set(self._subagent_session_to_tool_key.keys())
        known.update(self._subagent_tool_key_to_session.values())
        for sid in known:
            if sid and sid in line_text:
                return sid
        # Fallback: session id appended as the last token.
        tokens = line_text.rstrip().split()
        if not tokens:
            return ""
        tail = tokens[-1].strip()
        if re.match(r"^(ses_[A-Za-z0-9]+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$", tail):
            return tail
        # Generic fallback: long opaque id-like token at end.
        if len(tail) >= 12 and re.match(r"^[A-Za-z0-9_-]+$", tail):
            return tail
        return ""

    def _open_subagent_session_view(self, window, session_id, line_text):
        """Open a read-only subagent chat view for *session_id*."""
        from .chatview import (
            _create_chat_view_and_session, SessionRole, CHAT_THREAD_ID,
            CHAT_THREAD_TITLE, TermChatNewThreadCommand, chat_threads,
        )
        session = self.session
        cwd = (session.agent_thread.cwd
               if session.agent_thread and session.agent_thread.cwd
               else session.cwd) or ""
        agent = chat_threads.get_setting_for_session(
            window, session, "chatview_agent_provider", "opencode"
        )
        summary = line_text[1:].strip()  # drop "⏺"
        task_num = TermChatNewThreadCommand(window)._next_task_number()
        new_view = _create_chat_view_and_session(
            window,
            role=SessionRole.TASK,
            cwd=cwd,
            session_id=session_id,
            initial_title=summary,
            subagent=True,
        )
        new_view.settings().set(CHAT_THREAD_ID, task_num)
        new_view.settings().set(CHAT_THREAD_TITLE, summary)
        new_session = chat_threads.sessions.get(new_view.id())
        if new_session is None:
            return
        new_session.has_sent_message = True
        new_session.set_thread_title(summary)
        new_session._clear_transcript()
        new_session._fetch_resume_history_async(
            agent, session_id, cwd, history_limit=0, render_full=True,
        )

    def open_local_file_link(self, line_text, window, view, point):
        """Open the local Markdown link under a double-click."""
        if view is None or point is None:
            return False
        cwd = (self.session.agent_thread.cwd
               if self.session.agent_thread else self.session.cwd)
        if not cwd:
            return False

        line_region = view.line(point)
        column_in_line = point - line_region.begin()
        for match in _MARKDOWN_LINK_RE.finditer(line_text):
            if not (match.start() <= column_in_line < match.end()):
                continue
            result = _parse_markdown_file_target(
                match.group(1) or match.group(2), cwd)
            if result is None:
                return False
            abs_path, line, column = result
            if line is not None:
                encoded = f"{abs_path}:{line}:{column or 0}"
                window.open_file(encoded, sublime.ENCODED_POSITION)
            else:
                window.open_file(abs_path)
            return True
        return False

    def _parse_diff_hunk_line(self, line_text, view, point, cwd):
        """Resolve a diff hunk header (@@ -a,b +c,d @@) to (abs_path, line).

        The hunk line carries no file path, so scan upward in the view for the
        nearest tool-call header line above the diff block and take its path.
        """
        if view is None or point is None or self._tool_file_re is None:
            return None
        m = _HUNK_LINE_RE.match(line_text.strip())
        if not m:
            return None
        dest_line = int(m.group(1))
        row = view.rowcol(point)[0]
        for r in range(row - 1, -1, -1):
            text = view.substr(view.line(view.text_point(r, 0))).strip()
            parsed = _parse_tool_file_line(text, self._tool_file_re, cwd)
            if parsed is not None:
                return (parsed[0], dest_line)
            if text.startswith("⏺"):
                return None
        return None


class ClaudeMessageProcessor(BaseChatMessageProcessor):
    _TOOL_FILE_NAMES = ("Read", "Edit", "Write")
    _RISKY_TOOLS = ("Bash",)

    @classmethod
    def get_default_think_presets(cls) -> list:
        return [
            {"value": "adaptive", "text": "adaptive", "description": "Dynamic thinking managed by model (default)"},
            {"value": "low", "text": "low", "description": "Light thinking budget (~2k tokens)"},
            {"value": "medium", "text": "medium", "description": "Standard thinking depth (~8k tokens)"},
            {"value": "high", "text": "high", "description": "Comprehensive reasoning (~16k tokens)"},
            {"value": "xhigh", "text": "xhigh", "description": "Extended thinking depth for Opus 4.7 (~32k tokens)"},
            {"value": "none", "text": "none", "description": "Disable extended thinking (0 tokens)"},
        ]

    @classmethod
    def get_think_presets(cls, active_model: dict = None) -> list:
        if active_model:
            model_efforts = (
                active_model.get("supportedReasoningEfforts")
                or active_model.get("supportedEffortLevels")
            )
            if model_efforts:
                return cls.normalize_think_efforts(model_efforts)
            if active_model.get("supportsAdaptiveThinking"):
                return cls.get_default_think_presets()
            return []
        return cls.get_default_think_presets()

    def _handle_typed_message(self, message):
        if message.type == "assistant":
            self.session.start_loading()

            text_content = ""
            if hasattr(message, "content"):
                for block in message.content:
                    if hasattr(block, "text"):
                        if self.last_is_tool_call:
                            text_content += "\n"
                        self.last_is_tool_call = False
                        text_content += block.text

                    elif isinstance(block, dict) and block.get("type") == "tool_use":
                        if text_content:
                            trailing = len(text_content) - len(text_content.rstrip("\n"))
                            if trailing == 0:
                                text_content += "\n"
                                trailing = 1
                            self.append_content(text_content, live=True)
                            text_content = ""
                            need_newline = (trailing < 2)
                        else:
                            need_newline = False

                        if block.get("name") in ("Edit", "Write"):
                            pass  # defer to tool_use_result
                        else:
                            if not self.last_is_tool_call and need_newline:
                                self.append_content("\n", is_tool=True)
                            self.last_is_tool_call = True
                            if _is_subagent_tool(block):
                                recorder = getattr(
                                    self.session, "record_subagent_tool_call", None
                                )
                                if recorder:
                                    recorder(
                                        block.get("id")
                                        or getattr(message, "id", None)
                                        or block.get("name"),
                                        block.get("name"),
                                        block.get("status"),
                                    )
                            rendered, phantoms = self.format_tool_call(block)
                            self.append_content(
                                (rendered + "\n") if rendered else "",
                                is_tool=True,
                                tool_phantoms=phantoms or None,
                            )

            if text_content:
                if not text_content.endswith("\n"):
                    text_content += "\n"
                self.append_content(text_content, live=True)
        elif message.type == "system":
            raw = getattr(message, "raw_data", {}) or {}
            if raw.get("subtype") == "status" and raw.get("status") == "compacting":
                self.session.start_loading(text="compacting")
            elif raw.get("subtype") == "thinking_tokens":
                self.session.start_loading(text="thinking")
            elif hasattr(message, "content") and isinstance(message.content, dict):
                session_id = message.content.get("session_id")
                if session_id and message.content.get("subtype") == "init":
                    LOG.info(f"system session_id: {session_id}")
                    # Store session_id in view settings for persistence across restarts
                    self.session.set_view_session_id(self.session.chat_view, session_id)

        elif message.type == "control_request":
            # Handle permission request directly
            request = message.content.get("request", {})
            subtype = request.get("subtype")
            if subtype == "can_use_tool":
                tool_name = request.get("tool_name")
                input_data = request.get("input", {})
                request_id = message.content.get("request_id")

                # Store input data for later use in response
                self.session.permission_requests[request_id] = (tool_name, input_data)

                self.session.show_permission_phantom(request_id, tool_name, input_data)

        elif message.type == "user":
            inner = message.content if isinstance(message.content, dict) else {}
            # Only update the rewind UUID for actual user prompts, not tool_result echo messages.
            # Tool results have a list content (array of tool_use_result blocks); user prompts
            # have a string or a dict with role/content keys.
            msg_content = inner.get("message", {}).get("content") if isinstance(inner.get("message"), dict) else inner.get("content")
            is_tool_result = isinstance(msg_content, list)
            user_uuid = message.id  # set from data.get("uuid") in _parse_message
            if user_uuid and not is_tool_result:
                self.session.update_last_prompt_uuid(user_uuid)
            user_content = inner.get("content") or inner.get("message", {}).get("content", "")
            if isinstance(user_content, str) and user_content.startswith("<local-command-stdout>"):
                local_output = xml.etree.ElementTree.fromstring(user_content)
                self.append_content(local_output.text)
                self.append_content("\n")

            # Render Edit/Write from tool_use_result (has filePath, oldString, newString, structuredPatch)
            tool_use_result = inner.get("tool_use_result")
            if isinstance(tool_use_result, dict) and tool_use_result.get("filePath"):
                rendered = self._format_edit_result(tool_use_result)
                if rendered:
                    self.last_is_tool_call = True
                    self.append_content("\n" + rendered + "\n", is_tool=True)
                self._record_file_change(tool_use_result)

        elif message.type == "error":
            self.append_error(message.content)
            self.session.stop_loading()

        elif message.type == "result":
            # Flush markdown formatter buffer
            self.append_content("", flush=True)
            self.ensure_trailing_newline()
            # Stop loading on turn completion (heuristic)
            self.session.stop_loading()
            # Show collapsed file-changes artifact after the turn output settles
            sublime.set_timeout(self.session.show_file_changes_artifact, 0)

        elif message.type == "control_response":
            if hasattr(message, "content") and isinstance(message.content, dict):
                response_outer = message.content.get("response", {})
                if response_outer.get("subtype") == "success":
                    response_data = response_outer.get("response", {})
                    models = response_data.get("models", [])
                    if models:
                        self.set_available_models(models)

        elif message.type == "models_update":
            if hasattr(message, "content") and isinstance(message.content, dict):
                models = message.content.get("models", [])
                if models:
                    self.set_available_models(models)

    def _format_tool_block(self, block, structure=None):
        name = block.get("name")
        input_data = block.get("input", {})

        if name == "Read":
            file_path = input_data.get("file_path", "")
            if file_path:
                try:
                    rel_path = os.path.relpath(file_path, self.session.agent_thread.cwd)
                except Exception:
                    rel_path = file_path

                offset = input_data.get("offset")
                limit = input_data.get("limit")
                if offset is not None and limit is not None:
                    start_line = offset
                    end_line = offset + limit - 1
                    text = f"⏺ Read {rel_path}#L{start_line}-L{end_line}"
                elif offset is not None:
                    text = f"⏺ Read {rel_path}#L{offset}"
                else:
                    text = f"⏺ Read {rel_path}"
                if structure is not None:
                    structure.fill_from_text(text, self._split_tool_header)
                return text

        elif name in ("Agent", "Task", "Subagent", "agent", "task", "subagent"):
            description = input_data.get("description", "")
            subagent_type = input_data.get("subagent_type", "")
            parts = [f"⏺ {name}"]
            if subagent_type:
                parts.append(subagent_type)
            if description:
                parts.append(description)
            session_id = self._subagent_session_id(block)
            if session_id:
                parts.append(str(session_id))
            text = " ".join(parts)
            if structure is not None:
                structure.kind = "subagent"
                structure.fill_from_text(text, self._split_tool_header)
            return text

        elif name == "Bash":
            command = input_data.get("command", "")
            if command:
                lines = command.rstrip().splitlines()
                if len(lines) > 1:
                    first_line = lines[0]
                    indented_rest = "\n".join("    " + line for line in lines[1:])
                    text = f"⏺ Bash {first_line}\n\n{indented_rest}\n"
                elif len(lines) == 1:
                    text = f"⏺ Bash {lines[0]}"
                else:
                    text = ""
                if structure is not None and text:
                    structure.kind = "command"
                    structure.set_header("⏺ Bash", first_line if len(lines) > 1 else lines[0])
                    structure.has_inline_body = "\n" in text
                return text

        elif name in ("Grep", "Glob"):
            pattern = input_data.get("pattern")
            if pattern:
                text = f"⏺ {name} {pattern}"
                if structure is not None:
                    structure.fill_from_text(text, self._split_tool_header)
                return text
        elif name == "WebFetch":
            url = input_data.get("url", "")
            if url:
                text = f"⏺ WebFetch {url}"
                if structure is not None:
                    structure.fill_from_text(text, self._split_tool_header)
                return text
        elif name == "WebSearch":
            query = input_data.get("query", "")
            if query:
                text = f"⏺ WebSearch ({query})"
                if structure is not None:
                    structure.fill_from_text(text, self._split_tool_header)
                return text

        text = f"⏺ {name}" if name else ""
        if structure is not None:
            structure.fill_from_text(text, self._split_tool_header)
        return text

    def _format_edit_result(self, tool_use_result):
        """Render Edit/Write header + diff from tool_use_result data."""
        file_path = tool_use_result.get("filePath", "")
        if not file_path:
            return None
        cwd = self.session.agent_thread.cwd if self.session.agent_thread else ""
        _, rel_path = _resolve_rel_path(file_path, cwd)

        patches = tool_use_result.get("structuredPatch") or []
        start_line = patches[0].get("newStart") if patches else None

        # Write creates a new file (no oldString); Edit has both
        old_str = tool_use_result.get("oldString")
        new_str = tool_use_result.get("newString")
        is_edit = old_str is not None

        name = "Edit" if is_edit else "Write"
        header = f"⏺ {name} {rel_path}#L{start_line}" if start_line else f"⏺ {name} {rel_path}"

        if is_edit:
            diff = _make_edit_diff(old_str, new_str or "", rel_path, start_line=start_line)
            if diff:
                return header + "\n" + self._render_diff_block(diff)

        return header

    def _record_file_change(self, tool_use_result):
        """Record an Edit/Write diff for the end-of-turn file changes artifact."""
        file_path = tool_use_result.get("filePath")
        cwd = (self.session.agent_thread.cwd
               if self.session.agent_thread else self.session.cwd) or ""
        abs_path, rel_path = _resolve_rel_path(file_path, cwd)

        patches = tool_use_result.get("structuredPatch")
        diff = _diff_from_structured_patch(patches)
        if diff is None:
            old_str = tool_use_result.get("oldString")
            new_str = tool_use_result.get("newString")
            if old_str is not None:
                start_line = patches[0].get("newStart") if patches else None
                diff = _make_edit_diff(old_str, new_str or "", rel_path, start_line=start_line)
            else:
                # Write with no patch data: treat as a new file
                content = tool_use_result.get("content") or ""
                if content:
                    lines = content.splitlines()
                    diff = ("@@ -0,0 +1,{} @@\n".format(len(lines))
                            + "\n".join("+" + l for l in lines) + "\n")

        self.session.record_file_change(abs_path, rel_path, diff)


class CodexMessageProcessor(BaseChatMessageProcessor):
    _TOOL_FILE_NAMES = ("fileChange", "ImageView")
    _RISKY_TOOLS = ("command_execution",)

    @classmethod
    def get_default_think_presets(cls) -> list:
        return [
            {"value": "auto", "text": "auto", "description": "Model default reasoning effort"},
            {"value": "low", "text": "low", "description": "Fast responses with light reasoning"},
            {"value": "medium", "text": "medium", "description": "Balanced speed and reasoning depth (standard)"},
            {"value": "high", "text": "high", "description": "Deep reasoning for complex coding tasks"},
            {"value": "xhigh", "text": "xhigh", "description": "Maximum reasoning effort"},
        ]

    @classmethod
    def get_think_presets(cls, active_model: dict = None) -> list:
        if active_model:
            model_efforts = active_model.get("supportedReasoningEfforts")
            if model_efforts:
                return cls.normalize_think_efforts(model_efforts)
            return []
        return cls.get_default_think_presets()

    def _handle_typed_message(self, message):
        if message.type == "assistant":
            self.session.start_loading()

            # Extract text content
            text_content = ""
            if hasattr(message, "content"):
                for block in message.content:
                    if hasattr(block, "text"):
                        if self.last_is_tool_call:
                            text_content += "\n"
                        self.last_is_tool_call = False

                        text_content += block.text

            if text_content:
                self.append_content(text_content + "\n", live=True)

        elif message.type == "tool_use":
            self.session.start_loading()
            self.append_tool_call(
                message.content,
                key=self.message_key(message),
            )
            if message.content.get("name") == "fileChange":
                self._record_file_changes(message.content)

        elif message.type == "error":
            self.append_error(message.content)
            self.session.stop_loading()

        elif message.type == "control_request":
            # Handle permission request directly
            request = message.content.get("request", {})
            subtype = request.get("subtype")
            if subtype == "can_use_tool":
                tool_name = request.get("tool_name")
                input_data = request.get("input", {})
                request_id = message.content.get("request_id")

                # Store input data for later use in response
                self.session.permission_requests[request_id] = (tool_name, input_data)

                self.session.show_permission_phantom(request_id, tool_name, input_data)

        elif message.type == "thread_started":
            if hasattr(message, "content") and isinstance(message.content, dict):
                session_id = message.content.get("session_id")
                if session_id:
                    LOG.info(f"Agent thread session_id: {session_id}")
                    # Store session_id in view settings for persistence across restarts
                    self.session.set_view_session_id(self.session.chat_view, session_id)

        elif message.type == "models_update":
            if hasattr(message, "content") and isinstance(message.content, dict):
                models = message.content.get("models", [])
                if models:
                    self.set_available_models(models)

        elif message.type == "result":
            # Flush markdown formatter buffer
            self.append_content("", flush=True)
            self.ensure_trailing_newline()
            # Stop loading on turn completion (heuristic)
            self.session.stop_loading()

        elif message.type == "plan_delta":
            # Codex plan mode: accumulate <proposed_plan> content
            content = message.content if isinstance(message.content, str) else ""
            if content:
                self._plan_text += content
                if self.last_is_tool_call and not content.startswith(("\n", "\r\n")):
                    self.append_content("\n")
                self.last_is_tool_call = False
                self.append_content(content, live=True)
                self._plan_text_rendered = True

        elif message.type == "turn_started":
            # Extract turnId if available
            turn_id = message.content.get("turnId")
            self._active_turn_id = turn_id
            turn_index = message.content.get("turnIndex")
            uuid_to_store = turn_id or (str(turn_index) if turn_index is not None else None)
            if uuid_to_store is not None:
                self.session.update_last_prompt_uuid(str(uuid_to_store))
            self._plan_text = ""
            self._plan_text_rendered = False
            self.session.start_loading()

        elif message.type in ("thinking", "text"):
            self.session.start_loading()

        elif message.type == "stop":
            # Codex agent sends "stop" when its process completes
            self.append_content("", flush=True)
            self.ensure_trailing_newline()
            self.session.stop_loading()
            if getattr(self.session, "subagent_tool_keys", None):
                self.session.subagent_tool_keys.clear()
                self.session.model_phantom.update()
            # If Codex plan mode produced a plan, open it in a new view
            if self._plan_text:
                plan_text = self._plan_text
                self._plan_text = ""
                rendered = self._plan_text_rendered
                self._plan_text_rendered = False

                if not rendered:
                    self.append_content("\n")
                    self.append_content(plan_text)
                    self.append_content("\n", flush=True)

                # Add Implement button if in plan mode
                if self.session.agent_thread and self.session.agent_thread.anthropic_config.get("plan_mode"):
                    sublime.set_timeout(lambda pt=plan_text: self.session.show_implement_plan_button(pt), 0)
            else:
                self._plan_text_rendered = False
            # Show the artifact last so later appends don't land at its fold boundary
            sublime.set_timeout(self.session.show_file_changes_artifact, 0)

    def _format_tool_block(self, block, structure=None):
        name = block.get("name")

        if name == "fileChange":
            changes = block.get("changes", [])
            cwd = (self.session.agent_thread.cwd
                   if self.session.agent_thread else self.session.cwd) or ""

            rendered_parts = []
            header_count = 0
            previous_path = None
            for change in changes:
                path = change.get("path", "")
                diff_text = change.get("diff") or ""
                rendered_diff = self._render_diff_block(diff_text)
                header = None

                if path:
                    abs_path, rel = _resolve_rel_path(path, cwd)
                    path_key = os.path.normcase(os.path.normpath(abs_path))
                    if path_key != previous_path:
                        line_no = (_diff_start_line(diff_text)
                                   if diff_text else None)
                        file_part = f"{rel}#L{line_no}" if line_no else rel
                        header = f"⏺ fileChange {file_part}"
                    previous_path = path_key
                else:
                    header = "⏺ fileChange"
                    previous_path = None

                if header and rendered_diff:
                    rendered_parts.append(f"{header}\n{rendered_diff}")
                    header_count += 1
                    if structure is not None:
                        structure.diffs.append({"header": header, "diff_text": diff_text})
                elif header:
                    rendered_parts.append(header)
                    header_count += 1
                    if structure is not None:
                        structure.diffs.append({"header": header, "diff_text": ""})
                elif rendered_diff:
                    rendered_parts.append(rendered_diff)

            text = ("\n\n".join(rendered_parts)
                    if rendered_parts else "⏺ fileChange")
            if structure is not None:
                structure.is_diff = True
                structure.is_multi = header_count > 1
                structure.has_inline_body = any(
                    d.get("diff_text") for d in structure.diffs
                )
                if not structure.is_multi:
                    structure.fill_from_text(text, self._split_tool_header)
                else:
                    structure.detail = text.partition("\n")[0].strip()
            return text
        elif name in ("mcpToolCall", "dynamicToolCall"):
            namespace = block.get("server") or block.get("namespace")
            tool = block.get("tool") or "unknown_tool"
            qualified_name = (
                f"{namespace}·{tool}"
                if namespace else tool
            )
            arguments = block.get("arguments", block.get("input", {}))
            detail = _format_tool_arguments(arguments)
            text = f"⏺ {qualified_name} ({detail})" if detail else f"⏺ {qualified_name}"
            if structure is not None:
                structure.set_header(f"⏺ {qualified_name}", f"({detail})" if detail else "")
                structure.fill_from_text(text, self._split_tool_header)
            return text
        elif name == "collabAgentToolCall":
            tool = block.get("tool") or "unknown_tool"
            arguments = {}
            receiver_ids = block.get("receiverThreadIds") or []
            if receiver_ids:
                arguments["agents"] = ", ".join(receiver_ids)
            if block.get("prompt"):
                arguments["prompt"] = block["prompt"]
            if block.get("model"):
                arguments["model"] = block["model"]
            if block.get("reasoningEffort"):
                arguments["reasoning"] = block["reasoningEffort"]
            detail = _format_tool_arguments(arguments)
            text = f"⏺ {tool} ({detail})" if detail else f"⏺ {tool}"
            if structure is not None:
                structure.set_header(f"⏺ {tool}", f"({detail})" if detail else "")
                structure.fill_from_text(text, self._split_tool_header)
            return text
        elif name == "webSearch":
            detail = block.get("query") or ""
            action = block.get("action") or {}
            action_type = action.get("type") if isinstance(action, dict) else None
            if action_type == "search":
                queries = action.get("queries") or []
                detail = action.get("query") or (queries[0] if queries else detail)
            elif action_type == "openPage":
                detail = action.get("url") or detail
            elif action_type == "findInPage":
                pattern = action.get("pattern")
                url = action.get("url")
                if pattern and url:
                    detail = f"'{pattern}' in {url}"
                else:
                    detail = pattern or url or detail
            text = f"⏺ {name} ({detail})" if detail else f"⏺ {name}"
            if structure is not None:
                structure.set_header(f"⏺ {name}", f"({detail})" if detail else "")
                structure.fill_from_text(text, self._split_tool_header)
            return text
        elif name == "imageView":
            path = block.get("path") or ""
            if path:
                cwd = (self.session.agent_thread.cwd
                       if self.session.agent_thread else self.session.cwd) or ""
                _, rel_path = _resolve_rel_path(path, cwd)
                text = f"⏺ {name} {rel_path}"
            else:
                text = f"⏺ {name}"
            if structure is not None:
                structure.fill_from_text(text, self._split_tool_header)
            return text
        elif name == "imageGeneration":
            text = f"⏺ {name}"
            if structure is not None:
                structure.set_header(f"⏺ {name}", "")
                structure.fill_from_text(text, self._split_tool_header)
            return text

        text = f"⏺ {name}" if name else ""
        if structure is not None:
            structure.fill_from_text(text, self._split_tool_header)
        return text


class OpenCodeMessageProcessor(BaseChatMessageProcessor):
    """Render normalized OpenCode server events in the native chat view."""

    _TOOL_FILE_NAMES = ("fileChange", "read", "write", "edit", "apply_patch")
    _RISKY_TOOLS = ("Bash",)

    def __init__(self, session):
        super().__init__(session)
        self._text_trailing_newlines = 1
        self._text_msg_id = None

    @staticmethod
    def _compaction_token_suffix(tokens):
        """Return `` · 12.3k tokens`` for a compaction token usage payload."""
        if not isinstance(tokens, dict):
            return ""
        total = 0.0
        for key in ("input", "output", "reasoning"):
            try:
                total += float(tokens.get(key) or 0)
            except (TypeError, ValueError):
                pass
        cache = tokens.get("cache")
        if isinstance(cache, dict):
            for key in ("read", "write"):
                try:
                    total += float(cache.get(key) or 0)
                except (TypeError, ValueError):
                    pass
        if total <= 0:
            return ""
        if total >= 1000:
            return f" · {total / 1000:.1f}k tokens"
        return f" · {int(total)} tokens"

    def _handle_typed_message(self, message):
        # Unlike Codex, OpenCode's adapter emits only streaming text and does
        # not repeat the completed assistant message.  Render deltas directly.
        if message.type == "compaction_started":
            content = message.content if isinstance(message.content, dict) else {}
            if content.get("manual"):
                self.session.start_loading(text="compacting")
            return

        if message.type == "compaction_ended":
            content = message.content if isinstance(message.content, dict) else {}
            manual = bool(content.get("manual"))
            summary = content.get("summary")
            if not isinstance(summary, str):
                summary = ""
            suffix = self._compaction_token_suffix(content.get("tokens"))
            heading = "Session compacted" if manual else "Context auto-compacted"
            block = f"\n🗜️ **{heading}**{suffix}\n"
            has_summary = bool(summary.strip())
            if has_summary:
                # The summary is the replacement context OpenCode installed.
                # Render it like the TUI (and the web client) so /compact has
                # visible output instead of only a status line.
                block += f"\n{summary.strip()}\n\n"
            self.ensure_trailing_newline()
            self.append_content(block, flush=True)
            if manual:
                self.session.stop_loading()
            self.last_is_tool_call = False
            self._text_trailing_newlines = 2 if has_summary else 1
            self._text_msg_id = None
            return

        if message.type == "compaction_failed":
            content = message.content if isinstance(message.content, dict) else {}
            error = content.get("error") or "Compaction failed"
            if content.get("manual"):
                self.append_error(error)
                self.session.stop_loading()
            else:
                LOG.warning("OpenCode auto-compaction failed: %s", error)
            return

        if message.type == "thinking":
            self.session.start_loading(text="thinking")
            return

        if message.type == "text":
            self.session.start_loading()
            content = message.content if isinstance(message.content, str) else ""
            if not content:
                return
            msg_id = self.message_key(message)
            if (msg_id and self._text_msg_id
                    and msg_id != self._text_msg_id
                    and not self.last_is_tool_call
                    and content[:1] not in ("\n", "\r")):
                # A new assistant message started mid-turn (for example after
                # a background command finished): keep the two messages from
                # running together on one line.
                needed = max(0, 2 - self._text_trailing_newlines)
                if needed:
                    content = "\n" * needed + content
            if msg_id:
                self._text_msg_id = msg_id
            if self.last_is_tool_call:
                if not content.startswith(("\n", "\r\n")):
                    self.append_content("\n")
                self.last_is_tool_call = False
            self.append_content(content, live=True)
            raw = content.replace("\r", "")
            trailing = len(raw) - len(raw.rstrip("\n"))
            if trailing == len(raw):
                self._text_trailing_newlines = min(2, self._text_trailing_newlines + trailing)
            else:
                self._text_trailing_newlines = min(2, trailing)
            return
        if message.type == "tool_use":
            block = message.content if isinstance(message.content, dict) else {}
            key = self.message_key(message)
            if not self.has_tool_call(key) and not self.last_is_tool_call:
                needed = max(0, 2 - self._text_trailing_newlines)
                if needed > 0:
                    self.append_content("\n" * needed, is_tool=True)
            self.last_is_tool_call = True
            self._text_trailing_newlines = 0
            self.append_tool_call(block, key=key)
            if block.get("name") == "fileChange":
                self._record_file_changes(block)
        elif message.type == "shell_finished":
            self._handle_shell_finished(message)
        elif message.type == "error":
            self.append_error(message.content)
            self.session.stop_loading()
        elif message.type == "control_request":
            request = message.content.get("request", {})
            if request.get("subtype") == "can_use_tool":
                tool_name = request.get("tool_name")
                input_data = request.get("input", {})
                request_id = message.content.get("request_id")
                self.session.permission_requests[request_id] = (tool_name, input_data)
                self.session.show_permission_phantom(request_id, tool_name, input_data)
        elif message.type == "thread_started":
            content = message.content if isinstance(message.content, dict) else {}
            session_id = content.get("session_id")
            if session_id:
                LOG.info(f"Agent thread session_id: {session_id}")
                self.session.set_view_session_id(self.session.chat_view, session_id)
        elif message.type == "models_update":
            content = message.content if isinstance(message.content, dict) else {}
            models = content.get("models", [])
            if models:
                self.set_available_models(models)
        elif message.type == "plan_delta":
            content = message.content if isinstance(message.content, str) else ""
            if content:
                self._plan_text += content
                if self.last_is_tool_call and not content.startswith(("\n", "\r\n")):
                    self.append_content("\n")
                self.last_is_tool_call = False
                self.append_content(content, live=True)
                self._plan_text_rendered = True
                raw = content.replace("\r", "")
                trailing = len(raw) - len(raw.rstrip("\n"))
                if trailing == len(raw):
                    self._text_trailing_newlines = min(
                        2, self._text_trailing_newlines + trailing
                    )
                else:
                    self._text_trailing_newlines = min(2, trailing)
        elif message.type == "turn_started":
            self._active_turn_id = None
            self.last_is_tool_call = False
            self._text_trailing_newlines = 1
            self._text_msg_id = None
            self._plan_text = ""
            self._plan_text_rendered = False
            self.session.start_loading()
        elif message.type == "user_message_id":
            content = message.content if isinstance(message.content, dict) else {}
            message_id = content.get("message_id")
            if message_id:
                self._active_turn_id = message_id
                self.session.update_last_prompt_uuid(str(message_id))
        elif message.type == "stop":
            self.append_content("", flush=True)
            self.ensure_trailing_newline()
            self.session.stop_loading()
            self.last_is_tool_call = False
            self._text_trailing_newlines = max(1, self._text_trailing_newlines)
            # Keep ``_text_msg_id``: a background shell often resumes with a
            # new assistant message after the stop, which must get its own
            # paragraph instead of joining the previous one.
            if getattr(self.session, "subagent_tool_keys", None):
                self.session.subagent_tool_keys.clear()
                self.session.model_phantom.update()
            if self._plan_text:
                plan_text = self._plan_text
                self._plan_text = ""
                rendered = self._plan_text_rendered
                self._plan_text_rendered = False
                if not rendered:
                    self.append_content("\n")
                    self.append_content(plan_text)
                    self.append_content("\n", flush=True)
                if (self.session.agent_thread
                        and self.session.agent_thread.anthropic_config.get("plan_mode")):
                    sublime.set_timeout(
                        lambda pt=plan_text: self.session.show_implement_plan_button(pt),
                        0,
                    )
            else:
                self._plan_text_rendered = False
            sublime.set_timeout(self.session.show_file_changes_artifact, 0)

    def apply_shell_finished(self, command, failed=False, output="",
                             tool_key=None, exit_code=None, shell_id=None):
        """Finish the background tool row and build the completion phantom.

        The tool result is stored before the shell actually exits, so the
        completion event carries the real output.  Updating the existing row
        (instead of appending a new one) keeps ``running in background`` from
        becoming a permanent second row.  The returned phantom is the
        TUI-style ``↳ Shell finished`` entry with its own ``show`` link.
        """
        command = str(command or "").strip()
        utils.DIAG.info(
            "apply_shell_finished command=%r failed=%r output_len=%d "
            "tool_key=%r shell_id=%r has_tool_call=%r",
            command, failed, len(output or ""), tool_key, shell_id,
            self.has_tool_call(tool_key) if tool_key else None)
        if not command:
            return None
        if tool_key and self.has_tool_call(tool_key):
            self.append_tool_call({
                "name": "command_execution",
                "command": command,
                "output": output if isinstance(output, str) else "",
                "status": "failed" if failed else "completed",
                "exitCode": exit_code,
            }, key=tool_key)
        key = f"shell_finished:{shell_id}" if shell_id else None
        return self.format_shell_finished(
            command, failed, output=output, key=key)

    def _handle_shell_finished(self, message):
        """Render an OpenCode background shell completion row."""
        content = message.content if isinstance(message.content, dict) else {}
        utils.DIAG.info("handle_shell_finished message.content=%r", content)
        phantom = self.apply_shell_finished(
            content.get("command"),
            bool(content.get("failed")),
            output=content.get("output"),
            tool_key=content.get("tool_key"),
            exit_code=content.get("exit"),
            shell_id=content.get("shell_id"),
        )
        utils.DIAG.info("handle_shell_finished phantom=%r", phantom)
        if not phantom:
            return
        # The phantom-only append inserts the line it anchors on, so the
        # buffer already has the paragraph break before the next message.
        self.last_is_tool_call = False
        self._text_trailing_newlines = 2
        self.append_content("", is_tool=True, tool_phantoms=[phantom])
        utils.DIAG.info("handle_shell_finished append_content done")

    def _format_tool_block(self, block, structure=None):
        name = block.get("name")
        if name == "fileChange":
            changes = block.get("changes", [])
            cwd = (self.session.agent_thread.cwd
                   if self.session.agent_thread else self.session.cwd) or ""
            rendered_parts = []
            header_count = 0
            previous_path = None
            for change in changes:
                path = change.get("path", "")
                diff_text = change.get("diff") or ""
                rendered_diff = self._render_diff_block(diff_text)
                header = None

                if path:
                    abs_path, rel_path = _resolve_rel_path(path, cwd)
                    path_key = os.path.normcase(os.path.normpath(abs_path))
                    if path_key != previous_path:
                        line_no = _diff_start_line(diff_text) if diff_text else None
                        file_part = f"{rel_path}#L{line_no}" if line_no else rel_path
                        header = f"⏺ fileChange {file_part}"
                    previous_path = path_key
                else:
                    header = "⏺ fileChange"
                    previous_path = None

                if header and rendered_diff:
                    rendered_parts.append(f"{header}\n{rendered_diff}")
                    header_count += 1
                    if structure is not None:
                        structure.diffs.append({"header": header, "diff_text": diff_text})
                elif header:
                    rendered_parts.append(header)
                    header_count += 1
                    if structure is not None:
                        structure.diffs.append({"header": header, "diff_text": ""})
                elif rendered_diff:
                    rendered_parts.append(rendered_diff)
            text = ("\n\n".join(rendered_parts)
                    if rendered_parts else "⏺ fileChange")
            if structure is not None:
                structure.is_diff = True
                structure.is_multi = header_count > 1
                structure.has_inline_body = any(
                    d.get("diff_text") for d in structure.diffs
                )
                if not structure.is_multi:
                    structure.fill_from_text(text, self._split_tool_header)
                else:
                    structure.detail = text.partition("\n")[0].strip()
            return text

        input_data = block.get("input") or {}
        if not isinstance(input_data, dict):
            input_data = {"value": input_data}
        lower_name = (name or "tool").lower()
        if lower_name in ("tool", "unknown", ""):
            inferred = ""
            alt = block.get("tool") or block.get("toolName") or block.get("tool_name")
            if isinstance(alt, str) and alt.strip() and alt.strip().lower() not in ("tool", "unknown"):
                inferred = alt.strip()
            if not inferred:
                if "oldString" in input_data or "newString" in input_data:
                    inferred = "edit"
                elif "patch" in input_data or "patchText" in input_data:
                    inferred = "apply_patch"
                elif "command" in input_data or "cmd" in input_data:
                    cmd_val = input_data.get("command") or input_data.get("cmd")
                    if isinstance(cmd_val, str) and cmd_val.strip():
                        inferred = "shell"
                elif input_data.get("filePath") or input_data.get("file_path") or input_data.get("path"):
                    if "content" in input_data and "oldString" not in input_data:
                        inferred = "write"
                    else:
                        inferred = "read"
                elif "query" in input_data and ("url" in input_data or "urls" in input_data):
                    inferred = "webfetch"
                elif "query" in input_data:
                    inferred = "websearch"
                elif "pattern" in input_data:
                    inferred = "glob"
            if inferred:
                name = inferred
                lower_name = str(inferred).lower()
        display_name = {
            "apply_patch": "edit",
            "webfetch": "webFetch",
            "websearch": "webSearch",
        }.get(lower_name, name or "tool")

        path = (
            input_data.get("filePath")
            or input_data.get("file_path")
            or input_data.get("path")
        )
        if path and lower_name in ("read", "write", "edit", "apply_patch", "patch"):
            cwd = (self.session.agent_thread.cwd
                   if self.session.agent_thread else self.session.cwd) or ""
            abs_path, rel_path = _resolve_rel_path(path, cwd)
            line_no = None
            diff_text = ""
            if lower_name == "read":
                line_no = input_data.get("offset") or input_data.get("line")
            elif lower_name in ("edit", "apply_patch", "patch"):
                line_no = (
                    input_data.get("line")
                    or input_data.get("lineNumber")
                    or input_data.get("line_number")
                )
                metadata = block.get("metadata") or {}
                if isinstance(metadata, dict):
                    diff_text = metadata.get("diff") or ""
                    filediff = metadata.get("filediff") or {}
                    if not diff_text and isinstance(filediff, dict):
                        diff_text = filediff.get("patch") or ""
                    if not line_no:
                        line_no = _diff_start_line(diff_text)
                    diff_text = _diff_hunks_only(diff_text)

                if not diff_text:
                    old_str = (input_data.get("oldString")
                               if "oldString" in input_data
                               else input_data.get("old_string"))
                    new_str = (input_data.get("newString")
                               if "newString" in input_data
                               else input_data.get("new_string"))
                    if old_str is not None:
                        try:
                            start_line = int(line_no) if line_no else None
                        except (TypeError, ValueError):
                            start_line = None
                        diff_text = _make_edit_diff(
                            old_str,
                            new_str or "",
                            rel_path,
                            start_line=start_line,
                        ) or ""

            elif lower_name == "write":
                new_str = (
                    input_data.get("content")
                    or input_data.get("contents")
                    or input_data.get("text")
                    or input_data.get("newString")
                    or input_data.get("new_string")
                )
                if new_str:
                    lines = str(new_str).splitlines()
                    diff_text = ("@@ -0,0 +1,{} @@\n".format(len(lines))
                                 + "\n".join("+" + l for l in lines) + "\n")

            # Record file changes for the artifact
            if abs_path and diff_text and lower_name in ("edit", "write", "apply_patch", "patch"):
                self.session.record_file_change(abs_path, rel_path, diff_text)

            try:
                line_no = int(line_no) if line_no is not None else None
            except (TypeError, ValueError):
                line_no = None
            if line_no is not None and line_no > 0:
                rel_path = f"{rel_path}#L{line_no}"
            header = f"⏺ {display_name} {rel_path}"
            rendered_diff = self._render_diff_block(diff_text)
            text = f"{header}\n{rendered_diff}" if rendered_diff else header
            if structure is not None:
                structure.kind = lower_name
                structure.set_header(f"⏺ {display_name}", rel_path)
                if rendered_diff:
                    structure.is_diff = True
                    structure.has_inline_body = True
                    structure.diff_text = diff_text
                    structure.diffs = [{"header": header, "diff_text": diff_text}]
            return text

        is_subagent = lower_name in ("task", "agent", "subagent")
        if is_subagent:
            subagent_type = input_data.get("subagent_type") or input_data.get("agent") or ""
            description = input_data.get("description") or ""
            parts = [str(display_name)]
            if subagent_type:
                parts.append(str(subagent_type))
            if description:
                parts.append(str(description))
            session_id = self._subagent_session_id(block)
            if session_id:
                parts.append(str(session_id))
            text = f"⏺ {' '.join(parts)}"
            if structure is not None:
                structure.kind = "subagent"
                structure.fill_from_text(text, self._split_tool_header)
            return text

        command_block = block
        if str(block.get("name") or "").lower() != lower_name:
            command_block = {**block, "name": name}
        command = self._command_text(command_block)
        if command:
            heading = self._command_heading(command_block)
            preview = self._command_preview(command)
            text = f"{heading} {preview}".strip()
            if structure is not None:
                structure.kind = "command"
                structure.set_header(heading, preview, detail=command.rstrip("\n"))
            return text

        detail = block.get("title") or _format_tool_arguments(input_data)
        if block.get("status") == "error" and block.get("error"):
            detail = detail or str(block["error"])
        text = f"⏺ {display_name} ({detail})" if detail else f"⏺ {display_name}"
        if structure is not None:
            structure.kind = lower_name
            structure.fill_from_text(text, self._split_tool_header)
        return text


class PiMessageProcessor(BaseChatMessageProcessor):
    _TOOL_FILE_NAMES = ("read", "edit", "write")
    _RISKY_TOOLS = ("Bash",)

    def is_risky_tool(self, tool_name: str) -> bool:
        """Return True if tool requires user confirmation under allow-edit mode."""
        if tool_name.startswith("Extension UI:") or tool_name.startswith("extension_ui_"):
            return True
        return super().is_risky_tool(tool_name)

    def __init__(self, session):
        super().__init__(session)
        self._in_plan = False
        self._plan_text = ""
        self._text_buffer = ""
        self._pending_edits = {}  # toolCallId -> block dict

    def _handle_typed_message(self, message):
        if message.type == "text_delta":
            self.session.start_loading()
            if self.last_is_tool_call:
                self.append_content("\n")
                self.last_is_tool_call = False
            
            self._text_buffer += message.content

            while True:
                if not self._in_plan:
                    if "<proposed_plan>" in self._text_buffer:
                        parts = self._text_buffer.split("<proposed_plan>", 1)
                        if parts[0]:
                            self.append_content(parts[0], live=True)
                        self._in_plan = True
                        self._text_buffer = parts[1]
                        self.append_content("\n\n**Proposed Plan**\n\n")
                    else:
                        flush_index = len(self._text_buffer)
                        for i in range(len(self._text_buffer)):
                            if "<proposed_plan>".startswith(self._text_buffer[i:]):
                                flush_index = i
                                break
                        if flush_index > 0:
                            self.append_content(self._text_buffer[:flush_index], live=True)
                            self._text_buffer = self._text_buffer[flush_index:]
                        break
                else:
                    if "</proposed_plan>" in self._text_buffer:
                        parts = self._text_buffer.split("</proposed_plan>", 1)
                        self._plan_text += parts[0]
                        self.append_content(parts[0], live=True)
                        self._in_plan = False
                        self._text_buffer = parts[1]
                    else:
                        flush_index = len(self._text_buffer)
                        for i in range(len(self._text_buffer)):
                            if "</proposed_plan>".startswith(self._text_buffer[i:]):
                                flush_index = i
                                break
                        if flush_index > 0:
                            self._plan_text += self._text_buffer[:flush_index]
                            self.append_content(self._text_buffer[:flush_index], live=True)
                            self._text_buffer = self._text_buffer[flush_index:]
                        break
            return
            
        if message.type == "text_end":
            if self._text_buffer:
                if self._in_plan:
                    self._plan_text += self._text_buffer
                self.append_content(self._text_buffer, live=True)
                self._text_buffer = ""
            return
            
        if message.type in ("thinking_start", "thinking_delta"):
            self.session.start_loading(text="thinking")
            return

        if message.type == "assistant":
            self.session.start_loading()

            # Extract tool blocks
            if hasattr(message, "content"):
                for block in message.content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        if block.get("name") in ("edit", "write"):
                            tool_call_id = block.get("id")
                            if tool_call_id:
                                args = block.get("arguments", {})
                                self._pending_edits[tool_call_id] = {
                                    "file_path": args.get("path") or args.get("file_path") or "",
                                    "old_text": args.get("oldText") or args.get("old_text"),
                                    "new_text": args.get("newText") or args.get("content") or args.get("new_text"),
                                    "edits": args.get("edits"),
                                }

                            # defer to toolResult
                        else:
                            self.append_tool_call(
                                block,
                                key=block.get("id") or self.message_key(message),
                            )

        elif message.type == "system":
            if hasattr(message, "content") and isinstance(message.content, dict):
                session_id = message.content.get("session_id")
                if session_id and message.content.get("subtype") == "init":
                    LOG.info(f"system session_id: {session_id}")
                    # Store session_id in view settings for persistence across restarts
                    self.session.set_view_session_id(self.session.chat_view, session_id)



        elif message.type == "extension_ui_request":
            request = message.content
            method = request.get("method")
            request_id = request.get("id")
            title = request.get("title", method)
            
            if method == "confirm" and isinstance(title, str) and title.startswith("Tool Permission: "):
                try:
                    import json
                    parsed_msg = json.loads(request.get("message", "{}"))
                    real_tool_name = parsed_msg.get("toolName")
                    real_input = parsed_msg.get("input", {})
                    if real_tool_name:
                        # Use the real tool name for the phantom so it formats correctly
                        # Capitalize to match Claude/Codex conventions like "Bash", "Edit"
                        display_tool_name = real_tool_name.capitalize() if real_tool_name in ("bash", "read", "write", "edit", "grep", "find", "ls") else real_tool_name
                        self.session.permission_requests[request_id] = ("termchat_tool_permission", request)
                        self.session.show_permission_phantom(request_id, display_tool_name, real_input)
                        return
                except Exception as e:
                    LOG.error(f"Failed to parse termchat tool permission request: {e}")

            tool_name = f"extension_ui_{method}"
            self.session.permission_requests[request_id] = (tool_name, request)
            
            # Show in permission phantom
            self.session.show_permission_phantom(request_id, f"Extension UI: {title}", request)

        elif message.type == "error":
            self.append_error(message.content)
            self.session.stop_loading()

        elif message.type == "result":
            # Flush markdown formatter buffer
            self.append_content("", flush=True)
            self.ensure_trailing_newline()
            # Stop loading on turn completion (heuristic)
            self.session.stop_loading()
            # Show collapsed file-changes artifact after the turn output settles
            sublime.set_timeout(self.session.show_file_changes_artifact, 0)

        elif message.type == "message_end":
            data = message.content if isinstance(message.content, dict) else {}
            if data.get("role") == "toolResult" and data.get("toolName") in ("edit", "write") and not data.get("isError"):
                tool_call_id = data.get("toolCallId")
                pending = self._pending_edits.pop(tool_call_id, None) if tool_call_id else None
                rendered = self._format_pi_edit_result(pending, data)
                if rendered:
                    self.last_is_tool_call = True
                    self.append_content("\n" + rendered + "\n", is_tool=True)

            if isinstance(message.content, dict) and message.content.get("customType") == "proposed-plan":
                plan_text = message.content.get("content", "")
                if plan_text.startswith("**Proposed Plan**\n\n"):
                    plan_text = plan_text[len("**Proposed Plan**\n\n"):]
                
                plan_text = plan_text.strip()
                sublime.set_timeout(lambda pt=plan_text: self.session.show_implement_plan_button(pt, tool_name="ImplementPlan"), 0)

        elif message.type == "control_response":
            if hasattr(message, "content") and isinstance(message.content, dict):
                response_outer = message.content.get("response", {})
                if response_outer.get("subtype") == "success":
                    response_data = response_outer.get("response", {})
                    models = response_data.get("models", [])
                    if models:
                        self.set_available_models(models)

        elif message.type == "models_update":
            if hasattr(message, "content") and isinstance(message.content, dict):
                models = message.content.get("models", [])
                if models:
                    self.set_available_models(models)

    def _format_tool_block(self, block, structure=None):
        name = block.get("name")
        input_data = block.get("arguments", {})

        if name == "read":
            file_path = input_data.get("file_path") or input_data.get("path") or ""
            if file_path:
                try:
                    if os.path.isabs(file_path):
                        rel_path = os.path.relpath(file_path, self.session.agent_thread.cwd)
                    else:
                        rel_path = file_path
                except Exception:
                    rel_path = file_path

                offset = input_data.get("offset")
                limit = input_data.get("limit")

                if offset is not None and limit is not None:
                    start_line = offset
                    end_line = offset + limit - 1
                    return f"⏺ read {rel_path}#L{start_line}-L{end_line}"
                elif offset is not None:
                    return f"⏺ read {rel_path}#L{offset}"
                else:
                    return f"⏺ read {rel_path}"

        elif name in ("agent", "task"):
            description = input_data.get("description", "")
            subagent_type = input_data.get("subagent_type", "")

            parts = [f"⏺ {name}"]
            if subagent_type:
                parts.append(subagent_type)
            if description:
                parts.append(description)

            return " ".join(parts)

        elif name == "bash":
            command = input_data.get("command", "")
            if command:
                lines = command.rstrip().splitlines()
                if len(lines) > 1:
                    first_line = lines[0]
                    indented_rest = "\n".join("    " + line for line in lines[1:])
                    return f"⏺ bash {first_line}\n\n{indented_rest}\n"
                elif len(lines) == 1:
                    return f"⏺ bash {lines[0]}"

        elif name in ("grep", "find", "ls"):
            pattern = input_data.get("pattern") or input_data.get("query") or input_data.get("path") or input_data.get("regex") or ""
            return f"⏺ {name} {pattern}".strip()

        return f"⏺ {name}" if name else ""

    def _format_pi_edit_result(self, pending, tool_result_data):
        """Render edit/write header + diff from Pi toolResult data and record artifact."""
        file_path = (pending or {}).get("file_path", "")
        if not file_path:
            return None
        cwd = self.session.agent_thread.cwd if self.session.agent_thread else ""
        abs_path, rel_path = _resolve_rel_path(file_path, cwd)

        name = tool_result_data.get("toolName", "edit")
        first_line = (tool_result_data.get("details") or {}).get("firstChangedLine")
        header = f"⏺ {name} {rel_path}#L{first_line}" if first_line else f"⏺ {name} {rel_path}"

        diff_text = None
        if name == "edit" and pending:
            edits = pending.get("edits")
            old_text = pending.get("old_text")
            new_text = pending.get("new_text")

            if edits and isinstance(edits, list):
                diffs = [d for e in edits
                         if (d := _make_edit_diff(e.get("oldText") or "", e.get("newText") or "", rel_path,
                                                  start_line=first_line))]
                if diffs:
                    diff_text = "\n".join(diffs)
            elif old_text is not None:
                diff_text = _make_edit_diff(old_text, new_text or "", rel_path, start_line=first_line)

        elif name == "write" and pending:
            new_text = pending.get("new_text") or ""
            if new_text:
                lines = new_text.splitlines()
                diff_text = ("@@ -0,0 +1,{} @@\n".format(len(lines))
                             + "\n".join("+" + l for l in lines) + "\n")

        # Record file changes for the artifact
        if abs_path:
            self.session.record_file_change(abs_path, rel_path, diff_text)

        if diff_text:
            return header + "\n" + self._render_diff_block(diff_text)
        return header


class AntigravityMessageProcessor(BaseChatMessageProcessor):
    """Render normalized Antigravity CLI events in the native chat view."""

    _TOOL_DISPLAY_NAMES = {
        "run_command": "RunCommand",
        "view_file": "ViewFile",
        "read_file": "ReadFile",
        "write_to_file": "WriteFile",
        "write_file": "WriteFile",
        "replace_file_content": "EditFile",
        "edit_file": "EditFile",
        "multi_replace_file_content": "EditFile",
        "list_dir": "ListDir",
        "grep_search": "GrepSearch",
        "search_web": "SearchWeb",
        "read_url_content": "ReadUrl",
        "browser_subagent": "BrowserSubagent",
        "ask_question": "AskQuestion",
        "schedule": "Schedule",
        "manage_task": "ManageTask",
        "generate_image": "GenerateImage",
    }

    _TOOL_FILE_NAMES = (
        "Read", "Edit", "Write",
        "view_file", "ViewFile",
        "read_file", "ReadFile",
        "write_to_file", "WriteFile",
        "write_file",
        "replace_file_content", "EditFile",
        "edit_file",
        "multi_replace_file_content",
        "sed_file",
    )
    _RISKY_TOOLS = ("run_command", "RunCommand")

    @classmethod
    def get_default_think_presets(cls) -> list:
        return [
            {"value": "low", "text": "low", "description": "Fast responses with light reasoning"},
            {"value": "medium", "text": "medium", "description": "Balanced speed and reasoning depth (standard)"},
            {"value": "high", "text": "high", "description": "Deep reasoning for complex coding tasks"},
        ]

    @classmethod
    def get_think_presets(cls, active_model: dict = None) -> list:
        if active_model:
            model_efforts = (
                active_model.get("supportedReasoningEfforts")
                or active_model.get("supportedEffortLevels")
            )
            if model_efforts:
                return cls.normalize_think_efforts(model_efforts)
            if active_model.get("supportsEffort"):
                return cls.get_default_think_presets()
            return []
        return cls.get_default_think_presets()
    def __init__(self, session):
        super().__init__(session)
        self._plan_path = None
        self._plan_summary = ""
        self._synced_artifact_mtimes = {}
        self._pending_tool_params = {}

    USER_ARTIFACT_SPECS = {
        "implementation_plan.md": {"icon": "📋", "can_execute": True, "label": "Plan"},
        "plan.md":                {"icon": "📋", "can_execute": True, "label": "Plan"},
        "walkthrough.md":          {"icon": "📖", "can_execute": False, "label": "Walkthrough"},
    }

    @classmethod
    def _is_user_artifact(cls, block):
        name = block.get("name") or ""
        if name not in ("write_to_file", "replace_file_content", "multi_replace_file_content"):
            return False
        params = block.get("parameters") or {}
        metadata = params.get("ArtifactMetadata") or {}
        target = params.get("TargetFile") or params.get("path") or ""
        basename = os.path.basename(target).lower() if target else ""
        if basename in cls.USER_ARTIFACT_SPECS:
            return True
        if metadata.get("RequestFeedback") is True or metadata.get("UserFacing") is True:
            return True
        low = target.lower()
        if "implementation_plan.md" in low or "walkthrough.md" in low or "plan.md" in low:
            return True
        return False

    _is_plan_artifact = _is_user_artifact

    @staticmethod
    def _extract_artifact_title(content):
        """Extract a short title/summary from markdown content (e.g. first # Heading)."""
        if not content:
            return ""
        for line in content.splitlines():
            s = line.strip()
            if s.startswith("#"):
                clean = s.lstrip("#").strip()
                if clean.startswith("[") and clean.endswith("]"):
                    clean = clean[1:-1].strip()
                if clean:
                    return clean
        return ""

    @staticmethod
    def _extract_plan_content(block):
        params = block.get("parameters") or {}
        code = params.get("CodeContent")
        if code:
            return code
        replacement = params.get("ReplacementContent")
        if replacement:
            return replacement
        target = params.get("TargetFile") or params.get("path") or ""
        if target and os.path.isfile(target):
            try:
                with open(target, "r", encoding="utf-8", errors="replace") as f:
                    return f.read()
            except Exception:
                pass
        return ""

    def _sync_user_artifacts(self, hint_path=None, content_fallback=None):
        """Dual-trigger synchronization of user artifacts (implementation_plan.md, walkthrough.md)."""
        s_id = getattr(getattr(self.session, "agent_thread", None), "session_id", None)
        if not s_id and hasattr(self.session, "chat_view") and hasattr(self.session.chat_view, "settings"):
            s_id = self.session.chat_view.settings().get("chatview_session_id")

        brain_dirs = []
        if s_id:
            for base in (
                os.path.expanduser("~/.gemini/antigravity-cli/brain"),
                os.path.expanduser("~/.gemini/antigravity-ide/brain"),
                os.path.expanduser("~/.gemini/antigravity/brain"),
            ):
                d = os.path.join(base, s_id)
                if os.path.isdir(d):
                    brain_dirs.append(d)

        paths_to_check = []
        if hint_path:
            paths_to_check.append(hint_path)

        for bdir in brain_dirs:
            try:
                for fname in os.listdir(bdir):
                    if fname.lower().endswith(".md"):
                        fpath = os.path.join(bdir, fname)
                        if fpath not in paths_to_check:
                            paths_to_check.append(fpath)
            except OSError:
                pass
            for name in self.USER_ARTIFACT_SPECS:
                fpath = os.path.join(bdir, name)
                if fpath not in paths_to_check:
                    paths_to_check.append(fpath)

        for path in paths_to_check:
            basename = os.path.basename(path).lower()
            spec = self.USER_ARTIFACT_SPECS.get(basename)
            if not spec:
                if "implementation_plan" in basename or "plan" in basename:
                    spec = self.USER_ARTIFACT_SPECS["implementation_plan.md"]
                elif "walkthrough" in basename:
                    spec = self.USER_ARTIFACT_SPECS["walkthrough.md"]
                else:
                    spec = {"icon": "📑", "can_execute": False, "label": "Artifact"}

            mtime = 0
            if os.path.isfile(path):
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    mtime = 0

            last_mtime = self._synced_artifact_mtimes.get(path)
            if last_mtime is not None and mtime > 0 and mtime <= last_mtime and path != hint_path:
                continue

            content = ""
            if os.path.isfile(path):
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                except Exception:
                    content = ""
            if not content and content_fallback and path == hint_path:
                content = content_fallback

            if content:
                if mtime > 0:
                    self._synced_artifact_mtimes[path] = mtime
                summary = self._extract_artifact_title(content)
                can_exec = spec.get("can_execute", False)
                if can_exec:
                    self._plan_path = path
                    self._plan_summary = summary
                    self._plan_text = ""
                if hasattr(self.session, "record_plan_artifact"):
                    self.session.record_plan_artifact(
                        path,
                        os.path.basename(path),
                        summary=summary,
                        content="",
                        can_execute=can_exec,
                        icon=spec.get("icon"),
                    )


    def _handle_typed_message(self, message):
        if message.type == "thinking":
            self.session.start_loading(text="thinking")
            return

        if message.type in ("text", "text_delta"):
            self.session.start_loading()
            if self.last_is_tool_call:
                self.append_content("\n")
                self.last_is_tool_call = False
            content = message.content if isinstance(message.content, str) else ""
            if content:
                self.append_content(content, live=True)
            return

        if message.type == "tool_use":
            self.session.start_loading()
            content_data = message.content if isinstance(message.content, dict) else {}
            state = content_data.get("state")
            if not state and isinstance(message.raw_data, dict):
                state = message.raw_data.get("state")

            tool_id = str(content_data.get("id") or getattr(message, "msg_id", "") or "")

            if state == "ACTIVE":
                if tool_id and content_data.get("parameters"):
                    self._pending_tool_params[tool_id] = content_data["parameters"]
                # Only command-like calls get a running row, and only when the
                # id is stable. Falling back to the turn id would merge every
                # command of the same turn into one row.
                stable_id = content_data.get("id")
                if (
                    stable_id
                    and stable_id != getattr(message, "msg_id", None)
                    and content_data.get("status") == "running"
                    and self._command_text(content_data)
                ):
                    self.session.start_loading()
                    self.append_tool_call(content_data, key=str(stable_id))
                return

            if tool_id and not content_data.get("parameters"):
                cached = self._pending_tool_params.pop(tool_id, None)
                if cached:
                    content_data["parameters"] = cached
            elif tool_id:
                self._pending_tool_params.pop(tool_id, None)

            call_key = content_data.get("id")
            if not call_key or call_key == getattr(message, "msg_id", None):
                call_key = None
            self.append_tool_call(
                content_data, key=str(call_key) if call_key else None)

            if self._is_user_artifact(content_data):
                params = content_data.get("parameters") or {}
                target = params.get("TargetFile") or params.get("path") or ""
                code_fallback = params.get("CodeContent") or params.get("ReplacementContent")
                self._sync_user_artifacts(hint_path=target, content_fallback=code_fallback)
            self._record_tool_file_change(content_data)

        elif message.type == "system":
            content = message.content if isinstance(message.content, dict) else {}
            session_id = content.get("session_id")
            if session_id:
                LOG.info(f"Antigravity session_id: {session_id}")
                self.session.set_view_session_id(self.session.chat_view, session_id)

        elif message.type in ("control_request", "tool_approval"):
            content = message.content if isinstance(message.content, dict) else {}
            if message.type == "control_request":
                request = content.get("request", {})
                tool_name = request.get("tool_name") or content.get("tool_name")
                input_data = request.get("input", {}) or content.get("parameters") or {}
                request_id = content.get("request_id")
            else:
                request_id = content.get("request_id")
                tool_name = content.get("tool_name")
                input_data = content.get("parameters") or {}

            if request_id:
                display_name = self._TOOL_DISPLAY_NAMES.get(
                    tool_name, "".join(part.capitalize() for part in (tool_name or "").split("_"))
                )
                self.session.permission_requests[request_id] = (tool_name, input_data)
                self.session.show_permission_phantom(request_id, display_name, input_data)

        elif message.type == "plan_delta":
            content = message.content if isinstance(message.content, str) else ""
            if content:
                self._plan_text += content

        elif message.type == "error":
            self.append_error(message.content)
            self.session.stop_loading()

        elif message.type == "models_update":
            if hasattr(message, "content") and isinstance(message.content, dict):
                models = message.content.get("models", [])
                if models:
                    self.set_available_models(models)

        elif message.type in ("stop", "result"):
            self.append_content("", flush=True)
            self.ensure_trailing_newline()
            self.session.stop_loading()
            self.last_is_tool_call = False
            self._pending_tool_params.clear()
            if getattr(self.session, "subagent_tool_keys", None):
                self.session.subagent_tool_keys.clear()
                self.session.model_phantom.update()

            # Dual-trigger final sync: scan and update any artifacts on disk
            self._sync_user_artifacts()

            plan_path = getattr(self, "_plan_path", None)
            plan_summary = getattr(self, "_plan_summary", "") or (self._plan_text.split("\n")[0] if self._plan_text else "")
            plan_text = self._plan_text
            self._plan_text = ""
            self._plan_path = None
            self._plan_summary = ""

            kwargs = {"tool_name": "AntigravityImplementPlan"}
            if plan_path:
                kwargs["plan_path"] = plan_path
                if plan_summary:
                    kwargs["summary"] = plan_summary

            if (plan_path or plan_text) and (self.session.agent_thread
                    and self.session.agent_thread.anthropic_config.get("plan_mode")):
                sublime.set_timeout(
                    lambda pt=plan_text, kw=kwargs: self.session.show_implement_plan_button(
                        pt, **kw
                    ),
                    0,
                )
            sublime.set_timeout(self.session.show_file_changes_artifact, 0)

    def _format_tool_block(self, block, structure=None):
        name = block.get("name") or "tool"
        display_name = self._TOOL_DISPLAY_NAMES.get(
            name, "".join(part.capitalize() for part in name.split("_"))
        )
        params = block.get("parameters") or {}
        cwd = (self.session.agent_thread.cwd
               if self.session.agent_thread else self.session.cwd) or ""

        if self._is_user_artifact(block):
            params = block.get("parameters") or {}
            target = params.get("TargetFile") or params.get("path") or "artifact.md"
            name = os.path.basename(target) or "artifact.md"
            metadata = params.get("ArtifactMetadata") or {}
            summary = metadata.get("Summary")
            if summary:
                first_summary = summary.strip().splitlines()[0]
                return f"⏺ CreateArtifact {name} ({first_summary})"
            return f"⏺ CreateArtifact {name}"

        if name in ("view_file", "read_file"):
            file_path = (
                params.get("Path")
                or params.get("AbsolutePath")
                or params.get("path")
                or ""
            )
            _, rel_p = _resolve_rel_path(file_path, cwd)
            start_line = params.get("StartLine")
            end_line = params.get("EndLine")
            if start_line is not None and end_line is not None:
                return f"⏺ {display_name} {rel_p}#L{start_line}-L{end_line}"
            elif start_line is not None:
                return f"⏺ {display_name} {rel_p}#L{start_line}"
            return f"⏺ {display_name} {rel_p}"

        if name in (
            "write_to_file", "write_file", "replace_file_content",
            "multi_replace_file_content", "edit_file"
        ):
            tgt = (
                params.get("TargetFile")
                or params.get("Path")
                or params.get("path")
                or ""
            )
            _, rel_p = _resolve_rel_path(tgt, cwd)
            return f"⏺ {display_name} {rel_p}"

        if name == "list_dir":
            dp = params.get("DirectoryPath") or params.get("path") or ""
            _, rel_p = _resolve_rel_path(dp, cwd)
            return f"⏺ {display_name} {rel_p}"

        if name == "grep_search":
            query = params.get("Query") or ""
            sp = params.get("SearchPath") or ""
            _, rel_p = _resolve_rel_path(sp, cwd)
            return f"⏺ {display_name} ({query}) in {rel_p}"

        if name == "search_web":
            query = params.get("query") or ""
            if query:
                lines = query.strip().splitlines()
                if len(lines) > 1:
                    first_line = lines[0]
                    indented_rest = "\n".join("    " + line for line in lines[1:])
                    return f"⏺ {display_name} ({first_line})\n\n{indented_rest}\n"
                return f"⏺ {display_name} ({lines[0]})"
            return f"⏺ {display_name}"

        if name == "read_url_content":
            url = params.get("Url") or ""
            return f"⏺ {display_name} ({url})"

        return f"⏺ {display_name}"

    def _record_tool_file_change(self, block):
        if self._is_user_artifact(block):
            return
        if block.get("error"):
            return
        name = block.get("name")
        params = block.get("parameters") or {}
        cwd = (self.session.agent_thread.cwd
               if self.session.agent_thread else self.session.cwd) or ""

        if name == "write_to_file":
            target = params.get("TargetFile") or ""
            if target:
                abs_p, rel_p = _resolve_rel_path(target, cwd)
                content = params.get("CodeContent") or ""
                lines = content.splitlines() if content else []
                if lines:
                    diff_text = ("@@ -0,0 +1,{} @@\n".format(len(lines))
                                 + "\n".join("+" + l for l in lines) + "\n")
                else:
                    diff_text = None
                self.session.record_file_change(abs_p, rel_p, diff_text)
        elif name == "replace_file_content":
            target = params.get("TargetFile") or ""
            if target:
                abs_p, rel_p = _resolve_rel_path(target, cwd)
                old_str = params.get("TargetContent") or ""
                new_str = params.get("ReplacementContent") or ""
                start_line = params.get("StartLine")
                diff_text = _make_edit_diff(old_str, new_str, rel_p, start_line)
                self.session.record_file_change(abs_p, rel_p, diff_text)
        elif name in ("multi_replace_file_content", "sed_file"):
            target = params.get("TargetFile") or params.get("path") or ""
            if target:
                abs_p, rel_p = _resolve_rel_path(target, cwd)
                self.session.record_file_change(abs_p, rel_p, None)


PROCESSOR_MAP = {
    "claude": ClaudeMessageProcessor,
    "codex": CodexMessageProcessor,
    "opencode": OpenCodeMessageProcessor,
    "pi": PiMessageProcessor,
    "antigravity": AntigravityMessageProcessor,
}


