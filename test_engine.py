"""Smoke test: drive several real TUIs through the Shellf-Driving engine."""
import time
from shellf.terminal import TerminalSession
from shellf import keys


def show(title, ts):
    print(f"\n===== {title} =====")
    print(ts.snapshot())
    print("=" * 40)


def test_bash():
    ts = TerminalSession("bash", ["--norc", "-i"], cols=80, rows=10)
    time.sleep(0.4)
    ts.send_text("echo hello-from-shellf\n")
    assert ts.wait_for_text("hello-from-shellf", timeout=3), "bash echo not seen"
    show("bash: echo", ts)
    ts.send_text("exit\n")
    print("[bash] OK")


def test_vim():
    ts = TerminalSession("vim", ["-u", "NONE", "-N"], cols=80, rows=12)
    time.sleep(0.6)
    ts.send_key(keys.resolve("escape"))
    ts.send_text("i")                      # insert mode
    ts.send_text("Shellf-Driving drives vim")
    ts.send_key(keys.resolve("escape"))
    assert ts.wait_for_text("Shellf-Driving drives vim", timeout=3), "vim text not seen"
    show("vim: inserted text", ts)
    ts.send_text(":q!\n")                   # quit without saving
    print("[vim] OK")


def test_htop_and_mouse():
    ts = TerminalSession("htop", [], cols=100, rows=24)
    assert ts.wait_for_text("CPU", timeout=4) or ts.wait_for_text("Mem", timeout=1), \
        "htop did not paint"
    show("htop: launched", ts)
    # Exercise mouse + key input paths (htop enables mouse reporting).
    ts.send_mouse("click", x=5, y=1)
    ts.send_mouse("scroll_down", x=50, y=10)
    time.sleep(0.2)
    ts.send_key(keys.resolve("f10"))        # quit
    ts.send_text("q")
    print("[htop + mouse] OK")


if __name__ == "__main__":
    test_bash()
    test_vim()
    test_htop_and_mouse()
    print("\nALL TESTS PASSED")
