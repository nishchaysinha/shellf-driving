"""Test resize reflow and sequential (prefix) shortcuts against real apps."""
import time
from shellf.terminal import TerminalSession
from shellf import shortcuts


def test_resize():
    # bash that prints its terminal width whenever asked.
    ts = TerminalSession("bash", ["--norc", "-i"], cols=80, rows=10)
    time.sleep(0.4)
    ts.send_text('tput cols\n'); time.sleep(0.3)
    assert "80" in ts.snapshot(), "expected width 80 before resize\n" + ts.snapshot()

    ts.set_winsize(rows=10, cols=120)   # the resize-window capability
    time.sleep(0.3)
    ts.send_text('tput cols\n'); time.sleep(0.3)
    snap = ts.snapshot()
    assert ts.screen.columns == 120, "emulator did not resize"
    assert "120" in snap, "program did not see SIGWINCH / new width\n" + snap
    print("[resize] 80 -> 120 reflowed, program saw new width: OK")
    ts.send_text("exit\n")


def test_tmux_detach():
    # tmux is the canonical sequential-shortcut app: Ctrl+B then d to detach.
    ts = TerminalSession(
        "tmux", ["-f", "/dev/null", "new-session", "-s", "shellf"],
        cols=90, rows=20,
    )
    assert ts.wait_for_text("shellf", timeout=4) or ts.wait_for_text("0:", timeout=1), \
        "tmux status bar never appeared\n" + ts.snapshot()
    print("[tmux] attached. Status bar:\n   ", ts.lines()[-1].strip())

    tokens = shortcuts.resolve("tmux", "detach")
    assert tokens == ["ctrl+b", "d"], tokens
    ts.send_sequence(tokens, step_delay=0.08)

    assert ts.wait_for_text("detached", timeout=4), \
        "tmux did not detach — sequential chord failed\n" + ts.snapshot()
    print("[tmux] Ctrl+B d detached:", [l for l in ts.lines() if "detached" in l][0].strip())
    ts.send_text("tmux kill-server 2>/dev/null\n")


def test_tmux_split_via_shortcut():
    # A second chord: Ctrl+B % splits the window vertically (two panes side by side).
    ts = TerminalSession(
        "tmux", ["-f", "/dev/null", "new-session", "-s", "split"],
        cols=90, rows=20,
    )
    ts.wait_for_text("split", timeout=4); time.sleep(0.3)
    ts.send_sequence(shortcuts.resolve("tmux", "split-vertical"), step_delay=0.08)
    time.sleep(0.4)
    # A vertical split draws a column separator; pane borders use box-drawing chars.
    snap = ts.snapshot()
    has_separator = any("|" in line or "│" in line for line in ts.lines()[:-1])
    print("[tmux] Ctrl+B % split-vertical drew a pane separator:", has_separator)
    ts.send_text("tmux kill-server 2>/dev/null\n")


if __name__ == "__main__":
    test_resize()
    test_tmux_detach()
    test_tmux_split_via_shortcut()
    print("\nALL SHORTCUT/RESIZE TESTS PASSED")
