#!/usr/bin/env bash
# RemoteOS MCP - One-line installer for Linux (systemd)
# Usage: curl -fsSL https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/main/install-linux.sh | sudo bash
#
# Optional settings, read from the environment (`... | sudo REMOTEOS_MCP_REF=<sha> bash`):
#   REMOTEOS_MCP_REF       the source to install: a commit sha, a tag or a branch (default: main)
#   REMOTEOS_MCP_AUTH_KEY  the auth key to install (1-128 letters/digits). Default: keep the key
#                          the box has (the running unit's, else config.json's), else a new one.
#   GH_TOKEN               sent as the download's Authorization header on curl's stdin (a fork)
#   REMOTEOS_MCP_MODE      desktop | headless (default: the existing unit's, else the session check)
#   REMOTEOS_MCP_USER      the account the service runs as (default: the existing unit's User=,
#                          else SUDO_USER, else root)
#   REMOTEOS_MCP_POLICY    restart (default: restart and prove the new install) | enable-only
#                          (install + enable for the next boot, never start: camera-box's cams)
# A secret never goes on a command line, sudo's included: `sudo REMOTEOS_MCP_AUTH_KEY=… bash`
# shows it in `ps`. Hand it to sudo's environment only (no export: curl never gets it):
#   read -rs k
#   curl -fsSL …/install-linux.sh | REMOTEOS_MCP_AUTH_KEY="$k" sudo --preserve-env=REMOTEOS_MCP_AUTH_KEY bash
#
# camera-box's layout (/opt/remoteos-mcp-venv → a build in /opt/remoteos-mcp-venvs/); the key
# only in 0600 files under /etc/remoteos-mcp, never in the unit, an argv or this output. The
# layout, the order and the rollback rules: README "Install" and .claude/rules/installers.md.
set -euo pipefail

say() { echo "        $*"; }

# fail MESSAGE: before anything was switched (the EXIT trap rolls back a switch in progress).
fail() {
    echo "        [X] $*"
    exit 1
}

# Account names, source ids: letters, digits, dot, underscore, dash. The bracket ranges are
# ASCII in any locale: main() sets globasciiranges.
name_ok() {
    [[ -n "${1-}" ]] || return 1
    case "$1" in
        *[!A-Za-z0-9._-]*) return 1 ;;
    esac
}

# A git ref the GitHub tarball API accepts: a sha, a tag or a branch name, no `..`, no option.
ref_ok() {
    local r="${1-}"
    [[ -n "$r" && ${#r} -le 200 ]] || return 1
    case "$r" in
        -* | .* | */ | *..* | *//* | */.*) return 1 ;;
        *[!A-Za-z0-9._/-]*) return 1 ;;
    esac
}

# --- the key -----------------------------------------------------------------------------------

# key_ok KEY: 1-128 ASCII letters/digits (the key charset of all three installers). Anything
# else would break config.json or the EnvironmentFile line.
key_ok() {
    local k="${1-}"
    [[ -n "$k" && ${#k} -le 128 ]] || return 1
    case "$k" in
        *[!A-Za-z0-9]*) return 1 ;;
    esac
}

# The value after `--auth-key` on a unit's ExecStart (what our pre-#12 installer wrote).
key_from_unit_text() {
    local rest
    rest=$(first_value 'ExecStart=' "${1-}")
    [[ "$rest" == *"--auth-key "* ]] || return 0
    rest="${rest#*--auth-key }"
    printf '%s' "${rest%% *}"
}

# first_value PREFIX TEXT: the rest of TEXT's first line that starts with PREFIX, or nothing.
first_value() {
    local line
    while IFS= read -r line; do
        case "$line" in
            "$1"*)
                printf '%s' "${line#"$1"}"
                return 0
                ;;
        esac
    done <<<"${2-}"
}

# REMOTEOS_AUTH_KEY of an EnvironmentFile.
key_from_env_text() {
    first_value 'REMOTEOS_AUTH_KEY=' "${1-}"
}

# The auth_key of a config.json, read by python from stdin (never an argv).
key_from_config_text() {
    printf '%s' "${1-}" | "$PYTHON" -c '
import json, sys
try:
    v = json.load(sys.stdin).get("auth_key", "")
except Exception:
    v = ""
print(v if isinstance(v, str) else "", end="")' 2>/dev/null || true
}

generate_key() {
    "$PYTHON" -c "import secrets, string; print(''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(32)), end='')"
}

# Sets AUTH_KEY and KEY_SOURCE (where from, never the value; empty = new). camera-box's order:
# explicit, the running unit's (a legacy --auth-key, else its EnvironmentFile), config.json, new.
# The first key the box has decides; one this installer cannot carry is refused, never replaced.
resolve_key() {
    local unit_key env_key cfg_key
    cfg_key=$(key_from_config_text "$(cat "$CONFIG_FILE" 2>/dev/null || true)")
    if [[ -n "$EXPLICIT_KEY" ]]; then
        key_ok "$EXPLICIT_KEY" \
            || fail "REMOTEOS_MCP_AUTH_KEY must be 1-128 letters/digits [A-Za-z0-9] — nothing was changed on this box"
        AUTH_KEY=$EXPLICIT_KEY
        KEY_SOURCE="REMOTEOS_MCP_AUTH_KEY"
    else
        unit_key=$(key_from_unit_text "$OLD_UNIT")
        env_key=$(key_from_env_text "$(cat "$ENV_FILE" 2>/dev/null || true)")
        if [[ -n "$unit_key" ]]; then
            AUTH_KEY=$unit_key
            KEY_SOURCE="the running unit (moved out of its ExecStart)"
        elif [[ -n "$env_key" ]]; then
            AUTH_KEY=$env_key
            KEY_SOURCE="$ENV_FILE"
        elif [[ -n "$cfg_key" ]]; then
            AUTH_KEY=$cfg_key
            KEY_SOURCE="$CONFIG_FILE"
        else
            AUTH_KEY=$(generate_key)
            KEY_SOURCE=""
        fi
        if [[ -n "$KEY_SOURCE" ]] && ! key_ok "$AUTH_KEY"; then
            fail "the key in ${KEY_SOURCE} is not 1-128 letters/digits, so it cannot be carried over; pass the key the clients use as REMOTEOS_MCP_AUTH_KEY — nothing was changed on this box"
        fi
    fi
    key_ok "$AUTH_KEY" || fail "could not make an auth key"
    if [[ -n "$KEY_SOURCE" ]]; then
        say "Auth key: kept, from ${KEY_SOURCE}"
    else
        say "Auth key: a NEW auth key was generated — the client's .mcp.json entry for this box needs it"
    fi
    if key_ok "$cfg_key" && [[ "$cfg_key" != "$AUTH_KEY" ]]; then
        say "[!] config.json held another key; it now holds the installed one"
    fi
}

# --- the box: python, the unit's mode and user ------------------------------------------------

# Sets PYTHON to the first system python 3.10+.
pick_python() {
    local cmd ver
    for cmd in python3 python /usr/bin/python3 /usr/local/bin/python3; do
        if command -v "$cmd" &>/dev/null || [[ -x "$cmd" ]]; then
            ver=$("$cmd" --version 2>&1 || true)
            if [[ "$ver" =~ Python\ 3\.([0-9]+) ]] && (( BASH_REMATCH[1] >= 10 )); then
                PYTHON="$cmd"
                say "Found $ver ($cmd)"
                return 0
            fi
        fi
    done
    return 1
}

find_python() {
    pick_python && return 0
    say "Python 3.10+ not found. Trying the package manager..."
    if command -v apt-get &>/dev/null; then
        apt-get install -y python3 python3-venv || true
    elif command -v dnf &>/dev/null; then
        dnf install -y python3 || true
    elif command -v yum &>/dev/null; then
        yum install -y python3 || true
    fi
    pick_python \
        || fail "Python 3.10+ required but not found. Install it (apt-get install python3 python3-venv, or dnf/yum) and rerun."
}

# The first graphical user session: sets SESSION_TYPE (x11 / wayland, or nothing) and
# SESSION_DISPLAY (an X11 session's display, e.g. :1; GDM's Xorg is often not :0).
graphical_session() {
    local s stype sclass
    SESSION_TYPE=""
    SESSION_DISPLAY=""
    command -v loginctl &>/dev/null || return 0
    for s in $(loginctl list-sessions --no-legend 2>/dev/null | awk '{print $1}'); do
        stype=$(loginctl show-session "$s" -p Type --value 2>/dev/null || true)
        sclass=$(loginctl show-session "$s" -p Class --value 2>/dev/null || true)
        if [[ "$sclass" == "user" && ( "$stype" == "x11" || "$stype" == "wayland" ) ]]; then
            SESSION_TYPE=$stype
            if [[ "$stype" == x11 ]]; then
                SESSION_DISPLAY=$(loginctl show-session "$s" -p Display --value 2>/dev/null || true)
            fi
            return 0
        fi
    done
}

# A local X display: :N or :N.S.
display_ok() {
    case "${1-}" in
        : | *[!0-9:.]* | :*:* | :.*) return 1 ;;
        :[0-9]*) return 0 ;;
    esac
    return 1
}

# The User= of a unit, trimmed.
user_from_unit_text() {
    local v
    v=$(first_value 'User=' "${1-}")
    v="${v#"${v%%[![:space:]]*}"}"
    printf '%s' "${v%"${v##*[![:space:]]}"}"
}

# Sets OLD_UNIT, MODE (desktop | headless), SERVICE_USER, SERVICE_UID. REMOTEOS_MCP_MODE/USER
# win (camera-box passes them), else the existing unit's (a re-run never changes the box's shape
# or account), else the session check / SUDO_USER / root.
detect_layout() {
    local u old_display
    OLD_UNIT=$(cat "$UNIT_PATH" 2>/dev/null || true)
    OLD_DESCRIPTION=$(grep -m1 '^Description=' <<<"$OLD_UNIT" || true)
    if [[ -e "$VENV_LINK" && ! -L "$VENV_LINK" && ! -d "$VENV_LINK" ]]; then
        fail "$VENV_LINK is neither a venv directory nor a link — nothing was changed on this box"
    fi
    graphical_session
    old_display=$(first_value "Environment=DISPLAY=" "$OLD_UNIT")
    if [[ -n "$GIVEN_MODE" ]]; then
        MODE=$GIVEN_MODE
        say "Mode: ${MODE} (REMOTEOS_MCP_MODE)"
    elif [[ -n "$old_display" ]] || grep -q '^After=.*graphical-session\.target' <<<"$OLD_UNIT"; then
        MODE=desktop
        say "Mode: desktop (the existing unit's)"
    elif [[ -n "$SESSION_TYPE" ]]; then
        MODE=desktop
        say "Mode: desktop (a ${SESSION_TYPE} session is active)"
    else
        MODE=headless
        say "Mode: headless (no graphical session)"
    fi
    # The unit's DISPLAY: the existing unit's, else the X11 session's, else :0 (camera-box's).
    # A Wayland session gets none: the server's own detection then reports it honestly.
    UNIT_DISPLAY=""
    if [[ "$MODE" == desktop ]]; then
        if display_ok "$old_display"; then
            UNIT_DISPLAY=$old_display
        elif [[ "$SESSION_TYPE" == x11 ]] && display_ok "$SESSION_DISPLAY"; then
            UNIT_DISPLAY=$SESSION_DISPLAY
        elif [[ "$SESSION_TYPE" == wayland ]]; then
            UNIT_DISPLAY=""
        elif [[ -z "$old_display" ]] && grep -q '^After=.*graphical-session\.target' <<<"$OLD_UNIT"; then
            UNIT_DISPLAY=""  # our desktop unit without DISPLAY: the saved Wayland choice
        else
            UNIT_DISPLAY=":0"
        fi
    fi
    SERVICE_USER=""
    if [[ -n "$GIVEN_USER" ]]; then
        SERVICE_USER=$GIVEN_USER
    else
        for u in "$(user_from_unit_text "$OLD_UNIT")" "${SUDO_USER:-}"; do
            if name_ok "$u"; then
                SERVICE_USER=$u
                break
            fi
        done
    fi
    SERVICE_USER=${SERVICE_USER:-root}
    SERVICE_UID=$(id -u "$SERVICE_USER" 2>/dev/null) \
        || fail "REMOTEOS_MCP_USER / the service account '${SERVICE_USER}' does not exist — nothing was changed on this box"
    say "Service account: ${SERVICE_USER}"
}

# Validate the optional inputs before anything changes.
check_inputs() {
    ref_ok "$REF" \
        || fail "REMOTEOS_MCP_REF must be a commit sha, a tag or a branch name — nothing was changed on this box"
    [[ "$GIVEN_MODE" =~ ^(desktop|headless)?$ ]] \
        || fail "REMOTEOS_MCP_MODE must be desktop or headless — nothing was changed on this box"
    if [[ -n "$GIVEN_USER" ]] && ! name_ok "$GIVEN_USER"; then
        fail "REMOTEOS_MCP_USER must be an account name — nothing was changed on this box"
    fi
    [[ "$POLICY" =~ ^(restart|enable-only)$ ]] \
        || fail "REMOTEOS_MCP_POLICY must be restart or enable-only — nothing was changed on this box"
}

# --- the source and the venv -------------------------------------------------------------------

# Download the GitHub tarball of REF into $WORK_DIR/src; sets SOURCE_ID (its top dir, e.g.
# zbynekdrlik-remoteos-mcp-8b4ce58: the commit). Code and constraints.txt always match.
fetch_source() {
    local url="https://api.github.com/repos/${REPO}/tarball/${REF}" tgz="$WORK_DIR/source.tar.gz" rc=0
    if [[ -n "$GH_TOKEN_VALUE" ]]; then
        printf 'Authorization: token %s\n' "$GH_TOKEN_VALUE" \
            | curl -fsSL --max-time 120 -H @- -H 'Accept: application/vnd.github+json' -o "$tgz" "$url" \
                >"$WORK_DIR/fetch.log" 2>&1 || rc=$?
    else
        curl -fsSL --max-time 120 -H 'Accept: application/vnd.github+json' -o "$tgz" "$url" \
            >"$WORK_DIR/fetch.log" 2>&1 || rc=$?
    fi
    if [[ "$rc" -ne 0 ]]; then
        echo "        [X] Could not download ${url} (curl exit ${rc}) — nothing was changed on this box:"
        cat "$WORK_DIR/fetch.log"
        exit 1
    fi
    SOURCE_ID=$(tar -tzf "$tgz" 2>/dev/null | awk -F/ 'NR == 1 { print $1 }') || true
    name_ok "$SOURCE_ID" || fail "${url} is not a source tarball — nothing was changed on this box"
    mkdir "$WORK_DIR/src"
    tar -xzf "$tgz" -C "$WORK_DIR/src" --strip-components=1 \
        || fail "could not unpack the ${REF} source — nothing was changed on this box"
    [[ -f "$WORK_DIR/src/pyproject.toml" && -f "$WORK_DIR/src/constraints.txt" ]] \
        || fail "the ${REF} source has no pyproject.toml + constraints.txt — nothing was changed on this box"
    SRC_DIR="$WORK_DIR/src"
    say "Source: ${REF} = ${SOURCE_ID}"
}

# Run a python under a minimal environment: it imports the package's code. A check reads,
# never writes (no __pycache__ in an adopted venv).
run_python() {
    local py="$1"
    shift
    env -i PATH="$PATH" HOME="$PIP_HOME" LANG="${LANG:-C.UTF-8}" PYTHONDONTWRITEBYTECODE=1 "$py" "$@"
}

venv_python() {
    local venv="$1"
    shift
    run_python "$venv/bin/python" "$@"
}

# The remoteos-mcp version a python (a venv's, or the unit's) has installed, or nothing.
python_version() {
    run_python "$1" -c 'import importlib.metadata as m; print(m.version("remoteos-mcp"))' 2>/dev/null || true
}

venv_version() {
    python_version "$1/bin/python"
}

# Import what the service runs (`python -m remoteos`), minus starting it.
venv_import_check() {
    venv_python "$1" -c 'import remoteos.__main__, fastmcp; from fastmcp import FastMCP' 2>&1
}

# The installed venv's source id: ours (.remoteos-mcp-source) or camera-box's. Both = camera-box's
# lib upgraded our build in place (it writes through the link): ours no longer says what is there.
installed_source() {
    local ours="$VENV_LINK/.remoteos-mcp-source" theirs="$VENV_LINK/.camera-box-source"
    if [[ -f "$ours" && -f "$theirs" ]]; then
        return 0
    fi
    cat "$ours" 2>/dev/null || cat "$theirs" 2>/dev/null || true
}

# The version in the downloaded source's pyproject.toml.
source_version() {
    sed -n '/^version *= *"/{s/^version *= *"\([^"]*\)".*/\1/p;q;}' "$SRC_DIR/pyproject.toml"
}

# Is the installed venv exactly the downloaded source (marker, version, import), working?
venv_is_current() {
    [[ -n "$SOURCE_ID" && "$(installed_source)" == "$SOURCE_ID" ]] || return 1
    [[ "$(venv_version "$VENV_LINK")" == "$(source_version)" ]] || return 1
    venv_import_check "$VENV_LINK" >/dev/null
}

# A build that failed: print why, remove it, change nothing else.
build_failed() {
    echo "        [X] $1 — nothing was changed on this box (the installed version keeps running). Full output:"
    cat "$2"
    remove_new_build
    NEW_BUILD=""
    exit 1
}

# Desktop mode: the venv imports the system's `gi` (AT-SPI) and nothing else of the system's: a
# build dir holding one link to it, named by a .pth in the venv's site-packages. 1 = no gi.
link_system_gi() {
    local build="$1" gi_pkg purelib
    gi_pkg=$("$PYTHON" -c 'import os, gi; print(os.path.dirname(os.path.abspath(gi.__file__)))' 2>/dev/null) \
        || gi_pkg=""
    if [[ -z "$gi_pkg" || ! -d "$gi_pkg" ]]; then
        say "[!] The system python has no gi (python3-gi): AnnotatedSnapshot's element tree will be empty"
        return 1
    fi
    mkdir -p "$build/system-gi"
    ln -sfn "$gi_pkg" "$build/system-gi/gi"
    purelib=$(venv_python "$build" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])') || purelib=""
    if [[ -z "$purelib" || ! -d "$purelib" ]]; then
        say "[!] Could not find the venv's site-packages; the system GI bindings are not linked"
        return 1
    fi
    printf '%s\n' "$build/system-gi" >"$purelib/remoteos-system-gi.pth"
    say "System GI bindings linked: ${gi_pkg}"
}

# An unchanged desktop build of ours that cannot import gi yet (python3-gi came later): link
# it now. camera-box's venv directory is adopted as it is and never written.
ensure_system_gi() {
    local build
    [[ "$MODE" == desktop && -L "$VENV_LINK" ]] || return 0
    venv_python "$VENV_LINK" -c 'import gi' >/dev/null 2>&1 && return 0
    "$PYTHON" -c 'import gi' >/dev/null 2>&1 || return 0  # no system gi: nothing to link
    build=$(readlink -f "$VENV_LINK")
    GI_LINKED=$build MODIFIED=1  # set first: restore_previous takes the link out again
    if link_system_gi "$build"; then
        CHANGED=1
    fi
}

# Build a fresh venv for SOURCE_ID next to the running one; sets NEW_BUILD and PKG_VER. pip runs
# build code, so under an env ALLOWLIST: no GH_TOKEN, no key, nothing the caller exported.
build_venv() {
    local log="$WORK_DIR/pip.log" pv out
    local -a pip_env
    if [[ ! -d "$VENV_STORE" ]]; then
        mkdir -p "$VENV_STORE"
        STORE_CREATED=1
    fi
    chmod 755 "$VENV_STORE"
    NEW_BUILD=$(mktemp -d "$VENV_STORE/${SOURCE_ID}.XXXXXX")
    chmod 755 "$NEW_BUILD"
    say "Building a fresh venv: ${NEW_BUILD}"
    if ! "$PYTHON" -m venv "$NEW_BUILD" >"$WORK_DIR/venv.log" 2>&1; then
        command -v apt-get &>/dev/null || build_failed "python3 -m venv failed" "$WORK_DIR/venv.log"
        say "python3 -m venv failed; installing python3-venv..."
        apt-get update -qq >>"$WORK_DIR/venv.log" 2>&1 || true
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv >>"$WORK_DIR/venv.log" 2>&1 || true
        rm -rf "$NEW_BUILD"
        mkdir -m 755 "$NEW_BUILD"
        "$PYTHON" -m venv "$NEW_BUILD" >>"$WORK_DIR/venv.log" 2>&1 \
            || build_failed "python3 -m venv failed" "$WORK_DIR/venv.log"
    fi
    mkdir -p "$WORK_DIR/pip-tmp"
    pip_env=(PATH="$PATH" HOME="$PIP_HOME" LANG="${LANG:-C.UTF-8}" TMPDIR="$WORK_DIR/pip-tmp")
    for pv in http_proxy https_proxy no_proxy all_proxy HTTP_PROXY HTTPS_PROXY NO_PROXY ALL_PROXY \
        SSL_CERT_FILE SSL_CERT_DIR REQUESTS_CA_BUNDLE CURL_CA_BUNDLE \
        PIP_INDEX_URL PIP_EXTRA_INDEX_URL PIP_TRUSTED_HOST PIP_CERT PIP_CONFIG_FILE; do
        [[ -z "${!pv:-}" ]] || pip_env+=("${pv}=${!pv}")
    done
    say "pip install (constraints.txt of the same source)..."
    if ! env -i "${pip_env[@]}" PIP_CONSTRAINT="$SRC_DIR/constraints.txt" PIP_DISABLE_PIP_VERSION_CHECK=1 \
        "$NEW_BUILD/bin/python" -m pip install --no-cache-dir "$SRC_DIR" >"$log" 2>&1; then
        build_failed "pip install failed" "$log"
    fi
    PKG_VER=$(venv_version "$NEW_BUILD")
    [[ -n "$PKG_VER" ]] || build_failed "pip reported success but no remoteos-mcp is installed" "$log"
    if ! out=$(venv_import_check "$NEW_BUILD"); then
        printf '%s\n' "$out" >>"$log"
        build_failed "v${PKG_VER} is installed but does not import" "$log"
    fi
    printf '%s\n' "$SOURCE_ID" >"$NEW_BUILD/.remoteos-mcp-source"
    [[ "$MODE" != desktop ]] || link_system_gi "$NEW_BUILD" || true
    say "Built v${PKG_VER}"
}

# A desktop box gets the desktop tools' backends (headless boxes, the cams, stay stub-only):
# ffmpeg = ScreenRecord (#8), pyatspi/gir/gi = the AT-SPI tree (#7), xrandr = frames (#28).
install_desktop_tools() {
    if command -v apt-get &>/dev/null; then
        apt-get update -qq || true
        apt-get install -y -qq xdotool scrot xclip wmctrl tesseract-ocr ffmpeg x11-xserver-utils \
            python3-pyatspi gir1.2-atspi-2.0 python3-gi \
            || say "[!] Some desktop tools failed to install; desktop control may be limited"
        say "Desktop tools ready (xdotool scrot xclip wmctrl xrandr tesseract-ocr ffmpeg AT-SPI)"
    else
        say "[!] Non-apt system — install 'xdotool scrot xclip wmctrl xrandr tesseract ffmpeg python3-pyatspi gir1.2-atspi-2.0' manually"
    fi
}

# --- the switch: venv link, key files, unit, service ------------------------------------------

# camera-box's unit shapes (remoteos_mcp_unit_text), line for line. The existing Description= is
# kept: a cosmetic difference alone must not restart a live box (nor flip-flop with camera-box).
unit_text() {
    local after="network.target"
    [[ "$MODE" == desktop ]] && after="network.target graphical-session.target"
    printf '%s\n' '[Unit]' "${OLD_DESCRIPTION:-Description=RemoteOS MCP Server (install-linux.sh, venv)}" \
        "After=${after}" 'Wants=network.target' '[Service]' 'Type=simple' "User=${SERVICE_USER}"
    if [[ "$MODE" == desktop ]]; then
        [[ -n "$UNIT_DISPLAY" ]] && printf 'Environment=DISPLAY=%s\n' "$UNIT_DISPLAY"
        printf 'Environment=XDG_RUNTIME_DIR=/run/user/%s\n' "$SERVICE_UID"
    fi
    printf '%s\n' "EnvironmentFile=${ENV_FILE}" \
        "ExecStart=${VENV_LINK}/bin/python -m remoteos --transport streamable-http --enable-all --host 0.0.0.0 --port ${PORT}" \
        'Restart=always' 'RestartSec=5' 'StandardOutput=journal' 'StandardError=journal' '[Install]' \
        'WantedBy=multi-user.target'
}

config_json_text() {
    printf '{\n  "port": %s,\n  "auth_key": "%s",\n  "host": "0.0.0.0"\n}\n' "$PORT" "$1"
}

env_file_text() {
    printf 'REMOTEOS_AUTH_KEY=%s\n' "$1"
}

# write_if_changed PATH MODE LABEL (content on stdin: a here-string, so CHANGED is set in this
# shell). Content, mode AND owner must match, or it is rewritten through a 0600 temp file.
write_if_changed() {
    local path="$1" mode="$2" label="$3" tmp
    WRITE_TMP=$(mktemp "${path}.XXXXXX")  # on_exit removes it (it may hold the key)
    tmp=$WRITE_TMP
    cat >"$tmp"
    if [[ -f "$path" ]] && cmp -s "$tmp" "$path" \
        && [[ "$(stat -c '%a %U:%G' "$path" 2>/dev/null)" == "${mode} $(id -un):$(id -gn)" ]]; then
        rm -f "$tmp"
        WRITE_TMP=""
        say "${label}: unchanged"
        return 0
    fi
    chmod "$mode" "$tmp"
    CHANGED=1 MODIFIED=1
    mv -f "$tmp" "$path"
    WRITE_TMP=""
    say "${label}: written (${path})"
}

# Keep the unit, the EnvironmentFile and config.json as they are now (by their distinct base
# names), for the rollback.
save_previous() {
    local path
    mkdir -m 700 "$WORK_DIR/prev"
    for path in "$UNIT_PATH" "$ENV_FILE" "$CONFIG_FILE"; do
        [[ ! -e "$path" ]] || cp -p "$path" "$WORK_DIR/prev/${path##*/}"
    done
}

# What the unit's InvocationID is now: systemd makes a new one at every start and keeps it while
# the unit is inactive. The pending mark holds it: another one = the unit started since.
invocation_id() {
    systemctl show -p InvocationID --value "$SERVICE_NAME" 2>/dev/null || true
}

# Point VENV_LINK at NEW_BUILD with one rename(2); a real venv directory there (camera-box's) is
# moved into the store first. The flags are set FIRST, so a signal at any step can be undone.
swap_venv() {
    TMP_LINK="$VENV_STORE/.link.$$"
    SWAPPED=1 MODIFIED=1 CHANGED=1
    if [[ -L "$VENV_LINK" ]]; then
        PREV_LINK_TARGET=$(readlink "$VENV_LINK")
    elif [[ -d "$VENV_LINK" ]]; then
        ADOPTED_DIR=$(mktemp -u "$VENV_STORE/adopted.XXXXXX")
        mv -T "$VENV_LINK" "$ADOPTED_DIR"
    fi
    local tmp_link="$TMP_LINK"
    ln -sfn "$NEW_BUILD" "$tmp_link"
    mv -Tf "$tmp_link" "$VENV_LINK"
    say "Switched ${VENV_LINK} -> ${NEW_BUILD}"
}

# Undo swap_venv from whatever step it reached.
restore_venv() {
    if [[ -n "$ADOPTED_DIR" && -d "$ADOPTED_DIR" ]]; then
        [[ ! -L "$VENV_LINK" ]] || rm -f "$VENV_LINK"  # moved aside: the link is ours, or none yet
        mv -T "$ADOPTED_DIR" "$VENV_LINK"
    elif [[ -n "$PREV_LINK_TARGET" ]]; then
        ln -sfn "$PREV_LINK_TARGET" "$TMP_LINK"
        mv -Tf "$TMP_LINK" "$VENV_LINK"
    elif [[ -L "$VENV_LINK" && "$(readlink "$VENV_LINK")" == "$NEW_BUILD" ]]; then
        rm -f "$VENV_LINK"  # there was no venv before
    fi
    rm -f "$TMP_LINK"
    remove_new_build
}

# Put the previous unit, key files and venv back (restart policy: and restart them). Never exits;
# never stops on a failed write (SIGPIPE ignored and set +e: the console may be gone).
restore_previous() {
    local path
    for path in "$UNIT_PATH" "$ENV_FILE" "$CONFIG_FILE"; do
        if [[ -e "$WORK_DIR/prev/${path##*/}" ]]; then
            cp -p "$WORK_DIR/prev/${path##*/}" "${path}.rollback"
            mv -f "${path}.rollback" "$path"
        else
            rm -f "$path"
        fi
    done
    [[ -z "$SWAPPED" ]] || restore_venv
    if [[ -n "$GI_LINKED" ]]; then
        rm -rf "$GI_LINKED/system-gi"
        find "$GI_LINKED" -name remoteos-system-gi.pth -delete
    fi
    systemctl daemon-reload || true
    [[ -n "$WAS_PENDING" ]] || rm -f "$PENDING_FILE"  # the marker goes back to its pre-run state
    if [[ "$POLICY" == enable-only ]]; then
        echo "        Rolled back to the previous unit, key files and venv (enable-only: the running service was not touched)"
    else
        systemctl restart "$SERVICE_NAME" || true
    fi
}

# Wait until what runs proves itself again (check_running_install: read-only, no key sent).
await_proven() {
    local i
    for ((i = 0; i < HEALTH_TRIES; i++)); do
        check_running_install
        [[ -z "$PREV_PROVEN" ]] || return 0
        sleep "$HEALTH_SLEEP"
    done
    return 1
}

# stop_unproven WHY: nothing proven runs, so nothing runs (it may be an agent with auth off).
stop_unproven() {
    systemctl stop "$SERVICE_NAME" >/dev/null 2>&1
    systemctl disable "$SERVICE_NAME" >/dev/null 2>&1
    [[ ! -e "$PENDING_FILE" ]] || invocation_id >"$PENDING_FILE"  # still pending: not started since
    echo "        [X] $1: ${SERVICE_NAME} is stopped and disabled; fix the cause and rerun. Logs: journalctl -u ${SERVICE_NAME} -n 50"
}

# The version the running service reports on /health; fails when nothing answers.
health_version() {
    local body
    body=$(curl -fsS --max-time 3 "http://127.0.0.1:${PORT}/health" 2>/dev/null) || return 1
    printf '%s' "$body" | "$PYTHON" -c 'import json, sys; print(json.load(sys.stdin).get("version", ""), end="")' \
        2>/dev/null || true
}

# What an unauthenticated POST /mcp answers (401 = the key is enforced). Sends no key.
unauth_code() {
    curl -s -o /dev/null -w '%{http_code}' --max-time 5 -X POST "http://127.0.0.1:${PORT}/mcp" 2>/dev/null || true
}

# PREV_PROVEN = what runs is proven: a unit, no pending switch, /health = the unit's python's
# version, an unauthenticated request refused. Read-only, no key sent. Only that is restored.
check_running_install() {
    local py running
    PREV_PROVEN=""
    [[ -n "$HAD_UNIT" && -z "$WAS_PENDING" ]] || return 0
    py=$(first_value 'ExecStart=' "$OLD_UNIT" | awk '{ print $1; exit }')
    [[ "${py##*/}" == python* ]] || return 0
    systemctl is-active --quiet "$SERVICE_NAME" && running=$(health_version) || return 0
    [[ -n "$running" && "$running" == "$(python_version "$py")" && "$(unauth_code)" == 401 ]] || return 0
    PREV_PROVEN=1
}

# Is the unit's own process the only listener on :PORT? The key is never sent to another one:
# a local account could hold the port while the service restarts, and fake the proof.
port_is_ours() {
    local main pids
    main=$(systemctl show -p MainPID --value "$SERVICE_NAME" 2>/dev/null) || return 1
    pids=$(ss -Hltnp "sport = :${PORT}" 2>/dev/null | grep -o 'pid=[0-9]*' | sort -u) || return 1
    [[ "$main" =~ ^[1-9][0-9]*$ && "$pids" == "pid=${main}" ]]
}

# Is the new install what answers, and does it enforce the key? Sets HEALTH_FAILURE. It writes
# nothing to the console: the proof's result alone decides what happens next.
prove_health() {
    local i=0 ver code
    local init='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"install-linux.sh","version":"1"}}}'
    while :; do
        if systemctl is-active --quiet "$SERVICE_NAME" && ver=$(health_version); then
            if [[ "$ver" != "$PKG_VER" ]]; then
                HEALTH_FAILURE="Installed v${PKG_VER} but the service reports v${ver:-?}"
                return 1
            fi
            break
        fi
        i=$((i + 1))
        if [[ "$i" -ge "$HEALTH_TRIES" ]]; then
            HEALTH_FAILURE="the service is not active + answering /health on :${PORT} (journalctl -u ${SERVICE_NAME} -n 50)"
            return 1
        fi
        sleep "$HEALTH_SLEEP"
    done
    if ! port_is_ours; then
        HEALTH_FAILURE="another process holds :${PORT}, not the unit's (ss -ltnp 'sport = :${PORT}'): the key was not sent"
        return 1
    fi
    code=$(printf 'Authorization: Bearer %s\n' "$AUTH_KEY" \
        | curl -s -o /dev/null -w '%{http_code}' --max-time 10 -H @- -H 'Content-Type: application/json' \
            -H 'Accept: application/json, text/event-stream' -X POST --data "$init" \
            "http://127.0.0.1:${PORT}/mcp" 2>/dev/null) || true
    if [[ "$code" != 200 ]]; then
        HEALTH_FAILURE="an authenticated initialize answered ${code:-nothing} (want 200)"
        return 1
    fi
    code=$(unauth_code)
    if [[ "$code" != 401 ]]; then
        HEALTH_FAILURE="authentication is not enforced: an unauthenticated /mcp request answered ${code:-nothing} (want 401)"
        return 1
    fi
}

# Undo a switch (set +e, SIGPIPE ignored). enable-only never starts or stops anything: the
# previous files go back (a fresh box: only disabled). restart: a PROVEN install gets its files
# back (when switched) and must prove itself again to stay up; anything else is stopped.
undo_switch() {
    if [[ "$POLICY" == enable-only && -n "$HAD_UNIT" ]]; then
        echo "        Rolling back to the previous files..."
        restore_previous
    elif [[ "$POLICY" == enable-only ]]; then
        systemctl disable "$SERVICE_NAME" >/dev/null 2>&1
        echo "        Nothing to go back to: ${SERVICE_NAME} is disabled (enable-only: nothing was started)"
    elif [[ -z "$PREV_PROVEN" ]]; then
        stop_unproven "Nothing proven to go back to"
    else
        if [[ -n "$MODIFIED" ]]; then
            echo "        Rolling back to the previous install..."
            restore_previous
        fi
        if ! await_proven; then
            stop_unproven "The previous install does not prove itself again (not running, another version or no authentication)"
        elif [[ -n "$MODIFIED" ]]; then
            echo "        Rolled back: the previous install runs again and proved itself; the old system copy was kept"
        else
            rm -f "$PENDING_FILE"
            echo "        Nothing was switched: the proven install that was running is left as it ran"
        fi
    fi
    [[ -z "$NEW_BUILD" || -n "$SWAPPED" ]] || remove_new_build
}

# This run's build (nothing points at it: no swap yet, or undone) and a store this run created.
remove_new_build() {
    rm -rf "$NEW_BUILD"
    [[ -z "$STORE_CREATED" ]] || rmdir "$VENV_STORE" 2>/dev/null || true
}

# The new install did not prove itself. Immune to signals and a lost console until the undo is
# done (disarming first let the next echo end the run before anything was restored).
health_failed() {
    trap '' PIPE HUP INT TERM
    ARMED=""
    set +e
    echo "        [X] The new install did not prove itself: ${HEALTH_FAILURE}"
    undo_switch
    exit 1
}

arm() {
    ARMED=1
    trap on_signal HUP INT TERM
    trap '' PIPE
}

disarm() {
    ARMED=""
    trap on_signal HUP INT TERM
    trap - PIPE
}

on_signal() {
    trap '' HUP INT TERM
    INTERRUPTED=1
    exit 1
}

# Whatever ends the run while ARMED (a failed step, a signal, a lost console: SIGPIPE is
# ignored, so a write fails and set -e exits) rolls the switch back. fd 3/4 = the console.
on_exit() {
    local rc=$?
    trap '' PIPE HUP INT TERM
    if [[ -n "$ARMED" ]]; then
        ARMED=""
        set +e
        exec 1>&3 2>&4
        if [[ -n "$INTERRUPTED" ]]; then
            echo "        [X] Interrupted while switching to the new install"
        else
            echo "        [X] The switch to the new install did not finish"
        fi
        undo_switch
        rc=1
    elif [[ -n "$NEW_BUILD" && -z "$SWAPPED" ]]; then
        remove_new_build  # a signal or a failure before the swap
    fi
    [[ -z "$WRITE_TMP" ]] || rm -f "$WRITE_TMP"
    rm -rf "$WORK_DIR"
    exit "$rc"
}

# Write the files, enable the unit (literally `enabled`, as camera-box verifies), (re)start it.
switch_to_new_install() {
    local enabled
    mkdir -p "$CONFIG_DIR"
    chmod 755 "$CONFIG_DIR"
    write_if_changed "$CONFIG_FILE" 600 "config.json" <<<"$(config_json_text "$AUTH_KEY")"
    write_if_changed "$ENV_FILE" 600 "EnvironmentFile" <<<"$(env_file_text "$AUTH_KEY")"
    write_if_changed "$UNIT_PATH" 644 "unit" <<<"$(unit_text)"
    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME" >"$WORK_DIR/enable.log" 2>&1 || true
    enabled=$(systemctl is-enabled "$SERVICE_NAME" 2>/dev/null || true)
    if [[ "${enabled%%$'\n'*}" != enabled ]]; then
        HEALTH_FAILURE="${SERVICE_NAME}.service is not enabled (is-enabled: '${enabled}'): $(tr '\n' ' ' <"$WORK_DIR/enable.log")"
        health_failed
    fi
    if [[ "$POLICY" == enable-only ]]; then
        say "Enabled for the next boot (enable-only: not started)"
        return 0
    fi
    # Unchanged files: only a proven running install is left as it runs. camera-box's enable-only
    # in-place upgrade, a hung process, a stopped one or authentication off get a restart.
    if [[ -z "$CHANGED" && -z "$PREV_PROVEN" ]]; then
        say "The running service is not a proven v${PKG_VER} (another version, no answer or no authentication): restarting"
        CHANGED=1
    fi
    if [[ -n "$CHANGED" ]]; then
        say "Restarting ${SERVICE_NAME}..."
        systemctl restart "$SERVICE_NAME" || true
    else
        say "Nothing changed; making sure ${SERVICE_NAME} runs"
        systemctl start "$SERVICE_NAME" || true
    fi
    say "Proving the install (health, authentication)..."
}

# --- after the proof ---------------------------------------------------------------------------

# Our pre-#12 installer's SYSTEM-python copy goes: the service runs the proven venv now.
remove_legacy_system_copy() {
    local ver
    ver=$("$PYTHON" -m pip show remoteos-mcp 2>/dev/null | sed -n 's/^Version: //p') || ver=""
    [[ -n "$ver" ]] || return 0
    say "Removing the old system-python copy (v${ver}): the venv proved itself"
    if "$PYTHON" -m pip uninstall -y --break-system-packages remoteos-mcp fastmcp fastmcp-slim \
        >"$WORK_DIR/legacy.log" 2>&1; then
        say "Removed the old system-python copy"
    else
        say "[!] Could not remove the old system-python copy (the service does not use it). Full pip output:"
        cat "$WORK_DIR/legacy.log"
    fi
}

# Remove every build in the store but the one the link points at.
prune_builds() {
    local keep entry
    [[ -d "$VENV_STORE" ]] || return 0
    keep=$(readlink -f "$VENV_LINK" 2>/dev/null || true)
    for entry in "$VENV_STORE"/* "$VENV_STORE"/.[!.]*; do
        [[ -e "$entry" || -L "$entry" ]] || continue  # an unmatched glob
        [[ -n "$keep" && "$(readlink -f "$entry")" == "$keep" ]] || rm -rf "$entry"
    done
}

configure_firewall() {
    if command -v ufw &>/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
        ufw allow "$PORT"/tcp comment "remoteos-mcp" 2>/dev/null || true
        say "ufw: allowed port $PORT"
    elif command -v firewall-cmd &>/dev/null && firewall-cmd --state &>/dev/null; then
        firewall-cmd --permanent --add-port="$PORT"/tcp 2>/dev/null || true
        firewall-cmd --reload 2>/dev/null || true
        say "firewalld: allowed port $PORT"
    else
        say "No active firewall detected; skipping firewall rule"
    fi
}

# Everything runs inside main(), called on the last line: a truncated `curl | bash` download
# fails to parse (or never reaches the call) instead of running half.
main() {
    REPO="zbynekdrlik/remoteos-mcp"
    PORT=8092
    SERVICE_NAME="remoteos-mcp"
    CONFIG_DIR="/etc/remoteos-mcp"
    UNIT_PATH="/etc/systemd/system/remoteos-mcp.service"
    VENV_LINK="/opt/remoteos-mcp-venv"
    VENV_STORE="/opt/remoteos-mcp-venvs"
    LOCK_FILE="/run/remoteos-mcp-install.lock"
    HEALTH_TRIES=12
    HEALTH_SLEEP=5
    CONFIG_FILE="$CONFIG_DIR/config.json"
    ENV_FILE="$CONFIG_DIR/remoteos-mcp.env"
    PENDING_FILE="$CONFIG_DIR/.switch-pending"
    umask 022
    shopt -s globasciiranges  # [A-Za-z0-9] = ASCII in any locale (bash >= 4.3)

    # bash keeps a caller-exported name's export flag (globals AND locals): unset every name that
    # holds the key or the unit text (a legacy key), so none reaches a child's environment.
    unset AUTH_KEY EXPLICIT_KEY GH_TOKEN_VALUE KEY_SOURCE OLD_UNIT OLD_DESCRIPTION \
        unit_key env_key cfg_key k key line rest v
    # The secrets are read once, then dropped from the environment every child inherits.
    EXPLICIT_KEY="${REMOTEOS_MCP_AUTH_KEY:-}"
    GH_TOKEN_VALUE="${GH_TOKEN:-}"
    unset REMOTEOS_MCP_AUTH_KEY GH_TOKEN
    REF="${REMOTEOS_MCP_REF:-main}"
    GIVEN_MODE="${REMOTEOS_MCP_MODE:-}"
    GIVEN_USER="${REMOTEOS_MCP_USER:-}"
    POLICY="${REMOTEOS_MCP_POLICY:-restart}"

    # The run's state (each set below where it happens).
    WORK_DIR="" TMP_LINK="" WAS_PENDING="" SESSION_TYPE="" SESSION_DISPLAY="" UNIT_DISPLAY=""
    ARMED="" INTERRUPTED="" CHANGED="" SWAPPED="" NEW_BUILD="" STORE_CREATED=""
    PREV_LINK_TARGET="" ADOPTED_DIR="" HAD_UNIT="" HEALTH_FAILURE="" MODIFIED="" PREV_PROVEN=""
    GI_LINKED="" WRITE_TMP=""

    printf '\n  RemoteOS MCP Installer (Linux)\n  Remote desktop control for Claude Code\n\n'

    if [[ "$EUID" -ne 0 ]]; then
        fail "This installer must be run as root (via sudo)."
    fi
    if [[ "$EUID" -eq 0 ]]; then PIP_HOME=/root; else PIP_HOME="${HOME:-/tmp}"; fi
    if command -v findmnt &>/dev/null && [[ ",$(findmnt -no OPTIONS / 2>/dev/null || true)," == *,ro,* ]]; then
        fail "Root filesystem is read-only — run: mount -o remount,rw / (and fix /etc/fstab: '/ ext4 ro' → 'defaults') then rerun."
    fi
    check_inputs
    # One run at a time: a second run's prune could delete the build this one just swapped in.
    command -v flock &>/dev/null || fail "flock (util-linux) is missing — nothing was changed on this box"
    [[ "$POLICY" != restart ]] || command -v ss &>/dev/null \
        || fail "ss (iproute2, the proof's port check) is missing — nothing was changed on this box"
    umask 077  # owner-only: flock(2) works on a read-only fd, so any reader could block us
    exec 9>"$LOCK_FILE"
    umask 022
    chmod 600 "$LOCK_FILE"  # an older run's lock was 0644
    flock -n 9 || fail "another install-linux.sh run holds ${LOCK_FILE} — nothing was changed on this box"

    echo "  [1/6] Checking Python and the box..."
    find_python
    detect_layout
    [[ -n "$OLD_UNIT" ]] && HAD_UNIT=1

    echo "  [2/6] Auth key..."
    resolve_key

    WORK_DIR=$(mktemp -d)
    exec 3>&1 4>&2
    trap on_exit EXIT
    trap on_signal HUP INT TERM

    echo "  [3/6] Downloading the source..."
    fetch_source

    echo "  [4/6] The venv..."
    # The desktop tools first: they bring python3-gi, which the venv links (camera-box's order).
    if [[ "$MODE" == desktop ]]; then
        install_desktop_tools
    fi
    if venv_is_current; then
        PKG_VER=$(venv_version "$VENV_LINK")
        say "${SOURCE_ID} (v${PKG_VER}) is already installed in ${VENV_LINK}"
    else
        build_venv
    fi

    echo "  [5/6] Switching to the new install..."
    save_previous
    # A switch an earlier run made that the unit has not started since (enable-only, a run killed
    # with no trap) left the files ahead of the process: restart, never take it as unchanged.
    WAS_PENDING=""
    if [[ -e "$PENDING_FILE" && "$(cat "$PENDING_FILE")" == "$(invocation_id)" ]]; then
        WAS_PENDING=1 CHANGED=1
        say "An earlier switch was never proven: $([[ "$POLICY" == restart ]] && echo "restarting into what the files say" || echo "it stays pending (enable-only)")"
    elif [[ -e "$PENDING_FILE" ]]; then
        say "An earlier switch has run since (the unit started again): it is checked as it runs"
    fi
    [[ "$POLICY" != restart ]] || check_running_install  # before the switch: what a failure may go back to
    arm
    mkdir -p "$CONFIG_DIR"
    invocation_id >"$PENDING_FILE"
    if [[ -n "$NEW_BUILD" ]]; then
        swap_venv
    else
        ensure_system_gi
    fi
    switch_to_new_install
    if [[ "$POLICY" == restart ]] && ! prove_health; then
        health_failed
    fi
    disarm
    exec 3>&- 4>&-
    if [[ "$POLICY" == restart ]]; then
        rm -f "$PENDING_FILE"
        say "Health check OK: v${PKG_VER}; an authenticated request answered 200, an unauthenticated one 401"
        remove_legacy_system_copy
        prune_builds
    else
        # A switch stays pending; a no-op on a proven running install leaves it proven. Old builds
        # go (the link points at the new one); an old system copy stays (a legacy process).
        [[ -n "$CHANGED" ]] || check_running_install
        if [[ -n "$PREV_PROVEN" ]]; then
            rm -f "$PENDING_FILE"
            say "Nothing changed (enable-only): the proven running install was not touched"
        else
            say "Not started (enable-only): the next restart run restarts and proves it; any old system-python copy stays until then"
        fi
        prune_builds
    fi

    echo "  [6/6] Configuring firewall and getting network info..."
    configure_firewall
    LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "LINUX_IP")
    HOSTNAME_SHORT=$(hostname -s 2>/dev/null | tr '[:upper:]' '[:lower:]' || echo "linux")
    say "$HOSTNAME_SHORT ($LOCAL_IP)"

    printf '%s\n' "" "  ============================================" "  SETUP COMPLETE" \
        "  ============================================" "" "  Computer:  $HOSTNAME_SHORT ($LOCAL_IP)" \
        "  Port:      $PORT" "  Version:   v${PKG_VER} (${SOURCE_ID})" \
        "  Auth key:  in ${ENV_FILE} and ${CONFIG_FILE} (root, 0600) — never printed." \
        "             Read it on this box: sudo cat ${CONFIG_FILE}"
    [[ -n "$KEY_SOURCE" ]] || echo "             It is a NEW key: update the client's .mcp.json entry for this box."
    printf '%s\n' "" "  On your Linux machine, run:" "" "  claude mcp add --transport http linux-${HOSTNAME_SHORT} \\" \
        "    http://${LOCAL_IP}:${PORT}/mcp \\" "    --header \"Authorization: Bearer <the auth key>\"" "" \
        "  Then restart Claude Code." "" "  To uninstall later:" \
        "  curl -fsSL https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/main/uninstall-linux.sh | sudo bash" ""
}

main "$@"
