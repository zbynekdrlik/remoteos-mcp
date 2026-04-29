#!/usr/bin/env bash
# RemoteOS MCP - Uninstaller for Linux (systemd)
# Usage: curl -fsSL https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/master/uninstall-linux.sh | sudo bash
set -euo pipefail

SERVICE_NAME="remoteos-mcp"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
CONFIG_DIR="/etc/remoteos-mcp"

echo ""
echo "  RemoteOS MCP Uninstaller (Linux)"
echo ""

# --- Check root ---
if [[ "$EUID" -ne 0 ]]; then
    echo "  [X] This uninstaller must be run as root (via sudo)."
    exit 1
fi

# --- [1/5] Stop service ---
echo "  [1/5] Stopping service..."
if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    systemctl stop "$SERVICE_NAME"
    echo "        Stopped $SERVICE_NAME"
else
    echo "        Service not active, skipping"
fi

# --- [2/5] Disable service ---
echo "  [2/5] Disabling service..."
if systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
    systemctl disable "$SERVICE_NAME"
    echo "        Disabled $SERVICE_NAME"
else
    echo "        Service not enabled, skipping"
fi

# --- [3/5] Remove service file ---
echo "  [3/5] Removing service file..."
if [[ -f "$SERVICE_FILE" ]]; then
    rm -f "$SERVICE_FILE"
    systemctl daemon-reload
    echo "        Removed $SERVICE_FILE"
else
    echo "        Service file not found, skipping"
fi

# --- [4/5] Remove config directory ---
echo "  [4/5] Removing config directory..."
if [[ -d "$CONFIG_DIR" ]]; then
    rm -rf "$CONFIG_DIR"
    echo "        Removed $CONFIG_DIR"
else
    echo "        Config directory not found, skipping"
fi

# --- [5/5] Uninstall pip package ---
echo "  [5/5] Uninstalling remoteos-mcp package..."
PYTHON=""
for cmd in python3 python /usr/bin/python3 /usr/local/bin/python3; do
    if command -v "$cmd" &>/dev/null || [[ -x "$cmd" ]]; then
        PYTHON="$cmd"
        break
    fi
done

if [[ -n "$PYTHON" ]]; then
    "$PYTHON" -m pip uninstall -y remoteos-mcp 2>/dev/null || true
    echo "        Package uninstalled"
else
    echo "        Python not found; skipping pip uninstall"
fi

echo ""
echo "  ============================================"
echo "  UNINSTALL COMPLETE"
echo "  ============================================"
echo ""
echo "  RemoteOS MCP has been removed."
echo ""
