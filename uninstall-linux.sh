#!/usr/bin/env bash
# RemoteOS MCP - Uninstaller for Linux (systemd)
# Usage: curl -fsSL https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/main/uninstall-linux.sh | sudo bash
#
# Removes what install-linux.sh installs (the venv layout, #12): the service, its unit, the key
# files in /etc/remoteos-mcp, the sudoers drop-in, /opt/remoteos-mcp-venv and the builds in
# /opt/remoteos-mcp-venvs, plus a copy in the system python that older installers left.
set -euo pipefail

SERVICE_NAME="remoteos-mcp"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
CONFIG_DIR="/etc/remoteos-mcp"
VENV_LINK="/opt/remoteos-mcp-venv"
VENV_STORE="/opt/remoteos-mcp-venvs"
SUDOERS_FILE="/etc/sudoers.d/90-remoteos-mcp"
UPDATER_SERVICE="/etc/systemd/system/remoteos-mcp-updater.service"
UPDATER_TIMER="/etc/systemd/system/remoteos-mcp-updater.timer"
UPGRADE_LOGS="/var/log/remoteos-mcp-upgrade"

echo ""
echo "  RemoteOS MCP Uninstaller (Linux)"
echo ""

# --- Check root ---
if [[ "$EUID" -ne 0 ]]; then
    echo "  [X] This uninstaller must be run as root (via sudo)."
    exit 1
fi
# From the agent's own Shell this run is inside the unit's cgroup: stopping the unit kills it.
CGROUP_FILE="/proc/self/cgroup"
if grep -q "/${SERVICE_NAME}\.service" "$CGROUP_FILE" 2>/dev/null; then
    echo "  [X] This run is inside ${SERVICE_NAME}.service (the agent's own Shell): stopping it would kill the run — nothing was changed. Run it over SSH."
    exit 1
fi

# --- [1/7] Stop service ---
echo "  [1/7] Stopping service..."
if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    systemctl stop "$SERVICE_NAME"
    echo "        Stopped $SERVICE_NAME"
else
    echo "        Service not active, skipping"
fi

# --- [2/7] Disable service ---
echo "  [2/7] Disabling service..."
if systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
    systemctl disable "$SERVICE_NAME"
    echo "        Disabled $SERVICE_NAME"
else
    echo "        Service not enabled, skipping"
fi

# --- [3/7] Remove service file (and the updater's timer: it must not run into a removed venv) ---
echo "  [3/7] Removing service file..."
if [[ -f "$UPDATER_TIMER" ]]; then
    systemctl disable --now "${UPDATER_TIMER##*/}" 2>/dev/null || true
    rm -f "$UPDATER_TIMER" "$UPDATER_SERVICE"
    systemctl daemon-reload
    echo "        Removed the updater's timer"
fi
if [[ -f "$SERVICE_FILE" ]]; then
    rm -f "$SERVICE_FILE"
    systemctl daemon-reload
    echo "        Removed $SERVICE_FILE"
else
    echo "        Service file not found, skipping"
fi

# --- [4/7] Remove config directory (the key files) ---
echo "  [4/7] Removing config directory..."
if [[ -d "$CONFIG_DIR" ]]; then
    rm -rf "$CONFIG_DIR"
    echo "        Removed $CONFIG_DIR"
else
    echo "        Config directory not found, skipping"
fi

# --- [5/7] Remove the service account's sudoers drop-in ---
echo "  [5/7] Removing the sudoers drop-in..."
if [[ -e "$SUDOERS_FILE" ]]; then
    rm -f "$SUDOERS_FILE"
    echo "        Removed $SUDOERS_FILE"
else
    echo "        $SUDOERS_FILE not found, skipping"
fi
rm -f "$UPGRADE_LOGS"-*.log  # SelfUpgrade's

# --- [6/7] Remove the venv (a link into the build store, or a real directory) ---
echo "  [6/7] Removing the venv..."
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

# --- [7/7] A copy in the system python (installers before the venv layout) ---
echo "  [7/7] Removing a remoteos-mcp copy in the system python..."
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
