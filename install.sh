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
        if [[ "$ver" =~ Python\ 3\.([0-9]+) ]] && (( BASH_REMATCH[1] >= 10 )); then
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
# No --ignore-installed here: macOS has no apt-owned python packages (that is
# the Linux/Debian case, #12 strih-pp).
PIP_FLAGS=(--no-cache-dir --break-system-packages)
SERVICE_DESC="the ${PLIST_LABEL} LaunchAgent"

# >>> shared install block — byte-identical in install-linux.sh and install.sh
# (tests/test_installer_preflight_rollback.py guards it).  Needs: PYTHON,
# PIP_FLAGS, SERVICE_DESC.  Leaves: PKG_VER (the installed, importable version).
#
# Order: pin one commit of main → record the installed version → dry-run
# preflight → plan + snapshot → pre-uninstall → install → verify (version +
# import).  Any failure before the pre-uninstall changes nothing; any failure
# (or SIGHUP/SIGINT/SIGTERM) after it rolls the snapshot back and exits non-zero
# without touching the service.  Full pip output is printed on every failure.

# Fresh work dir: pip's TMPDIR (so pip cannot reuse stale build artifacts from a
# prior failed install — the cam1 incident), the pip logs and report, the
# rollback helper and the snapshot.  Removed on exit unless a failed rollback
# needs it kept.
WORK_DIR=$(mktemp -d)
KEEP_WORK_DIR=""
# fd 3/4 = the console: the interrupt handler can fire inside a command whose
# output is redirected to a log, and must still reach the operator.
exec 3>&1 4>&2
trap '[[ -n "$KEEP_WORK_DIR" ]] || rm -rf "$WORK_DIR"' EXIT
mkdir -p "$WORK_DIR/pip-tmp"
CHANGED_DEPS=()

# Rollback helper (python: it ships with what we install).  It reads
# `pip show -v -f` output, whose Files: section is exactly each dist's RECORD,
# so the snapshot holds exactly what `pip uninstall` removes — pip's own
# uninstall rollback works the same way.
#   names    <report.json>                list the resolved packages
#   plan     <report.json> <show>         pip-owned installed deps whose version
#                                         changes; exit 3 = remoteos-mcp has no RECORD
#   snapshot <show> <dir> <names...>      copy those dists' files (bytes + modes)
#   restore  <dir>                        put them back
ROLLBACK_PY="$WORK_DIR/rollback.py"
cat >"$ROLLBACK_PY" <<'PY'
import json, os, re, shutil, sys

OVERLAPPING = ("remoteos-mcp", "fastmcp", "fastmcp-slim")


def norm(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def resolved(report_file):
    with open(report_file, encoding="utf-8") as f:
        return {norm(i["metadata"]["name"]): i["metadata"]["version"] for i in json.load(f).get("install", [])}


def dists(show_file):
    found, cur, loc, in_files = {}, None, None, False
    with open(show_file, encoding="utf-8", errors="surrogateescape") as f:
        for line in f.read().splitlines():
            if line == "---":
                cur, loc, in_files = None, None, False
            elif in_files and cur is not None:
                if line.startswith("  ") and loc:
                    cur["files"].append(os.path.normpath(os.path.join(loc, line[2:])))
                elif line.startswith("Cannot locate RECORD"):
                    cur["record"] = False
            elif line.startswith("Name: "):
                cur = {"version": "", "installer": "", "files": [], "record": True}
                found[norm(line[6:].strip())] = cur
            elif cur is not None and line.startswith("Version: "):
                cur["version"] = line[9:].strip()
            elif cur is not None and line.startswith("Installer: "):
                cur["installer"] = line[11:].strip()
            elif line.startswith("Location: "):
                loc = line[10:].strip()
            elif line.startswith("Files:"):
                in_files = True
    return found


mode, args = sys.argv[1], sys.argv[2:]
if mode == "names":
    print("\n".join(resolved(args[0])))
elif mode == "plan":
    want, have = resolved(args[0]), dists(args[1])
    if "remoteos-mcp" in have and not have["remoteos-mcp"]["record"]:
        sys.exit(3)
    for name, info in sorted(have.items()):
        if (name not in OVERLAPPING and name in want and info["version"] != want[name]
                and info["installer"] == "pip" and info["record"] and info["files"]):
            print(name)
elif mode == "snapshot":
    have, snap, names = dists(args[0]), args[1], [norm(n) for n in args[2:]]
    paths = [p for n in names for p in have.get(n, {}).get("files", []) if os.path.isfile(p) or os.path.islink(p)]
    os.makedirs(snap)
    for path in paths:
        dst = snap + "/files" + path
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(path, dst, follow_symlinks=False)
    with open(snap + "/manifest", "w", encoding="utf-8", errors="surrogateescape") as f:
        f.write("\n".join(paths))
    print(len(paths))
elif mode == "restore":
    snap = args[0]
    with open(snap + "/manifest", encoding="utf-8", errors="surrogateescape") as f:
        paths = [p for p in f.read().split("\n") if p]
    for path in paths:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.islink(path) or os.path.isfile(path):
            os.remove(path)
        shutil.copy2(snap + "/files" + path, path, follow_symlinks=False)
    print(len(paths))
else:
    sys.exit("unknown mode " + mode)
PY

installed_version() {
    "$PYTHON" -m pip show remoteos-mcp 2>/dev/null | sed -n 's/^Version: //p' || true
}

# What the service runs (`python -m remoteos`), minus starting it.
import_check() {
    "$PYTHON" -c 'import remoteos.__main__, fastmcp; from fastmcp import FastMCP' 2>&1
}

# Pre-uninstall the three packages so no stale RECORD can delete files the new
# install writes.  Root cause (#12 wheel evidence): monolithic fastmcp 2.x
# RECORD claims fastmcp/__init__.py — same file fastmcp-slim 4.x writes.  Plus
# every pip-owned dep whose pinned version changes: --ignore-installed would
# otherwise leave its old dist-info + RECORD behind next to the new files.
uninstall_overlapping() {
    "$PYTHON" -m pip uninstall -y --break-system-packages remoteos-mcp fastmcp fastmcp-slim ${CHANGED_DEPS[@]+"${CHANGED_DEPS[@]}"} 2>&1 \
        | sed 's/^/        /' || true
}

# The pinned install; extra pip args (--dry-run ...) go first.  The preflight
# and the real install both run through here, so their flags are identical.
pip_install_remoteos() {
    TMPDIR="$WORK_DIR/pip-tmp" "$PYTHON" -m pip install "${PIP_FLAGS[@]}" "$@" "git+https://github.com/zbynekdrlik/remoteos-mcp.git@${MAIN_SHA}"
}

# Put the snapshot back after a failed or interrupted install and check the
# recorded version works again.  Never exits: a rolled-back upgrade is still a
# failed upgrade, the caller exits non-zero.
rollback_install() {
    trap '' HUP INT TERM
    echo "        Rolling back (restoring the previous package files)..."
    uninstall_overlapping >/dev/null  # clear what the failed install half-wrote
    if ! "$PYTHON" "$ROLLBACK_PY" restore "$WORK_DIR/snapshot" >/dev/null; then
        echo "        [X] Rollback FAILED — the previous files could not be restored; do NOT restart ${SERVICE_DESC}"
    elif [[ -z "$OLD_VER" ]]; then
        echo "        [X] No previous version to roll back to — remoteos is NOT installed on this box"
        return
    elif [[ "$(installed_version)" == "$OLD_VER" ]] && import_check >/dev/null; then
        echo "        Rolled back: v${OLD_VER} is installed again and imports; ${SERVICE_DESC} was not restarted"
        return
    else
        echo "        [X] Rollback FAILED — v${OLD_VER} is not back in a working state; do NOT restart ${SERVICE_DESC}"
    fi
    KEEP_WORK_DIR=1
    echo "        Snapshot kept for manual recovery: $PYTHON $ROLLBACK_PY restore $WORK_DIR/snapshot"
}

# An interrupt (ssh drop = SIGHUP, a timeout's SIGTERM, Ctrl-C) between the
# pre-uninstall and a verified install must not leave the box without its
# package.  The terminal may be gone: ignore SIGPIPE, keep going on errors.
on_interrupt() {
    trap '' PIPE
    set +e
    exec 1>&3 2>&4
    echo "        [X] Interrupted during the upgrade"
    rollback_install
    exit 1
}

# Pin the whole run to ONE commit of main: preflight, install and constraints
# all see the same tree (a merge between two fetches of @main would mix trees,
# and raw.githubusercontent.com caches .../main/ for minutes).
MAIN_SHA=$(git ls-remote https://github.com/zbynekdrlik/remoteos-mcp.git refs/heads/main 2>"$WORK_DIR/ls-remote.log" \
    | awk '{print $1}' || true)
if [[ ! "$MAIN_SHA" =~ ^[0-9a-f]{40}$ ]]; then
    echo "        [X] Could not resolve main on GitHub — nothing was changed on this box:"
    cat "$WORK_DIR/ls-remote.log"
    exit 1
fi
export PIP_CONSTRAINT="https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/${MAIN_SHA}/constraints.txt"
echo "        Source: main @ ${MAIN_SHA:0:12}"

OLD_VER=$(installed_version)
if [[ -n "$OLD_VER" ]]; then
    echo "        Currently installed: v${OLD_VER}"
else
    echo "        No previous installation"
fi

# Preflight: resolve the whole pinned set with the real install's flags and
# constraints BEFORE anything is uninstalled.  A failure here changes nothing.
echo "        Preflight: pip install --dry-run..."
if ! pip_install_remoteos --dry-run --report "$WORK_DIR/report.json" >"$WORK_DIR/dry-run.log" 2>&1; then
    echo "        [X] Preflight failed — nothing was changed on this box. Full pip output:"
    cat "$WORK_DIR/dry-run.log"
    exit 1
fi
grep '^Would install' "$WORK_DIR/dry-run.log" | sed 's/^/        /' || true

# Plan + snapshot what the pre-uninstall will remove.
if ! "$PYTHON" "$ROLLBACK_PY" names "$WORK_DIR/report.json" >"$WORK_DIR/resolved.txt"; then
    echo "        [X] Could not read the preflight's resolution report — nothing was changed on this box"
    exit 1
fi
RESOLVED=()
while IFS= read -r name; do
    if [[ -n "$name" ]]; then RESOLVED+=("$name"); fi
done <"$WORK_DIR/resolved.txt"
"$PYTHON" -m pip show -v -f remoteos-mcp fastmcp fastmcp-slim ${RESOLVED[@]+"${RESOLVED[@]}"} \
    >"$WORK_DIR/show-files.txt" 2>/dev/null || true
PLAN_RC=0
"$PYTHON" "$ROLLBACK_PY" plan "$WORK_DIR/report.json" "$WORK_DIR/show-files.txt" >"$WORK_DIR/changed-deps.txt" \
    || PLAN_RC=$?
if [[ "$PLAN_RC" -eq 3 ]]; then
    echo "        [X] v${OLD_VER} has no RECORD, so pip can neither replace nor roll it back — nothing was changed."
    echo "            Remove it by hand (its files under: $("$PYTHON" -m pip show remoteos-mcp 2>/dev/null | sed -n 's/^Location: //p')), then rerun."
    exit 1
elif [[ "$PLAN_RC" -ne 0 ]]; then
    echo "        [X] Could not plan the upgrade — nothing was changed on this box"
    exit 1
fi
while IFS= read -r name; do
    if [[ -n "$name" ]]; then CHANGED_DEPS+=("$name"); fi
done <"$WORK_DIR/changed-deps.txt"
if [[ ${#CHANGED_DEPS[@]} -gt 0 ]]; then
    echo "        Pinned version changes for pip-installed deps: ${CHANGED_DEPS[*]}"
fi
if ! SNAP_COUNT=$("$PYTHON" "$ROLLBACK_PY" snapshot "$WORK_DIR/show-files.txt" "$WORK_DIR/snapshot" \
    remoteos-mcp fastmcp fastmcp-slim ${CHANGED_DEPS[@]+"${CHANGED_DEPS[@]}"}); then
    echo "        [X] Could not keep a rollback copy of the installed packages — nothing was changed on this box"
    exit 1
fi
echo "        Kept ${SNAP_COUNT} files for rollback"

trap on_interrupt HUP INT TERM
uninstall_overlapping
if pip_install_remoteos >"$WORK_DIR/install.log" 2>&1; then
    tail -3 "$WORK_DIR/install.log"
else
    echo "        [X] pip install failed. Full pip output:"
    cat "$WORK_DIR/install.log"
    rollback_install
    exit 1
fi

PKG_VER=$(installed_version)
if [[ -z "$PKG_VER" ]]; then
    echo "        [X] pip reported success but no remoteos package is installed. Full pip output:"
    cat "$WORK_DIR/install.log"
    rollback_install
    exit 1
fi
if ! IMPORT_OUT=$(import_check); then
    echo "        [X] v${PKG_VER} is installed but does not import:"
    echo "$IMPORT_OUT"
    rollback_install
    exit 1
fi
trap - HUP INT TERM
echo "        Installed v${PKG_VER}"
# <<< shared install block

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
