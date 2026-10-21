---
name: remoteos-mcp-config
description: >
  How to set up, restore, or add .mcp.json configs for remoteos-mcp managed
  machines. Use when: adding MCP to a new project, recovering a lost .mcp.json
  after a fresh clone or worktree, adding a new managed machine to a project,
  a machine key changed (401 / AUTH_HEADER_REJECTED), or working on dev2 with a
  different machine set.
triggers:
  - mcp.json
  - mcp config
  - link-mcp
  - add MCP to project
  - restore MCP
  - new machine
  - mcp connection lost
  - set-key
  - sync-configs
  - AUTH_HEADER_REJECTED
---

# MCP Config Architecture — remoteos-mcp

## The rule since #51: fleet entries are GENERATED, never hand-edited

Every project's fleet entries (`win-*`, `linux-*`, `mac-*`) are written by
`remoteos-gateway sync-configs` on dev1 from two files there:

- **The key store** `~/.config/remoteos-gateway/machines.json`: one url + key per machine.
- **The manifest** `~/.config/remoteos-gateway/projects.toml`: per project, its host
  (dev1 | dev2), account, config path and machines.

A fleet entry points at the gateway with the PROJECT's own token, under the machine's usual
server name, so tool names (`mcp__win-stream-snv__Shell`) never change:

```json
"win-stream-snv": {"type": "http", "url": "http://100.104.8.125:8796/win-stream-snv/mcp",
                   "headers": {"Authorization": "Bearer <the project's token>"}}
```

The exception is the remoteos-mcp project itself (`mode = "direct"`). It keeps DIRECT entries
(machine url + machine key, from the key store), so it can debug a machine while the gateway is
down. Every other entry (n8n, playwright, codex-bridge, ...) is never touched: it stays
byte-equal. Edit those by hand as before.

A session's 401 / `AUTH_HEADER_REJECTED` has TWO causes since #51. Tell them apart first, on dev1,
with `remoteos-gateway log --tail 20` (NO `--consumer`: a refused token has no consumer, so its
line shows consumer `-` and a `--consumer` filter would hide it), and find the machine's line:

- outcome `upstream_denied` (and `machines-status.json` says `401` for that machine): the
  MACHINE key is stale → `machine set-key`.
- outcome `denied_unknown_token` (consumer `-`; `machines-status.json` says the machine is
  `ok`): the PROJECT token is not registered (revoked, `--prune`d, a restored old config) →
  `remoteos-gateway sync-configs` (it mints and registers a new one; the session then needs one
  restart).

| Situation | Do this (on dev1) |
|---|---|
| A machine key changed (`upstream_denied`) | `remoteos-gateway machine set-key <machine>` with the key on stdin. Every gateway project gets it on its next call: no config change, no reconnect. Then `remoteos-gateway sync-configs` refreshes the direct remoteos-mcp copy. |
| A project's token is refused (`denied_unknown_token`) | `remoteos-gateway sync-configs`, then restart that project's sessions once. |
| Add a machine to a project | Add it to `machines` in `projects.toml`, then `remoteos-gateway sync-configs`. |
| A brand-new machine | `remoteos-gateway machine set-key <machine> --url http://<box>:8092/mcp` (key on stdin, it is probed), then the line above. |
| A new project | One `[projects.<name>]` table, then `remoteos-gateway sync-configs`, then `link-mcp.sh <project>` (below). |
| What would change? | `remoteos-gateway sync-configs --dry-run`: every key and token is masked. |
| Which machines work? | `~/.local/state/remoteos-gateway/machines-status.json`, written every 10 min by `remoteos-gateway check`. Each change is announced once. |

Claude Code reads `.mcp.json` only at session start. A session started BEFORE `sync-configs`
changed its project's config (the first migration, an added or removed machine, a new token)
needs one restart (`claude -c`). A machine key change never needs one: it changes nothing in
any project config.

Never paste a key into a project config, a chat, or an argv: `set-key` reads it from stdin,
and `machine list` shows keys masked. Full reference: README "MCP gateway (dev1)" →
"Every project through the gateway (#51)".

## Why the files are outside git

`.mcp.json` files contain bearer tokens — they MUST NOT be committed to git.
But they also must survive fresh clones and worktree recreations without manual rebuilds.

## Source of truth for each project's file

```
~/.claude/mcp-configs/<project>.mcp.json   (chmod 600, dir chmod 700, never tracked)
```

- This is the path the manifest's `config` names. sync-configs writes it atomically, with a
  backup in `.backup-<date>/` next to it.
- The project's `.mcp.json` is a **symlink** to this master file (sync writes through the
  symlink, so it stays one).
- `.gitignore` must include `.mcp.json` in every project that uses MCP.
  Exception: projects with no-secret configs (e.g. `companion` uses npx without tokens) may track it.

## Setup script

```bash
~/.claude/bin/link-mcp.sh              # auto-detects from cwd
~/.claude/bin/link-mcp.sh --all        # relinks ALL projects with master configs
~/.claude/bin/link-mcp.sh <project>    # explicit project name
```

## After fresh clone or new worktree

```bash
cd <project-dir>
link-mcp.sh     # instantly restores the symlink from the master config
```

## Multi-machine notes

Each dev machine has its own `~/.claude/mcp-configs/`. sync-configs reaches them:

- dev2 configs: over `ssh -o BatchMode=yes <account>@dev2`.
- Another account's configs (e.g. `fohmixer` on dev1): over `sudo -n -u <account>`.

When that access is missing, sync prints the exact one-time step and syncs the rest.
The same `link-mcp.sh` setup must exist on every dev machine.

Currently replicated on: dev1 (primary) and dev2 (10.77.8.134 / `dev2`).
