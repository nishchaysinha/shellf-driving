"""Named keyboard shortcuts / chords for terminal applications.

Many TUIs use *sequential* shortcuts — a prefix key followed by one or more keys,
e.g. tmux's ``Ctrl+B d`` to detach, GNU screen's ``Ctrl+A d``, or Emacs's
``Ctrl+X Ctrl+S`` to save. Firing these reliably needs (a) the keys sent in order
and (b) a small beat between steps so the app registers the prefix first.

A shortcut is a list of *tokens*. Each token is either a key name resolvable by
``keys.resolve`` (``"ctrl+b"``, ``"enter"``, ``"f10"``) or, failing that, literal
text to type (``"d"``, ``":"``, ``"%"``). This mirrors how `press` handles tokens,
so a chord is just a named, reusable key sequence.
"""

from __future__ import annotations

# app -> { shortcut_name -> [token, token, ...] }
DEFAULT_SHORTCUTS: dict[str, dict[str, list[str]]] = {
    "tmux": {
        "prefix": ["ctrl+b"],
        "detach": ["ctrl+b", "d"],
        "command-prompt": ["ctrl+b", ":"],
        "new-window": ["ctrl+b", "c"],
        "next-window": ["ctrl+b", "n"],
        "prev-window": ["ctrl+b", "p"],
        "last-window": ["ctrl+b", "l"],
        "rename-window": ["ctrl+b", ","],
        "list-windows": ["ctrl+b", "w"],
        "split-horizontal": ["ctrl+b", '"'],
        "split-vertical": ["ctrl+b", "%"],
        "next-pane": ["ctrl+b", "o"],
        "zoom-pane": ["ctrl+b", "z"],
        "kill-pane": ["ctrl+b", "x"],
        "copy-mode": ["ctrl+b", "["],
        "paste": ["ctrl+b", "]"],
    },
    "screen": {
        "prefix": ["ctrl+a"],
        "detach": ["ctrl+a", "d"],
        "new-window": ["ctrl+a", "c"],
        "next-window": ["ctrl+a", "n"],
        "prev-window": ["ctrl+a", "p"],
        "split": ["ctrl+a", "S"],
        "kill-window": ["ctrl+a", "k"],
        "command-prompt": ["ctrl+a", ":"],
    },
    "emacs": {
        "save": ["ctrl+x", "ctrl+s"],
        "quit": ["ctrl+x", "ctrl+c"],
        "find-file": ["ctrl+x", "ctrl+f"],
        "switch-buffer": ["ctrl+x", "b"],
        "kill-buffer": ["ctrl+x", "k"],
        "split-horizontal": ["ctrl+x", "2"],
        "split-vertical": ["ctrl+x", "3"],
        "other-window": ["ctrl+x", "o"],
        "cancel": ["ctrl+g"],
    },
    "vim": {
        "save": [":", "w", "enter"],
        "quit": [":", "q", "enter"],
        "save-quit": [":", "w", "q", "enter"],
        "force-quit": [":", "q", "!", "enter"],
        "insert": ["i"],
        "escape": ["escape"],
        "goto-top": ["g", "g"],
        "goto-bottom": ["G"],
        "undo": ["u"],
        "redo": ["ctrl+r"],
    },
    "nano": {
        "save": ["ctrl+o", "enter"],
        "exit": ["ctrl+x"],
        "search": ["ctrl+w"],
        "cut-line": ["ctrl+k"],
        "paste": ["ctrl+u"],
        "goto-line": ["ctrl+_"],
    },
    "less": {
        "quit": ["q"],
        "search": ["/"],
        "next-match": ["n"],
        "goto-start": ["g"],
        "goto-end": ["G"],
    },
}

# Runtime additions/overrides (populated via define()).
_user_shortcuts: dict[str, dict[str, list[str]]] = {}


def define(app: str, name: str, tokens: list[str]) -> None:
    """Register or override a shortcut at runtime."""
    _user_shortcuts.setdefault(app, {})[name] = list(tokens)


def resolve(app: str, name: str) -> list[str]:
    """Return the token sequence for ``app``'s shortcut ``name``.

    Raises KeyError with helpful context if the app or shortcut is unknown.
    """
    for table in (_user_shortcuts, DEFAULT_SHORTCUTS):
        if app in table and name in table[app]:
            return list(table[app][name])
    known_apps = sorted(set(DEFAULT_SHORTCUTS) | set(_user_shortcuts))
    if app not in known_apps:
        raise KeyError(f"No shortcuts known for app {app!r}. Known apps: {known_apps}")
    raise KeyError(f"App {app!r} has no shortcut {name!r}. Available: {sorted(catalog()[app])}")


def catalog() -> dict[str, dict[str, list[str]]]:
    """Merged view of default + user shortcuts (user wins on conflicts)."""
    merged: dict[str, dict[str, list[str]]] = {
        app: dict(table) for app, table in DEFAULT_SHORTCUTS.items()
    }
    for app, table in _user_shortcuts.items():
        merged.setdefault(app, {}).update(table)
    return merged
