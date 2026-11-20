import logging
import difflib
import os
import re
import tempfile

import sublime


LOG = logging.getLogger("TermMate")

#: TEMPORARY diagnostic channel for tool-row insertion.  Writes to
#: ``%TEMP%/termmate-tool-rows.log`` regardless of the configured log level so
#: a live run can be inspected without attaching a debugger.
DIAG = logging.getLogger("TermMate.Diag")
if not DIAG.handlers:
    DIAG.setLevel(logging.DEBUG)
    DIAG.propagate = False
    try:
        _diag_handler = logging.FileHandler(
            os.path.join(tempfile.gettempdir(), "termmate-tool-rows.log"),
            mode="w",
            encoding="utf-8",
        )
        _diag_handler.setFormatter(logging.Formatter(
            "%(asctime)s %(message)s"))
        DIAG.addHandler(_diag_handler)
    except OSError:
        DIAG.addHandler(logging.NullHandler())

def get_log_level(level_name):
    """Maps log level names to logging constants."""
    return getattr(logging, level_name.upper(), logging.ERROR)


def update_log_level(settings):
    """
    Reads the log_level from settings and reconfigures the logger.
    """
    level_name = settings.get("log_level", "ERROR")
    level = get_log_level(level_name)
    LOG.setLevel(level)
    LOG.propagate = False
    if not LOG.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter('%(message)s')
        handler.setFormatter(formatter)
        LOG.addHandler(handler)


def show_diff(window, old_text, new_text, name):
    """
    Generate and show a git-style unified diff between old and new text.

    Args:
        window: Sublime Text window to create the view in
        old_text: Original text content
        new_text: Modified text content
        name: Name for the diff view tab
    """
    a = old_text.splitlines(keepends=True)
    b = new_text.splitlines(keepends=True)

    # Generate unified diff with context
    diff_lines = list(difflib.unified_diff(
        a, b,
        fromfile="a/" + name,
        tofile="b/" + name,
        lineterm='',
        n=5  # lines of context
    ))

    if not diff_lines:
        sublime.status_message("No changes")
        return

    # Build git-style diff output
    output_parts = []

    # Add git diff header
    output_parts.append(f"diff a/{name} b/{name}\n")
    output_parts.append(f"--- a/{name}\n")
    output_parts.append(f"+++ b/{name}\n")

    # Add the actual diff content (skip the default --- +++ lines from unified_diff)
    for line in diff_lines[2:]:
        if not line.endswith('\n'):
            line += '\n'
        output_parts.append(line)

    difftxt = "".join(output_parts)

    # Create and configure the diff view
    v = window.new_file()
    v.set_name(name)
    v.set_scratch(True)
    v.settings().set("line_numbers", False)
    v.assign_syntax('Packages/TermMate/chatview/ChatDiff.sublime-syntax')
    v.run_command('append', {'characters': difftxt, 'disable_tab_translation': True})
    v.set_read_only(True)


def show_diff_text(window, diff_text, name):
    """
    Show pre-built unified diff text in a new read-only scratch view.

    Returns the created view.
    """
    v = window.new_file()
    v.set_name(name)
    v.set_scratch(True)
    v.settings().set("line_numbers", False)
    v.assign_syntax('Packages/TermMate/chatview/ChatDiff.sublime-syntax')
    v.run_command('append', {'characters': diff_text, 'disable_tab_translation': True})
    v.set_read_only(True)
    return v


def format_token_count(count):
    """Format token counts into human-readable strings (e.g., 217, 34.8k, 1.2M)."""
    if count is None:
        return "0"
    try:
        n = float(count)
    except (ValueError, TypeError):
        return str(count)
    if n < 1000:
        return str(int(n))
    if n < 999_950:
        val = n / 1000.0
        if val < 100:
            formatted = f"{val:.1f}"
            return f"{formatted[:-2] if formatted.endswith('.0') else formatted}k"
        r = round(val)
        return "1M" if r >= 1000 else f"{r}k"
    val = n / 1_000_000.0
    formatted = f"{val:.1f}"
    return f"{formatted[:-2] if formatted.endswith('.0') else formatted}M"


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_ANSI_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def strip_ansi(text):
    """Remove ANSI escape sequences (SGR colours, OSC titles) from tool output."""
    if not text:
        return ""
    return _ANSI_OSC_RE.sub("", _ANSI_RE.sub("", text))


def backtick_fence_for(text):
    """Return a backtick fence longer than any run of backticks inside text."""
    longest = 0
    run = 0
    for char in text or "":
        if char == "`":
            run += 1
            if run > longest:
                longest = run
        else:
            run = 0
    return "`" * max(3, longest + 1)


def clean_tool_output(text):
    """Normalise line endings, strip ANSI codes and trailing blank lines."""
    if not text:
        return ""
    cleaned = strip_ansi(text).replace("\r\n", "\n").replace("\r", "\n")
    return cleaned.rstrip("\n")


def format_fenced_output(text, info="text"):
    """Wrap tool output in a fenced code block that Sublime can fold."""
    cleaned = clean_tool_output(text)
    if not cleaned.strip():
        return ""
    fence = backtick_fence_for(cleaned)
    return f"{fence}{info}\n{cleaned}\n{fence}\n"


def classify_tool_output(text, inline_lines=3, inline_max_chars=400):
    """Decide how tool output should be rendered.

    Returns (mode, cleaned_text) where mode is:
      "inline"    - short enough to write straight into the chat buffer
      "collapsed" - long enough to hide behind a phantom show button
      None        - nothing worth rendering
    """
    cleaned = clean_tool_output(text)
    if not cleaned.strip():
        return None, ""
    try:
        line_limit = int(inline_lines)
    except (TypeError, ValueError):
        line_limit = 3
    try:
        char_limit = int(inline_max_chars)
    except (TypeError, ValueError):
        char_limit = 400
    line_count = len(cleaned.splitlines())
    if line_count <= line_limit and len(cleaned) <= char_limit:
        return "inline", cleaned
    return "collapsed", cleaned


