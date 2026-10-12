"""
Minimale client voor podcast-mcp (bramdehart/podcast-mcp) over streamable HTTP.
Standaard: de publieke AI Report-demo (alleen-lezen, rate-limited).
"""
from __future__ import annotations

import json
import os
import time

import requests

URL = os.environ.get("PODCAST_MCP_URL", "https://ai-report.bramdehart.nl/mcp/")  # met slash: anders redirect naar http zonder token
TOKEN = os.environ.get("PODCAST_MCP_TOKEN", "9b55e1de7f3e0e8972377d3d9a77330929f6446bccf0927ccc648bb0d512018c")  # demo-token uit de README


class PodMCP:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json",
                               "Accept": "application/json, text/event-stream"})
        self.n = 0
        r = self._rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                     "clientInfo": {"name": "stemmy-supercut", "version": "0.1"}})
        self._notify("notifications/initialized")

    def _parse(self, r):
        if "text/event-stream" in r.headers.get("Content-Type", ""):
            for line in r.text.splitlines():
                if line.startswith("data:"):
                    return json.loads(line[5:])
            return {}
        return r.json()

    def _rpc(self, method, params=None, retries=4):
        self.n += 1
        for k in range(retries):
            r = self.s.post(URL, json={"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}}, timeout=60)
            if r.status_code == 429:
                time.sleep(2 * (k + 1)); continue
            r.raise_for_status()
            if "mcp-session-id" in r.headers:
                self.s.headers["mcp-session-id"] = r.headers["mcp-session-id"]
            d = self._parse(r)
            if "error" in d:
                raise RuntimeError(d["error"])
            return d.get("result", {})
        raise RuntimeError("rate limit")

    def _notify(self, method):
        self.s.post(URL, json={"jsonrpc": "2.0", "method": method}, timeout=30)

    def tools(self):
        return self._rpc("tools/list")["tools"]

    def call(self, name, **args):
        res = self._rpc("tools/call", {"name": name, "arguments": args})
        if res.get("structuredContent"):
            return res["structuredContent"]
        txt = "".join(c.get("text", "") for c in res.get("content", []))
        try:
            return json.loads(txt)
        except Exception:
            return txt
