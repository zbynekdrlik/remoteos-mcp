---
name: remoteos-mcp-config
description: >
  How to set up, restore, or add .mcp.json configs for remoteos-mcp managed
  machines. Use when: adding MCP to a new project, recovering a lost .mcp.json
  after a fresh clone or worktree, adding a new managed machine to a project,
  or working on dev2 with a different machine set.
triggers:
  - mcp.json
  - mcp config
  - link-mcp
  - add MCP to project
  - restore MCP
  - new machine
  - mcp connection lost
---

# MCP Config Architecture — remoteos-mcp

## Why this setup exists

`.mcp.json` files contain bearer tokens — they MUST NOT be committed to git.
But they also must survive fresh clones and worktree recreations without manual rebuilds.

## Source of truth

```
~/.claude/mcp-configs/<project>.mcp.json   (chmod 600, dir chmod 700, never tracked)
```

- The project's `.mcp.json` is a **symlink** to this master file.
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

## Adding MCP to a new project

1. Create master: `~/.claude/mcp-configs/<project>.mcp.json` (chmod 600)
2. Run: `link-mcp.sh <project>` (or `cd` into project and run with no args)
3. Add `.mcp.json` to project's `.gitignore`

## Multi-machine notes

Master configs do NOT sync across machines automatically. Each machine has its own
`~/.claude/mcp-configs/` (different machines may need different MCP servers / tokens).
The same setup (`link-mcp.sh`) must exist on every dev machine.

Currently replicated on: dev1 (primary) and dev2 (10.77.8.134 / `dev2`).
