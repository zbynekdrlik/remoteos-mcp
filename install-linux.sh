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
        if [[ "$ver" =~ Python\ 3\.([0-9]+) ]] && (( BASH_REMATCH[1] >= 10 )); then
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
            if [[ "$ver" =~ Python\ 3\.([0-9]+) ]] && (( BASH_REMATCH[1] >= 10 )); then
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
# --ignore-installed: on Ubuntu 26.04+ / Debian 13 several pinned deps
# (cryptography, cffi, PyJWT, psutil, ...) are apt python3-* packages that
# Debian's pip refuses to uninstall ("installed by debian", #12 strih-pp).
# Installing over them into /usr/local shadows the apt copies instead.  Safe
# because the shared block pre-uninstalls everything pip itself owns and is
# about to replace (remoteos-mcp, fastmcp, fastmcp-slim, changed pip-owned deps).
PIP_FLAGS=(--no-cache-dir --break-system-packages --ignore-installed)
SERVICE_DESC="the ${SERVICE_NAME} service"

# >>> shared install block — byte-identical in install-linux.sh and install.sh
# (tests/test_installer_preflight_rollback.py guards it).  Needs: PYTHON,
# PIP_FLAGS, SERVICE_DESC.  Leaves: PKG_VER (the installed, importable version).
#
# Order: pin one commit of main → record the installed version → dry-run
# preflight → plan + snapshot → ARM → pre-uninstall → install → verify (version
# + import) → DISARM.  Before ARM nothing on the box has changed.  Whatever ends
# the run while ARMED — a failed step, a signal (ssh drop, timeout, Ctrl-C), a
# lost console (SIGPIPE is ignored, so the write fails and set -e exits), a
# truncated download — goes through the EXIT trap, which rolls the snapshot back
# and exits non-zero without touching the service.  pip always writes to a log;
# the full log is printed on failure.

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
# otherwise leave its old dist-info + RECORD behind next to the new files.  The
# plan guarantees each of these has a RECORD, so pip cannot refuse one of them.
uninstall_overlapping() {
    "$PYTHON" -m pip uninstall -y --break-system-packages remoteos-mcp fastmcp fastmcp-slim ${CHANGED_DEPS[@]+"${CHANGED_DEPS[@]}"} >>"$WORK_DIR/uninstall.log" 2>&1 || true
}

# The pinned install; extra pip args (--dry-run ...) go first.  The preflight
# and the real install both run through here, so their flags are identical.
pip_install_remoteos() {
    TMPDIR="$WORK_DIR/pip-tmp" "$PYTHON" -m pip install "${PIP_FLAGS[@]}" "$@" "git+https://github.com/zbynekdrlik/remoteos-mcp.git@${MAIN_SHA}"
}

# Undo a failed or unfinished upgrade: take out every pip-owned copy of what
# the install may have written (the three, the changed deps, the deps it added)
# — one name per pip call, so one refusal cannot skip the rest — then restore
# the snapshot and check the recorded version works again.  Never exits.
rollback_install() {
    local name
    trap '' HUP INT TERM
    echo "        Rolling back (restoring the previous package files)..."
    if "$PYTHON" "$ROLLBACK_PY" pipowned remoteos-mcp fastmcp fastmcp-slim ${CHANGED_DEPS[@]+"${CHANGED_DEPS[@]}"} \
        ${ADDED_DEPS[@]+"${ADDED_DEPS[@]}"} >"$WORK_DIR/remove.txt" 2>>"$WORK_DIR/rollback.log"; then
        while IFS= read -r name; do
            if [[ -n "$name" ]]; then
                "$PYTHON" -m pip uninstall -y --break-system-packages "$name" >>"$WORK_DIR/rollback.log" 2>&1 || true
            fi
        done <"$WORK_DIR/remove.txt"
    fi
    if ! "$PYTHON" "$ROLLBACK_PY" restore "$WORK_DIR/snapshot" >>"$WORK_DIR/rollback.log" 2>&1; then
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
    echo "        Rollback log: $WORK_DIR/rollback.log"
}

on_signal() {
    trap '' HUP INT TERM
    INTERRUPTED=1
    exit 1
}

on_exit() {
    local rc=$?
    if [[ -n "$ROLLBACK_ARMED" ]]; then
        ROLLBACK_ARMED=""
        trap '' PIPE HUP INT TERM
        set +e
        exec 1>&3 2>&4
        if [[ -n "$INTERRUPTED" ]]; then
            echo "        [X] Interrupted during the upgrade"
        elif [[ "$rc" -eq 0 ]]; then
            echo "        [X] The installer ended before the upgrade finished (truncated download?)"
        fi
        rollback_install
        rc=1
    fi
    if [[ -z "$KEEP_WORK_DIR" ]]; then
        rm -rf "$WORK_DIR"
    fi
    exit "$rc"
}

# Fresh work dir: pip's TMPDIR (so pip cannot reuse stale build artifacts from a
# prior failed install — the cam1 incident), the pip logs and report, the
# rollback helper and the snapshot.  Removed on exit unless a failed rollback
# needs it kept.  fd 3/4 = the console, for the EXIT trap (it can fire inside a
# command whose output is redirected to a log).
WORK_DIR=$(mktemp -d)
KEEP_WORK_DIR=""
ROLLBACK_ARMED=""
INTERRUPTED=""
OLD_VER=""
CHANGED_DEPS=()
ADDED_DEPS=()
exec 3>&1 4>&2
trap on_exit EXIT
mkdir -p "$WORK_DIR/pip-tmp"

# Rollback helper (python: it ships with what we install).  It reads installed
# dists through importlib.metadata — RECORD + INSTALLER, exactly what pip itself
# uses — so the snapshot holds exactly what `pip uninstall` removes; pip's own
# uninstall rollback works the same way.  Exit 3 = no safe rollback possible.
#   plan     <report.json> <installed version> <dir>  → dir/changed.txt: pip-owned
#            deps whose pinned version changes; dir/added.txt: deps not pip-owned yet
#   snapshot <dir> <names...>   copy those dists' RECORD files (bytes + modes)
#   pipowned <names...>         the names whose visible dist pip owns
#   restore  <dir>              put the snapshot back
ROLLBACK_PY="$WORK_DIR/rollback.py"
cat >"$ROLLBACK_PY" <<'PY'
import importlib.metadata as md
import json, os, re, shutil, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.curdir) != HERE]  # only what pip sees
OVERLAPPING = ("remoteos-mcp", "fastmcp", "fastmcp-slim")


def norm(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def fail(message):
    sys.stderr.write(message + "\n")
    sys.exit(3)


def visible(name):
    """The dist pip sees for `name` (the first one on sys.path), or None."""
    try:
        return md.distribution(name)
    except md.PackageNotFoundError:
        return None


def pip_owned(dist):
    return dist is not None and dist.files is not None and (dist.read_text("INSTALLER") or "").strip() == "pip"


def record_files(dist):
    paths = (os.path.normpath(str(dist.locate_file(f))) for f in dist.files or [])
    return [p for p in paths if os.path.isfile(p) or os.path.islink(p)]


def resolved(report_file):
    with open(report_file, encoding="utf-8") as f:
        return {norm(i["metadata"]["name"]): i["metadata"]["version"] for i in json.load(f).get("install", [])}


mode, args = sys.argv[1], sys.argv[2:]
if mode == "plan":
    want, old_ver, out = resolved(args[0]), args[1], args[2]
    for name in OVERLAPPING:
        dist = visible(name)
        if dist is not None and dist.files is None:
            fail(name + " " + dist.version + " has no RECORD, so pip can neither replace nor roll it back")
    if old_ver and visible("remoteos-mcp") is None:
        fail("pip reports remoteos-mcp " + old_ver + " but its metadata cannot be read")
    changed, added = [], []
    for name, version in sorted(want.items()):
        if name in OVERLAPPING:
            continue
        dist = visible(name)
        if not pip_owned(dist):
            added.append(name)
        elif dist.version != version:
            changed.append(name)
    os.makedirs(out, exist_ok=True)
    for fname, names in (("changed.txt", changed), ("added.txt", added)):
        with open(os.path.join(out, fname), "w", encoding="utf-8") as f:
            f.write("".join(n + "\n" for n in names))
elif mode == "snapshot":
    snap, paths = args[0], []
    for name in args[1:]:
        dist = visible(name)
        if dist is None:
            continue
        files = record_files(dist)
        if not any(os.path.basename(p) in ("METADATA", "PKG-INFO") for p in files):
            fail("the RECORD of " + name + " " + dist.version + " does not list its own metadata")
        paths += files
    os.makedirs(snap)
    for path in paths:
        dst = snap + "/files" + path
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(path, dst, follow_symlinks=False)
    with open(snap + "/manifest", "w", encoding="utf-8", errors="surrogateescape") as f:
        f.write("\n".join(paths))
    print(len(paths))
elif mode == "pipowned":
    for name in args:
        if pip_owned(visible(name)):
            print(name)
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

# Plan + snapshot what the upgrade will replace.
if ! "$PYTHON" "$ROLLBACK_PY" plan "$WORK_DIR/report.json" "$OLD_VER" "$WORK_DIR" 2>"$WORK_DIR/plan.err"; then
    echo "        [X] No safe rollback possible — nothing was changed on this box:"
    sed 's/^/            /' "$WORK_DIR/plan.err"
    exit 1
fi
while IFS= read -r name; do
    if [[ -n "$name" ]]; then CHANGED_DEPS+=("$name"); fi
done <"$WORK_DIR/changed.txt"
while IFS= read -r name; do
    if [[ -n "$name" ]]; then ADDED_DEPS+=("$name"); fi
done <"$WORK_DIR/added.txt"
if [[ ${#CHANGED_DEPS[@]} -gt 0 ]]; then
    echo "        Pinned version changes for pip-installed deps: ${CHANGED_DEPS[*]}"
fi
if ! SNAP_COUNT=$("$PYTHON" "$ROLLBACK_PY" snapshot "$WORK_DIR/snapshot" \
    remoteos-mcp fastmcp fastmcp-slim ${CHANGED_DEPS[@]+"${CHANGED_DEPS[@]}"} 2>"$WORK_DIR/snapshot.err"); then
    echo "        [X] Could not keep a rollback copy of the installed packages — nothing was changed on this box:"
    sed 's/^/            /' "$WORK_DIR/snapshot.err"
    exit 1
fi
echo "        Kept ${SNAP_COUNT} files for rollback"

# ARM: from here until the install is verified, any exit rolls back.
ROLLBACK_ARMED=1
trap on_signal HUP INT TERM
trap '' PIPE
uninstall_overlapping
sed 's/^/        /' "$WORK_DIR/uninstall.log"
if ! pip_install_remoteos >"$WORK_DIR/install.log" 2>&1; then
    echo "        [X] pip install failed. Full pip output:"
    cat "$WORK_DIR/install.log"
    exit 1
fi
tail -3 "$WORK_DIR/install.log"

PKG_VER=$(installed_version)
if [[ -z "$PKG_VER" ]]; then
    echo "        [X] pip reported success but no remoteos package is installed. Full pip output:"
    cat "$WORK_DIR/install.log"
    exit 1
fi
if ! IMPORT_OUT=$(import_check); then
    echo "        [X] v${PKG_VER} is installed but does not import:"
    echo "$IMPORT_OUT"
    exit 1
fi
# DISARM: the new version is installed and imports.
ROLLBACK_ARMED=""
trap - HUP INT TERM PIPE
exec 3>&- 4>&-
echo "        Installed v${PKG_VER}"
# <<< shared install block

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
