# --- enrollment (#26): only with REMOTEOS_ENROLL set; a plain run never reaches this ------------

# The one-time URL `remoteos-gateway enroll` prints: https, a host, one token segment.
enroll_url_ok() {
    [[ "${1-}" =~ ^https://[a-z0-9.-]+/[A-Za-z0-9_-]+/$ ]] && [[ ${#1} -le 300 ]]
}

# One public key line: no options (`command=...`), no second line.
ssh_key_ok() {
    [[ "${1-}" =~ ^(ssh-ed25519|ecdsa-sha2-nistp256|ssh-rsa)\ [A-Za-z0-9+/=]+(\ [A-Za-z0-9@._-]+)?$ ]] \
        && [[ ${#1} -le 2000 ]]
}

# Before anything changes. A box that is only enabled is never proven, so nothing could be enrolled.
check_enroll_inputs() {
    [[ -n "$ENROLL_URL" ]] || return 0
    enroll_url_ok "$ENROLL_URL" \
        || fail "REMOTEOS_ENROLL is not an enrollment URL (https://<host>/<token>/) — nothing was changed on this box"
    ssh_key_ok "$ENROLL_SSH_KEY" \
        || fail "REMOTEOS_ENROLL_SSH_KEY must be one ssh public key line — nothing was changed on this box"
    [[ "$POLICY" == restart ]] \
        || fail "REMOTEOS_ENROLL needs the restart policy: enrollment proves the running server — nothing was changed on this box"
}

# The OpenSSH server, up at every boot. Debian's unit is ssh, RHEL's sshd.
enable_ssh_server() {
    if ! type -P sshd >/dev/null; then
        say "Installing the OpenSSH server..."
        if command -v apt-get &>/dev/null; then
            apt-get install -y openssh-server || true
        elif command -v dnf &>/dev/null; then
            dnf install -y openssh-server || true
        elif command -v yum &>/dev/null; then
            yum install -y openssh-server || true
        fi
        type -P sshd >/dev/null || fail "the OpenSSH server could not be installed"
    fi
    systemctl enable --now ssh >/dev/null 2>&1 || systemctl enable --now sshd >/dev/null 2>&1 \
        || fail "the ssh service could not be enabled"
    if command -v ufw &>/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
        ufw allow 22/tcp comment "remoteos-mcp enrollment" 2>/dev/null \
            || say "[!] ufw refused port 22: dev1 may not reach this box's ssh"
    elif command -v firewall-cmd &>/dev/null && firewall-cmd --state &>/dev/null; then
        { firewall-cmd --permanent --add-service=ssh && firewall-cmd --reload; } 2>/dev/null \
            || say "[!] firewalld refused ssh: dev1 may not reach this box's ssh"
    fi
    say "OpenSSH server: enabled"
}

# dev1's key for the service account: added once, the file's other lines kept.
install_ssh_key() {
    local home keys
    home=$(getent passwd "$SERVICE_USER" | cut -d: -f6) || fail "the home folder of ${SERVICE_USER} is unknown"
    [[ -n "$home" ]] || fail "the home folder of ${SERVICE_USER} is unknown"
    keys="$home/.ssh/authorized_keys"
    mkdir -p "$home/.ssh"
    touch "$keys"
    if ! grep -qxF "$ENROLL_SSH_KEY" "$keys"; then
        [[ ! -s "$keys" || -z "$(tail -c 1 "$keys")" ]] || printf '\n' >>"$keys"
        printf '%s\n' "$ENROLL_SSH_KEY" >>"$keys"
    fi
    chmod 700 "$home/.ssh"
    chmod 600 "$keys"
    chown -R "$SERVICE_USER:" "$home/.ssh"
    say "SSH key: in ${keys}"
}

# The registration, key included: printf is a builtin, so it reaches no argv. The name goes through
# name_ok, the addresses keep digits and dots, the key is key_ok's: nothing here can break the JSON.
enrollment_body() {
    local name ip ips=""
    name=$HOSTNAME_SHORT
    name_ok "$name" || name="unknown"
    for ip in $(hostname -I 2>/dev/null || true); do
        case "$ip" in
            *[!0-9.]*) ;;
            *) ips="${ips:+$ips, }\"$ip\"" ;;
        esac
    done
    printf '{"hostname": "%s", "ips": [%s], "port": %s, "version": "%s", "os": "linux", "account": "%s", "auth_key": "%s"}' \
        "$name" "$ips" "$PORT" "$PKG_VER" "$SERVICE_USER" "$AUTH_KEY"
}

# POST it. The URL (its token is the receiver's only auth) comes through a config on fd 5 and the
# body on stdin: neither is in curl's argv. -q: no .curlrc trace. The proxy of the box stays usable.
# Only the receiver's own `ok` counts: curl -f takes a redirect (the Access 302) as a success.
post_enrollment() {
    local try reply
    for try in 1 2 3; do
        reply=$(curl -q -fsS --proto =https --max-time 30 -A remoteos-enroll -H 'Content-Type: application/json' \
            -K /dev/fd/5 --data-binary @- 5<<<"url = \"$ENROLL_URL\"" <<<"$(enrollment_body)") || reply=""
        if [[ "$reply" == ok ]]; then
            say "Enrolled: dev1 received this box's registration"
            return 0
        fi
        say "[!] Registration attempt ${try} failed"
        [[ "$try" -eq 3 ]] || sleep 5
    done
    fail "the registration got no answer from dev1: the install stands, check on dev1 whether the box is enrolled"
}

# After the proof, last: a failure leaves the proven install up, and the run exits 1. The registration
# goes first: SSH is the break-glass path, and a box dev1 reaches over MCP can have it repaired.
enroll_box() {
    [[ -n "$ENROLL_URL" ]] || return 0
    echo "  Enrolling this box..."
    post_enrollment
    enable_ssh_server
    install_ssh_key
}

