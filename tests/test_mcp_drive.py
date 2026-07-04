"""End-to-end MCP test: drive a real editor entirely through MCP tool calls.

This exercises the exact surface a real client (Claude) uses — every action goes over
JSON-RPC to the server: launch, get_modes, type_text, press, shortcut, screenshot
(image content), wait_for_stable — and we verify a real artifact lands on disk.
"""
import asyncio
import base64
import json
import os

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

OUT = "/tmp/mcp_demo.txt"
SHOT = "/tmp/mcp_drive.png"


def text_of(result):
    return "\n".join(c.text for c in result.content if getattr(c, "type", None) == "text")


def data_of(result):
    """Structured (dict/list) tool output: prefer structuredContent, else parse JSON text."""
    if getattr(result, "structuredContent", None):
        return result.structuredContent
    return json.loads(text_of(result))


async def main():
    if os.path.exists(OUT):
        os.remove(OUT)
    params = StdioServerParameters(command=".venv/bin/python", args=["-m", "shellf.server"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = [t.name for t in (await session.list_tools()).tools]
            print(f"server exposes {len(tools)} tools")

            # 1. launch vim on a new file
            r = await session.call_tool("launch", {
                "command": "vim",
                "args": ["-u", "NONE", "-N", OUT],
                "cols": 80, "rows": 20,
            })
            print("\n[launch] header:", text_of(r).splitlines()[0])

            # 2. modes — vim should be on the alternate screen
            modes = data_of(await session.call_tool("get_modes", {}))
            print("[get_modes]:", {k: v for k, v in modes.items() if v})

            # 3. enter insert mode, type content, leave insert
            await session.call_tool("press", {"keys_": ["i"]})
            for line in ["# written by an LLM through MCP", "",
                         "def hello():", "    return 'shellf-driving'"]:
                await session.call_tool("type_text", {"text": line + "\n"})
            await session.call_tool("press", {"keys_": ["escape"]})

            # 4. screenshot via MCP (image content type) -> save for visual check
            r = await session.call_tool("screenshot", {})
            img = next(c for c in r.content if getattr(c, "type", None) == "image")
            png = base64.b64decode(img.data)
            open(SHOT, "wb").write(png)
            assert png[:8] == b"\x89PNG\r\n\x1a\n"
            print(f"[screenshot] {img.mimeType}, {len(png)} bytes -> {SHOT}")

            # 5. save + quit via the named shortcut, then confirm exit
            await session.call_tool("shortcut", {"app": "vim", "name": "save-quit"})
            r = await session.call_tool("snapshot", {})
            print("[after save-quit] status:", text_of(r).splitlines()[0])

    # 6. verify the artifact actually landed on disk
    print("\n--- file on disk ---")
    content = open(OUT).read()
    print(content)
    assert "def hello():" in content and "shellf-driving" in content, "file not written!"
    print("MCP DRIVE TEST PASSED — vim driven over MCP wrote a real file")


asyncio.run(main())
