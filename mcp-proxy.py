#!/usr/bin/env python3
"""MCP reverse proxy that keeps Claude Code connected during remote server restarts.

Sits between Claude Code (localhost) and remote remoteos-mcp servers.
Returns valid JSON-RPC errors when the remote is down instead of TCP failures,
so Claude Code doesn't mark the server as disconnected.

Usage:
    python mcp-proxy.py --config proxies.json

proxies.json example:
    [
        {"local_port": 18092, "remote_url": "http://stream.lan:8092", "name": "stream-snv"},
        {"local_port": 18091, "remote_url": "http://10.78.2.10:8092", "name": "pz-snv"}
    ]

Then in .mcp.json, point Claude Code to localhost:
    "win-stream-snv": {"type": "http", "url": "http://localhost:18092/mcp", ...}
"""

import argparse
import json
import sys
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen
from urllib.error import URLError


class MCPProxyHandler(BaseHTTPRequestHandler):
    """Forwards requests to remote MCP server, returns clean errors if down."""

    def do_POST(self):
        remote_url = self.server.remote_url + self.path
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len) if content_len else b""

        # Forward all headers except Host
        headers = {}
        for key, val in self.headers.items():
            if key.lower() not in ("host", "content-length"):
                headers[key] = val
        headers["Content-Length"] = str(len(body))

        try:
            req = Request(remote_url, data=body, headers=headers, method="POST")
            with urlopen(req, timeout=300) as resp:
                resp_body = resp.read()
                self.send_response(resp.status)
                for key, val in resp.getheaders():
                    if key.lower() not in ("transfer-encoding", "connection"):
                        self.send_header(key, val)
                self.end_headers()
                self.wfile.write(resp_body)
        except URLError as e:
            # Remote is down — return valid JSON-RPC error so Claude stays connected
            error_resp = json.dumps({
                "jsonrpc": "2.0",
                "id": None,
                "error": {
                    "code": -32000,
                    "message": f"Remote MCP server ({self.server.proxy_name}) is temporarily unavailable: {e.reason}"
                }
            }).encode()
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(error_resp)))
            self.end_headers()
            self.wfile.write(error_resp)
        except Exception as e:
            error_resp = json.dumps({
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32000, "message": f"Proxy error: {e}"}
            }).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(error_resp)))
            self.end_headers()
            self.wfile.write(error_resp)

    def do_GET(self):
        remote_url = self.server.remote_url + self.path
        headers = {}
        for key, val in self.headers.items():
            if key.lower() not in ("host",):
                headers[key] = val

        try:
            req = Request(remote_url, headers=headers, method="GET")
            with urlopen(req, timeout=30) as resp:
                resp_body = resp.read()
                self.send_response(resp.status)
                for key, val in resp.getheaders():
                    if key.lower() not in ("transfer-encoding", "connection"):
                        self.send_header(key, val)
                self.end_headers()
                self.wfile.write(resp_body)
        except URLError:
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            body = b'{"error":"Remote server temporarily unavailable"}'
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def do_DELETE(self):
        # MCP session termination — just acknowledge
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        print(f"[{self.server.proxy_name}:{self.server.server_port}] {format % args}")


def start_proxy(name, local_port, remote_url):
    server = HTTPServer(("127.0.0.1", local_port), MCPProxyHandler)
    server.remote_url = remote_url.rstrip("/")
    server.proxy_name = name
    print(f"  {name}: localhost:{local_port} → {remote_url}")
    server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description="MCP reverse proxy")
    parser.add_argument("--config", required=True, help="JSON config file")
    args = parser.parse_args()

    with open(args.config) as f:
        proxies = json.load(f)

    print(f"Starting {len(proxies)} MCP proxies...")
    threads = []
    for p in proxies:
        t = threading.Thread(
            target=start_proxy,
            args=(p["name"], p["local_port"], p["remote_url"]),
            daemon=True,
        )
        t.start()
        threads.append(t)

    print("All proxies running. Ctrl+C to stop.")
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        print("\nStopping proxies.")
        sys.exit(0)


if __name__ == "__main__":
    main()
