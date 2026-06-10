"""MCP server exposing Shellf-Driving to any MCP client (Claude Code, Desktop, ...).

The LLM drives terminal UIs through a small set of tools that mirror how a human
operates a terminal: launch a program, look at the screen, type, press keys, click
with the mouse, scroll, and wait for things to appear.

Multiple named sessions can run at once (default name: "default"), so an agent can
juggle several TUIs (e.g. an editor and a file manager) in one conversation.

Run it:
    python -m shellf.server
"""

from __future__ import annotations

import functools
import os
import sys
import time

from mcp.server.fastmcp import FastMCP, Image

from . import keys, observe, shortcuts
from .render import Renderer
from .terminal import TerminalSession

mcp = FastMCP("shellf-driving")

# name -> TerminalSession
_sessions: dict[str, TerminalSession] = {}

# One cached renderer (fonts/metrics) shared across screenshot calls.
_renderer = Renderer()


# --- Observability dashboard (opt-in via SHELLF_OBSERVE_PORT) --------------- #
def _maybe_start_observer() -> None:
    port = os.environ.get("SHELLF_OBSERVE_PORT")
    if port and not observe.hub.active:
        url = observe.start(int(port))
        print(f"[shellf-driving] observability dashboard: {url}", file=sys.stderr, flush=True)


_maybe_start_observer()


def _wire_observer(name: str, ts: TerminalSession) -> None:
    """Stream a session's PTY bytes / resizes / exit to the dashboard."""
    if not observe.hub.active:
        return
    observe.hub.register_session(name, ts.cols, ts.rows, ts.command)
    ts.on_output = lambda data, n=name: observe.hub.publish_output(n, data)
    ts.on_resize = lambda c, r, n=name, cmd=ts.command: observe.hub.register_session(n, c, r, cmd)
    ts.on_exit = lambda status, n=name: observe.hub.mark_exited(n, status)


def _summarize_args(kwargs: dict) -> dict:
    """Compact, safe view of a tool call's args for the timeline."""
    out = {}
    for k, v in kwargs.items():
        if isinstance(v, str) and len(v) > 60:
            v = v[:60] + "…"
        out[k] = v
    return out


def observed(fn):
    """Publish a tool-call event to the dashboard, preserving the signature so
    FastMCP still derives the correct schema (inspect.signature follows __wrapped__)."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if observe.hub.active:
            observe.hub.publish_event({
                "kind": "tool", "name": fn.__name__,
                "session": kwargs.get("session", "default"),
                "args": _summarize_args(kwargs),
            })
        return fn(*args, **kwargs)
    return wrapper


def _get(session: str) -> TerminalSession:
    if session not in _sessions:
        raise ValueError(
            f"No session named {session!r}. Call launch first. "
            f"Active sessions: {sorted(_sessions) or 'none'}"
        )
    return _sessions[session]


def _render(ts: TerminalSession) -> str:
    """A screen snapshot annotated with cursor + liveness, for the model to read."""
    cur = ts.cursor()
    status = "running" if ts.alive else f"exited (status={ts.exit_status})"
    m = ts.modes
    flags = "".join(
        f" {n}" for n, on in (
            ("alt-screen", m.alt_screen), ("app-cursor", m.app_cursor_keys),
            ("mouse", m.mouse_tracking), ("paste", m.bracketed_paste),
        ) if on
    )
    header = (
        f"[{ts.cols}x{ts.rows} | {status} | "
        f"cursor=({cur['x']},{cur['y']})"
        f"{' hidden' if cur['hidden'] else ''}{flags}]"
    )
    return header + "\n" + ts.snapshot()


def _act(ts: TerminalSession, send, quiet: float = 0.12, timeout: float = 2.0) -> str:
    """Run an input action, then auto-wait for the resulting repaint to settle.

    Captures the repaint version before sending so we wait for *this action's* effect
    (not the already-quiet prior screen), then for a short quiet window. Returns the
    settled screen. This is the Playwright-style auto-wait baked into every action.
    """
    baseline = ts.version()
    send()
    ts.wait_for_stable(quiet=quiet, timeout=timeout, since_version=baseline)
    return _render(ts)


@mcp.tool()
@observed
def launch(
    command: str,
    args: list[str] | None = None,
    session: str = "default",
    cols: int = 80,
    rows: int = 24,
    cwd: str | None = None,
    settle: float = 0.3,
) -> str:
    """Launch a terminal program inside a virtual terminal.

    command: executable to run (e.g. "vim", "htop", "python").
    args: argument list (e.g. ["file.txt"]).
    session: name to address this instance in later calls.
    cols/rows: terminal size. cwd: working directory.
    settle: seconds to wait for first paint before snapshotting.
    Returns the initial screen.
    """
    if session in _sessions and _sessions[session].alive:
        raise ValueError(f"Session {session!r} is already running. Kill it first.")
    ts = TerminalSession(command, args=args, cols=cols, rows=rows, cwd=cwd)
    _sessions[session] = ts
    _wire_observer(session, ts)
    # Wait for the first paint to settle (programs often need a query answered first).
    ts.wait_for_stable(quiet=settle, timeout=max(2.0, settle * 5), since_version=0)
    return _render(ts)


@mcp.tool()
@observed
def snapshot(session: str = "default") -> str:
    """Return the current screen contents (the 'what does it look like now' tool)."""
    return _render(_get(session))


@mcp.tool()
@observed
def screenshot(session: str = "default") -> Image:
    """Return a PNG image of the terminal — colors, bold, the block cursor outline,
    and a red marker where the mouse last acted.

    Use this when layout or color matters (lazygit, k9s, btop) or when the plain-text
    snapshot is ambiguous. For pure text content, `snapshot` is cheaper.
    """
    ts = _get(session)
    png = _renderer.render(ts.screen, ts.cursor(), ts.last_mouse)
    return Image(data=png, format="png")


@mcp.tool()
@observed
def type_text(text: str, session: str = "default", settle: float = 0.12) -> str:
    """Type literal text into the program, then return the updated screen.

    Auto-waits for the resulting repaint to settle before returning.
    """
    ts = _get(session)
    return _act(ts, lambda: ts.send_text(text), quiet=settle)


@mcp.tool()
@observed
def press(
    keys_: list[str],
    session: str = "default",
    settle: float = 0.15,
    step_delay: float = 0.04,
) -> str:
    """Press a sequence of keys in order, then return the updated screen.

    Key names: enter, tab, backtab, escape, backspace, delete, space,
    up/down/left/right, home, end, pageup, pagedown, insert,
    f1..f12, and ctrl combos like "ctrl+c", "ctrl+x". Unknown tokens are typed
    literally, so plain characters work too (e.g. ["g", "g"] or ["%"]).

    This handles SEQUENTIAL chords: ["ctrl+b", "d"] sends Ctrl+B, waits step_delay,
    then 'd' — exactly what tmux detach needs. For known apps, `shortcut` is easier.
    """
    ts = _get(session)
    return _act(ts, lambda: ts.send_sequence(keys_, step_delay=step_delay), quiet=settle)


@mcp.tool()
@observed
def shortcut(
    app: str,
    name: str,
    session: str = "default",
    settle: float = 0.2,
    step_delay: float = 0.05,
) -> str:
    """Run a named application shortcut (a sequential chord), then return the screen.

    Examples: shortcut("tmux", "detach") → Ctrl+B d; shortcut("emacs", "save") →
    Ctrl+X Ctrl+S; shortcut("vim", "save-quit") → :wq<Enter>.
    Use `list_shortcuts` to discover what's available, or `define_shortcut` to add one.
    """
    ts = _get(session)
    tokens = shortcuts.resolve(app, name)  # raises with guidance if unknown
    return _act(ts, lambda: ts.send_sequence(tokens, step_delay=step_delay), quiet=settle)


@mcp.tool()
@observed
def list_shortcuts(app: str | None = None) -> dict:
    """List known application shortcuts (all apps, or just `app`).

    Returns {app: {name: ["token", ...]}} so the agent can see the exact key sequence.
    """
    cat = shortcuts.catalog()
    if app is not None:
        if app not in cat:
            raise ValueError(f"No shortcuts for {app!r}. Known: {sorted(cat)}")
        return {app: cat[app]}
    return cat


@mcp.tool()
@observed
def define_shortcut(app: str, name: str, keys_: list[str]) -> dict:
    """Register a custom shortcut so it can be invoked later via `shortcut`.

    keys_ is a token sequence, e.g. ["ctrl+b", "%"] for a tmux vertical split.
    Overrides any built-in of the same app/name. Lives for this server session.
    """
    shortcuts.define(app, name, keys_)
    return {app: shortcuts.catalog()[app]}


@mcp.tool()
@observed
def mouse(
    action: str,
    x: int,
    y: int,
    button: str = "left",
    session: str = "default",
    settle: float = 0.15,
) -> str:
    """Send a mouse event at 1-based column x, row y; returns the updated screen.

    action: click | press | release | move | scroll_up | scroll_down
    button: left | middle | right (ignored for scroll actions).
    Note: the target TUI must have mouse reporting enabled to respond — check the
    `mouse` flag in the snapshot header (or get_modes). If it's off, the click is a
    no-op and this returns a hint instead of silently doing nothing.
    """
    ts = _get(session)
    if not ts.modes.mouse_tracking:
        return (
            f"[warning: '{ts.command}' has not enabled mouse reporting; this "
            f"{action} likely had no effect]\n" + _act(ts, lambda: ts.send_mouse(
                action, x, y, button=button), quiet=settle)
        )
    return _act(ts, lambda: ts.send_mouse(action, x, y, button=button), quiet=settle)


@mcp.tool()
@observed
def wait_for_text(
    text: str,
    session: str = "default",
    timeout: float = 5.0,
) -> str:
    """Wait until `text` appears on screen (or timeout). Returns the screen.

    Raises if the text never appears, so the agent knows the wait failed.
    """
    ts = _get(session)
    if not ts.wait_for_text(text, timeout=timeout):
        raise TimeoutError(
            f"Text {text!r} did not appear within {timeout}s.\n\n{_render(ts)}"
        )
    return _render(ts)


@mcp.tool()
@observed
def wait_for_stable(
    session: str = "default",
    quiet: float = 0.3,
    timeout: float = 5.0,
) -> str:
    """Wait until the screen stops repainting for `quiet` seconds, then return it.

    Use after triggering slow work (a build, a load, an animation) to read a settled
    screen. Note the action tools already auto-wait; this is for waits not tied to a
    single action.
    """
    ts = _get(session)
    stable = ts.wait_for_stable(quiet=quiet, timeout=timeout)
    prefix = "" if stable else "[note: screen still repainting at timeout]\n"
    return prefix + _render(ts)


@mcp.tool()
@observed
def find_text(text: str, session: str = "default") -> list[dict]:
    """Return every (x, y) coordinate where `text` appears (useful before a click)."""
    return _get(session).find_text(text)


@mcp.tool()
@observed
def read_history(session: str = "default", max_lines: int = 200) -> str:
    """Return scrolled-off output (scrollback), oldest→newest, up to max_lines.

    The visible screen only shows the current page; this recovers earlier output —
    long command output, log tails, REPL history that scrolled away.
    """
    hist = _get(session).history_lines()
    if not hist:
        return "[no scrollback yet]"
    return "\n".join(hist[-max_lines:])


@mcp.tool()
@observed
def get_modes(session: str = "default") -> dict:
    """Return the terminal modes the program has enabled (sniffed from its output).

    Keys: app_cursor_keys (arrows use ESC O x), alt_screen, mouse_tracking, mouse_sgr,
    bracketed_paste, focus_events. Useful to know if a click will register, or why
    arrows behave a certain way.
    """
    return _get(session).modes.as_dict()


@mcp.tool()
@observed
def resize(cols: int, rows: int, session: str = "default") -> str:
    """Resize the terminal (sends SIGWINCH). Returns the reflowed screen."""
    ts = _get(session)
    return _act(ts, lambda: ts.set_winsize(rows, cols), quiet=0.15)


@mcp.tool()
@observed
def kill(session: str = "default") -> str:
    """Terminate the program in a session."""
    ts = _get(session)
    ts.kill()
    time.sleep(0.2)
    return _render(ts)


@mcp.tool()
@observed
def list_sessions() -> list[dict]:
    """List active sessions and their state."""
    return [
        {
            "session": name,
            "command": ts.command,
            "alive": ts.alive,
            "size": f"{ts.cols}x{ts.rows}",
            "exit_status": ts.exit_status,
        }
        for name, ts in _sessions.items()
    ]


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
