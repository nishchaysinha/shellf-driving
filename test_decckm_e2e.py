"""Definitive DECCKM proof: a child enables application cursor keys and echoes the
exact bytes it receives, so we confirm arrows arrive as ESC O A (not ESC [ A)."""
import time
from shellf.terminal import TerminalSession

CHILD = r'''
import os, sys, tty
tty.setraw(0)
sys.stdout.write("\033[?1h")          # enable application cursor keys (DECCKM)
sys.stdout.write("ready\r\n"); sys.stdout.flush()
for _ in range(3):
    d = os.read(0, 8)
    sys.stdout.write("recv " + repr(d) + "\r\n"); sys.stdout.flush()
'''


def main():
    ts = TerminalSession("python3", ["-c", CHILD], cols=60, rows=10)
    assert ts.wait_for_text("ready", timeout=3), "child didn't start\n" + ts.snapshot()
    print("app_cursor_keys sniffed from output:", ts.modes.app_cursor_keys)
    assert ts.modes.app_cursor_keys, "DECCKM not tracked — arrows would use wrong form"

    for arrow in ["up", "down", "left"]:
        v = ts.version()
        ts.send_sequence([arrow])
        ts.wait_for_stable(quiet=0.15, timeout=2, since_version=v)

    recv = [l.strip() for l in ts.lines() if "recv" in l]
    print("what the program actually received:")
    for l in recv:
        print("   ", l)
    blob = " ".join(recv)
    assert "OA" in blob and "OB" in blob and "OD" in blob, \
        "arrows did not arrive in application-mode (ESC O x) form: " + blob
    assert "[A" not in blob, "arrow leaked the normal-mode ESC [ A form: " + blob
    print("\nDECCKM E2E PASSED — arrows correctly sent as ESC O x inside the app")


if __name__ == "__main__":
    main()
