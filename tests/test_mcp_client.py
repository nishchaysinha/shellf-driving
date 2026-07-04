"""End-to-end MCP test: act as an MCP client, drive the server over stdio.

This exercises the exact path a real client (Claude Code/Desktop) uses, including
the image content type returned by the `screenshot` tool.
"""
import asyncio
import base64

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    params = StdioServerParameters(command=".venv/bin/python", args=["-m", "shellf.server"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            print("tools:", [t.name for t in tools.tools])

            r = await session.call_tool("launch", {"command": "vim",
                                                    "args": ["-u", "NONE", "-N"],
                                                    "cols": 70, "rows": 12})
            print("\n[launch] first text block:\n", r.content[0].text[:120])

            await session.call_tool("type_text", {"text": "i"})
            await session.call_tool("type_text", {"text": "driven over MCP by an LLM"})
            await session.call_tool("press", {"keys_": ["escape"]})

            r = await session.call_tool("snapshot", {})
            assert "driven over MCP by an LLM" in r.content[0].text, "text not on screen"
            print("[snapshot] text present: OK")

            r = await session.call_tool("screenshot", {})
            img = next(c for c in r.content if c.type == "image")
            png = base64.b64decode(img.data)
            assert png[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
            open("/tmp/mcp_vim.png", "wb").write(png)
            print(f"[screenshot] got {img.mimeType}, {len(png)} bytes -> /tmp/mcp_vim.png")

            await session.call_tool("press", {"keys_": ["escape"]})
            await session.call_tool("type_text", {"text": ":q!\n"})
            print("\nMCP CLIENT TEST PASSED")


asyncio.run(main())
