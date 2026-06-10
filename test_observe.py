"""Test the observability dashboard: schema integrity + live SSE data flow."""
import base64
import os
import socket
import time
import urllib.request

os.environ["SHELLF_OBSERVE_PORT"] = "7333"
import shellf.server as S  # noqa: E402  (import after env so the observer starts)


def read_sse(url, seconds=1.5, max_bytes=400000):
    """Read raw bytes from an SSE endpoint for a short window (line-oriented)."""
    data = b""
    try:
        r = urllib.request.urlopen(url, timeout=seconds)  # socket timeout per read
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and len(data) < max_bytes:
            try:
                line = r.readline()
            except (socket.timeout, TimeoutError):
                break
            if not line:
                break
            data += line
    except (socket.timeout, TimeoutError):
        pass
    except Exception as e:
        print("  (read_sse note:", e, ")")
    return data


def main():
    # 1. schema still intact after @observed?
    import asyncio
    tools = {t.name: t for t in asyncio.run(S.mcp.list_tools())}
    tt = tools["type_text"]
    props = list(tt.inputSchema.get("properties", {}))
    print("1. type_text schema props:", props)
    assert "text" in props and "session" in props, "schema lost params!"
    print("   schema OK across all", len(tools), "tools")

    # 2. drive a session through the tool functions
    S.launch(command="bash", args=["--norc", "-i"], session="demo", cols=80, rows=12)
    S.type_text("echo hello-observe-XYZ\n", session="demo")
    time.sleep(0.3)

    # direct hub check (isolates publish from transport)
    from shellf import observe
    print("   hub.active:", observe.hub.active,
          "| event history:", len(observe.hub._event_history),
          "| sessions:", list(observe.hub._sessions))

    # 3. index page serves xterm dashboard
    html = urllib.request.urlopen("http://127.0.0.1:7333/", timeout=3).read().decode()
    print("2. dashboard page:", "xterm" in html and "Shellf-Driving" in html)

    # 4. events stream carries the session + tool calls
    ev = read_sse("http://127.0.0.1:7333/events", seconds=1.0).decode("utf-8", "replace")
    print("3. events stream has session event:", '"kind": "session"' in ev,
          "| tool events:", ev.count('"kind": "tool"'))

    # 5. output stream mirrors the actual PTY bytes
    raw = read_sse("http://127.0.0.1:7333/stream/demo", seconds=1.0)
    decoded = b""
    for line in raw.split(b"\n"):
        if line.startswith(b"data: "):
            try:
                decoded += base64.b64decode(line[6:])
            except Exception:
                pass
    print("4. output stream mirrors PTY (sees the echoed command):",
          b"hello-observe-XYZ" in decoded)

    S.kill(session="demo")
    print("\nOBSERVE TEST PASSED" if (
        "text" in props and b"hello-observe-XYZ" in decoded and "xterm" in html
    ) else "\nOBSERVE TEST FAILED")


main()
