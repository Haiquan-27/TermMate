"""Collapsed tool rows rendered as phantoms.

Long tool results are not written into the chat buffer by default. Instead a
single phantom row is anchored below the tool header:

    ⏺ Bash npm test
    ⎿ 42 lines                                  [show] [copy]

Command tool calls (``command_execution`` / ``run_command``) are phantom-only:
the command text never enters the buffer while collapsed.

    ⏺ command (npm test -- --watch …)           [show]
    ⎿ 42 lines                                  [show] [copy]

Clicking ``show`` inserts the full text as a fenced code block right after the
anchor line (and toggles the link to ``hide``). Clicking ``copy`` puts the raw
output on the clipboard without changing the expanded state.
"""

import html
import logging

import sublime

from .utils import format_fenced_output

LOG = logging.getLogger("TermMate")


class ToolOutputManager:
    """Own collapsed tool-output rows and their show/copy phantoms."""

    REGION_PREFIX = "term_chat_tool_output_"

    def __init__(self, view, get_input_start=None, set_input_start=None):
        self.view = view
        self._get_input_start = get_input_start
        self._set_input_start = set_input_start
        self.phantom_set = sublime.PhantomSet(view, "chatview_tool_output")
        self.entries = []
        self.next_id = 0

    # ---------------------------------------------------------------- public

    def add(self, anchor, summary, detail, key=None, kind="output", heading="",
            show_copy=True):
        """Register a collapsed row anchored at ``anchor`` (a Region).

        ``kind`` is ``"output"`` (the ``⎿`` row) or ``"command"`` (the
        phantom-only command preview). ``heading`` is the text placed before
        the summary for command rows (e.g. ``⏺ command``).
        """
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
            "kind": kind,
            "heading": heading,
            "summary": summary,
            "detail": detail,
            "show_copy": show_copy and kind == "output",
            "expanded": False,
            "copied": False,
        }
        self.entries.append(entry)
        self._update()
        return entry

    def toggle(self, edit, key):
        """Expand or collapse the output row identified by ``key``."""
        entry = self._find(key)
        if entry is None:
            return
        if entry["expanded"]:
            self._collapse(edit, entry)
        else:
            self._expand(edit, entry)
        self._update()

    def copy(self, key):
        """Copy the raw tool output to the clipboard."""
        entry = self._find(key)
        if entry is None:
            sublime.status_message("Tool output is no longer available")
            return
        text = entry["detail"]
        sublime.set_clipboard(text)
        line_count = len(text.splitlines())
        sublime.status_message(
            f"Copied {line_count} line{'s' if line_count != 1 else ''} to clipboard"
        )
        entry["copied"] = True
        self._update()
        sublime.set_timeout(lambda: self._clear_copied(key), 1200)

    def truncate(self, cut_point):
        """Drop rows that start at or after ``cut_point`` (rewind support)."""
        kept = []
        for entry in self.entries:
            regions = self.view.get_regions(entry["region_key"])
            if regions and regions[0].begin() < cut_point:
                if entry["expanded"]:
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
        for entry in self.entries:
            if entry["key"] == key:
                return entry
        return None

    def _clear_copied(self, key):
        entry = self._find(key)
        if entry is not None:
            entry["copied"] = False
            self._update()

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
        action, _, key = href.partition(":")
        if action == "copy":
            self.copy(key)
        elif action == "toggle":
            view = self.view
            if view and view.is_valid():
                view.run_command("term_chat_tool_output_toggle", {"key": key})

    def _row_html(self, entry):
        summary = html.escape(entry.get("summary") or "")
        toggle_label = "hide" if entry["expanded"] else "show"
        copy_label = "copied" if entry["copied"] else "copy"
        body_id = f"chatview-tool-output-{entry['key']}"
        is_command = entry.get("kind") == "command"
        row_class = "termmate-command-row" if is_command else "termmate-tool-row"
        if is_command:
            label = html.escape(entry.get("heading") or "⏺ command")
            body = f"<span>{label} ({summary})</span>"
            links = f"&nbsp;&nbsp;<a href='toggle:{entry['key']}'>{toggle_label}</a>"
        else:
            body = f"<span>⎿ {summary}</span>"
            links = f"&nbsp;&nbsp;<a href='toggle:{entry['key']}'>{toggle_label}</a>"
            if entry.get("show_copy"):
                links += f"&nbsp;&nbsp;<a href='copy:{entry['key']}'>{copy_label}</a>"
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
            f"<div class='{row_class}'>"
            f"{body}{links}"
            "</div>"
            "</body>"
        )
