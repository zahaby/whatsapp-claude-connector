# Shared paths. Sourced by the other scripts.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UPSTREAM="${UPSTREAM:-$HOME/whatsapp-mcp}"
UPSTREAM_COMMIT="${UPSTREAM_COMMIT:-7d6a06dcdce1f01dfb24f60e1030d5efba9f3b88}"  # audited revision
ENV_FILE="$REPO/connector/.env"
LA="$HOME/Library/LaunchAgents"
LOGS="$HOME/Library/Logs/wa-connector"
LABELS=(com.wa.bridge com.wa.connector com.wa.tunnel)

svc_restart() { launchctl kickstart -k "gui/$(id -u)/$1" 2>/dev/null || true; }
svc_stop()    { launchctl bootout "gui/$(id -u)/$1" 2>/dev/null || true; }
svc_start()   { launchctl bootstrap "gui/$(id -u)" "$LA/$1.plist" 2>/dev/null || svc_restart "$1"; }
env_get()     { grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2-; }
env_set() {   # env_set KEY VALUE  (in place, keeps file mode 600)
  if grep -qE "^$1=" "$ENV_FILE"; then
    /usr/bin/sed -i '' -E "s|^$1=.*|$1=$2|" "$ENV_FILE"
  else
    echo "$1=$2" >> "$ENV_FILE"
  fi
}
