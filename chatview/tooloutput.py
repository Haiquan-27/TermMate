"""Collapsed tool rows rendered as phantoms.

Long tool results are not written into the chat buffer by default. Instead a
single phantom row is anchored below the tool header:

    ⏺ Bash npm test
    ⎿ 42 lines success                            [show]

Command tool calls (``command_execution`` / ``run_command``) are phantom-only:
the command text never enters the buffer while collapsed.

    ⏺ command (npm test -- --watch …)             [show]
    ⎿ 42 lines success                            [show]

Clicking ``show`` inserts the full text as a fenced code block right after the
anchor line (and toggles the link to ``hide``).
"""

import html
import logging

import sublime

from .utils import format_fenced_output

LOG = logging.getLogger("TermMate")


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

    def add_tool_call(self, anchor, command, output=None, key=None):
        """Register a command tool call as one composite phantom.

        A single phantom holds both the command preview row and the output
        summary row. Keeping them in one phantom is what makes their order
        stable: phantoms anchored at the same point have no defined order.

        ``command`` is ``{"heading", "summary", "detail"}``; ``output`` is
        ``{"summary", "detail"}`` or ``None``.
        """
        command = command or {}
        if not command.get("detail"):
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
            "kind": "tool_call",
            "command": {
                "heading": command.get("heading") or "⏺ command",
                "summary": command.get("summary") or "",
                "detail": command["detail"],
            },
            "output": None,
            "show_command": False,
            "show_output": False,
        }
        if output and output.get("detail"):
            entry["output"] = {
                "summary": output.get("summary") or "",
                "detail": output["detail"],
            }
        self.entries.append(entry)
        self._update()
        return entry

    def toggle(self, edit, key, section=None):
        """Expand or collapse the row identified by ``key``.

        ``section`` selects a half of a composite command tool call
        (``"command"`` or ``"output"``); it is ignored by output rows.
        """
        entry = self._find(key)
        if entry is None:
            return
        if entry["kind"] == "tool_call":
            self._toggle_tool_call(edit, entry, section)
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
                    if entry["kind"] == "tool_call":
                        entry["show_command"] = False
                        entry["show_output"] = False
                    else:
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
        for entry in self.entries:
            if entry["key"] == key:
                return entry
        return None

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

    def _toggle_tool_call(self, edit, entry, section):
        if section == "output":
            if not entry.get("output"):
                return
            entry["show_output"] = not entry["show_output"]
        elif section == "command":
            entry["show_command"] = not entry["show_command"]
        else:
            field = "show_output" if entry.get("output") else "show_command"
            entry[field] = not entry[field]
        self._render_tool_call_detail(edit, entry)
        self._update()

    def _render_tool_call_detail(self, edit, entry):
        """Re-render the shared detail block for a composite tool call.

        Both expanded fences live in one hidden region, in a fixed order
        (command first, output second), so toggling either half never moves
        the other. The region is erased and re-inserted at the same point.
        """
        blocks = []
        if entry["show_command"]:
            blocks.append(format_fenced_output(entry["command"]["detail"]))
        if entry.get("output") and entry["show_output"]:
            blocks.append(format_fenced_output(entry["output"]["detail"]))
        text = "\n".join(block for block in blocks if block)

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
        command_label = "hide" if entry["show_command"] else "show"
        rows = [
            "<div class='termmate-command-row'>"
            f"<span>{html.escape(command['heading'])} "
            f"({html.escape(command['summary'])})</span>"
            f"&nbsp;&nbsp;<a href='toggle:{key}:command'>{command_label}</a>"
            "</div>"
        ]
        output = entry.get("output")
        if output:
            output_label = "hide" if entry["show_output"] else "show"
            rows.append(
                "<div class='termmate-tool-row'>"
                f"<span>⎿ {html.escape(output['summary'])}</span>"
                f"&nbsp;&nbsp;<a href='toggle:{key}:output'>{output_label}</a>"
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

    def _row_html(self, entry):
        body_id = f"chatview-tool-output-{entry['key']}"
        if entry["kind"] == "tool_call":
            rows = self._tool_call_rows_html(entry)
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
