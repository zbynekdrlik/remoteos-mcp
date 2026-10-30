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

# poll_running_install await|read: re-check until proven (await) or while not answering yet (read).
poll_running_install() {
    local i
    for ((i = 0; i < HEALTH_TRIES; i++)); do
        check_running_install
        [[ -z "$PREV_PROVEN" && ( "$1" == await || -n "$NO_ANSWER" ) ]] || return 0
        sleep "$HEALTH_SLEEP"
    done
}

# stop_unproven WHY: nothing proven runs, so nothing runs (it may be an agent with auth off).
stop_unproven() {
    systemctl stop "$SERVICE_NAME" >/dev/null 2>&1
    systemctl disable "$SERVICE_NAME" >/dev/null 2>&1
    [[ ! -e "$PENDING_FILE" ]] || invocation_id >"$PENDING_FILE"  # after the disable: still pending
    echo "        [X] $1: ${SERVICE_NAME} is stopped and disabled; fix the cause and rerun. Logs: journalctl -u ${SERVICE_NAME} -n 50"
}

# The version the service reports on /health (fails when nothing answers). Every loopback curl
# is `-q --noproxy '*'`: no proxy (pip keeps the env's) and no .curlrc option (a trace logs the key).
health_version() {
    local body
    body=$(curl -q --noproxy '*' -fsS --max-time 3 "http://127.0.0.1:${PORT}/health" 2>/dev/null) || return 1
    printf '%s' "$body" | "$PYTHON" -c 'import json, sys; print(json.load(sys.stdin).get("version", ""), end="")' \
        2>/dev/null || true
}

# What an unauthenticated POST /mcp answers (401 = the key is enforced). Sends no key.
unauth_code() {
    curl -q --noproxy '*' -s -o /dev/null -w '%{http_code}' --max-time 5 -X POST "http://127.0.0.1:${PORT}/mcp" 2>/dev/null || true
}

# PREV_PROVEN = what runs is proven: a unit, no pending switch, /health = the unit's python's
# version, an unauthenticated request refused. Read-only, no key sent. Only that is restored.
check_running_install() {
    local py running
    PREV_PROVEN="" NO_ANSWER=""
    [[ -n "$HAD_UNIT" && -z "$WAS_PENDING" ]] || return 0
    py=$(first_value 'ExecStart=' "$OLD_UNIT" | awk '{ print $1; exit }')
    [[ "${py##*/}" == python* ]] || return 0
    systemctl is-active --quiet "$SERVICE_NAME" || return 0
    running=$(health_version) || { NO_ANSWER=1; return 0; }
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
        | curl -q --noproxy '*' -s -o /dev/null -w '%{http_code}' --max-time 10 -H @- -H 'Content-Type: application/json' \
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

# Undo a switch (set +e, SIGPIPE ignored). enable-only starts/stops nothing (files back, or only
# disabled). restart: a PROVEN install gets its files back and must prove itself again; else stop.
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
        poll_running_install await
        if [[ -z "$PREV_PROVEN" ]]; then
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

# The new install did not prove itself: immune to signals and a lost console until undone.
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

