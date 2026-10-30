# --- the per-box updater (#65) -------------------------------------------------------------------

updater_service_text() {
    printf '%s\n' "[Unit]" "Description=RemoteOS MCP updater: move this box to its ring's release" \
        "After=network-online.target" "Wants=network-online.target" "" \
        "[Service]" "Type=oneshot" "ExecStart=${VENV_LINK}/bin/python -m remoteos.updater"
}

# Daily at a random time, plus after a boot; Persistent= runs a date missed while the box was off.
updater_timer_text() {
    printf '%s\n' "[Unit]" "Description=RemoteOS MCP updater: daily, and after a boot" "" \
        "[Timer]" "OnCalendar=daily" "RandomizedDelaySec=6h" "Persistent=true" "OnBootSec=10min" "" \
        "[Install]" "WantedBy=timers.target"
}

# After the proof: nothing here touches the running server, and a failure leaves the proven install
# up. Not on enable-only boxes: camera-box's provisioning updates its cams (#60), and a timer here
# would fight its pin (one a default run left there goes). The ring stays what the box has, unless
# REMOTEOS_MCP_RING names one.
install_updater() {
    if [[ "$POLICY" != restart ]]; then
        if [[ -f "$UPDATER_TIMER" ]]; then
            systemctl disable --now "${UPDATER_TIMER##*/}" >/dev/null 2>&1 || true
            rm -f "$UPDATER_TIMER" "$UPDATER_SERVICE"
            systemctl daemon-reload
        fi
        say "Updater: none (enable-only: camera-box's provisioning updates this box)"
        return 0
    fi
    if [[ -n "$GIVEN_RING" || ! -f "$UPDATER_CONFIG" ]]; then
        write_if_changed "$UPDATER_CONFIG" 644 "Updater ring" <<<"{\"ring\": \"${GIVEN_RING:-stable}\"}"
    fi
    CHANGED=""
    # here-strings, not pipes: a pipe would run the writer in a subshell and lose CHANGED and WRITE_TMP
    write_if_changed "$UPDATER_SERVICE" 644 "Updater unit" <<<"$(updater_service_text)"
    write_if_changed "$UPDATER_TIMER" 644 "Updater timer" <<<"$(updater_timer_text)"
    if [[ -n "$CHANGED" ]]; then systemctl daemon-reload; fi
    if ! systemctl enable --now "${UPDATER_TIMER##*/}" >/dev/null 2>&1; then
        say "[!] The updater's timer could not be enabled: this box stays on v${PKG_VER} until a re-run"
        return 0
    fi
    say "Updater: daily, ring $(sed -n 's/.*"ring": "\([a-z]*\)".*/\1/p' "$UPDATER_CONFIG")"
}

