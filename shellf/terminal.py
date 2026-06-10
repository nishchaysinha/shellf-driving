"""Headless terminal session: spawn a TUI in a PTY and observe/drive it.

This is the engine under Shellf-Driving. It is the "browser + page" equivalent for
terminal UIs:

  * A pseudo-terminal (PTY) hosts the target program so it behaves exactly as if
    running in a real terminal (line discipline, controlling tty, SIGWINCH, ...).
  * A pyte screen emulator parses the program's ANSI/VT output into a structured
    grid of cells -- the "DOM" you can snapshot and assert against.
  * Input methods translate high-level intents (type text, press a key, click)
    into the raw byte sequences a terminal would send to the program.

Everything is thread-safe: a background reader thread continuously drains the PTY
into the emulator, guarded by a lock shared with the snapshot/query methods.
"""

from __future__ import annotations

import copy
import fcntl
import os
import pty
import signal
import struct
import termios
import threading
import time

import pyte

from . import keys as _keys
from . import modes as _modes


class HardenedScreen(pyte.HistoryScreen):
    """A pyte screen that keeps scrollback, swaps the alt buffer, and tolerates the
    sequences real TUIs emit.

    * Subclasses HistoryScreen so output that scrolls off the top is retained and
      readable (see TerminalSession.history_lines).
    * Implements the **alternate screen buffer** (modes 47/1047/1049), which pyte 0.8
      does NOT: entering saves the main buffer + cursor and clears; leaving restores
      them. Without this, the shell prompt never comes back after vim/htop/less exit —
      snapshots stay stuck on the dead app's last frame.
    * Programs like vim send private SGR sequences (e.g. ESC[>4;2m for modifyOtherKeys)
      which pyte dispatches with a ``private=True`` keyword its handlers don't accept,
      raising TypeError and killing the reader thread. We swallow the private flag and
      extra args so emulation degrades gracefully instead of crashing.
    """

    _ALT_MODES = (47, 1047, 1049)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._alt_active = False
        self._saved_buffer = None
        self._saved_cursor = None

    def select_graphic_rendition(self, *attrs, **kwargs):
        super().select_graphic_rendition(*attrs)

    def set_mode(self, *modes, **kwargs):
        if kwargs.get("private") and any(m in self._ALT_MODES for m in modes):
            if not self._alt_active:
                self._alt_active = True
                self._saved_buffer = copy.deepcopy(self.buffer)
                self._saved_cursor = (self.cursor.x, self.cursor.y)
                super().set_mode(*modes, **kwargs)
                self.erase_in_display(2)   # blank the alternate screen
                self.cursor_position()      # home the cursor
                return
        super().set_mode(*modes, **kwargs)

    def reset_mode(self, *modes, **kwargs):
        if kwargs.get("private") and any(m in self._ALT_MODES for m in modes):
            if self._alt_active:
                self._alt_active = False
                super().reset_mode(*modes, **kwargs)
                if self._saved_buffer is not None:
                    self.buffer.clear()
                    self.buffer.update(self._saved_buffer)
                    self.cursor.x, self.cursor.y = self._saved_cursor
                    self._saved_buffer = self._saved_cursor = None
                return
        super().reset_mode(*modes, **kwargs)


# SGR (1006) mouse button codes.
_MOUSE_BUTTONS = {"left": 0, "middle": 1, "right": 2}
_SCROLL_UP = 64
_SCROLL_DOWN = 65
_MOTION_FLAG = 32


class TerminalSession:
    """A single running TUI program observed through a virtual screen."""

    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        cols: int = 80,
        rows: int = 24,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ):
        self.command = command
        self.args = args or []
        self.cols = cols
        self.rows = rows

        # The emulated screen + the byte parser that drives it.
        # history=2000: keep up to 2000 scrolled-off lines per direction.
        self.screen = HardenedScreen(cols, rows, history=2000, ratio=0.5)
        self.stream = pyte.ByteStream(self.screen)

        # Live mode tracking (DECCKM, alt-screen, mouse, ...) sniffed from output.
        self.modes = _modes.TerminalModes()
        self._scanbuf = b""

        self._lock = threading.RLock()
        self._alive = True
        self.exit_status: int | None = None
        # (x, y) of the most recent mouse action, for screenshot overlays.
        self.last_mouse: tuple[int, int] | None = None
        # Repaint tracking, for wait_for_stable.
        self._version = 0
        self._last_change = time.monotonic()
        # Optional observer callbacks (set by the server when the dashboard is on).
        self.on_output = None   # (data: bytes) -> None : raw PTY bytes, for mirroring
        self.on_resize = None   # (cols, rows) -> None
        self.on_exit = None     # (status) -> None

        argv = [command, *self.args]

        pid, fd = pty.fork()
        if pid == 0:
            # ---- child process: become the target program ----
            child_env = os.environ.copy()
            child_env.setdefault("TERM", "xterm-256color")
            child_env["LINES"] = str(rows)
            child_env["COLUMNS"] = str(cols)
            if env:
                child_env.update(env)
            try:
                if cwd:
                    os.chdir(cwd)
                os.execvpe(command, argv, child_env)
            except Exception:  # pragma: no cover - exec failure path
                os._exit(127)

        # ---- parent process: the controller ----
        self.pid = pid
        self.fd = fd
        self.set_winsize(rows, cols)

        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def _read_loop(self) -> None:
        """Drain PTY output into the emulator until the program exits."""
        while True:
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                data = b""
            if not data:
                break
            with self._lock:
                try:
                    self.stream.feed(data)
                except Exception:
                    # Never let an unsupported escape sequence kill the session;
                    # skip the offending byte and keep emulating.
                    for b in data:
                        try:
                            self.stream.feed(bytes([b]))
                        except Exception:
                            continue
                # Mark a repaint (for wait_for_stable).
                self._version += 1
                self._last_change = time.monotonic()
                # Sniff mode changes and answer queries (after feeding, so the
                # cursor-position report reflects the program's latest cursor move).
                self._scan_output(data)
            # Mirror the raw bytes to any observer (outside the lock).
            if self.on_output:
                try:
                    self.on_output(data)
                except Exception:
                    pass

        with self._lock:
            self._alive = False
        try:
            _, status = os.waitpid(self.pid, 0)
            self.exit_status = os.waitstatus_to_exitcode(status)
        except OSError:
            pass
        if self.on_exit:
            try:
                self.on_exit(self.exit_status)
            except Exception:
                pass

    def _scan_output(self, data: bytes) -> None:
        """Track mode changes and answer terminal queries. Called under lock."""
        buf = self._scanbuf + data
        cursor = (self.screen.cursor.x + 1, self.screen.cursor.y + 1)
        reply, consumed = _modes.scan(buf, self.modes, cursor)
        if reply:
            try:
                os.write(self.fd, reply)   # answer on the same channel as keystrokes
            except OSError:
                pass
        # Retain only the unconsumed tail so a sequence split across reads still
        # matches next time; bound it so a query-free stream can't grow the buffer.
        tail = buf[consumed:]
        self._scanbuf = tail[-32:] if len(tail) > 32 else tail

    @property
    def alive(self) -> bool:
        return self._alive

    def set_winsize(self, rows: int, cols: int) -> None:
        """Resize the PTY (sends SIGWINCH to the program) and the emulator."""
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, winsize)
        with self._lock:
            self.rows, self.cols = rows, cols
            self.screen.resize(rows, cols)
        if self.on_resize:
            try:
                self.on_resize(cols, rows)
            except Exception:
                pass

    def kill(self, sig: int = signal.SIGTERM) -> None:
        """Signal the program to terminate."""
        try:
            os.kill(self.pid, sig)
        except ProcessLookupError:
            pass

    # ------------------------------------------------------------------ #
    # Observation
    # ------------------------------------------------------------------ #
    def snapshot(self) -> str:
        """Return the current screen as plain text (one string per row, joined)."""
        with self._lock:
            return "\n".join(self.screen.display)

    def lines(self) -> list[str]:
        with self._lock:
            return list(self.screen.display)

    def cursor(self) -> dict:
        with self._lock:
            c = self.screen.cursor
            return {"x": c.x, "y": c.y, "hidden": c.hidden}

    def cell(self, x: int, y: int) -> dict:
        """Return the character + styling at a 0-based (x, y) position."""
        with self._lock:
            ch = self.screen.buffer[y][x]
            return {
                "char": ch.data,
                "fg": ch.fg,
                "bg": ch.bg,
                "bold": ch.bold,
                "italics": ch.italics,
                "underscore": ch.underscore,
                "reverse": ch.reverse,
            }

    def find_text(self, needle: str) -> list[dict]:
        """Return every (x, y) where `needle` begins on a row."""
        hits = []
        with self._lock:
            for y, line in enumerate(self.screen.display):
                start = 0
                while True:
                    idx = line.find(needle, start)
                    if idx == -1:
                        break
                    hits.append({"x": idx, "y": y})
                    start = idx + 1
        return hits

    def wait_for_text(self, needle: str, timeout: float = 5.0, poll: float = 0.05) -> bool:
        """Block until `needle` appears on screen or `timeout` seconds elapse."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if any(needle in line for line in self.screen.display):
                    return True
            if not self._alive:
                # one last check after exit drains
                with self._lock:
                    return any(needle in line for line in self.screen.display)
            time.sleep(poll)
        return False

    def version(self) -> int:
        """Monotonic repaint counter — snapshot it before an action to detect change."""
        with self._lock:
            return self._version

    def wait_for_stable(
        self,
        quiet: float = 0.3,
        timeout: float = 5.0,
        poll: float = 0.03,
        since_version: int | None = None,
        react: float = 0.35,
    ) -> bool:
        """Block until the screen has stopped repainting for `quiet` seconds.

        The key sync primitive: after an action the TUI may still be drawing, so wait
        for a quiet window before reading.

        since_version: if given, also require a repaint *after* that version before
        declaring stability — i.e. wait for the action's own effect to land, not the
        already-quiet screen from before it. Pass `version()` captured before the
        action. This is what makes the action tools auto-wait correctly.

        react: when since_version is given, how long to wait for the action's *first*
        repaint. If nothing changes within `react`, the action was a no-op (e.g. a key
        the app ignored) and we return immediately instead of stalling for `timeout`.

        Returns True once stable; on timeout returns whether the screen is quiet now.
        """
        start = time.monotonic()
        deadline = start + timeout
        seen_change = since_version is None
        while True:
            now = time.monotonic()
            with self._lock:
                idle = now - self._last_change
                if not seen_change and self._version > since_version:
                    seen_change = True
                alive = self._alive
            if not alive:
                return True
            if seen_change and idle >= quiet:
                return True
            # No reaction to our action within the react window → treat as a no-op.
            if not seen_change and (now - start) >= react:
                return True
            if now >= deadline:
                return idle >= quiet
            time.sleep(poll)

    def history_lines(self) -> list[str]:
        """Return scrolled-off lines (oldest first) from the top scrollback buffer."""
        with self._lock:
            cols = self.screen.columns
            out = []
            for row in self.screen.history.top:
                out.append("".join(row[x].data for x in range(cols)).rstrip())
            return out

    # ------------------------------------------------------------------ #
    # Input
    # ------------------------------------------------------------------ #
    def _write(self, data: bytes) -> None:
        os.write(self.fd, data)

    def send_text(self, text: str) -> None:
        """Type literal text (UTF-8) as if at the keyboard."""
        self._write(text.encode("utf-8"))

    def send_key(self, key: bytes) -> None:
        """Write a pre-resolved key byte sequence (see keys.resolve)."""
        self._write(key)

    def send_sequence(self, tokens: list[str], step_delay: float = 0.04) -> None:
        """Send an ordered sequence of tokens, pausing `step_delay` between each.

        Each token is resolved as a named key (keys.resolve); anything unknown is
        typed literally. Cursor keys automatically use the application-mode (ESC O x)
        form when the program has enabled DECCKM, so arrows work inside vim/less/fzf.
        The inter-step pause matters for prefix chords like tmux's ``Ctrl+B d`` — the
        app needs a beat to register the prefix before the next key.
        """
        app_cursor = self.modes.app_cursor_keys
        for i, token in enumerate(tokens):
            if i:
                time.sleep(step_delay)
            try:
                self._write(_keys.resolve(token, app_cursor=app_cursor))
            except KeyError:
                # A token that looks like a modifier combo (e.g. "ctrl+end") but didn't
                # resolve must NOT be typed literally — that inserts "ctrl+end" into the
                # document. Surface it so the caller fixes the key name.
                if "+" in token and len(token) > 1:
                    raise KeyError(
                        f"Unknown key combo {token!r}; refusing to type it literally. "
                        f"Use a supported key name, or type_text for literal text."
                    )
                self.send_text(token)

    def send_mouse(
        self,
        action: str,
        x: int,
        y: int,
        button: str = "left",
    ) -> None:
        """Send an SGR(1006) mouse event at 1-based column `x`, row `y`.

        action: click | press | release | move | scroll_up | scroll_down
        """
        col, row = x, y  # SGR mouse coords are 1-based
        self.last_mouse = (x, y)

        def seq(code: int, final: str) -> bytes:
            return f"\x1b[<{code};{col};{row}{final}".encode()

        btn = _MOUSE_BUTTONS.get(button, 0)

        if action == "click":
            self._write(seq(btn, "M"))   # press
            self._write(seq(btn, "m"))   # release
        elif action == "press":
            self._write(seq(btn, "M"))
        elif action == "release":
            self._write(seq(btn, "m"))
        elif action == "move":
            self._write(seq(btn + _MOTION_FLAG, "M"))
        elif action == "scroll_up":
            self._write(seq(_SCROLL_UP, "M"))
        elif action == "scroll_down":
            self._write(seq(_SCROLL_DOWN, "M"))
        else:
            raise ValueError(f"Unknown mouse action: {action!r}")
