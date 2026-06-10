"""Mapping of human-friendly key names to the byte sequences a terminal sends.

These are the sequences an *application* receives on stdin when a key is pressed
in an xterm-compatible terminal. We write them straight into the PTY master so the
target TUI sees exactly what it would from a real keyboard.
"""

# Control characters: Ctrl-A .. Ctrl-Z map to 0x01 .. 0x1a
def _ctrl(letter: str) -> bytes:
    return bytes([ord(letter.lower()) - ord("a") + 1])


KEYS: dict[str, bytes] = {
    # Whitespace / editing
    "enter": b"\r",
    "return": b"\r",
    "tab": b"\t",
    "backtab": b"\x1b[Z",      # Shift-Tab
    "space": b" ",
    "backspace": b"\x7f",
    "delete": b"\x1b[3~",
    "escape": b"\x1b",
    "esc": b"\x1b",

    # Arrows
    "up": b"\x1b[A",
    "down": b"\x1b[B",
    "right": b"\x1b[C",
    "left": b"\x1b[D",

    # Navigation block
    "home": b"\x1b[H",
    "end": b"\x1b[F",
    "pageup": b"\x1b[5~",
    "pagedown": b"\x1b[6~",
    "insert": b"\x1b[2~",

    # Function keys
    "f1": b"\x1bOP",
    "f2": b"\x1bOQ",
    "f3": b"\x1bOR",
    "f4": b"\x1bOS",
    "f5": b"\x1b[15~",
    "f6": b"\x1b[17~",
    "f7": b"\x1b[18~",
    "f8": b"\x1b[19~",
    "f9": b"\x1b[20~",
    "f10": b"\x1b[21~",
    "f11": b"\x1b[23~",
    "f12": b"\x1b[24~",
}

# Add Ctrl-<letter> aliases: "ctrl+c", "ctrl-c", "c-c"
for _c in "abcdefghijklmnopqrstuvwxyz":
    seq = _ctrl(_c)
    KEYS[f"ctrl+{_c}"] = seq
    KEYS[f"ctrl-{_c}"] = seq
    KEYS[f"c-{_c}"] = seq

# A few commonly-needed named control combos
KEYS["ctrl+space"] = b"\x00"

# Modified special keys (Ctrl/Shift/Alt + arrows / Home / End / nav), using the xterm
# encoding CSI 1 ; <mod> <final> for cursor keys and CSI <n> ; <mod> ~ for the ~-keys.
# mod = 1 + shift(1) + alt(2) + ctrl(4): shift=2, alt=3, ctrl=5, ctrl+shift=6.
_MODS = {"shift": 2, "alt": 3, "ctrl": 5, "ctrl+shift": 6, "shift+ctrl": 6}
_CURSOR_FINALS = {"up": "A", "down": "B", "right": "C", "left": "D",
                  "home": "H", "end": "F"}
_TILDE_KEYS = {"insert": 2, "delete": 3, "pageup": 5, "pagedown": 6}

for _mod_name, _mod in _MODS.items():
    for _name, _final in _CURSOR_FINALS.items():
        KEYS[f"{_mod_name}+{_name}"] = f"\x1b[1;{_mod}{_final}".encode()
    for _name, _n in _TILDE_KEYS.items():
        KEYS[f"{_mod_name}+{_name}"] = f"\x1b[{_n};{_mod}~".encode()

# Cursor/edit keys that change form under DECCKM (application cursor keys): the
# program receives ESC O x instead of ESC [ x. We send the right one based on the
# mode the program enabled (tracked in modes.py).
_APP_CURSOR = {
    "up": b"\x1bOA",
    "down": b"\x1bOB",
    "right": b"\x1bOC",
    "left": b"\x1bOD",
    "home": b"\x1bOH",
    "end": b"\x1bOF",
}

_ALT_PREFIXES = ("alt+", "alt-", "meta+", "meta-", "m-")


def resolve(key: str, app_cursor: bool = False) -> bytes:
    """Resolve a key name like 'enter', 'ctrl+c', 'f5', 'alt+x' to its byte sequence.

    app_cursor: when True, cursor keys use the application-mode (ESC O x) form, which
    is what a program receives once it has enabled DECCKM. Get this from the session's
    tracked modes so arrows work inside vim/less/fzf.

    Alt/Meta combos (alt+x, meta-f, m-b) are encoded as ESC followed by the inner key.

    Raises KeyError if the key is unknown.
    """
    norm = key.strip().lower().replace(" ", "")

    # Alt/Meta: ESC prefix + the inner key (recursively resolved).
    for pre in _ALT_PREFIXES:
        if norm.startswith(pre) and len(norm) > len(pre):
            inner = norm[len(pre):]
            try:
                return b"\x1b" + resolve(inner, app_cursor=app_cursor)
            except KeyError:
                if len(inner) == 1:
                    return b"\x1b" + inner.encode()
                raise KeyError(f"Unknown key after alt/meta: {inner!r}")

    if app_cursor and norm in _APP_CURSOR:
        return _APP_CURSOR[norm]
    if norm in KEYS:
        return KEYS[norm]
    raise KeyError(f"Unknown key: {key!r}")
