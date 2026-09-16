"""Response shaping — how much screen an action gives back to the model.

Every action tool used to return the full grid. A 30-step agent session on an 80x24
terminal therefore shipped ~30 near-identical screens into the conversation, and the
model paid for all of them. Most of those rows had not changed.

This module turns one settled screen into the *smallest faithful* description of it,
given what the agent was last shown:

    screen   full trimmed grid              (the old behavior; always correct)
    diff     header + only the changed rows (cheap when a TUI repaints locally)
    auto     diff when the change is small, full screen otherwise  (the default)
    quiet    header only — "something changed, ask if you care"
    none     a bare "ok"

`auto` is the important one. A diff is a *win* on vim, less, nano, tmux, a REPL — apps
that repaint a few rows — and pure *noise* on htop/btop/top/watch, which redraw nearly
every row on a timer. So auto measures the change and picks: below the threshold you get
the diff, above it the full screen. The agent never has to know which kind of app it is
driving, and never silently loses information.

Disclosure rule: the diff baseline advances only when content is actually shown. After a
run of `quiet` steps, the next `diff` reports everything that happened since the agent
last *looked* — not just since the last call.
"""

from __future__ import annotations

MODES = ("auto", "screen", "diff", "quiet", "none")

class ViewState:
    """Per-session memory of the last rows the agent was actually shown."""

    __slots__ = ("rows",)

    def __init__(self) -> None:
        self.rows: list[str] | None = None


def _trim(rows: list[str]) -> list[str]:
    """Strip trailing whitespace per row and drop trailing blank rows."""
    out = [r.rstrip() for r in rows]
    while out and not out[-1]:
        out.pop()
    return out


def _cap(lines: list[str], limit: int) -> list[str]:
    """Bound a response to `limit` rows, elliding the middle (keeps top and bottom —
    a TUI's header and status bar are usually the informative parts)."""
    if limit <= 0 or len(lines) <= limit:
        return lines
    head = limit // 2
    tail = limit - head - 1
    return lines[:head] + [f"[… {len(lines) - head - tail} rows elided (SHELLF_MAX_ROWS) …]"] + (
        lines[-tail:] if tail else []
    )


def header(ts, note: str = "") -> str:
    """The one line that is always worth its tokens: size, liveness, cursor, modes."""
    cur = ts.cursor()
    status = "running" if ts.alive else f"exited (status={ts.exit_status})"
    m = ts.modes
    flags = "".join(
        f" {n}" for n, on in (
            ("alt-screen", m.alt_screen), ("app-cursor", m.app_cursor_keys),
            ("mouse", m.mouse_tracking), ("paste", m.bracketed_paste),
        ) if on
    )
    extra = f" | {note}" if note else ""
    return (
        f"[{ts.cols}x{ts.rows} | {status} | cursor=({cur['x']},{cur['y']})"
        f"{' hidden' if cur['hidden'] else ''}{flags}{extra}]"
    )


def changed_rows(before: list[str] | None, after: list[str]) -> list[int]:
    """0-based indices of rows that differ (a shorter `before` counts as blank rows)."""
    if before is None:
        return list(range(len(after)))
    n = max(len(before), len(after))
    out = []
    for i in range(n):
        b = before[i] if i < len(before) else ""
        a = after[i] if i < len(after) else ""
        if b != a:
            out.append(i)
    return out


def _diff_body(after: list[str], idx: list[int]) -> list[str]:
    """Changed rows, each prefixed with its 1-based row number.

    A row that is now empty prints as just its number — that is how the model sees
    "this line was cleared" without a second notation to learn.
    """
    width = max(2, len(str(len(after))))
    return [f"{i + 1:>{width}}| {after[i] if i < len(after) else ''}" for i in idx]


def render(ts, state: ViewState, mode: str = "auto", max_rows: int = 0,
           note: str = "") -> str:
    """Render the settled screen for `ts` according to `mode`, updating `state`.

    Returns the text the tool hands back to the model.
    """
    if mode not in MODES:
        raise ValueError(f"Unknown view {mode!r}. Choose from: {list(MODES)}")

    rows = _trim(ts.lines())
    idx = changed_rows(state.rows, rows)
    total = max(len(rows), len(state.rows or []))
    ratio = (len(idx) / total) if total else 0.0

    if mode == "none":
        return "ok"

    if mode == "quiet":
        # Nothing disclosed → the baseline must NOT advance, so a later diff still
        # reports everything that happened while the agent was not looking.
        if state.rows is None:
            return header(ts, f"{len(rows)} rows, not shown (view=quiet)")
        return header(ts, f"{len(idx)} row(s) changed" if idx else "no change")

    if mode == "auto":
        # Measured, not assumed: on a sparse screen (vim showing eight lines and a
        # column of "~") the row-number prefixes make a diff BIGGER than just resending
        # the screen. So auto builds both and returns whichever is actually shorter —
        # a diff can then never be a regression, only a win.
        if state.rows is None or not idx:
            mode = "diff" if (state.rows is not None and not idx) else "screen"
        else:
            diff_text = _diff_text(ts, rows, idx, total, max_rows)
            screen_text = _screen_text(ts, rows, max_rows, note)
            state.rows = rows
            return diff_text if len(diff_text) <= len(screen_text) else screen_text

    if mode == "diff":
        if state.rows is None:
            mode = "screen"          # nothing to diff against yet
        elif not idx:
            return header(ts, "no change")
        else:
            state.rows = rows
            return _diff_text(ts, rows, idx, total, max_rows)

    # -- full screen --
    state.rows = rows
    return _screen_text(ts, rows, max_rows, note)


def _diff_text(ts, rows: list[str], idx: list[int], total: int, max_rows: int) -> str:
    body = _cap(_diff_body(rows, idx), max_rows)
    return header(ts, f"{len(idx)}/{total} rows changed") + "\n" + "\n".join(body)


def _screen_text(ts, rows: list[str], max_rows: int, note: str = "") -> str:
    if not rows:
        return header(ts, "blank screen") + "\n[blank screen]"
    body = _cap(rows, max_rows)
    trimmed = ts.rows - len(rows)
    footer = f"\n[+{trimmed} blank row(s) below]" if trimmed > 0 else ""
    return header(ts, note) + "\n" + "\n".join(body) + footer
