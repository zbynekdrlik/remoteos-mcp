#!/usr/bin/env bash
# RemoteOS MCP - One-line installer for Linux (systemd)
# Usage: curl -fsSL https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/main/install-linux.sh | sudo bash
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

# --- Pre-flight: check for read-only root filesystem ---
if command -v findmnt &>/dev/null; then
    ROOT_OPTS=$(findmnt -no OPTIONS / 2>/dev/null || true)
    if echo ",$ROOT_OPTS," | grep -q ',ro,'; then
        echo "  [X] Root filesystem is read-only — run: mount -o remount,rw / (and fix /etc/fstab: '/ ext4 ro' → 'defaults') then rerun."
        exit 1
    fi
fi

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

# Ensure git is available (needed for pip install from GitHub)
if ! command -v git &>/dev/null; then
    echo "        git not found, installing..."
    if command -v apt-get &>/dev/null; then
        apt-get update -qq && apt-get install -y -qq git
    elif command -v dnf &>/dev/null; then
        dnf install -y git
    elif command -v yum &>/dev/null; then
        yum install -y git
    fi
fi

# Ensure pip is available
if ! "$PYTHON" -m pip --version &>/dev/null; then
    echo "        pip not found, installing..."
    if command -v apt-get &>/dev/null; then
        apt-get update -qq && apt-get install -y -qq python3-pip python3-venv
    elif command -v dnf &>/dev/null; then
        dnf install -y python3-pip
    elif command -v yum &>/dev/null; then
        yum install -y python3-pip
    else
        "$PYTHON" -m ensurepip --upgrade 2>/dev/null || true
    fi
    if ! "$PYTHON" -m pip --version &>/dev/null; then
        echo "        [X] pip could not be installed"
        exit 1
    fi
    echo "        pip installed"
fi

# --- [2/5] Install remoteos-mcp ---
echo "  [2/5] Installing remoteos-mcp..."
export PIP_CONSTRAINT="https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/main/constraints.txt"
# --ignore-installed: on Ubuntu 26.04+ / Debian 13 several pinned deps
# (cryptography, cffi, PyJWT, psutil, ...) are apt python3-* packages that
# Debian's pip refuses to uninstall ("installed by debian", #12 strih-pp).
# Installing over them into /usr/local shadows the apt copies instead.  Safe
# because the three packages that must never overlap (remoteos-mcp, fastmcp,
# fastmcp-slim) are explicitly pre-uninstalled below.
PIP_FLAGS=(--no-cache-dir --break-system-packages --ignore-installed)
# Fresh work dir: pip's TMPDIR (so pip cannot reuse stale build artifacts from
# a prior failed install — the cam1 incident), the pip logs, and the rollback
# snapshot.  Removed on every exit.
WORK_DIR=$(mktemp -d)
trap 'rm -rf "$WORK_DIR"' EXIT
mkdir -p "$WORK_DIR/pip-tmp"

# Snapshot / restore the files of the packages the pre-uninstall removes —
# exactly the RECORD entries `pip show -f` lists — so a failed upgrade puts the
# previous version back byte for byte, offline (pip's own uninstall rollback
# works the same way).  Usage: snapshot <pip show -f output> <dir> | restore <dir>
ROLLBACK_PY="$WORK_DIR/rollback.py"
cat >"$ROLLBACK_PY" <<'PY'
import os, shutil, sys
mode, args = sys.argv[1], sys.argv[2:]
if mode == "snapshot":
    show_file, snap = args
    paths, loc, in_files = [], None, False
    with open(show_file, encoding="utf-8", errors="surrogateescape") as f:
        for line in f.read().splitlines():
            if line == "---":
                loc, in_files = None, False
            elif line.startswith("Location: "):
                loc = line[len("Location: "):].strip()
            elif line.startswith("Files:"):
                in_files = True
            elif in_files and loc and line.startswith("  "):
                path = os.path.normpath(os.path.join(loc, line[2:]))
                if os.path.isfile(path) or os.path.islink(path):
                    paths.append(path)
    os.makedirs(snap)
    for path in paths:
        dst = snap + "/files" + path
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(path, dst, follow_symlinks=False)
    with open(snap + "/manifest", "w", encoding="utf-8", errors="surrogateescape") as f:
        f.write("\n".join(paths))
    print(len(paths))
elif mode == "restore":
    (snap,) = args
    with open(snap + "/manifest", encoding="utf-8", errors="surrogateescape") as f:
        paths = [p for p in f.read().split("\n") if p]
    for path in paths:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.islink(path) or os.path.isfile(path):
            os.remove(path)
        shutil.copy2(snap + "/files" + path, path, follow_symlinks=False)
    print(len(paths))
else:
    sys.exit(f"unknown mode {mode}")
PY

installed_version() {
    "$PYTHON" -m pip show remoteos-mcp 2>/dev/null | sed -n 's/^Version: //p' || true
}

# Pre-uninstall all three packages so no stale RECORD can delete files the new
# install writes.  Root cause (#12 wheel evidence): monolithic fastmcp 2.x
# RECORD claims fastmcp/__init__.py — same file fastmcp-slim 4.x writes.
uninstall_overlapping() {
    "$PYTHON" -m pip uninstall -y --break-system-packages remoteos-mcp fastmcp fastmcp-slim 2>&1 | tail -1 || true
}

# The pinned install; extra pip args (--dry-run) go first.  The preflight and
# the real install both run through here, so their flags are identical.
pip_install_remoteos() {
    TMPDIR="$WORK_DIR/pip-tmp" "$PYTHON" -m pip install "${PIP_FLAGS[@]}" "$@" "git+https://github.com/zbynekdrlik/remoteos-mcp.git@main"
}

# Put the recorded previous version back after a failed install.  Never exits:
# the caller still exits non-zero — a rolled-back upgrade is a failed upgrade.
rollback_install() {
    if [[ -z "$OLD_VER" ]]; then
        echo "        [X] No previous version to roll back to — remoteos is NOT installed on this box"
        return
    fi
    echo "        Rolling back to v${OLD_VER} (restoring the previous package files)..."
    uninstall_overlapping >/dev/null  # clear what the failed install half-wrote for the three packages
    if "$PYTHON" "$ROLLBACK_PY" restore "$WORK_DIR/snapshot" >/dev/null \
        && [[ "$(installed_version)" == "$OLD_VER" ]]; then
        echo "        Rolled back: v${OLD_VER} is installed again; the running service was not restarted"
    else
        echo "        [X] Rollback FAILED — v${OLD_VER} could not be restored; do NOT restart ${SERVICE_NAME}"
    fi
}

OLD_VER=$(installed_version)
if [[ -n "$OLD_VER" ]]; then
    echo "        Currently installed: v${OLD_VER}"
else
    echo "        No previous installation"
fi

# Preflight: resolve the whole pinned set with the real install's flags and
# constraints BEFORE anything is uninstalled.  A failure here changes nothing.
echo "        Preflight: pip install --dry-run..."
if ! pip_install_remoteos --dry-run >"$WORK_DIR/dry-run.log" 2>&1; then
    echo "        [X] Preflight failed — nothing was changed on this box. Full pip output:"
    cat "$WORK_DIR/dry-run.log"
    exit 1
fi
grep '^Would install' "$WORK_DIR/dry-run.log" | sed 's/^/        /' || true

if [[ -n "$OLD_VER" ]]; then
    "$PYTHON" -m pip show -f remoteos-mcp fastmcp fastmcp-slim >"$WORK_DIR/show-files.txt" 2>/dev/null || true
    if ! SNAP_COUNT=$("$PYTHON" "$ROLLBACK_PY" snapshot "$WORK_DIR/show-files.txt" "$WORK_DIR/snapshot"); then
        echo "        [X] Could not keep a rollback copy of v${OLD_VER} — nothing was changed on this box"
        exit 1
    fi
    echo "        Kept ${SNAP_COUNT} files of v${OLD_VER} for rollback"
fi

uninstall_overlapping
if pip_install_remoteos >"$WORK_DIR/install.log" 2>&1; then
    tail -3 "$WORK_DIR/install.log"
else
    echo "        [X] pip install failed. Full pip output:"
    cat "$WORK_DIR/install.log"
    rollback_install
    exit 1
fi

# Verify installation
PKG_VER=$(installed_version)
if [[ -n "$PKG_VER" ]]; then
    echo "        Installed v${PKG_VER}"
else
    echo "        [X] pip reported success but no remoteos package is installed. Full pip output:"
    cat "$WORK_DIR/install.log"
    rollback_install
    exit 1
fi

# --- [2b] Install desktop-control tools (only when a graphical session exists) ---
# Headless boxes (cam1..4) stay stub-only and skip these; a machine with an
# active X11/Wayland session gets xdotool/scrot/xclip/wmctrl + tesseract so the
# desktop tools (Snapshot, Click, Type, clipboard, OCR, ...) actually work.
echo "  [+]   Checking for a graphical session..."
GRAPHICAL=""
if command -v loginctl &>/dev/null; then
    for s in $(loginctl list-sessions --no-legend 2>/dev/null | awk '{print $1}'); do
        stype=$(loginctl show-session "$s" -p Type --value 2>/dev/null || true)
        sclass=$(loginctl show-session "$s" -p Class --value 2>/dev/null || true)
        if [[ "$sclass" == "user" && ( "$stype" == "x11" || "$stype" == "wayland" ) ]]; then
            GRAPHICAL="$stype"
            break
        fi
    done
fi

if [[ -n "$GRAPHICAL" ]]; then
    echo "        Graphical session detected ($GRAPHICAL) — installing desktop tools"
    if command -v apt-get &>/dev/null; then
        apt-get update -qq || true
        # idempotent: apt-get install is a no-op for already-present packages
        #   ffmpeg                     → ScreenRecord (x11grab, issue #8)
        #   python3-pyatspi/gir/gi     → AnnotatedSnapshot AT-SPI tree (issue #7)
        #   x11-xserver-utils (xrandr) → per-monitor Snapshot/input frames (issue #28)
        apt-get install -y -qq xdotool scrot xclip wmctrl tesseract-ocr ffmpeg x11-xserver-utils \
            python3-pyatspi gir1.2-atspi-2.0 python3-gi \
            || echo "        [!] Some desktop tools failed to install; desktop control may be limited"
        echo "        Desktop tools ready (xdotool scrot xclip wmctrl xrandr tesseract-ocr ffmpeg AT-SPI)"
    else
        echo "        [!] Non-apt system — install 'xdotool scrot xclip wmctrl xrandr tesseract ffmpeg python3-pyatspi gir1.2-atspi-2.0' manually"
    fi
else
    echo "        Headless (no graphical session) — skipping desktop tools"
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
    # head -c closes pipe, tr gets SIGPIPE → pipefail aborts. Use python instead.
    AUTH_KEY=$("$PYTHON" -c "import secrets, string; print(''.join(secrets.choice(string.ascii_letters+string.digits) for _ in range(32)))")
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

# --- Post-install: verify service reports the correct version ---
echo "  [+]   Verifying installed version matches running service..."
HEALTH_OK=false
for _attempt in $(seq 1 6); do
    HEALTH_JSON=$(curl -sf "http://127.0.0.1:${PORT}/health" 2>/dev/null || true)
    if [[ -n "$HEALTH_JSON" ]]; then
        HEALTH_VER=$(echo "$HEALTH_JSON" | "$PYTHON" -c "import sys,json; print(json.load(sys.stdin).get('version',''))" 2>/dev/null || true)
        if [[ "$HEALTH_VER" == "$PKG_VER" ]]; then
            HEALTH_OK=true
            echo "        Health check OK: v${HEALTH_VER}"
            break
        elif [[ -n "$HEALTH_VER" ]]; then
            echo "  [X] Installed ${PKG_VER} but service reports ${HEALTH_VER}"
            exit 1
        fi
    fi
    sleep 5
done
if [[ "$HEALTH_OK" != "true" ]]; then
    echo "  [X] Health endpoint (http://127.0.0.1:${PORT}/health) did not respond within 30s"
    exit 1
fi

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
echo "  curl -fsSL https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/main/uninstall-linux.sh | sudo bash"
echo ""
