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
        say "[!] config.json holds another key; the switch writes the installed one into it"
    fi
}

