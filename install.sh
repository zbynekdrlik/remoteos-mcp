#!/usr/bin/env bash
# RemoteOS MCP - One-line installer for macOS
# Usage: curl -fsSL https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/main/install.sh | bash
set -euo pipefail

# Everything runs inside main(), called on the very last line: bash parses the
# whole function before running any of it, so a truncated `curl | bash`
# download fails to parse (or never reaches the call) instead of running half.
main() {

PORT=8092
CONFIG_DIR="$HOME/.remoteos-mcp"
CONFIG_FILE="$CONFIG_DIR/config.json"
PLIST_LABEL="com.remoteos-mcp"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"

# bash keeps the export flag of a name the caller exported (globals AND locals): unset every
# name that ever holds the key, so none of them reaches a child's environment (#12).
unset AUTH_KEY EXPLICIT_KEY CFG_KEY PLIST_KEY KEY_SOURCE k key
# The explicit key is read once and dropped from the environment BEFORE the first child
# (`python3 --version`, a brew fallback): every child inherits it (review round 9).
EXPLICIT_KEY="${REMOTEOS_MCP_AUTH_KEY:-}"
unset REMOTEOS_MCP_AUTH_KEY
# The source (#64): a commit sha, a tag or a branch (default main, empty = main), resolved below to
# ONE commit that the install and its constraints both use, like install-linux.sh.
REF="${REMOTEOS_MCP_REF:-main}"

echo ""
echo "  RemoteOS MCP Installer (macOS)"
echo "  Remote desktop control for Claude Code"
echo ""

# A git ref name: no option, no `..`, letters/digits/._/- only (the ASCII range under LC_ALL=C).
ref_ok() {
    (
        LC_ALL=C
        r="${1-}"
        [[ -n "$r" && ${#r} -le 200 ]] || exit 1
        case "$r" in
            -* | .* | */ | *..* | *//* | */.*) exit 1 ;;
            *[!A-Za-z0-9._/-]*) exit 1 ;;
        esac
    )
}
if ! ref_ok "$REF"; then
    echo "        [X] REMOTEOS_MCP_REF must be a commit sha, a tag or a branch name — nothing was changed on this box"
    exit 1
fi

# python_ok "<python --version output>": 3.11+ (Pillow 13, the fix of its CVEs, needs 3.11; #64)
python_ok() {
    [[ "${1-}" =~ Python\ 3\.([0-9]+) ]] && (( BASH_REMATCH[1] >= 11 ))
}

# --- [1/5] Check Python ---
echo "  [1/5] Checking Python..."
PYTHON=""
# Check PATH and common install locations (curl|bash may have a minimal PATH)
for cmd in python3 python /usr/local/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3.13 /usr/local/bin/python3.12 /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12; do
    if command -v "$cmd" &>/dev/null || [[ -x "$cmd" ]]; then
        ver=$("$cmd" --version 2>&1 || true)
        if python_ok "$ver"; then
            PYTHON="$cmd"
            echo "        Found $ver ($cmd)"
            break
        fi
    fi
done

if [[ -z "$PYTHON" ]]; then
    echo "        Python 3.11+ not found. Trying Homebrew..."
    if command -v brew &>/dev/null; then
        brew install python@3.12
        # The one brew installed, never an older python3 first on PATH (#64)
        for cmd in /opt/homebrew/bin/python3.12 /usr/local/bin/python3.12 python3; do
            ver=$("$cmd" --version 2>&1 || true)
            if python_ok "$ver"; then
                PYTHON="$cmd"
                echo "        Python installed via Homebrew: $ver ($cmd)"
                break
            fi
        done
    fi
fi

if [[ -z "$PYTHON" ]]; then
    echo "        [X] Python 3.11+ required but not found."
    echo "        Install Homebrew (https://brew.sh) then run:"
    echo "          brew install python@3.12"
    echo "        Or install Python from https://python.org"
    exit 1
fi

# --- The auth key (read before anything changes: an invalid explicit key is refused) ---
# The key precedence is the Linux installer's (#12): an explicit REMOTEOS_MCP_AUTH_KEY, else
# the key the running LaunchAgent uses (a legacy `--auth-key` in its ProgramArguments, else
# its EnvironmentVariables), else config.json, else a new key.  A re-run never changes it.
# The key reaches the server through the plist's EnvironmentVariables (REMOTEOS_AUTH_KEY),
# never an argv, and is never printed.

# 1-128 ASCII letters/digits.  LC_ALL=C in the subshell makes the bracket range ASCII
# (bash 3.2 has no globasciiranges).
key_ok() {
    (
        LC_ALL=C
        k="${1-}"
        [[ -n "$k" && ${#k} -le 128 ]] || exit 1
        case "$k" in
            *[!A-Za-z0-9]*) exit 1 ;;
        esac
    )
}

# The auth_key of a config.json (python reads the file; the key only comes back on stdout).
key_from_config() {
    [[ -f "$1" ]] || return 0
    "$PYTHON" -c '
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        v = json.load(f).get("auth_key", "")
except Exception:
    v = ""
print(v if isinstance(v, str) else "", end="")' "$1" 2>/dev/null || true
}

# The key a LaunchAgent plist hands the server: a legacy `--auth-key <key>` in
# ProgramArguments, else EnvironmentVariables' REMOTEOS_AUTH_KEY.
key_from_plist() {
    [[ -f "$1" ]] || return 0
    "$PYTHON" -c '
import plistlib, sys
try:
    with open(sys.argv[1], "rb") as f:
        p = plistlib.load(f)
except Exception:
    sys.exit(0)
args = p.get("ProgramArguments") or []
key = ""
if "--auth-key" in args and args.index("--auth-key") + 1 < len(args):
    key = args[args.index("--auth-key") + 1]
elif isinstance(p.get("EnvironmentVariables"), dict):
    key = p["EnvironmentVariables"].get("REMOTEOS_AUTH_KEY", "")
print(key if isinstance(key, str) else "", end="")' "$1" 2>/dev/null || true
}

KEY_SOURCE=""
CFG_KEY=$(key_from_config "$CONFIG_FILE")
if [[ -n "$EXPLICIT_KEY" ]]; then
    if ! key_ok "$EXPLICIT_KEY"; then
        echo "  [X] REMOTEOS_MCP_AUTH_KEY must be 1-128 letters/digits [A-Za-z0-9] — nothing was changed on this mac"
        exit 1
    fi
    AUTH_KEY="$EXPLICIT_KEY"
    KEY_SOURCE="REMOTEOS_MCP_AUTH_KEY"
else
    # The first key the mac has decides; one this installer cannot carry is refused, never
    # replaced by a new key (that would silently cut the clients off).
    PLIST_KEY=$(key_from_plist "$PLIST_PATH")
    if [[ -n "$PLIST_KEY" ]]; then
        AUTH_KEY="$PLIST_KEY"
        KEY_SOURCE="the running LaunchAgent"
    elif [[ -n "$CFG_KEY" ]]; then
        AUTH_KEY="$CFG_KEY"
        KEY_SOURCE="$CONFIG_FILE"
    else
        # Not `tr … < /dev/urandom | head -c 32`: tr dies of SIGPIPE when head
        # exits, and pipefail turns that into an abort on a fresh mac (#12).
        AUTH_KEY=$("$PYTHON" -c "import secrets, string; print(''.join(secrets.choice(string.ascii_letters+string.digits) for _ in range(32)), end='')")
    fi
    if [[ -n "$KEY_SOURCE" ]] && ! key_ok "$AUTH_KEY"; then
        echo "  [X] The key in ${KEY_SOURCE} is not 1-128 letters/digits, so it cannot be carried over; pass the key the clients use as REMOTEOS_MCP_AUTH_KEY — nothing was changed on this mac"
        exit 1
    fi
fi
if ! key_ok "$AUTH_KEY"; then
    echo "  [X] Could not make an auth key"
    exit 1
fi

# --- [2/5] Install remoteos-mcp ---
echo "  [2/5] Installing remoteos-mcp..."
# No --ignore-installed here: macOS has no apt-owned python packages (that is
# the Linux/Debian case, #12 strih-pp).
PIP_FLAGS=(--no-cache-dir --break-system-packages)
SERVICE_DESC="the ${PLIST_LABEL} LaunchAgent"

# >>> shared install block — the system-python install with preflight + rollback.
# install-linux.sh carried the same block until 0.7.0.dev32, when Linux moved to
# its own venv (#12); tests/test_installer_preflight_rollback.py runs it here.
# Needs: PYTHON, PIP_FLAGS, SERVICE_DESC.  Leaves: PKG_VER (the installed,
# importable version).
#
# Order: pin one commit of main → record the installed version → dry-run
# preflight → plan + snapshot → ARM → pre-uninstall → install → verify (version
# + import) → DISARM.  Before ARM nothing on the box has changed.  Whatever ends
# the run while ARMED — a failed step, a signal (ssh drop, timeout, Ctrl-C), a
# lost console (SIGPIPE is ignored, so the write fails and set -e exits) — goes
# through the EXIT trap, which rolls the snapshot back and exits non-zero
# without touching the service.  Armed pip steps are shielded from HUP/INT/TERM
# so pip never dies half-way through a wheel.  pip always writes to a log; the
# full log is printed on failure.

installed_version() {
    "$PYTHON" -m pip show remoteos-mcp 2>/dev/null | sed -n 's/^Version: //p' || true
}

# Import what was installed the way `python -m remoteos` (the service) does,
# minus starting it.  Runs as the installer's user.
import_check() {
    "$PYTHON" -c 'import remoteos.__main__, fastmcp; from fastmcp import FastMCP' 2>&1
}

# Run an armed pip step immune to HUP/INT/TERM (Ctrl-C, a tty hang-up and a
# killpg timeout hit the whole process group): pip writes a wheel's RECORD
# last, so a pip killed mid-wheel leaves files no rollback can find.  The
# installer's own trap still sees the signal and rolls back once pip is done.
shield() {
    ( trap '' HUP INT TERM; exec "$@" )
}

# Pre-uninstall the three packages so no stale RECORD can delete files the new
# install writes.  Root cause (#12 wheel evidence): monolithic fastmcp 2.x
# RECORD claims fastmcp/__init__.py — same file fastmcp-slim 4.x writes.  Plus
# every pip-owned dep whose pinned version changes: --ignore-installed would
# otherwise leave its old dist-info + RECORD behind next to the new files.  The
# plan guarantees each of these has a RECORD, so pip cannot refuse one of them.
uninstall_overlapping() {
    shield "$PYTHON" -m pip uninstall -y --break-system-packages remoteos-mcp fastmcp fastmcp-slim ${CHANGED_DEPS[@]+"${CHANGED_DEPS[@]}"} >>"$WORK_DIR/uninstall.log" 2>&1
}

# The pinned install; extra pip args (--dry-run ...) go first.  The preflight
# and the real install both run through here, so their flags are identical.
pip_install_remoteos() {
    local pip_cmd=("$PYTHON" -m pip install "${PIP_FLAGS[@]}" "$@" "git+https://github.com/zbynekdrlik/remoteos-mcp.git@${SOURCE_SHA}")
    if [[ -n "$ROLLBACK_ARMED" ]]; then
        TMPDIR="$WORK_DIR/pip-tmp" shield "${pip_cmd[@]}"
    else
        TMPDIR="$WORK_DIR/pip-tmp" "${pip_cmd[@]}"
    fi
}

rollback_failed() {
    KEEP_WORK_DIR=1
    echo "        [X] Rollback FAILED — $1; do NOT restart ${SERVICE_DESC}"
    echo "        Snapshot kept for manual recovery: $PYTHON $ROLLBACK_PY restore $WORK_DIR/snapshot"
    echo "        Rollback log: $WORK_DIR/rollback.log"
}

# Undo a failed or unfinished upgrade: take out every pip-owned copy of what
# the install may have written (the three, the changed deps, the deps it added)
# — one name per pip call, so one refusal cannot skip the rest — restore the
# snapshot, check nothing new is left behind, and check the recorded version
# works again.  Never exits.
rollback_install() {
    local name leftovers
    trap '' HUP INT TERM
    echo "        Rolling back (restoring the previous package files)..."
    if ! "$PYTHON" "$ROLLBACK_PY" pipowned remoteos-mcp fastmcp fastmcp-slim ${CHANGED_DEPS[@]+"${CHANGED_DEPS[@]}"} \
        ${ADDED_DEPS[@]+"${ADDED_DEPS[@]}"} >"$WORK_DIR/remove.txt" 2>>"$WORK_DIR/rollback.log"; then
        rollback_failed "could not tell which packages the failed install wrote"
        return
    fi
    while IFS= read -r name; do
        if [[ -n "$name" ]]; then
            "$PYTHON" -m pip uninstall -y --break-system-packages "$name" >>"$WORK_DIR/rollback.log" 2>&1 || true
        fi
    done <"$WORK_DIR/remove.txt"
    if ! "$PYTHON" "$ROLLBACK_PY" restore "$WORK_DIR/snapshot" >>"$WORK_DIR/rollback.log" 2>&1; then
        rollback_failed "the previous files could not be restored"
        return
    fi
    if ! leftovers=$("$PYTHON" "$ROLLBACK_PY" leftovers "$WORK_DIR" 2>>"$WORK_DIR/rollback.log"); then
        rollback_failed "could not check what the failed install left behind"
        return
    fi
    if [[ -n "$leftovers" ]]; then
        rollback_failed "the failed install left packages pip cannot remove: $leftovers"
        return
    fi
    if [[ -z "$OLD_VER" ]]; then
        echo "        [X] No previous version to roll back to — remoteos is NOT installed on this box"
    elif [[ "$(installed_version)" == "$OLD_VER" ]] && import_check >/dev/null; then
        echo "        Rolled back: v${OLD_VER} is installed again and imports; ${SERVICE_DESC} was not restarted"
    else
        rollback_failed "v${OLD_VER} is not back in a working state"
    fi
}

on_signal() {
    trap '' HUP INT TERM
    INTERRUPTED=1
    exit 1
}

on_exit() {
    local rc=$?
    trap '' PIPE HUP INT TERM
    if [[ -n "$ROLLBACK_ARMED" ]]; then
        ROLLBACK_ARMED=""
        set +e
        exec 1>&3 2>&4
        if [[ -n "$INTERRUPTED" ]]; then
            echo "        [X] Interrupted during the upgrade"
        fi
        rollback_install
        rc=1
    fi
    if [[ -z "$KEEP_WORK_DIR" ]]; then
        rm -rf "$WORK_DIR"
    fi
    [[ -z "$WRITE_TMP" ]] || rm -f "$WRITE_TMP"
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
WRITE_TMP=""
OLD_VER=""
CHANGED_DEPS=()
ADDED_DEPS=()
exec 3>&1 4>&2
trap on_exit EXIT
mkdir -p "$WORK_DIR/pip-tmp"

# Rollback helper (python: it ships with what we install).  It reads installed
# dists through importlib.metadata — RECORD + INSTALLER, exactly what pip itself
# uses — so the snapshot holds exactly what `pip uninstall` removes; pip's own
# uninstall rollback works the same way.  Exit 3 = no safe upgrade possible.
#   plan      <report.json> <installed version> <dir>  → dir/changed.txt: pip-owned
#             deps whose pinned version changes; dir/added.txt: deps not pip-owned
#             yet; dir/dists-before.txt: every dist-info of the affected names
#   snapshot  <dir> <names...>  copy those dists' RECORD files (bytes + modes)
#   pipowned  <names...>        the names whose visible dist pip owns
#   restore   <dir>             put the snapshot back
#   leftovers <dir>             dist-infos of the affected names that were not
#                               there before (one line, space-separated)
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


def installer(dist):
    return (dist.read_text("INSTALLER") or "").strip()


def pip_owned(dist):
    return dist is not None and dist.files is not None and installer(dist) == "pip"


def record_files(dist):
    paths = (os.path.normpath(str(dist.locate_file(f))) for f in dist.files or [])
    return [p for p in paths if os.path.isfile(p) or os.path.islink(p)]


def resolved(report_file):
    with open(report_file, encoding="utf-8") as f:
        return {norm(i["metadata"]["name"]): i["metadata"]["version"] for i in json.load(f).get("install", [])}


def dist_dirs(names):
    """Every .dist-info / .egg-info dir of `names` on sys.path, complete or not."""
    wanted, found = {norm(n) for n in names}, set()
    for entry in sys.path:
        base = os.path.abspath(entry or os.curdir)
        try:
            children = os.listdir(base)
        except OSError:
            continue
        for child in children:
            for suffix in (".dist-info", ".egg-info"):
                if child.endswith(suffix) and norm(child[: -len(suffix)].split("-", 1)[0]) in wanted:
                    found.add(os.path.join(base, child))
    return sorted(found)


def write_lines(path, lines):
    with open(path, "w", encoding="utf-8", errors="surrogateescape") as f:
        f.write("".join(line + "\n" for line in lines))


def read_lines(path):
    with open(path, encoding="utf-8", errors="surrogateescape") as f:
        return [line for line in f.read().split("\n") if line]


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
        if pip_owned(dist):
            if dist.version != version:
                changed.append(name)
            continue
        if dist is not None and dist.files is not None and dist.version != version:
            fail(name + " " + dist.version + " was installed by " + (installer(dist) or "an unknown tool")
                 + ", not pip; the pinned " + version + " would overwrite it. Upgrade or remove it with that"
                 + " tool, then rerun.")
        added.append(name)
    os.makedirs(out, exist_ok=True)
    write_lines(os.path.join(out, "changed.txt"), changed)
    write_lines(os.path.join(out, "added.txt"), added)
    write_lines(os.path.join(out, "affected.txt"), sorted(set(OVERLAPPING) | set(want)))
    write_lines(os.path.join(out, "dists-before.txt"), dist_dirs(set(OVERLAPPING) | set(want)))
elif mode == "snapshot":
    snap, paths = args[0], []
    for name in args[1:]:
        dist = visible(name)
        if dist is None or dist.files is None:
            continue  # nothing installed, or an apt copy: never removed, nothing to restore
        files = record_files(dist)
        if not any(os.path.basename(p) in ("METADATA", "PKG-INFO") for p in files):
            fail("the RECORD of " + name + " " + dist.version + " does not list its own metadata")
        paths += files
    os.makedirs(snap)
    for path in paths:
        dst = snap + "/files" + path
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(path, dst, follow_symlinks=False)
    write_lines(snap + "/manifest", paths)
    print(len(paths))
elif mode == "pipowned":
    for name in args:
        if pip_owned(visible(name)):
            print(name)
elif mode == "restore":
    snap = args[0]
    paths = read_lines(snap + "/manifest")
    for path in paths:
        if os.path.isdir(path) and not os.path.islink(path):
            fail("a directory is in the way of " + path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.islink(path) or os.path.isfile(path):
            os.remove(path)
        shutil.copy2(snap + "/files" + path, path, follow_symlinks=False)
    print(len(paths))
elif mode == "leftovers":
    work = args[0]
    before = set(read_lines(os.path.join(work, "dists-before.txt")))
    print(" ".join(d for d in dist_dirs(read_lines(os.path.join(work, "affected.txt"))) if d not in before))
else:
    sys.exit("unknown mode " + mode)
PY

# Pin the whole run to ONE commit of $REF: preflight, install and constraints
# all see the same tree (a merge between two fetches of @main would mix trees,
# and raw.githubusercontent.com caches .../main/ for minutes). A full sha is
# taken as it is; else an annotated tag's commit (`^{}`), a tag, then a branch,
# in git's own order.
if [[ "$REF" =~ ^[0-9a-f]{40}$ ]]; then
    SOURCE_SHA="$REF"
else
    SOURCE_SHA=$(git ls-remote https://github.com/zbynekdrlik/remoteos-mcp.git "refs/tags/${REF}^{}" \
        "refs/tags/${REF}" "refs/heads/${REF}" 2>"$WORK_DIR/ls-remote.log" | awk -v r="$REF" '
            ($2 == ("refs/tags/" r "^{}")) { peeled = $1 }
            ($2 == ("refs/tags/" r)) { tag = $1 }
            ($2 == ("refs/heads/" r)) { head = $1 }
            END { if (peeled != "") print peeled; else if (tag != "") print tag; else print head }' || true)
fi
if [[ ! "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]]; then
    echo "        [X] Could not resolve '$REF' on GitHub — nothing was changed on this box:"
    cat "$WORK_DIR/ls-remote.log"
    exit 1
fi
export PIP_CONSTRAINT="https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/${SOURCE_SHA}/constraints.txt"
echo "        Source: $REF @ ${SOURCE_SHA:0:12}"

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
    echo "        [X] No safe upgrade possible — nothing was changed on this box:"
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
if ! SNAP_COUNT=$("$PYTHON" "$ROLLBACK_PY" snapshot "$WORK_DIR/snapshot" remoteos-mcp fastmcp fastmcp-slim \
    ${CHANGED_DEPS[@]+"${CHANGED_DEPS[@]}"} ${ADDED_DEPS[@]+"${ADDED_DEPS[@]}"} 2>"$WORK_DIR/snapshot.err"); then
    echo "        [X] Could not keep a rollback copy of the installed packages — nothing was changed on this box:"
    sed 's/^/            /' "$WORK_DIR/snapshot.err"
    exit 1
fi
echo "        Kept ${SNAP_COUNT} files for rollback"

# ARM: from here until the install is verified, any exit rolls back.
ROLLBACK_ARMED=1
trap on_signal HUP INT TERM
trap '' PIPE
UNINSTALL_RC=0
uninstall_overlapping || UNINSTALL_RC=$?
if [[ "$UNINSTALL_RC" -ne 0 ]]; then
    echo "        [X] The pre-uninstall failed. Full pip output:"
    cat "$WORK_DIR/uninstall.log"
    exit 1
fi
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

# --- [3/5] Write the auth key ---
echo "  [3/5] Configuring auth key..."
if [[ -n "$KEY_SOURCE" ]]; then
    echo "        Auth key: kept, from ${KEY_SOURCE}"
else
    echo "        Auth key: a NEW auth key was generated — the client's .mcp.json entry for this mac needs it"
fi
if key_ok "$CFG_KEY" && [[ "$CFG_KEY" != "$AUTH_KEY" ]]; then
    echo "        [!] config.json held another key; it now holds the installed one"
fi

# write_private PATH (content on stdin): through a temp file that mktemp makes 0600, then
# renamed, so the key is never in a file another account can read.
write_private() {
    local tmp
    WRITE_TMP=$(mktemp "$1.XXXXXX")  # on_exit removes it: it may hold the key
    tmp=$WRITE_TMP
    cat >"$tmp"
    chmod 600 "$tmp"
    mv -f "$tmp" "$1"
    WRITE_TMP=""
}

mkdir -p "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR"
write_private "$CONFIG_FILE" <<<"$(printf '{\n  "port": %s,\n  "auth_key": "%s",\n  "host": "0.0.0.0"\n}' "$PORT" "$AUTH_KEY")"
echo "        Config: $CONFIG_FILE (0600)"

# --- [4/5] Set up LaunchAgent auto-start ---
echo "  [4/5] Setting up auto-start..."

PYTHON_PATH=$(command -v "$PYTHON")

mkdir -p "$HOME/Library/LaunchAgents"

# The key is in EnvironmentVariables (remoteos reads REMOTEOS_AUTH_KEY), never in
# ProgramArguments: those are the server's argv, which `ps` shows to every account.
write_private "$PLIST_PATH" <<PLIST
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
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>REMOTEOS_AUTH_KEY</key>
        <string>${AUTH_KEY}</string>
    </dict>
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
echo "        LaunchAgent: $PLIST_PATH (0600)"

# Load the agent
launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load "$PLIST_PATH"
echo "        LaunchAgent loaded: $PLIST_LABEL"

# --- Post-install: verify service reports the correct version ---
echo "  [+]   Verifying installed version matches running service..."
HEALTH_OK=false
for _attempt in $(seq 1 6); do
    HEALTH_JSON=$(curl -q --noproxy '*' -sf "http://127.0.0.1:${PORT}/health" 2>/dev/null || true)
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
echo "  Auth key:  in ${CONFIG_FILE} and the LaunchAgent plist (0600) — never printed."
echo "             Read it on this mac: cat ${CONFIG_FILE}"
if [[ -z "$KEY_SOURCE" ]]; then
    echo "             It is a NEW key: update the client's .mcp.json entry for this mac."
fi
echo ""
echo "  On your Linux machine, run:"
echo ""
echo "  claude mcp add --transport http mac-${HOSTNAME_SHORT} \\"
echo "    http://${LOCAL_IP}:${PORT}/mcp \\"
echo "    --header \"Authorization: Bearer <the auth key>\""
echo ""
echo "  Then restart Claude Code."
echo ""
echo "  To uninstall later:"
echo "  curl -fsSL https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/main/uninstall.sh | bash"
echo ""
}

main "$@"
