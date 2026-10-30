# --- the box: python, the unit's mode and user ------------------------------------------------

# python_ok "<python --version output>": 3.11+ (Pillow 13, the fix of its CVEs, needs 3.11; #64)
python_ok() {
    [[ "${1-}" =~ Python\ 3\.([0-9]+) ]] && (( BASH_REMATCH[1] >= 11 ))
}

pick_python() {
    local cmd ver
    for cmd in python3 python /usr/bin/python3 /usr/local/bin/python3; do
        if command -v "$cmd" &>/dev/null || [[ -x "$cmd" ]]; then
            ver=$("$cmd" --version 2>&1 || true)
            if python_ok "$ver"; then
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
    say "Python 3.11+ not found. Trying the package manager..."
    if command -v apt-get &>/dev/null; then
        apt-get install -y python3 python3-venv || true
    elif command -v dnf &>/dev/null; then
        dnf install -y python3 || true
    elif command -v yum &>/dev/null; then
        yum install -y python3 || true
    fi
    pick_python \
        || fail "Python 3.11+ required but not found. Install it (apt-get install python3 python3-venv, or dnf/yum) and rerun."
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

