# WinRemote MCP Setup

One-line installer for [WinRemote MCP](https://github.com/dddabtc/winremote-mcp) on Windows machines, enabling remote control from Claude Code on Linux.

## What it does

Installs and configures WinRemote MCP server on Windows so Claude Code (running on Linux) can:

- Execute PowerShell/CMD commands as the desktop user
- Take screenshots
- Control mouse/keyboard and interact with GUI apps
- Access files, registry, services, and processes

No more SSH quoting hell or headless session limitations.

## Install

On the Windows machine, open PowerShell **as Administrator** and run:

```powershell
irm https://raw.githubusercontent.com/zbynekdrlik/winremote-setup/main/install.ps1 | iex
```

That's it. The script will:

1. Check/install Python 3.10+
2. Install winremote-mcp via pip
3. Generate a random auth key
4. Create a start script and auto-start scheduled task
5. Open the firewall port
6. Start the server immediately
7. Print the exact `claude mcp add` command to run on your Linux machine

## Connect from Claude Code

After install, the script prints a command like:

```bash
claude mcp add --transport http win-mypc \
  http://192.168.1.100:8090/mcp \
  --header "Authorization: Bearer abc123..."
```

Run it on your Linux machine, restart Claude Code, and you're connected.

## Restrict access per project

To limit which Claude session controls which Windows machine, use project-scoped config.

Create `.mcp.json` in your project directory:

```json
{
  "mcpServers": {
    "win-strih": {
      "type": "http",
      "url": "http://192.168.1.10:8090/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_KEY"
      }
    }
  }
}
```

Claude Code only loads MCP servers from the current project's `.mcp.json`, so different projects = different Windows machines.

## Uninstall

```powershell
irm https://raw.githubusercontent.com/zbynekdrlik/winremote-setup/main/uninstall.ps1 | iex
```

## Security

- Auth key is required for all connections
- Firewall rule is scoped to private/domain networks only (not public)
- On LAN this is fine over HTTP
- For internet exposure, use TLS reverse proxy or SSH tunnel

## Powered by

- [WinRemote MCP](https://github.com/dddabtc/winremote-mcp) - the actual MCP server (40+ tools)
- [Model Context Protocol](https://modelcontextprotocol.io) - the transport layer
