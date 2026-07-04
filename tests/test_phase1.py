"""Phase 1 correctness tests: mode-sniffer, query responder, wait_for_stable,
scrollback, DECCKM arrows, Alt modifier — verified against real programs."""
import time
from shellf.terminal import TerminalSession
from shellf import keys


def test_query_responder_cpr():
    # A child that asks the terminal "where is the cursor?" (ESC[6n) and blocks for
    # the answer. If we don't reply it hangs; a correct reply is ESC[row;colR.
    prog = (
        "import os,sys,time,tty\n"
        "tty.setraw(0)\n"                                  # real apps query in raw mode
        "sys.stdout.write('\\033[6n'); sys.stdout.flush()\n"
        "time.sleep(0.5)\n"
        "data=os.read(0,32)\n"
        "sys.stdout.write('\\r\\nGOT:'+repr(data)+'\\r\\n'); sys.stdout.flush()\n"
        "time.sleep(0.3)\n"
    )
    ts = TerminalSession("python3", ["-c", prog], cols=80, rows=24)
    assert ts.wait_for_text("GOT:", timeout=4), "child never got a CPR reply (hung)\n" + ts.snapshot()
    got_line = [l for l in ts.lines() if "GOT:" in l][0]
    assert "\\x1b[" in got_line and "R" in got_line, "reply was not a CPR: " + got_line
    print("[query] cursor-position report answered:", got_line.strip())


def test_decckm_mode_tracking():
    ts = TerminalSession("bash", ["--norc", "-i"], cols=80, rows=10)
    time.sleep(0.4)
    assert ts.modes.app_cursor_keys is False
    ts.send_text("printf '\\033[?1h'\n")      # program enables application cursor keys
    time.sleep(0.3)
    assert ts.modes.app_cursor_keys is True, "DECCKM enable not tracked"
    ts.send_text("printf '\\033[?1l'\n")      # ...and disables it
    time.sleep(0.3)
    assert ts.modes.app_cursor_keys is False, "DECCKM disable not tracked"
    print("[modes] DECCKM enable/disable tracked from output: OK")
    ts.send_text("exit\n")


def test_altscreen_tracking_vim():
    ts = TerminalSession("vim", ["-u", "NONE", "-N"], cols=80, rows=12)
    time.sleep(0.6)
    assert ts.modes.alt_screen is True, "vim should have switched to the alt screen"
    print("[modes] vim alt-screen tracked: OK")
    ts.send_text(":q!\n")


def test_wait_for_stable():
    # Idle shell settles quickly.
    ts = TerminalSession("bash", ["--norc", "-i"], cols=80, rows=10)
    assert ts.wait_for_stable(quiet=0.25, timeout=3), "idle shell should be stable"
    print("[stable] idle shell settled: OK")
    # A continuous flood never settles within the window -> returns False.
    # Use baseline-version semantics (how the action tools auto-wait): wait for the
    # action's effect AND a quiet window.
    v = ts.version()
    ts.send_text("yes shellf\n")        # infinite output, constant repaint
    settled = ts.wait_for_stable(quiet=0.25, timeout=1.0, since_version=v)
    print("[stable] during continuous flood settled within 1s?", settled, "(expected False)")
    assert settled is False, "flood should not be reported stable"
    v = ts.version()
    ts.send_text("\x03")  # Ctrl-C to stop the flood
    time.sleep(0.3)
    assert ts.wait_for_stable(quiet=0.25, timeout=3), "should settle after Ctrl-C"
    print("[stable] settled again after Ctrl-C: OK")
    ts.send_text("exit\n")


def test_scrollback_history():
    ts = TerminalSession("bash", ["--norc", "-i"], cols=80, rows=8)
    time.sleep(0.3)
    ts.send_text("seq 1 60\n")        # 60 lines into an 8-row screen -> lots scrolls off
    time.sleep(0.5)
    hist = ts.history_lines()
    joined = "\n".join(hist)
    assert "1" in hist or any(l.strip() == "1" for l in hist), \
        "early line '1' should be in scrollback\n" + joined[-200:]
    print(f"[history] captured {len(hist)} scrolled-off lines; earliest含 '1': OK")
    ts.send_text("exit\n")


def test_alt_modifier_unit():
    assert keys.resolve("alt+x") == b"\x1bx"
    assert keys.resolve("meta+f") == b"\x1bf"
    assert keys.resolve("alt+enter") == b"\x1b\r"
    print("[keys] alt/meta encodings: OK")


if __name__ == "__main__":
    test_query_responder_cpr()
    test_decckm_mode_tracking()
    test_altscreen_tracking_vim()
    test_wait_for_stable()
    test_scrollback_history()
    test_alt_modifier_unit()
    print("\nALL PHASE 1 TESTS PASSED")
