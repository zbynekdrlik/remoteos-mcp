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

