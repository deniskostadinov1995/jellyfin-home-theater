"""
Jellyfin Home Theater — keep console output from crashing.

These scripts print arrows, em dashes and the odd emoji. Python on Windows
picks the console's legacy code page for stdout (cp1252 here), which cannot
encode any of them — so a stray "→" in a status line raises
UnicodeEncodeError and takes the whole script down. That is a miserable way to
lose a diagnostic tool.

Calling `enable()` first thing in an entry point switches stdout and stderr to
UTF-8 and tells them to substitute anything the terminal genuinely cannot
render, so output degrades to a "?" instead of a traceback. Modern terminals
(Windows Terminal, VS Code, the desktop app) show the real characters.
"""

import sys


def enable():
    for stream in (sys.stdout, sys.stderr):
        # reconfigure() exists on 3.7+; a redirected or replaced stream may not
        # have it, in which case there is nothing to fix.
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
