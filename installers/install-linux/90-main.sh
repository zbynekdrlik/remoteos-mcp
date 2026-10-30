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
    CGROUP_FILE="/proc/self/cgroup"
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
        fail "Root filesystem is read-only — nothing was changed on this box. A camera-box cam is read-only by design: update it through camera-box's provisioning (its rw window). Elsewhere: mount -o remount,rw / for this run, then rerun."
    fi
    check_inputs
    ! grep -q "/${SERVICE_NAME}\.service" "$CGROUP_FILE" 2>/dev/null || fail "this run is inside ${SERVICE_NAME}.service (the agent's own Shell): its restart (or a rollback's stop) would kill the run — nothing was changed on this box. Run it over SSH."
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
    [[ "$POLICY" != restart ]] || poll_running_install read  # before the switch: what a failure may go back to
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
        # A switch stays pending (a legacy process keeps its system copy); a proven no-op stays proven.
        [[ -n "$CHANGED" ]] || poll_running_install read
        if [[ -n "$PREV_PROVEN" ]]; then
            rm -f "$PENDING_FILE"
            say "Nothing changed (enable-only): the proven running install was not touched"
            remove_legacy_system_copy  # the proven process runs our unit's venv, not the system copy
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
