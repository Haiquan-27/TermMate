"""Collapsed tool-output rows rendered as phantoms.

Long tool results are not written into the chat buffer by default. Instead a
single phantom row is anchored below the tool header:

    ⏺ Bash npm test
    ⎿ 42 lines · completed                          [show] [copy]

Clicking ``show`` inserts the full output as a fenced code block right after
the header line (and toggles the link to ``hide``). Clicking ``copy`` puts the
raw output on the clipboard without changing the expanded state.
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
            "summary": summary,
            "detail": detail,
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
        inserted = self.view.insert(edit, insert_at, text)
        self.view.add_regions(
            entry["detail_key"],
            [sublime.Region(insert_at, insert_at + inserted)],
            flags=sublime.HIDDEN,
        )
        entry["expanded"] = True
        self._shift_input_start(insert_at, inserted)

    def _collapse(self, edit, entry):
        regions = self.view.get_regions(entry["detail_key"])
        if regions:
            region = regions[0]
            size = region.size()
            self.view.erase(edit, region)
            self._shift_input_start(region.begin(), -size)
        self.view.erase_regions(entry["detail_key"])
        entry["expanded"] = False

    def _shift_input_start(self, position, delta):
        if not self._get_input_start or not self._set_input_start:
            return
        start = self._get_input_start(self.view, 0)
        if start is None:
            return
        if position <= start:
            self._set_input_start(self.view, max(0, start + delta))

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
        return (
            f"<body id='{body_id}' style='margin:0;padding:0;"
            "background-color:transparent;'>"
            "<style>"
            ".termmate-tool-row{font-family:var(--font-mono);font-size:0.88em;"
            "color:color(var(--foreground) alpha(0.55));padding:1px 0 3px 2px;}"
            ".termmate-tool-row a{color:var(--bluish);text-decoration:none;"
            "margin-left:12px;}"
            ".termmate-tool-row a:hover{text-decoration:underline;}"
            "</style>"
            "<div class='termmate-tool-row'>"
            f"<span>⎿ {summary}</span>"
            f"<a href='toggle:{entry['key']}'>{toggle_label}</a>"
            f"<a href='copy:{entry['key']}'>{copy_label}</a>"
            "</div>"
            "</body>"
        )
