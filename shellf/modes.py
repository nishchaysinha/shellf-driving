"""Terminal mode tracking + query auto-responses.

pyte parses the screen but deliberately ignores most DEC *private* modes (it tracks
only DECAWM/DECCOLM/DECOM/DECSCNM/DECTCEM/IRM/LNM). The modes that matter for *driving*
a TUI — application cursor keys, alt-screen, mouse reporting, bracketed paste — are
silently dropped. So we sniff them ourselves off the program's output stream.

We also answer the cursor/identity *queries* programs emit and then block waiting on:
without a reply, apps like vim or fzf can stall or mis-detect the terminal size.

All scanning is byte-level and tolerant of sequences split across read() boundaries
(the caller keeps a small carry-over tail).
"""

from __future__ import annotations

import re

# ---- DEC private mode numbers we care about ----
DECCKM = 1                       # application cursor keys (arrows: ESC O x vs ESC [ x)
_ALT_SCREEN = {47, 1047, 1049}   # alternate screen buffer
_BRACKETED_PASTE = 2004
_FOCUS_EVENT = 1004
_MOUSE_TRACK = {9, 1000, 1002, 1003}   # X10 / VT200 / btn-event / any-event
_MOUSE_SGR = 1006

# CSI ? <params> (h|l)  -> set/reset private mode
_PRIV_RE = re.compile(rb"\x1b\[\?([0-9;]+)([hl])")

# Queries the program may send and block on. Order matters: match the more specific
# secondary-DA (CSI > c) before primary-DA (CSI c).
_PRIMARY_DA = re.compile(rb"\x1b\[(?:0)?c")
_SECONDARY_DA = re.compile(rb"\x1b\[>(?:0)?c")
_DSR_STATUS = re.compile(rb"\x1b\[5n")
_DSR_CURSOR = re.compile(rb"\x1b\[6n")

# Static responses (xterm-compatible). The exact DA value rarely matters — programs
# mostly just need *a* reply to proceed.
_RESP_PRIMARY_DA = b"\x1b[?1;2c"      # VT100 with advanced video
_RESP_SECONDARY_DA = b"\x1b[>0;95;0c"  # "xterm-ish", version 95
_RESP_DSR_OK = b"\x1b[0n"


class TerminalModes:
    """Live view of the modes the program has enabled, updated from its output."""

    __slots__ = (
        "app_cursor_keys",
        "alt_screen",
        "bracketed_paste",
        "mouse_tracking",
        "mouse_sgr",
        "focus_events",
    )

    def __init__(self) -> None:
        self.app_cursor_keys = False
        self.alt_screen = False
        self.bracketed_paste = False
        self.mouse_tracking = False
        self.mouse_sgr = False
        self.focus_events = False

    def _apply(self, params: list[int], enable: bool) -> None:
        for p in params:
            if p == DECCKM:
                self.app_cursor_keys = enable
            elif p in _ALT_SCREEN:
                self.alt_screen = enable
            elif p == _BRACKETED_PASTE:
                self.bracketed_paste = enable
            elif p in _MOUSE_TRACK:
                self.mouse_tracking = enable
            elif p == _MOUSE_SGR:
                self.mouse_sgr = enable
            elif p == _FOCUS_EVENT:
                self.focus_events = enable

    def as_dict(self) -> dict:
        return {
            "app_cursor_keys": self.app_cursor_keys,
            "alt_screen": self.alt_screen,
            "bracketed_paste": self.bracketed_paste,
            "mouse_tracking": self.mouse_tracking,
            "mouse_sgr": self.mouse_sgr,
            "focus_events": self.focus_events,
        }


def scan(data: bytes, modes: TerminalModes, cursor: tuple[int, int]) -> tuple[bytes, int]:
    """Scan program output for private-mode changes and queries.

    Updates `modes` in place. Returns (reply_bytes, consumed_upto) where:
      * reply_bytes is what should be written back to the PTY (query answers), and
      * consumed_upto is the index past the last recognized sequence, so the caller
        can retain only the unconsumed tail (handles boundary-split sequences).

    cursor is the current (col, row), 1-based, used for the cursor-position report.
    """
    reply = bytearray()
    consumed = 0

    for m in _PRIV_RE.finditer(data):
        params = [int(x) for x in m.group(1).split(b";") if x]
        modes._apply(params, m.group(2) == b"h")
        consumed = max(consumed, m.end())

    for m in _SECONDARY_DA.finditer(data):
        reply += _RESP_SECONDARY_DA
        consumed = max(consumed, m.end())
    for m in _PRIMARY_DA.finditer(data):
        # Skip ones that were really the secondary form (CSI > c): those have '>'
        # right before 'c', which _PRIMARY_DA can't match anyway, so this is safe.
        reply += _RESP_PRIMARY_DA
        consumed = max(consumed, m.end())
    for m in _DSR_STATUS.finditer(data):
        reply += _RESP_DSR_OK
        consumed = max(consumed, m.end())
    for m in _DSR_CURSOR.finditer(data):
        col, row = cursor
        reply += f"\x1b[{row};{col}R".encode()
        consumed = max(consumed, m.end())

    return bytes(reply), consumed
