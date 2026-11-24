"""Shared diagnostic logger for tool-row troubleshooting.

Both ``genfoundry.opencode_agent`` and ``chatview.utils`` use this logger, so
the file handler is installed exactly once no matter which module imports it
first.  It is deliberately independent of the configured log level so a live
run can be inspected without attaching a debugger.
"""

import logging
import os
import tempfile


DIAG = logging.getLogger("TermMate.Diag")
if not DIAG.handlers:
    DIAG.setLevel(logging.DEBUG)
    DIAG.propagate = False
    try:
        _handler = logging.FileHandler(
            os.path.join(tempfile.gettempdir(), "termmate-tool-rows.log"),
            mode="w",
            encoding="utf-8",
        )
        _handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        DIAG.addHandler(_handler)
    except OSError:
        DIAG.addHandler(logging.NullHandler())
