#!/usr/bin/env bash
# RemoteOS MCP - Uninstaller for macOS
# Usage: curl -fsSL https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/master/uninstall.sh | bash
set -euo pipefail

PLIST_LABEL="com.remoteos-mcp"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
CONFIG_DIR="$HOME/.remoteos-mcp"

echo ""
echo "  RemoteOS MCP Uninstaller (macOS)"
echo ""

# --- Stop LaunchAgent ---
echo "  [1/4] Stopping LaunchAgent..."
launchctl unload "$PLIST_PATH" 2>/dev/null || true
echo "        LaunchAgent unloaded"

# --- Kill running processes ---
echo "  [2/4] Stopping running processes..."
pkill -f "remoteos.*streamable-http" 2>/dev/null || true
echo "        Processes stopped"

# --- Remove plist ---
echo "  [3/4] Removing LaunchAgent plist..."
if [[ -f "$PLIST_PATH" ]]; then
    rm -f "$PLIST_PATH"
    echo "        Removed $PLIST_PATH"
else
    echo "        Plist not found (already removed)"
fi

# --- Remove config directory ---
echo "  [4/4] Removing config directory..."
if [[ -d "$CONFIG_DIR" ]]; then
    rm -rf "$CONFIG_DIR"
    echo "        Removed $CONFIG_DIR"
else
    echo "        Config directory not found (already removed)"
fi

# --- Optional: pip uninstall ---
echo ""
read -rp "  Uninstall remoteos-mcp pip package? [y/N] " answer
if [[ "${answer,,}" == "y" ]]; then
    for cmd in python3 python; do
        if command -v "$cmd" &>/dev/null; then
            "$cmd" -m pip uninstall -y remoteos-mcp 2>/dev/null || true
            echo "        Package uninstalled"
            break
        fi
    done
else
    echo "        Skipped (run: pip uninstall remoteos-mcp)"
fi

# --- Summary ---
echo ""
echo "  Uninstall complete."
echo ""
echo "  Remember to remove the MCP config from Claude Code:"
echo "  claude mcp remove mac-HOSTNAME"
echo ""
