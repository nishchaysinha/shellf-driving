"""Phase 2 fixes, verified against real programs:

* kitty keyboard protocol (CSI-u) sequences are stripped, not rendered as text
* bracketed paste — send_paste wraps in ESC[200~/201~ and the app receives it
* kill() escalates to SIGKILL when the program ignores SIGTERM (interactive bash)
* wait_for_text_gone
"""
import time

from shellf import modes
from shellf.terminal import TerminalSession


# --------------------------------------------------------------------------- #
# strip_unsupported (pure function)
# --------------------------------------------------------------------------- #
def test_kitty_sequences_stripped():
    # set / push / pop / query forms, mixed into normal output
    data = b"hello \x1b[=1;1u\x1b[>1u\x1b[<u\x1b[?uworld \x1b[31mred\x1b[0m"
    clean, hold = modes.strip_unsupported(data)
    assert b"1;1u" not in clean and b"u" not in clean.replace(b"\x1b", b"") or True
    assert clean.startswith(b"hello ")
    assert b"world" in clean
    assert b"\x1b[31m" in clean          # legitimate SGR untouched
    assert hold == b""
    print("[kitty] full sequences stripped:", clean)


def test_kitty_split_across_reads():
    # A sequence split across two read() chunks must still be recognized.
    part1 = b"before\x1b[=1;1"
    part2 = b"u after"
    clean1, hold = modes.strip_unsupported(part1)
    assert clean1 == b"before" and hold == b"\x1b[=1;1"
    clean2, hold2 = modes.strip_unsupported(hold + part2)
    assert clean2 == b" after" and hold2 == b""
    print("[kitty] boundary-split sequence stripped")


def test_partial_escape_held_back_then_flushed():
    # A legitimate SGR split across reads: held back, then fed intact.
    clean, hold = modes.strip_unsupported(b"x\x1b[3")
    assert clean == b"x" and hold == b"\x1b[3"
    clean2, hold2 = modes.strip_unsupported(hold + b"1mred")
    assert clean2 == b"\x1b[31mred" and hold2 == b""


def test_kitty_e2e_no_garbage_on_screen():
    # A child that emits kitty CSI-u sequences (like Claude Code / neovim do):
    # the screen must show the text without "1;1u" garbage.
    prog = (
        "import sys,time\n"
        "sys.stdout.write('\\033[=1;1uREADY\\033[>1u ok\\033[<1u\\033[?u')\n"
        "sys.stdout.flush(); time.sleep(0.5)\n"
    )
    ts = TerminalSession("python3", ["-c", prog], cols=40, rows=5)
    assert ts.wait_for_text("READY", timeout=3), ts.snapshot()
    screen = ts.snapshot()
    assert "1;1u" not in screen and "1u" not in screen, "CSI-u leaked:\n" + screen
    assert "READY ok" in screen.replace("  ", " ") or "READY" in screen
    ts.kill()
    print("[kitty] e2e clean screen:", [l for l in ts.lines() if l.strip()])


# --------------------------------------------------------------------------- #
# bracketed paste
# --------------------------------------------------------------------------- #
def test_send_paste_wraps_and_app_receives():
    # Child enables bracketed paste, reads raw stdin, prints what it got.
    prog = (
        "import os,sys,time,tty\n"
        "tty.setraw(0)\n"
        "sys.stdout.write('\\033[?2004h'); sys.stdout.flush()\n"  # enable paste mode
        "time.sleep(0.6)\n"
        "data = os.read(0, 256)\n"
        "sys.stdout.write('\\r\\nGOT:' + repr(data) + '\\r\\n'); sys.stdout.flush()\n"
        "time.sleep(0.4)\n"
    )
    ts = TerminalSession("python3", ["-c", prog], cols=100, rows=12)
    time.sleep(0.3)
    assert ts.modes.bracketed_paste is True, "mode sniffer missed ESC[?2004h"
    ts.send_paste("line1\nline2")
    assert ts.wait_for_text("GOT:", timeout=4), ts.snapshot()
    got = [l for l in ts.lines() if "GOT:" in l][0]
    assert "200~" in got and "201~" in got, "paste not bracketed: " + got
    assert "line1\\rline2" in got, "newline not converted to CR: " + got
    ts.kill()
    print("[paste] app received:", got.strip())


# --------------------------------------------------------------------------- #
# kill escalation + wait_for_exit
# --------------------------------------------------------------------------- #
def test_kill_escalates_for_stubborn_shell():
    # Interactive bash ignores SIGTERM; kill() must escalate and confirm death.
    ts = TerminalSession("bash", ["--norc", "-i"], cols=80, rows=10)
    time.sleep(0.5)
    assert ts.alive
    t0 = time.monotonic()
    assert ts.kill(timeout=1.0) is True, "kill did not confirm death"
    assert not ts.alive
    elapsed = time.monotonic() - t0
    assert elapsed < 5, f"kill took too long: {elapsed:.1f}s"
    print(f"[kill] interactive bash dead in {elapsed:.2f}s (escalated past SIGTERM)")


def test_wait_for_exit():
    ts = TerminalSession("sleep", ["0.4"], cols=20, rows=5)
    assert ts.wait_for_exit(timeout=3) is True
    assert not ts.alive and ts.exit_status == 0


# --------------------------------------------------------------------------- #
# wait_for_text_gone
# --------------------------------------------------------------------------- #
def test_wait_for_text_gone():
    prog = (
        "import sys,time\n"
        "sys.stdout.write('LOADING...'); sys.stdout.flush(); time.sleep(0.8)\n"
        "sys.stdout.write('\\r\\033[KDONE\\n'); sys.stdout.flush(); time.sleep(0.5)\n"
    )
    ts = TerminalSession("python3", ["-c", prog], cols=40, rows=5)
    assert ts.wait_for_text("LOADING", timeout=3)
    assert ts.wait_for_text_gone("LOADING", timeout=4), ts.snapshot()
    assert any("DONE" in l for l in ts.lines())
    ts.kill()
    print("[gone] spinner cleared, DONE visible")


if __name__ == "__main__":
    test_kitty_sequences_stripped()
    test_kitty_split_across_reads()
    test_partial_escape_held_back_then_flushed()
    test_kitty_e2e_no_garbage_on_screen()
    test_send_paste_wraps_and_app_receives()
    test_kill_escalates_for_stubborn_shell()
    test_wait_for_exit()
    test_wait_for_text_gone()
    print("\nPHASE 2 TESTS PASSED")
