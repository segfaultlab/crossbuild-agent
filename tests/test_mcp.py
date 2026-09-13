import asyncio
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))

from mcp import Client
from mcp.client.stdio import StdioServerParameters

import mcp_server


class McpServer(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.work = self.tmp / "proj"
        self.work.mkdir()
        (self.work / "a.txt").write_text("hello")
        (self.tmp / "secret.txt").write_text("secret")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_tools_share_boundaries_with_agent(self):
        async def run():
            async with Client(mcp_server.create_server(self.work, "bm25")) as c:
                names = sorted(t.name for t in (await c.list_tools()).tools)
                ok = await c.call_tool("read_file", {"path": "a.txt"})
                escaped = await c.call_tool("read_file", {"path": "../secret.txt"})
                blocked = await c.call_tool("run_command", {"command": "cmake -E cat ../secret.txt"})
                wrote = await c.call_tool("write_file", {"path": "b.txt", "content": "x"})
                kb = await c.call_tool("search_knowledge", {"query": "Could NOT find ZLIB"})
                return names, ok, escaped, blocked, wrote, kb

        names, ok, escaped, blocked, wrote, kb = asyncio.run(run())
        self.assertEqual(names, ["list_files", "read_file", "run_command", "search_knowledge", "write_file"])
        self.assertFalse(ok.is_error)
        self.assertEqual(ok.content[0].text, "hello")
        self.assertTrue(escaped.is_error)
        self.assertIn("越界", escaped.content[0].text)
        self.assertTrue(blocked.is_error)
        self.assertIn("工作目录外", blocked.content[0].text)
        self.assertFalse(wrote.is_error)
        self.assertTrue((self.work / "b.txt").exists())
        self.assertIn("k002", kb.content[0].text)

    def test_stdio_entrypoint_prepares_toolchain(self):
        async def run():
            params = StdioServerParameters(
                command=sys.executable,
                args=[str(ROOT / "agent" / "mcp_server.py"), "--workdir", str(self.work), "--kb", "none"])
            async with Client(params) as c:
                names = sorted(t.name for t in (await c.list_tools()).tools)
                listing = await c.call_tool("list_files", {"path": ".xbuild"})
                return names, listing

        names, listing = asyncio.run(run())
        self.assertNotIn("search_knowledge", names)
        self.assertIn("zig.cmake", listing.content[0].text)


if __name__ == "__main__":
    unittest.main()
