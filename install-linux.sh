#!/usr/bin/env bash
# RemoteOS MCP - One-line installer for Linux (systemd)
# Usage: curl -fsSL https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/master/install-linux.sh | sudo bash
set -euo pipefail

PORT=8092
CONFIG_DIR="/etc/remoteos-mcp"
CONFIG_FILE="$CONFIG_DIR/config.json"
SERVICE_NAME="remoteos-mcp"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

echo ""
echo "  RemoteOS MCP Installer (Linux)"
echo "  Remote desktop control for Claude Code"
echo ""

# --- Check root ---
if [[ "$EUID" -ne 0 ]]; then
    echo "  [X] This installer must be run as root (via sudo)."
    exit 1
fi

# Determine the actual user who invoked sudo
ACTUAL_USER="${SUDO_USER:-root}"

# --- [1/5] Check Python ---
echo "  [1/5] Checking Python..."
PYTHON=""
for cmd in python3 python /usr/bin/python3 /usr/local/bin/python3; do
    if command -v "$cmd" &>/dev/null || [[ -x "$cmd" ]]; then
        ver=$("$cmd" --version 2>&1 || true)
        if [[ "$ver" =~ Python\ 3\.([0-9]+) ]] && (( ${BASH_REMATCH[1]} >= 10 )); then
            PYTHON="$cmd"
            echo "        Found $ver ($cmd)"
            break
        fi
    fi
done

if [[ -z "$PYTHON" ]]; then
    echo "        Python 3.10+ not found. Trying package manager..."
    if command -v apt-get &>/dev/null; then
        apt-get install -y python3 python3-pip
    elif command -v dnf &>/dev/null; then
        dnf install -y python3 python3-pip
    elif command -v yum &>/dev/null; then
        yum install -y python3 python3-pip
    fi
    # Re-check after install
    for cmd in python3 python; do
        if command -v "$cmd" &>/dev/null; then
            ver=$("$cmd" --version 2>&1 || true)
            if [[ "$ver" =~ Python\ 3\.([0-9]+) ]] && (( ${BASH_REMATCH[1]} >= 10 )); then
                PYTHON="$cmd"
                echo "        Python installed: $ver"
                break
            fi
        fi
    done
fi

if [[ -z "$PYTHON" ]]; then
    echo "        [X] Python 3.10+ required but not found."
    echo "        Install manually: apt-get install python3  (or dnf/yum)"
    exit 1
fi

PYTHON_PATH=$(command -v "$PYTHON")

# --- [2/5] Install remoteos-mcp ---
echo "  [2/5] Installing remoteos-mcp..."
"$PYTHON" -m pip install --no-cache-dir --break-system-packages \
    "git+https://github.com/zbynekdrlik/remoteos-mcp.git" 2>&1 | tail -1 || true

# Verify installation
PKG_VER=$("$PYTHON" -m pip show remoteos-mcp 2>/dev/null | grep "^Version:" | awk '{print $2}' || true)
if [[ -n "$PKG_VER" ]]; then
    echo "        Installed v${PKG_VER}"
else
    echo "        [X] pip install failed"
    echo "        Try manually: $PYTHON -m pip install --break-system-packages git+https://github.com/zbynekdrlik/remoteos-mcp.git"
    exit 1
fi

# --- [3/5] Configure auth key ---
echo "  [3/5] Configuring auth key..."
mkdir -p "$CONFIG_DIR"

AUTH_KEY=""
if [[ -f "$CONFIG_FILE" ]]; then
    # Try to read existing key
    EXISTING_KEY=$("$PYTHON" -c "import json; print(json.load(open('$CONFIG_FILE')).get('auth_key',''))" 2>/dev/null || true)
    if [[ -n "$EXISTING_KEY" ]]; then
        AUTH_KEY="$EXISTING_KEY"
        echo "        Reusing existing auth key"
    fi
fi

if [[ -z "$AUTH_KEY" ]]; then
    AUTH_KEY=$(LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 32)
    echo "        Generated new auth key"
fi

# Write config
"$PYTHON" -c "
import json, pathlib
cfg = {'port': $PORT, 'auth_key': '$AUTH_KEY', 'host': '0.0.0.0'}
pathlib.Path('$CONFIG_FILE').write_text(json.dumps(cfg, indent=2))
"
chmod 600 "$CONFIG_FILE"
echo "        Config: $CONFIG_FILE"

# --- [4/5] Set up systemd service ---
echo "  [4/5] Setting up systemd service..."

cat > "$SERVICE_FILE" <<SERVICE
[Unit]
Description=RemoteOS MCP Server
After=network.target

[Service]
Type=simple
ExecStart=${PYTHON_PATH} -m remoteos --transport streamable-http --enable-all --host 0.0.0.0 --port ${PORT} --auth-key ${AUTH_KEY}
Restart=always
RestartSec=5
User=${ACTUAL_USER}

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
echo "        systemd service enabled and started: $SERVICE_NAME"

# --- [5/5] Configure firewall and get network info ---
echo "  [5/5] Configuring firewall and getting network info..."

# ufw (Ubuntu/Debian)
if command -v ufw &>/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
    ufw allow "$PORT"/tcp comment "remoteos-mcp" 2>/dev/null || true
    echo "        ufw: allowed port $PORT"
# firewalld (RHEL/Fedora/CentOS)
elif command -v firewall-cmd &>/dev/null && firewall-cmd --state &>/dev/null 2>&1; then
    firewall-cmd --permanent --add-port="$PORT"/tcp 2>/dev/null || true
    firewall-cmd --reload 2>/dev/null || true
    echo "        firewalld: allowed port $PORT"
else
    echo "        No active firewall detected; skipping firewall rule"
fi

LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "LINUX_IP")
HOSTNAME_SHORT=$(hostname -s 2>/dev/null | tr '[:upper:]' '[:lower:]' || echo "linux")
echo "        $HOSTNAME_SHORT ($LOCAL_IP)"

# --- Summary ---
echo ""
echo "  ============================================"
echo "  SETUP COMPLETE"
echo "  ============================================"
echo ""
echo "  Computer:  $HOSTNAME_SHORT ($LOCAL_IP)"
echo "  Port:      $PORT"
echo "  Auth Key:  $AUTH_KEY"
echo ""
echo "  On your Linux machine, run:"
echo ""
echo "  claude mcp add --transport http linux-${HOSTNAME_SHORT} \\"
echo "    http://${LOCAL_IP}:${PORT}/mcp \\"
echo "    --header \"Authorization: Bearer ${AUTH_KEY}\""
echo ""
echo "  Then restart Claude Code."
echo ""
echo "  To uninstall later:"
echo "  curl -fsSL https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/master/uninstall-linux.sh | sudo bash"
echo ""
