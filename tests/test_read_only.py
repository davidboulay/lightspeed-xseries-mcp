"""The read-only guarantee, checked without a network or a token.

Personal tokens have no scopes, so the code is the only thing keeping this
server read-only. These tests fail if a write sneaks in.
"""

import ast
import asyncio
import os
import pathlib
import unittest

os.environ.setdefault("LIGHTSPEED_DOMAIN_PREFIX", "example")
os.environ.setdefault("LIGHTSPEED_TOKEN", "test")

from lightspeed_xseries_mcp import server  # noqa: E402

SOURCE = pathlib.Path(server.__file__).read_text()
# Reads that Lightspeed takes as a POST with a JSON filter body.
POST_READS = {"/inventory", "/inventory_levels"}


class ReadOnly(unittest.TestCase):
    def test_only_get_and_post_reads(self):
        for node in ast.walk(ast.parse(SOURCE)):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_request"):
                continue
            method, path = node.args[0], node.args[1]
            if isinstance(method, ast.Name):  # the generic call inside _request's own retry / get()
                continue
            self.assertIsInstance(method, ast.Constant)
            if method.value == "GET":
                continue
            self.assertEqual(method.value, "POST", ast.unparse(node))
            self.assertIn(getattr(path, "value", None), POST_READS, ast.unparse(node))

    def test_every_tool_is_annotated_read_only(self):
        tools = asyncio.run(server.mcp.list_tools())
        self.assertGreaterEqual(len(tools), 30)
        for t in tools:
            self.assertTrue(t.annotations.read_only_hint, t.name)
            self.assertFalse(t.annotations.destructive_hint, t.name)
            self.assertFalse(t.name.startswith(("create", "update", "delete", "set", "add")), t.name)

    def test_api_get_denies_integration_plumbing(self):
        for path in ("/webhooks", "/webhooks/1", "/partner/x", "/channel_requests", "/security_events"):
            with self.assertRaises(server.LightspeedError):
                server.api_get(path)
        for path in ("/../retailer", "/sales?x=1", "sales"):
            with self.assertRaises(server.LightspeedError):
                server.api_get(path)


if __name__ == "__main__":
    unittest.main()
