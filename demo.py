"""Quick way to see the live observability dashboard.

    python demo.py            # dashboard on :7331, launches htop + vim, idles
    python demo.py 8080       # use a different port

Open http://127.0.0.1:<port> (forward the port in VS Code SSH). htop self-updates,
so you'll see the live byte-mirror moving; the tool-call timeline fills as sessions
launch. Ctrl-C to stop.
"""
import os
import sys
import time

port = sys.argv[1] if len(sys.argv) > 1 else "7331"
os.environ.setdefault("SHELLF_OBSERVE_PORT", port)

import shellf.server as S  # noqa: E402  (import after env so the dashboard starts)


def main():
    # A live, self-updating app makes the mirror visibly move.
    S.launch(command="htop", session="htop", cols=120, rows=34)
    # A second session to show the tab switcher + an alt-screen editor.
    S.launch(command="vim", args=["-u", "NONE", "-N"], session="vim", cols=100, rows=30)
    S.type_text("iShellf-Driving live dashboard — watch this update.", session="vim")
    S.press(["escape"], session="vim")

    print(f"\n  dashboard:  http://127.0.0.1:{port}")
    print("  sessions:   htop (live), vim (alt-screen)")
    print("  Ctrl-C to stop.\n")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        for s in ("htop", "vim"):
            try:
                S.kill(session=s)
            except Exception:
                pass
        print("\nstopped.")


if __name__ == "__main__":
    main()
