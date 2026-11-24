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

    ↳ Shell finished · npm run dev

The label is tinted blue and the row carries no links; the shell output
stays available on the originating tool row.
"""

import html
import logging

import sublime

from .utils import DIAG, format_fenced_output

LOG = logging.getLogger("TermMate")

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
                      status=None, edit=None, status_only=False):
        """Register or update a tool call row.

        With ``status_only`` the header already lives in the buffer as real
        text and this entry only renders the ``⎿`` status row. Without it the
        phantom holds the header and the status row together (the older
        command layout).

        ``command`` is ``{"heading", "summary", "detail", "show_command"}``;
        ``output`` is ``{"summary", "detail"}`` or ``None``; ``status`` is
        ``"running"``, ``"background"`` or ``None``.

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
                    existing, command, output, status, edit,
                    status_only=status_only)
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
            "status_only": bool(status_only),
            "command": {
                "heading": command.get("heading") or "⏺ command",
                "summary": command.get("summary") or "",
                "detail": command["detail"],
                "show_command": command.get("show_command", True),
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
        is informational: its label is tinted blue and it carries no links,
        because the shell output already lives on the originating tool row.
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
        if entry["kind"] == "tool_call":
            self._toggle_tool_call(edit, entry)
            return
        if entry["kind"] == "shell_finished":
            # Informational row: no links, nothing to expand.
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

    def _update_tool_call(self, entry, command, output, status, edit,
                          status_only=None):
        """Apply a later event to an existing tool row in place."""
        if command.get("detail"):
            entry["command"] = {
                "heading": command.get("heading") or entry["command"]["heading"],
                "summary": command.get("summary") or "",
                "detail": command["detail"],
                "show_command": command.get(
                    "show_command",
                    entry["command"].get("show_command", True)),
            }
        if output is not None:
            summary = output.get("summary") or ""
            detail = output.get("detail") or ""
            if detail:
                entry["output"] = {"summary": summary, "detail": detail}
            elif summary and not entry.get("output"):
                entry["output"] = {"summary": summary, "detail": ""}
        if status_only is not None:
            entry["status_only"] = bool(status_only)
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
            if entry["command"].get("show_command", True):
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
        label = "hide" if entry["expanded"] else "show"
        output = entry.get("output")
        row_summary = ""
        if output:
            row_summary = output.get("summary") or ""
        elif entry.get("status") in STATUS_LABELS:
            row_summary = STATUS_LABELS[entry["status"]]
        command = entry["command"]
        can_expand = bool(command.get("show_command")) or bool(
            (output or {}).get("detail"))
        links = (f"&nbsp;&nbsp;<a href='toggle:{key}'>{label}</a>"
                 if can_expand else "")

        if entry.get("status_only"):
            if not row_summary:
                return ""
            return (
                "<div class='termmate-tool-row'>"
                f"<span>⎿ {html.escape(row_summary)}</span>"
                f"{links}"
                "</div>"
            )

        # Legacy composite row: header and status share one phantom.
        command_links = "" if row_summary else links
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
                f"{links}"
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
        label = html.escape(entry.get("label") or "Shell finished")
        summary = html.escape(entry.get("summary") or "")
        detail = f" · {summary}" if summary else ""
        return (
            "<div class='termmate-tool-row'>"
            f"<span class='termmate-shell-finished'>↳ {label}</span>"
            f"<span>{detail}</span>"
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
            ".termmate-shell-finished{color:var(--bluish);}"
            ".termmate-tool-row a:hover,.termmate-command-row a:hover"
            "{text-decoration:underline;}"
            "</style>"
            f"{rows}"
            "</body>"
        )
