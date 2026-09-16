"""A/B benchmark: what driving a real TUI costs an agent, before vs after.

Runs the same realistic task against the same real program twice — once the way the
server behaved before (one action per MCP call, full screen returned every time), once
the way it behaves now (batched `run` steps, `view="auto"`) — and reports the characters
the model would have had to read.

Characters, not tokens, is the honest unit here: no tokenizer is installed, and the ratio
is what matters anyway. ~3.6 chars/token is a reasonable estimate for terminal text.

    .venv/bin/python bench/token_bench.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shellf import server, view as _view  # noqa: E402

CHARS_PER_TOKEN = 3.6


def _fresh(session: str) -> None:
    ts = server._sessions.pop(session, None)
    if ts is not None:
        try:
            ts.kill()
        except Exception:
            pass
    server._views.pop(session, None)


def _words_file() -> str:
    path = os.path.join(tempfile.gettempdir(), "shellf_bench_words.txt")
    if not os.path.exists(path):
        with open(path, "w") as fh:
            for i in range(4000):
                fh.write(f"line {i:05d} the quick brown fox jumps over the lazy dog\n")
    return path


# Each scenario: (name, launch kwargs, list of step-strings for `run`)
def scenarios():
    words = _words_file()
    edit = os.path.join(tempfile.gettempdir(), "shellf_bench_edit.txt")
    return [
        ("vim", dict(command="vim", args=["-u", "NONE", edit], cols=80, rows=24),
         ["press:i", "type:first line", "press:enter", "type:second line",
          "press:escape", "type::w", "press:enter", "press:g g", "press:G"]),
        ("less", dict(command="less", args=[words], cols=80, rows=24),
         ["press:pagedown", "press:pagedown", "press:pagedown",
          "type:/fox", "press:enter", "press:n", "press:n"]),
        ("python-repl", dict(command="python3", args=["-q"], cols=80, rows=24),
         ["type:2+2", "press:enter", "type:sum(range(10))", "press:enter",
          "type:'-'*20", "press:enter", "type:len('shellf')", "press:enter"]),
        ("bash", dict(command="bash", args=["--norc", "--noprofile"], cols=80, rows=24),
         ["type:echo one", "press:enter", "type:echo two", "press:enter",
          "type:pwd", "press:enter", "type:date +%Y", "press:enter"]),
        ("htop", dict(command="htop", cols=80, rows=24, settle=1.5),
         ["press:f6", "press:down", "press:down", "press:enter",
          "press:f5", "press:f5"]),
        ("btop", dict(command="btop", cols=100, rows=30, settle=2.5),
         ["type:2", "type:3", "type:4", "press:down", "press:down", "type:1"]),
        ("nano", dict(command="nano", args=["--ignorercfiles", edit + ".nano"], cols=80, rows=24),
         ["type:hello from nano", "press:enter", "type:second line",
          "press:ctrl+o", "press:enter", "press:ctrl+x"]),
        ("tmux", dict(command="tmux", args=["-L", "shellfbench", "new", "-A", "-s", "b"],
                      cols=80, rows=24),
         ["press:ctrl+b c", "press:ctrl+b %", "press:ctrl+b o",
          "type:echo pane", "press:enter", "press:ctrl+b d"]),
    ]


def measure(name, launch_kwargs, steps):
    """Return (before_chars, after_chars, n_calls_before, n_calls_after, notes)."""
    notes = []

    # ---- BEFORE: one tool call per step, full screen each time ----
    _fresh("bench")
    try:
        first = server.launch(session="bench", view="screen", **launch_kwargs)
    except Exception as e:
        return None, None, 0, 0, [f"launch failed: {e}"]
    before = len(first)
    calls_before = 1
    for step in steps:
        try:
            server._run_step(server._sessions["bench"], step, 0.15)
        except Exception as e:
            notes.append(f"step {step!r}: {e}")
        before += len(server.snapshot(session="bench", view="screen"))
        calls_before += 1
    _fresh("bench")

    # ---- AFTER: launch + one batched run, view=auto ----
    _fresh("bench")
    try:
        first = server.launch(session="bench", view="screen", **launch_kwargs)
    except Exception as e:
        return None, None, 0, 0, [f"launch failed: {e}"]
    after = len(first)
    calls_after = 1
    after += len(server.run(steps, session="bench", view="auto"))
    calls_after += 1
    _fresh("bench")

    # ---- also: per-step but view=auto (no batching), to separate the two effects ----
    _fresh("bench")
    server.launch(session="bench", view="screen", **launch_kwargs)
    diffonly = len(first)
    for step in steps:
        try:
            server._run_step(server._sessions["bench"], step, 0.15)
        except Exception:
            pass
        diffonly += len(server.snapshot(session="bench", view="auto"))
    _fresh("bench")

    return before, after, calls_before, calls_after, notes, diffonly


def main():
    rows = []
    for name, kwargs, steps in scenarios():
        sys.stderr.write(f"  running {name} ...\n")
        sys.stderr.flush()
        t0 = time.monotonic()
        try:
            res = measure(name, kwargs, steps)
        except Exception as e:
            sys.stderr.write(f"    {name} errored: {type(e).__name__}: {e}\n")
            continue
        if res[0] is None:
            sys.stderr.write(f"    {name} skipped: {res[4]}\n")
            continue
        before, after, cb, ca, notes, diffonly = res
        rows.append((name, before, diffonly, after, cb, ca, round(time.monotonic() - t0, 1), notes))

    print()
    print(f"{'scenario':<12} {'BEFORE':>9} {'auto-view':>10} {'+batched':>10} "
          f"{'calls':>9} {'saving':>8}")
    print("-" * 66)
    tb = td = ta = 0
    for name, before, diffonly, after, cb, ca, secs, notes in rows:
        tb += before; td += diffonly; ta += after
        print(f"{name:<12} {before:>9,} {diffonly:>10,} {after:>10,} "
              f"{cb:>4}→{ca:<4} {100 - after * 100 // before:>7}%")
        for n in notes:
            print(f"             note: {n}")
    print("-" * 66)
    if tb:
        print(f"{'TOTAL':<12} {tb:>9,} {td:>10,} {ta:>10,} "
              f"{'':>9} {100 - ta * 100 // tb:>7}%")
        print()
        print(f"chars → est. tokens (@{CHARS_PER_TOKEN}/tok):  "
              f"before {tb / CHARS_PER_TOKEN:,.0f}  "
              f"auto-view {td / CHARS_PER_TOKEN:,.0f}  "
              f"auto+batched {ta / CHARS_PER_TOKEN:,.0f}")


if __name__ == "__main__":
    main()
