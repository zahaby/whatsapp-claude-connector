# Shared paths and helpers. Sourced by the other scripts.
# Every helper that changes a service FAILS LOUDLY: callers rely on set -e,
# and nothing reports success unless the change was observed.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UPSTREAM="${UPSTREAM:-$HOME/whatsapp-mcp}"
UPSTREAM_COMMIT="${UPSTREAM_COMMIT:-7d6a06dcdce1f01dfb24f60e1030d5efba9f3b88}"  # audited revision
# WhatsApp rejects old client libraries ("Client outdated (405)"), so the
# bridge is built against this newer whatsmeow instead of upstream's pin.
# Bump it (and re-run install.sh) if WhatsApp starts refusing it again.
WHATSMEOW_VERSION="${WHATSMEOW_VERSION:-v0.0.0-20261102152715-54038813c47f}"
ENV_FILE="${WA_ENV_FILE:-$REPO/connector/.env}"
PY="${WA_PYTHON:-$REPO/connector/.venv/bin/python}"
export WA_CONFIG="$ENV_FILE"          # verify.py / server.py read the same file
LA="$HOME/Library/LaunchAgents"
LOGS="$HOME/Library/Logs/wa-connector"
LABELS=(com.wa.bridge com.wa.connector com.wa.tunnel)
DOMAIN="gui/$(id -u)"

die() { echo "ERROR: $*" >&2; exit 1; }

# ---- .env ------------------------------------------------------------------
sed_inplace() {   # sed_inplace <sed args...> <file>   (GNU and BSD/macOS sed)
  local file="${*: -1}"
  sed -i.bak "$@" && rm -f "$file.bak"
}
env_get() { { grep -E "^$1=" "$ENV_FILE" 2>/dev/null || true; } | head -1 | cut -d= -f2-; }
env_set() {   # env_set KEY VALUE   (values here never contain | or &)
  if grep -qE "^$1=" "$ENV_FILE"; then
    sed_inplace -E "s|^$1=.*|$1=$2|" "$ENV_FILE"
  else
    echo "$1=$2" >> "$ENV_FILE"
  fi
  chmod 600 "$ENV_FILE"
}
gen_token() { env_set MCP_AUTH_TOKEN "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"; }

# ---- launchd ----------------------------------------------------------------
svc_loaded() { launchctl print "$DOMAIN/$1" >/dev/null 2>&1; }
svc_pid()    { launchctl print "$DOMAIN/$1" 2>/dev/null | awk '$1=="pid" && $2=="=" {print $3; exit}' || true; }
svc_restart() { launchctl kickstart -k "$DOMAIN/$1" || die "launchctl could not restart $1"; }
svc_stop() {
  svc_loaded "$1" || return 0
  launchctl bootout "$DOMAIN/$1" || die "launchctl could not stop $1"
}
svc_start() {
  [ -f "$LA/$1.plist" ] || die "$LA/$1.plist missing; run macos/install.sh"
  if svc_loaded "$1"; then svc_restart "$1"; else
    launchctl bootstrap "$DOMAIN" "$LA/$1.plist" || die "launchctl could not start $1"
  fi
}

# Restart the connector and wait until a NEW process is serving. Waiting for a
# new PID matters: right after kickstart the old process could still answer.
restart_connector() {
  local old new i=0 ticks=$(( ${WA_RESTART_TIMEOUT:-20} * 4 ))
  old="$(svc_pid com.wa.connector)"
  svc_restart com.wa.connector
  while :; do
    new="$(svc_pid com.wa.connector)"
    [ -n "$new" ] && [ "$new" != "$old" ] && break
    i=$((i + 1)); [ "$i" -ge "$ticks" ] && die "connector did not restart (still pid ${old:-none})"
    sleep 0.25
  done
  "$PY" "$REPO/connector/verify.py" wait-healthy || die "restarted connector is not healthy"
}
