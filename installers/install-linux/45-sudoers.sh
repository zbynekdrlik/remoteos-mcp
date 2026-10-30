# --- the service account's admin (#71) ----------------------------------------------------------

# The drop-in a reprovision keeps: passwordless sudo for the unit's account (a root unit has no use
# for one). One line: a hand-made file with this text is adopted as it is.
sudoers_text() {
    printf '%s ALL=(ALL) NOPASSWD:ALL\n' "$SERVICE_USER"
}

# Before anything changes: render the drop-in and let visudo judge it. Sets SUDOERS_TMP (empty = none).
prepare_sudoers() {
    SUDOERS_TMP=""
    [[ "$SERVICE_UID" -ne 0 ]] || return 0
    command -v visudo &>/dev/null \
        || fail "visudo (the sudo package) is missing, and the service account ${SERVICE_USER} needs its sudoers drop-in — nothing was changed on this box"
    SUDOERS_TMP="$WORK_DIR/sudoers"
    sudoers_text >"$SUDOERS_TMP"
    if ! visudo -cf "$SUDOERS_TMP" >"$WORK_DIR/visudo.log" 2>&1; then
        echo "        [X] visudo refused the sudoers line for ${SERVICE_USER} — nothing was changed on this box:"
        cat "$WORK_DIR/visudo.log"
        exit 1
    fi
}

# After the proof: 0440 root, through a temp name sudo ignores (it skips names with a dot), then one
# rename(2). Text, mode and owner already right = nothing is written. No restart: sudo reads it per call.
install_sudoers() {
    [[ -n "$SUDOERS_TMP" ]] || return 0
    if [[ -f "$SUDOERS_FILE" ]] && cmp -s "$SUDOERS_TMP" "$SUDOERS_FILE" \
        && [[ "$(stat -c '%a %U:%G' "$SUDOERS_FILE" 2>/dev/null)" == "440 $(id -un):$(id -gn)" ]]; then
        say "sudoers drop-in: unchanged (${SUDOERS_FILE})"
        return 0
    fi
    [[ ! -e "$SUDOERS_FILE" ]] || say "sudoers drop-in: replacing what ${SUDOERS_FILE} held (another text, mode or owner)"
    mkdir -p "${SUDOERS_FILE%/*}"
    WRITE_TMP="${SUDOERS_FILE}.new"  # on_exit removes it
    install -m 0440 "$SUDOERS_TMP" "$WRITE_TMP"
    mv -f "$WRITE_TMP" "$SUDOERS_FILE"
    WRITE_TMP=""
    say "sudoers drop-in: written (${SUDOERS_FILE}, passwordless sudo for ${SERVICE_USER})"
}

