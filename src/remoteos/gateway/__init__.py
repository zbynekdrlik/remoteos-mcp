"""remoteos MCP gateway — every project reaches every managed machine through dev1.

A transparent streaming reverse proxy on dev1 (the box that holds the LAN routes to the
managed machines). Each consumer — a remote fleet box, or (#51) one of the owner's own
projects — authenticates with its OWN bearer token, which the gateway knows only by its
sha256 hash; the gateway forwards the request with the machine's real key, resolved from the
key store. A machine key lives in ONE place: a key change reaches every consumer on its next
call.

Modules:

- ``registry``  — the consumer/exposure registry file, the machine source reader (the key
  store, or the legacy master MCP config), and the live (auto-reloading) view of both.
- ``accesslog`` — the JSONL audit log (one line per request, never a token or arguments).
- ``app``       — the Starlette ASGI app: auth, routing, streaming proxy.
- ``cli``       — the ``remoteos-gateway`` command (``serve`` + registry management).
- ``keystore``  — the key store: atomic edits, ``set-key``, the import from project configs (#51).
- ``manifest``  — ``projects.toml``: which project reaches which machines (#51).
- ``sync``      — ``sync-configs``: generate each project's config from the manifest (#51).
- ``targets``   — read/write a config on this account, another account (sudo), dev2 (ssh) (#51).
- ``fileops``   — stdlib-only atomic 0600 writes + dated backups, also run remotely (#51).
- ``probe``     — the ``initialize`` probe (ok / 401 / unreachable / error) (#51).
- ``check``     — the key-drift watchdog behind the 10-minute timer (#51).
- ``cli_fleet`` — the ``machine``, ``sync-configs`` and ``check`` commands (#51).
"""
