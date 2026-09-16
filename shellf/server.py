"""MCP server exposing Shellf-Driving to any MCP client (Claude Code, Desktop, ...).

The LLM drives terminal UIs through a small set of tools that mirror how a human
operates a terminal: launch a program, look at the screen, type, press keys, click
with the mouse, scroll, and wait for things to appear.

Multiple named sessions can run at once (default name: "default"), so an agent can
juggle several TUIs (e.g. an editor and a file manager) in one conversation.

Every tool that returns a screen accepts `view` (auto | screen | diff | quiet | none)
so the agent pays for what changed rather than for the whole grid each step; the
server-wide default comes from SHELLF_VIEW. See shellf/view.py and shellf/config.py.

Run it:
    python -m shellf.server
"""

from __future__ import annotations

import functools
import os
import signal
import sys
import time

from mcp.server.fastmcp import FastMCP, Image

from . import config, observe, shortcuts, view as _view
from .render import Renderer
from .terminal import TerminalSession

mcp = FastMCP("shellf-driving")

# name -> TerminalSession
_sessions: dict[str, TerminalSession] = {}
# name -> what the agent was last shown (the diff baseline)
_views: dict[str, _view.ViewState] = {}

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


def _summarize_result(res) -> str:
    """First line / short form of a tool result, for the dashboard timeline."""
    if isinstance(res, Image):
        return "<png image>"
    if isinstance(res, str):
        return res.split("\n", 1)[0][:160]
    s = str(res)
    return s[:160]


def observed(fn):
    """Publish a timed tool-call event (args, duration, result/error) to the
    dashboard, preserving the signature so FastMCP still derives the correct
    schema (inspect.signature follows __wrapped__)."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not observe.hub.active:
            return fn(*args, **kwargs)
        t0 = time.monotonic()
        event = {
            "kind": "tool", "name": fn.__name__,
            "session": kwargs.get("session", "default"),
            "args": _summarize_args(kwargs),
        }
        try:
            result = fn(*args, **kwargs)
        except Exception as e:
            event["ms"] = round((time.monotonic() - t0) * 1000)
            event["error"] = f"{type(e).__name__}: {e}"[:200]
            observe.hub.publish_event(event)
            raise
        event["ms"] = round((time.monotonic() - t0) * 1000)
        event["result"] = _summarize_result(result)
        observe.hub.publish_event(event)
        return result
    return wrapper


def _get(session: str) -> TerminalSession:
    if session not in _sessions:
        raise ValueError(
            f"No session named {session!r}. Call launch first. "
            f"Active sessions: {sorted(_sessions) or 'none'}"
        )
    return _sessions[session]


def _state(session: str) -> _view.ViewState:
    if session not in _views:
        _views[session] = _view.ViewState()
    return _views[session]


def _show(session: str, ts: TerminalSession, mode: str | None = None, note: str = "") -> str:
    """Render the screen for the model in the requested (or configured) shape."""
    return _view.render(
        ts, _state(session), mode or config.view_mode(),
        max_rows=config.max_rows(), note=note,
    )


def _render(ts: TerminalSession) -> str:
    """Full-screen render with no diff baseline — kept for library/test callers."""
    return _view.render(ts, _view.ViewState(), "screen", max_rows=config.max_rows())


def _act(ts: TerminalSession, send, session: str = "default", mode: str | None = None,
         quiet: float = 0.12, timeout: float = 2.0, note: str = "") -> str:
    """Run an input action, then auto-wait for the resulting repaint to settle.

    Captures the repaint version before sending so we wait for *this action's* effect
    (not the already-quiet prior screen), then for a short quiet window. Returns the
    settled screen. This is the Playwright-style auto-wait baked into every action.
    """
    baseline = ts.version()
    send()
    ts.wait_for_stable(quiet=quiet, timeout=timeout, since_version=baseline)
    return _show(session, ts, mode, note=note)


@mcp.tool()
@observed
def launch(
    command: str,
    args: list[str] | None = None,
    session: str = "default",
    cols: int = 0,
    rows: int = 0,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    settle: float = 0.3,
    view: str | None = None,
) -> str:
    """Launch a terminal program inside a virtual terminal.

    command: executable to run (e.g. "vim", "htop", "python").
    args: argument list (e.g. ["file.txt"]).
    session: name to address this instance in later calls.
    cols/rows: terminal size (0 = server default, normally 80x24).
    cwd: working directory.
    env: extra environment for the program — the lever for making a TUI cheaper and
      more predictable to drive. e.g. {"NO_COLOR": "1"} (plainer output),
      {"PAGER": "cat"} (stop git/man paging), {"TERM": "xterm-256color"} to override,
      {"LANG": "C.UTF-8"}. Merged over SHELLF_ENV and the server's own environment.
    settle: seconds to wait for first paint before snapshotting.
    view: auto | screen | diff | quiet | none (see `snapshot`). Launch defaults to the
      full screen regardless, since there is nothing to diff against yet.
    Returns the initial screen.
    """
    if session in _sessions and _sessions[session].alive:
        raise ValueError(f"Session {session!r} is already running. Kill it first.")
    merged = {**config.base_env(), **(env or {})}
    ts = TerminalSession(
        command, args=args, cols=cols or config.cols(), rows=rows or config.rows(),
        cwd=cwd, env=merged or None,
    )
    _sessions[session] = ts
    _views[session] = _view.ViewState()
    _wire_observer(session, ts)
    # Wait for the first paint to settle (programs often need a query answered first).
    ts.wait_for_stable(quiet=settle, timeout=max(2.0, settle * 5), since_version=0)
    return _show(session, ts, view)


@mcp.tool()
@observed
def snapshot(
    session: str = "default",
    view: str | None = None,
    region: list[int] | None = None,
) -> str:
    """Return the current screen contents (the 'what does it look like now' tool).

    view: how much to return. Defaults to the FULL screen here (an explicit look always
      shows you the screen); the action tools default to `auto` instead.
      * auto — only the rows that changed since you last saw the screen,
        unless most of the screen changed, in which case the whole thing. Cheap on
        editors/menus, honest on live dashboards that redraw everything.
      * screen — the full grid, always.
      * diff — only changed rows (falls back to the full screen on first look).
      * quiet — the header line only: size, liveness, cursor, and how many rows moved.
      * none — just "ok".
      The server-wide default is set by SHELLF_VIEW.
    region: [x, y, width, height], 0-based, to read one rectangle (a status bar, one
      tmux pane) instead of the whole screen. Implies view="screen" for that rectangle.
    """
    ts = _get(session)
    if region is not None:
        if len(region) != 4:
            raise ValueError("region must be [x, y, width, height]")
        x, y, w, h = region
        lines = ts.lines()[y:y + h]
        body = "\n".join(line[x:x + w].rstrip() for line in lines) or "[blank region]"
        return _view.header(ts, f"region x={x} y={y} {w}x{h}") + "\n" + body
    # An explicit look always shows. Diffing is right for observe-after-act, where the
    # agent already knows what it just did — but an agent that calls `snapshot` is
    # re-grounding itself, and answering "no change" would blind it at exactly the
    # moment it asked to see. Pass view= to override.
    return _show(session, ts, view or "screen")


@mcp.tool()
@observed
def screenshot(
    session: str = "default",
    scale: float = 0.0,
    grid: bool = False,
) -> Image:
    """Return a PNG image of the terminal — colors, bold, the block cursor outline,
    and a red marker where the mouse last acted.

    Use this when layout or color matters (lazygit, k9s, btop) or when the plain-text
    snapshot is ambiguous. For pure text content, `snapshot` is far cheaper: an image
    of an 80x24 terminal costs roughly a thousand tokens.

    scale: shrink the image (0.5 halves each dimension → ~a quarter of the image
      tokens). 0 = the SHELLF_SHOT_SCALE default (normally 1.0).
    grid: draw a labelled 1-based column/row ruler over the image, so coordinates for
      `mouse` can be read straight off the picture instead of counted.
    """
    ts = _get(session)
    png = _renderer.render(
        ts.screen, ts.cursor(), ts.last_mouse,
        scale=scale or config.shot_scale(), grid=grid,
    )
    return Image(data=png, format="png")


@mcp.tool()
@observed
def type_text(
    text: str,
    session: str = "default",
    settle: float = 0.12,
    paste: bool | None = None,
    view: str | None = None,
) -> str:
    """Type literal text into the program, then return the updated screen.

    paste: how to deliver the text.
      * None (default) — auto: multi-line text is sent as a bracketed paste when the
        app has enabled bracketed-paste mode, so editors insert it verbatim instead
        of autoindent-mangling each line. Single-line text is typed normally.
      * True — force a bracketed paste (text inserted verbatim, not interpreted as
        keystrokes; don't use for commands like ":wq").
      * False — always type raw characters.

    Auto-waits for the resulting repaint to settle before returning.
    """
    ts = _get(session)
    use_paste = paste if paste is not None else ("\n" in text and ts.modes.bracketed_paste)
    send = (lambda: ts.send_paste(text)) if use_paste else (lambda: ts.send_text(text))
    return _act(ts, send, session=session, mode=view, quiet=settle)


@mcp.tool()
@observed
def press(
    keys: list[str],
    session: str = "default",
    settle: float = 0.15,
    step_delay: float = 0.04,
    view: str | None = None,
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
    return _act(ts, lambda: ts.send_sequence(keys, step_delay=step_delay),
                session=session, mode=view, quiet=settle)


@mcp.tool()
@observed
def run(
    steps: list[str],
    session: str = "default",
    view: str | None = None,
    settle: float = 0.15,
    stop_on_error: bool = True,
) -> str:
    """Run several actions in ONE call and return only the final screen.

    The single biggest saving available to a driving agent: five separate tool calls
    means five screens in the transcript, four of which were only ever a stepping
    stone. This returns one.

    Each step is a "verb:payload" string:
      type:<text>        type literal text (payload may contain colons)
      paste:<text>       force a bracketed paste
      press:<k> <k> …    space-separated key tokens, in order ("press:ctrl+b d")
      click:<text>       find text on screen and click its centre
      mouse:<act>,<x>,<y>  e.g. "mouse:scroll_down,40,12"
      wait:<text>        wait until text appears (fails the step if it never does)
      waitgone:<text>    wait until text disappears
      regex:<pattern>    wait until a regex matches on screen
      stable[:secs]      wait for the repainting to stop (default 0.3s quiet)
      sleep:<secs>       unconditional pause — use only when nothing observable marks
                         readiness; the waits above are strictly better
      raw:<escaped>      raw bytes, Python escapes decoded (e.g. "raw:\\x1b[A")

    Example — write a file in vim in one call:
      ["press:i", "type:hello world", "press:escape", "type::wq", "press:enter"]

    stop_on_error: stop at the first failing step (default) and report which one;
    otherwise keep going and list every failure. The returned screen is the state
    after the last step that ran.
    """
    ts = _get(session)
    if not steps:
        raise ValueError("steps is empty — pass at least one \"verb:payload\" string.")
    log: list[str] = []
    baseline = ts.version()
    for i, step in enumerate(steps, 1):
        try:
            note = _run_step(ts, step, settle)
            if note:
                log.append(f"  {i}. {step} → {note}")
        except Exception as e:
            log.append(f"  {i}. {step} → FAILED: {type(e).__name__}: {e}")
            if stop_on_error:
                ts.wait_for_stable(quiet=settle, timeout=2.0, since_version=baseline)
                head = f"[run: stopped at step {i}/{len(steps)}]\n" + "\n".join(log)
                return head + "\n" + _show(session, ts, view)
    ts.wait_for_stable(quiet=settle, timeout=2.0, since_version=baseline)
    failures = [line for line in log if "FAILED" in line]
    head = f"[run: {len(steps)} step(s)"
    head += f", {len(failures)} failed]\n" + "\n".join(log) + "\n" if failures else "]\n"
    return head + _show(session, ts, view)


def _run_step(ts: TerminalSession, step: str, settle: float) -> str:
    """Execute one "verb:payload" step. Returns a short note, or "" when unremarkable."""
    verb, _, payload = step.partition(":")
    verb = verb.strip().lower()

    if verb == "type":
        ts.send_text(payload)
    elif verb == "paste":
        ts.send_paste(payload)
    elif verb in ("press", "key", "keys"):
        tokens = payload.split()
        if not tokens:
            raise ValueError("press needs at least one key name")
        ts.send_sequence(tokens, step_delay=0.04)
    elif verb == "click":
        hits = ts.find_text(payload)
        if not hits:
            raise ValueError(f"text {payload!r} not on screen")
        ts.send_mouse("click", hits[0]["x"] + len(payload) // 2 + 1, hits[0]["y"] + 1)
    elif verb == "mouse":
        parts = [p.strip() for p in payload.split(",")]
        if len(parts) < 3:
            raise ValueError("mouse needs 'action,x,y'")
        ts.send_mouse(parts[0], int(parts[1]), int(parts[2]),
                      button=parts[3] if len(parts) > 3 else "left")
    elif verb == "wait":
        if not ts.wait_for_text(payload, timeout=10.0):
            raise TimeoutError(f"{payload!r} never appeared")
        return "appeared"
    elif verb == "waitgone":
        if not ts.wait_for_text_gone(payload, timeout=10.0):
            raise TimeoutError(f"{payload!r} still on screen")
        return "gone"
    elif verb == "regex":
        hit = ts.wait_for_regex(payload, timeout=10.0)
        if hit is None:
            raise TimeoutError(f"pattern {payload!r} never matched")
        return f"matched {hit!r}"
    elif verb == "stable":
        ts.wait_for_stable(quiet=float(payload) if payload else 0.3, timeout=10.0)
    elif verb == "sleep":
        time.sleep(min(float(payload), 10.0))
    elif verb == "raw":
        ts.send_bytes(payload.encode().decode("unicode_escape").encode("latin-1"))
    else:
        raise ValueError(
            f"Unknown step verb {verb!r}. Use: type, paste, press, click, mouse, "
            f"wait, waitgone, regex, stable, sleep, raw."
        )
    # Input steps settle briefly so the next step acts on a drawn screen.
    if verb in ("type", "paste", "press", "key", "keys", "click", "mouse", "raw"):
        ts.wait_for_stable(quiet=settle, timeout=2.0)
    return ""


@mcp.tool()
@observed
def shortcut(
    app: str,
    name: str,
    session: str = "default",
    settle: float = 0.2,
    step_delay: float = 0.05,
    view: str | None = None,
) -> str:
    """Run a named application shortcut (a sequential chord), then return the screen.

    Examples: shortcut("tmux", "detach") → Ctrl+B d; shortcut("emacs", "save") →
    Ctrl+X Ctrl+S; shortcut("vim", "save-quit") → :wq<Enter>.
    Use `list_shortcuts` to discover what's available, or `define_shortcut` to add one.
    """
    ts = _get(session)
    tokens = shortcuts.resolve(app, name)  # raises with guidance if unknown
    return _act(ts, lambda: ts.send_sequence(tokens, step_delay=step_delay),
                session=session, mode=view, quiet=settle)


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
def define_shortcut(app: str, name: str, keys: list[str]) -> dict:
    """Register a custom shortcut so it can be invoked later via `shortcut`.

    keys is a token sequence, e.g. ["ctrl+b", "%"] for a tmux vertical split.
    Overrides any built-in of the same app/name. Lives for this server session.
    """
    shortcuts.define(app, name, keys)
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
    to_x: int = 0,
    to_y: int = 0,
    view: str | None = None,
) -> str:
    """Send a mouse event at 1-based column x, row y; returns the updated screen.

    action: click | double_click | triple_click | press | release | move | drag |
            scroll_up | scroll_down
      double_click selects a word in most apps, triple_click a line.
      drag needs to_x/to_y: presses at (x,y), moves across, releases at (to_x,to_y) —
      one gesture, because apps track the motion between the ends.
    button: left | middle | right (ignored for scroll actions).
    Note: the target TUI must have mouse reporting enabled to respond — check the
    `mouse` flag in the snapshot header (or get_modes). If it's off, the click is a
    no-op and this returns a hint instead of silently doing nothing.
    """
    ts = _get(session)
    if action == "drag":
        if not (to_x and to_y):
            raise ValueError("drag needs to_x and to_y (the release point)")
        send = lambda: ts.send_drag(x, y, to_x, to_y, button=button)
    else:
        send = lambda: ts.send_mouse(action, x, y, button=button)
    warn = "" if ts.modes.mouse_tracking else (
        f"[warning: '{ts.command}' has not enabled mouse reporting; this "
        f"{action} likely had no effect]\n"
    )
    return warn + _act(ts, send, session=session, mode=view, quiet=settle)


@mcp.tool()
@observed
def click_text(
    text: str,
    session: str = "default",
    occurrence: int = 1,
    button: str = "left",
    settle: float = 0.15,
    view: str | None = None,
) -> str:
    """Find `text` on screen and click the center of it — no coordinate math needed.

    occurrence: 1-based index when the text appears more than once (top-to-bottom,
    left-to-right order). Raises with the match list if out of range, and warns when
    the app has not enabled mouse reporting (the click would be a no-op).
    """
    ts = _get(session)
    hits = ts.find_text(text)
    if not hits:
        raise ValueError(f"Text {text!r} not found on screen.\n\n{_render(ts)}")
    if not (1 <= occurrence <= len(hits)):
        raise ValueError(
            f"occurrence={occurrence} out of range: {len(hits)} match(es) at {hits}"
        )
    hit = hits[occurrence - 1]
    # find_text is 0-based; mouse coords are 1-based. Aim at the text's center.
    x = hit["x"] + len(text) // 2 + 1
    y = hit["y"] + 1
    warn = "" if ts.modes.mouse_tracking else (
        f"[warning: '{ts.command}' has not enabled mouse reporting; this click "
        f"likely had no effect]\n"
    )
    return warn + _act(ts, lambda: ts.send_mouse("click", x, y, button=button),
                       session=session, mode=view, quiet=settle)


@mcp.tool()
@observed
def wait_for_text(
    text: str,
    session: str = "default",
    timeout: float = 5.0,
    regex: bool = False,
    view: str | None = None,
) -> str:
    """Wait until `text` appears on screen (or timeout). Returns the screen.

    regex: treat `text` as a regular expression, matched per row and across the whole
    screen. Status lines are usually shaped rather than fixed — "3 of 17", "[##  ] 40%",
    "ERROR \\d+" — and only a pattern can wait for those.
    Raises if the text never appears, so the agent knows the wait failed.
    """
    ts = _get(session)
    if regex:
        hit = ts.wait_for_regex(text, timeout=timeout)
        if hit is None:
            raise TimeoutError(
                f"Pattern {text!r} did not match within {timeout}s.\n\n{_render(ts)}"
            )
        return _show(session, ts, view, note=f"matched {hit!r}")
    if not ts.wait_for_text(text, timeout=timeout):
        raise TimeoutError(
            f"Text {text!r} did not appear within {timeout}s.\n\n{_render(ts)}"
        )
    return _show(session, ts, view)


@mcp.tool()
@observed
def wait_for_text_gone(
    text: str,
    session: str = "default",
    timeout: float = 5.0,
    view: str | None = None,
) -> str:
    """Wait until `text` disappears from the screen (spinner, "Loading…", a dialog).

    Raises if the text is still visible at timeout, so the agent knows the wait failed.
    """
    ts = _get(session)
    if not ts.wait_for_text_gone(text, timeout=timeout):
        raise TimeoutError(
            f"Text {text!r} still on screen after {timeout}s.\n\n{_render(ts)}"
        )
    return _show(session, ts, view)


@mcp.tool()
@observed
def wait_for_stable(
    session: str = "default",
    quiet: float = 0.3,
    timeout: float = 5.0,
    view: str | None = None,
) -> str:
    """Wait until the screen stops repainting for `quiet` seconds, then return it.

    Use after triggering slow work (a build, a load, an animation) to read a settled
    screen. Note the action tools already auto-wait; this is for waits not tied to a
    single action.
    """
    ts = _get(session)
    stable = ts.wait_for_stable(quiet=quiet, timeout=timeout)
    prefix = "" if stable else "[note: screen still repainting at timeout]\n"
    return prefix + _show(session, ts, view)


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
    """Return the terminal modes the program has enabled (sniffed from its output),
    plus the server's effective configuration.

    Mode keys: app_cursor_keys (arrows use ESC O x), alt_screen, mouse_tracking,
    mouse_sgr, bracketed_paste, focus_events. Useful to know if a click will register,
    or why arrows behave a certain way.
    """
    return {**_get(session).modes.as_dict(), "server_config": config.as_dict()}


@mcp.tool()
@observed
def resize(cols: int, rows: int, session: str = "default", view: str | None = None) -> str:
    """Resize the terminal (sends SIGWINCH). Returns the reflowed screen."""
    ts = _get(session)
    return _act(ts, lambda: ts.set_winsize(rows, cols), session=session, mode=view, quiet=0.15)


_SIGNALS = {
    "term": signal.SIGTERM,
    "int": signal.SIGINT,
    "hup": signal.SIGHUP,
    "quit": signal.SIGQUIT,
    "kill": signal.SIGKILL,
}


@mcp.tool()
@observed
def kill(session: str = "default", sig: str = "term", view: str | None = None) -> str:
    """Terminate the program in a session and wait for it to actually die.

    sig: term | int | hup | quit | kill. Escalates to SIGKILL automatically if the
    program ignores the signal (interactive shells ignore SIGTERM), so the session
    name is immediately free for a new `launch` when this returns.
    """
    ts = _get(session)
    signum = _SIGNALS.get(sig)
    if signum is None:
        raise ValueError(f"Unknown signal {sig!r}. Choose from: {sorted(_SIGNALS)}")
    dead = ts.kill(signum)
    prefix = "" if dead else "[warning: process still alive after SIGKILL]\n"
    return prefix + _show(session, ts, view)


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
