#!/usr/bin/env bash
# RemoteOS MCP - One-line installer for macOS
# Usage: curl -fsSL https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/main/install.sh | bash
set -euo pipefail

PORT=8092
CONFIG_DIR="$HOME/.remoteos-mcp"
CONFIG_FILE="$CONFIG_DIR/config.json"
PLIST_LABEL="com.remoteos-mcp"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"

echo ""
echo "  RemoteOS MCP Installer (macOS)"
echo "  Remote desktop control for Claude Code"
echo ""

# --- [1/5] Check Python ---
echo "  [1/5] Checking Python..."
PYTHON=""
# Check PATH and common install locations (curl|bash may have a minimal PATH)
for cmd in python3 python /usr/local/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3.13 /usr/local/bin/python3.12 /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12; do
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
    echo "        Python 3.10+ not found. Trying Homebrew..."
    if command -v brew &>/dev/null; then
        brew install python@3.12
        if command -v python3 &>/dev/null; then
            PYTHON="python3"
            echo "        Python installed via Homebrew"
        fi
    fi
fi

if [[ -z "$PYTHON" ]]; then
    echo "        [X] Python 3.10+ required but not found."
    echo "        Install Homebrew (https://brew.sh) then run:"
    echo "          brew install python@3.12"
    echo "        Or install Python from https://python.org"
    exit 1
fi

# --- [2/5] Install remoteos-mcp ---
echo "  [2/5] Installing remoteos-mcp..."
export PIP_CONSTRAINT="https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/main/constraints.txt"
# No --ignore-installed here: macOS has no apt-owned python packages (that is
# the Linux/Debian case, #12 strih-pp).
PIP_FLAGS=(--no-cache-dir --break-system-packages)
# Fresh work dir: pip's TMPDIR (so pip cannot reuse stale build artifacts from
# a prior failed install), the pip logs, and the rollback snapshot.  Removed on
# every exit.
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
        echo "        [X] Rollback FAILED — v${OLD_VER} could not be restored; do NOT reload the LaunchAgent ${PLIST_LABEL}"
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
echo "        Config: $CONFIG_FILE"

# --- [4/5] Set up LaunchAgent auto-start ---
echo "  [4/5] Setting up auto-start..."

PYTHON_PATH=$(command -v "$PYTHON")

mkdir -p "$HOME/Library/LaunchAgents"

cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON_PATH}</string>
        <string>-m</string>
        <string>remoteos</string>
        <string>--transport</string>
        <string>streamable-http</string>
        <string>--enable-all</string>
        <string>--host</string>
        <string>0.0.0.0</string>
        <string>--port</string>
        <string>${PORT}</string>
        <string>--auth-key</string>
        <string>${AUTH_KEY}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${CONFIG_DIR}/stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${CONFIG_DIR}/stderr.log</string>
</dict>
</plist>
PLIST

# Load the agent
launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load "$PLIST_PATH"
echo "        LaunchAgent loaded: $PLIST_LABEL"

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

# --- [5/5] Get network info ---
echo "  [5/5] Getting network info..."
LOCAL_IP=$(ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}' || echo "MAC_IP")
HOSTNAME_SHORT=$(hostname -s 2>/dev/null | tr '[:upper:]' '[:lower:]' || echo "mac")
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
echo "  claude mcp add --transport http mac-${HOSTNAME_SHORT} \\"
echo "    http://${LOCAL_IP}:${PORT}/mcp \\"
echo "    --header \"Authorization: Bearer ${AUTH_KEY}\""
echo ""
echo "  Then restart Claude Code."
echo ""
echo "  To uninstall later:"
echo "  curl -fsSL https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/main/uninstall.sh | bash"
echo ""
