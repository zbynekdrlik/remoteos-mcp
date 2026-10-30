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

