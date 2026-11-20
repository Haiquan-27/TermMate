"""Collapsed tool rows rendered as phantoms.

Long tool results are not written into the chat buffer by default. Instead a
single phantom row is anchored below the tool header:

    ⏺ Bash npm test
    ⎿ 42 lines success                            [show]

Command tool calls (``command_execution`` / ``run_command``) are phantom-only:
the command text never enters the buffer while collapsed. A single ``show``
button reveals both blocks:

    ⏺ command (npm test -- --watch …)
    ⎿ 42 lines success                            [show]

    Command
    ```bash
    npm test -- --watch
    ```

    Return
    ```text
    ...
    ```

Clicking ``show`` inserts both blocks right after the anchor line and toggles
the link to ``hide``.

Background shells get a second, standalone row when they finish (the TUI
reports them as their own message):

    ↳ Shell finished · npm run dev                  [show]

Its ``show`` link reveals the original command and the output the shell
produced after its tool call had already returned.
"""

import html
import logging

import sublime

from .utils import format_fenced_output

LOG = logging.getLogger("TermMate")
DIAG = logging.getLogger("TermMate.Diag")

#: Status row text for command calls that have not finished yet.
STATUS_LABELS = {
    "running": "running…",
    "background": "running in background",
    "interrupted": "interrupted",
}


class ToolOutputManager:
    """Own collapsed tool rows and their show/hide phantoms."""

    REGION_PREFIX = "term_chat_tool_output_"

    def __init__(self, view, get_input_start=None, set_input_start=None):
        self.view = view
        self._get_input_start = get_input_start
        self._set_input_start = set_input_start
        self.phantom_set = sublime.PhantomSet(view, "chatview_tool_output")
        self.entries = []
        self.next_id = 0

    # ---------------------------------------------------------------- public

    def add(self, anchor, summary, detail, key=None):
        """Register a collapsed output row anchored at ``anchor`` (a Region)."""
        if not detail:
            return None
        if key is None:
            key = self.REGION_PREFIX + str(self.next_id)
            self.next_id += 1
        region_key = key + "_anchor"
        self.view.add_regions(region_key, [anchor], flags=sublime.HIDDEN)
        entry = {
            "key": key,
            "region_key": region_key,
            "detail_key": key + "_detail",
            "kind": "output",
            "summary": summary,
            "detail": detail,
            "expanded": False,
        }
        self.entries.append(entry)
        self._update()
        return entry

    def add_tool_call(self, anchor, command, output=None, key=None,
                      status=None, edit=None):
        """Register or update a command tool call as one composite phantom.

        A single phantom holds both the command preview row and the output
        summary row. Keeping them in one phantom is what makes their order
        stable: phantoms anchored at the same point have no defined order.

        ``command`` is ``{"heading", "summary", "detail"}``; ``output`` is
        ``{"summary", "detail"}`` or ``None``; ``status`` is ``"running"``,
        ``"background"`` or ``None``.

        When ``key`` already exists the existing row is updated in place
        instead of creating a second phantom. That is how ``running…`` turns
        into ``N lines success`` without the row moving on screen.
        """
        command = command or {}
        if not command.get("detail"):
            return None
        if key is not None:
            existing = self.find(key)
            if existing is not None:
                return self._update_tool_call(
                    existing, command, output, status, edit)
        if key is None:
            key = self.REGION_PREFIX + str(self.next_id)
            self.next_id += 1
        region_key = key + "_anchor"
        self.view.add_regions(region_key, [anchor], flags=sublime.HIDDEN)
        entry = {
            "key": key,
            "region_key": region_key,
            "detail_key": key + "_detail",
            "kind": "tool_call",
            "command": {
                "heading": command.get("heading") or "⏺ command",
                "summary": command.get("summary") or "",
                "detail": command["detail"],
            },
            "output": None,
            "status": status,
            "expanded": False,
        }
        if output:
            summary = output.get("summary") or ""
            detail = output.get("detail") or ""
            if detail:
                entry["output"] = {"summary": summary, "detail": detail}
            elif summary:
                entry["output"] = {"summary": summary, "detail": ""}
        self.entries.append(entry)
        self._update()
        return entry

    def add_shell_finished(self, anchor, command, output="", failed=False,
                           key=None, summary=None, edit=None):
        """Register a standalone ``↳ Shell finished`` row for a background job.

        Mirrors the TUI, which reports a completed background shell as its
        own message instead of as a second line of the tool call.  The row
        has a single ``show`` link that reveals the original command and the
        output the shell produced after the tool call had already returned.
        """
        command = str(command or "").strip()
        if not command:
            DIAG.info("add_shell_finished ignored empty command key=%r", key)
            return None
        if key is not None:
            existing = self.find(key)
            if existing is not None:
                DIAG.info("add_shell_finished update key=%r output_len=%d",
                          key, len(output or ""))
                return self._update_shell_finished(
                    existing, command, output, failed, summary, edit=edit)
        if key is None:
            key = self.REGION_PREFIX + str(self.next_id)
            self.next_id += 1
        region_key = key + "_anchor"
        self.view.add_regions(region_key, [anchor], flags=sublime.HIDDEN)
        entry = {
            "key": key,
            "region_key": region_key,
            "detail_key": key + "_detail",
            "kind": "shell_finished",
            "label": "Shell failed" if failed else "Shell finished",
            "summary": summary or "",
            "command": {"detail": command.rstrip("\n")},
            "output": {"summary": "", "detail": output or ""},
            "expanded": False,
        }
        self.entries.append(entry)
        self._update()
        DIAG.info(
            "add_shell_finished created key=%r anchor=%d summary=%r "
            "output_len=%d entries=%d",
            key, anchor.begin(), entry["summary"], len(output or ""),
            len(self.entries))
        return entry

    def find(self, key):
        """Return the entry registered under ``key``, if any."""
        for entry in self.entries:
            if entry["key"] == key:
                return entry
        return None

    def finish_running(self):
        """Mark commands still running when their turn ended as interrupted."""
        changed = False
        for entry in self.entries:
            if entry["kind"] == "tool_call" and entry.get("status") == "running":
                entry["status"] = "interrupted"
                changed = True
        if changed:
            self._update()

    def toggle(self, edit, key, section=None):
        """Expand or collapse the row identified by ``key``.

        ``section`` is accepted for compatibility with older phantom links;
        composite command calls share a single show/hide state.
        """
        entry = self._find(key)
        if entry is None:
            return
        if entry["kind"] in ("tool_call", "shell_finished"):
            self._toggle_tool_call(edit, entry)
            return
        if entry["expanded"]:
            self._collapse(edit, entry)
        else:
            self._expand(edit, entry)
        self._update()

    def truncate(self, cut_point):
        """Drop rows that start at or after ``cut_point`` (rewind support)."""
        kept = []
        for entry in self.entries:
            regions = self.view.get_regions(entry["region_key"])
            if regions and regions[0].begin() < cut_point:
                detail = self.view.get_regions(entry["detail_key"])
                if detail and detail[0].end() <= cut_point:
                    entry["expanded"] = False
                kept.append(entry)
            else:
                self.view.erase_regions(entry["region_key"])
                self.view.erase_regions(entry["detail_key"])
        self.entries = kept
        self._update()

    def clear(self):
        for entry in self.entries:
            self.view.erase_regions(entry["region_key"])
            self.view.erase_regions(entry["detail_key"])
        self.entries = []
        self.phantom_set.update([])

    # --------------------------------------------------------------- internal

    def _find(self, key):
        return self.find(key)

    def _update_tool_call(self, entry, command, output, status, edit):
        """Apply a later event to an existing command row in place."""
        if command.get("detail"):
            entry["command"] = {
                "heading": command.get("heading") or entry["command"]["heading"],
                "summary": command.get("summary") or "",
                "detail": command["detail"],
            }
        if output is not None:
            summary = output.get("summary") or ""
            detail = output.get("detail") or ""
            if detail:
                entry["output"] = {"summary": summary, "detail": detail}
            elif summary and not entry.get("output"):
                entry["output"] = {"summary": summary, "detail": ""}
        entry["status"] = status
        if edit is not None and entry["expanded"]:
            self._render_tool_call_detail(edit, entry)
        self._update()
        return entry

    def _update_shell_finished(self, entry, command, output, failed, summary,
                               edit=None):
        """Apply a repeated completion event to an existing shell row."""
        entry["label"] = "Shell failed" if failed else "Shell finished"
        entry["summary"] = summary or entry.get("summary") or ""
        entry["command"] = {"detail": command.rstrip("\n")}
        entry["output"] = {"summary": "", "detail": output or ""}
        if edit is not None and entry["expanded"]:
            self._render_tool_call_detail(edit, entry)
        self._update()
        return entry

    def _anchor(self, entry):
        regions = self.view.get_regions(entry["region_key"])
        return regions[0] if regions else None

    def _expand(self, edit, entry):
        anchor = self._anchor(entry)
        if anchor is None:
            return
        text = format_fenced_output(entry["detail"])
        if not text:
            return
        insert_at = self.view.full_line(anchor).end()
        # Capture the input anchor before editing: regions shift with the
        # insert, so adding the delta afterwards would double-count.
        start_before = self._input_start()
        inserted = self.view.insert(edit, insert_at, text)
        self.view.add_regions(
            entry["detail_key"],
            [sublime.Region(insert_at, insert_at + inserted)],
            flags=sublime.HIDDEN,
        )
        entry["expanded"] = True
        if start_before is not None and insert_at <= start_before:
            self._set_input_start(self.view, max(0, start_before + inserted))

    def _collapse(self, edit, entry):
        regions = self.view.get_regions(entry["detail_key"])
        if regions:
            region = regions[0]
            size = region.size()
            start_before = self._input_start()
            self.view.erase(edit, region)
            if start_before is not None and region.begin() <= start_before:
                self._set_input_start(self.view, max(0, start_before - size))
        self.view.erase_regions(entry["detail_key"])
        entry["expanded"] = False

    def _toggle_tool_call(self, edit, entry):
        entry["expanded"] = not entry["expanded"]
        self._render_tool_call_detail(edit, entry)
        self._update()

    def _render_tool_call_detail(self, edit, entry):
        """Re-render the shared detail block for a composite tool call.

        The command and its output are inserted as one hidden region in a
        fixed order, so the two rows can never swap or drift apart.
        """
        sections = []
        if entry["expanded"]:
            command_block = format_fenced_output(
                entry["command"]["detail"], info="bash")
            if command_block:
                sections.append(f"Command\n{command_block}")
            output = entry.get("output")
            if output:
                output_block = format_fenced_output(output["detail"])
                if output_block:
                    sections.append(f"Return\n{output_block}")
                elif entry["kind"] == "shell_finished":
                    # A silent background command still needs a Return block
                    # so the row never looks like it lost its output.
                    sections.append("Return\n```text\n(no output)\n```")
        text = "\n".join(sections)

        old_regions = self.view.get_regions(entry["detail_key"])
        old = old_regions[0] if old_regions else None
        # Capture the input anchor before editing: regions shift with the
        # edits, so adding the delta afterwards would double-count.
        start_before = self._input_start()
        if old is not None:
            insert_at = old.begin()
            size = old.size()
            self.view.erase(edit, old)
            if start_before is not None and insert_at <= start_before:
                start_before = max(0, start_before - size)
                self._set_input_start(self.view, start_before)
        else:
            anchor = self._anchor(entry)
            if anchor is None:
                return
            insert_at = self.view.full_line(anchor).end()

        if not text:
            self.view.erase_regions(entry["detail_key"])
            return
        inserted = self.view.insert(edit, insert_at, text)
        self.view.add_regions(
            entry["detail_key"],
            [sublime.Region(insert_at, insert_at + inserted)],
            flags=sublime.HIDDEN,
        )
        if start_before is not None and insert_at <= start_before:
            self._set_input_start(self.view, max(0, start_before + inserted))

    def _input_start(self):
        if not self._get_input_start:
            return None
        return self._get_input_start(self.view, 0)

    def _update(self):
        phantoms = []
        for entry in self.entries:
            anchor = self._anchor(entry)
            if anchor is None:
                continue
            phantoms.append(sublime.Phantom(
                anchor,
                self._row_html(entry),
                sublime.LAYOUT_BLOCK,
                self._on_navigate,
            ))
        self.phantom_set.update(phantoms)

    def _on_navigate(self, href):
        if not href:
            return
        parts = href.split(":", 2)
        action = parts[0]
        key = parts[1] if len(parts) > 1 else ""
        section = parts[2] if len(parts) > 2 else None
        # ``copy:`` links from older phantoms are ignored.
        if action == "toggle":
            view = self.view
            if view and view.is_valid():
                view.run_command(
                    "term_chat_tool_output_toggle",
                    {"key": key, "section": section},
                )

    def _tool_call_rows_html(self, entry):
        key = entry["key"]
        command = entry["command"]
        label = "hide" if entry["expanded"] else "show"
        output = entry.get("output")
        row_summary = ""
        if output:
            row_summary = output.get("summary") or ""
        elif entry.get("status") in STATUS_LABELS:
            row_summary = STATUS_LABELS[entry["status"]]
        command_links = ""
        if not row_summary:
            command_links = f"&nbsp;&nbsp;<a href='toggle:{key}'>{label}</a>"
        rows = [
            "<div class='termmate-command-row'>"
            f"<span>{html.escape(command['heading'])} "
            f"({html.escape(command['summary'])})</span>"
            f"{command_links}"
            "</div>"
        ]
        if row_summary:
            rows.append(
                "<div class='termmate-tool-row'>"
                f"<span>⎿ {html.escape(row_summary)}</span>"
                f"&nbsp;&nbsp;<a href='toggle:{key}'>{label}</a>"
                "</div>"
            )
        return "".join(rows)

    def _output_row_html(self, entry):
        summary = html.escape(entry.get("summary") or "")
        toggle_label = "hide" if entry["expanded"] else "show"
        key = entry["key"]
        body = f"<span>⎿ {summary}</span>"
        links = f"&nbsp;&nbsp;<a href='toggle:{key}'>{toggle_label}</a>"
        return f"<div class='termmate-tool-row'>{body}{links}</div>"

    def _shell_finished_rows_html(self, entry):
        key = entry["key"]
        toggle_label = "hide" if entry["expanded"] else "show"
        return (
            "<div class='termmate-tool-row'>"
            f"<span>↳ {html.escape(entry.get('label') or 'Shell finished')}"
            f" · {html.escape(entry.get('summary') or '')}</span>"
            f"&nbsp;&nbsp;<a href='toggle:{key}'>{toggle_label}</a>"
            "</div>"
        )

    def _row_html(self, entry):
        body_id = f"chatview-tool-output-{entry['key']}"
        if entry["kind"] == "tool_call":
            rows = self._tool_call_rows_html(entry)
        elif entry["kind"] == "shell_finished":
            rows = self._shell_finished_rows_html(entry)
        else:
            rows = self._output_row_html(entry)
        return (
            f"<body id='{body_id}' style='margin:0;padding:0;"
            "background-color:transparent;'>"
            "<style>"
            ".termmate-tool-row{font-family:var(--font-mono);font-size:0.88em;"
            "color:color(var(--foreground) alpha(0.55));padding:1px 0 3px 2px;}"
            ".termmate-command-row{font-family:var(--font-mono);font-size:0.92em;"
            "color:color(var(--foreground) alpha(0.85));padding:1px 0 3px 2px;}"
            ".termmate-tool-row a,.termmate-command-row a{color:var(--bluish);"
            "text-decoration:none;}"
            ".termmate-tool-row a:hover,.termmate-command-row a:hover"
            "{text-decoration:underline;}"
            "</style>"
            f"{rows}"
            "</body>"
        )
