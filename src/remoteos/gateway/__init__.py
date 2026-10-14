"""remoteos MCP gateway — expose a managed machine's MCP to remote fleet consumers.

A transparent streaming reverse proxy on dev1 (the box that holds the LAN routes to the
managed machines). Each remote consumer authenticates with its OWN bearer token, which the
gateway knows only by its sha256 hash; the gateway forwards the request with the machine's
real API key, resolved from the master MCP config. The machine key never leaves dev1.

Modules:

- ``registry``  — the consumer/exposure registry file, the master MCP config reader, and
  the live (auto-reloading) view of both.
- ``accesslog`` — the JSONL audit log (one line per request, never a token or arguments).
- ``app``       — the Starlette ASGI app: auth, routing, streaming proxy.
- ``cli``       — the ``remoteos-gateway`` command (``serve`` + registry management).
"""
