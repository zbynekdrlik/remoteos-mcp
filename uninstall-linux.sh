#!/usr/bin/env bash
# RemoteOS MCP - Uninstaller for Linux (systemd)
# Usage: curl -fsSL https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/main/uninstall-linux.sh | sudo bash
#
# Removes what install-linux.sh installs (the venv layout, #12): the service, its unit, the key
# files in /etc/remoteos-mcp, /opt/remoteos-mcp-venv and the builds in /opt/remoteos-mcp-venvs,
# plus a copy in the system python that older installers left.
set -euo pipefail

SERVICE_NAME="remoteos-mcp"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
CONFIG_DIR="/etc/remoteos-mcp"
VENV_LINK="/opt/remoteos-mcp-venv"
VENV_STORE="/opt/remoteos-mcp-venvs"

echo ""
echo "  RemoteOS MCP Uninstaller (Linux)"
echo ""

# --- Check root ---
if [[ "$EUID" -ne 0 ]]; then
    echo "  [X] This uninstaller must be run as root (via sudo)."
    exit 1
fi

# --- [1/6] Stop service ---
echo "  [1/6] Stopping service..."
if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    systemctl stop "$SERVICE_NAME"
    echo "        Stopped $SERVICE_NAME"
else
    echo "        Service not active, skipping"
fi

# --- [2/6] Disable service ---
echo "  [2/6] Disabling service..."
if systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
    systemctl disable "$SERVICE_NAME"
    echo "        Disabled $SERVICE_NAME"
else
    echo "        Service not enabled, skipping"
fi

# --- [3/6] Remove service file ---
echo "  [3/6] Removing service file..."
if [[ -f "$SERVICE_FILE" ]]; then
    rm -f "$SERVICE_FILE"
    systemctl daemon-reload
    echo "        Removed $SERVICE_FILE"
else
    echo "        Service file not found, skipping"
fi

# --- [4/6] Remove config directory (the key files) ---
echo "  [4/6] Removing config directory..."
if [[ -d "$CONFIG_DIR" ]]; then
    rm -rf "$CONFIG_DIR"
    echo "        Removed $CONFIG_DIR"
else
    echo "        Config directory not found, skipping"
fi

# --- [5/6] Remove the venv (a link into the build store, or a real directory) ---
echo "  [5/6] Removing the venv..."
if [[ -L "$VENV_LINK" ]]; then
    rm -f "$VENV_LINK"
    echo "        Removed $VENV_LINK"
elif [[ -d "$VENV_LINK" ]]; then
    rm -rf "$VENV_LINK"
    echo "        Removed $VENV_LINK"
else
    echo "        $VENV_LINK not found, skipping"
fi
if [[ -d "$VENV_STORE" ]]; then
    rm -rf "$VENV_STORE"
    echo "        Removed $VENV_STORE"
fi

# --- [6/6] A copy in the system python (installers before the venv layout) ---
echo "  [6/6] Removing a remoteos-mcp copy in the system python..."
PYTHON=""
for cmd in python3 python /usr/bin/python3 /usr/local/bin/python3; do
    if command -v "$cmd" &>/dev/null || [[ -x "$cmd" ]]; then
        PYTHON="$cmd"
        break
    fi
done

if [[ -z "$PYTHON" ]]; then
    echo "        Python not found; skipping"
elif ! "$PYTHON" -m pip show remoteos-mcp &>/dev/null; then
    echo "        None found"
elif OUT=$("$PYTHON" -m pip uninstall -y --break-system-packages remoteos-mcp fastmcp fastmcp-slim 2>&1); then
    echo "        Removed it"
else
    echo "        [!] pip could not remove it:"
    echo "$OUT"
fi

echo ""
echo "  ============================================"
echo "  UNINSTALL COMPLETE"
echo "  ============================================"
echo ""
echo "  RemoteOS MCP has been removed."
echo ""
