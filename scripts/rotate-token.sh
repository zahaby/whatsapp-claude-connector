#!/usr/bin/env bash
# Generate a new 256-bit token and PROVE the running connector switched to it.
#   scripts/rotate-token.sh               rotate + restart + verify
#   scripts/rotate-token.sh --no-restart  only write a token (used by install.sh)
source "$(dirname "$0")/common.sh"
OLD="$(env_get MCP_AUTH_TOKEN)"
gen_token

if [ "${1:-}" = "--no-restart" ]; then
  echo "New token written to $ENV_FILE (connector not restarted)."
elif svc_loaded com.wa.connector; then
  if ! ( restart_connector && WA_OLD_TOKEN="$OLD" "$PY" "$REPO/connector/verify.py" token ); then
    echo "ERROR: rotation NOT verified. The OLD connector URL may STILL WORK." >&2
    echo "       Check scripts/status.sh and $LOGS/connector.log, then re-run this script." >&2
    exit 1
  fi
  echo "Verified: the running connector accepts the new token and rejects the old one."
else
  echo "Connector service is not running, so no URL works right now."
  echo "The new token takes effect on scripts/start.sh."
fi

if [ -n "$(env_get MCP_PUBLIC_HOSTNAME)" ]; then
  echo "Update the connector URL in claude.ai (Settings > Connectors): scripts/connector-url.sh"
else
  echo "No public hostname yet; run scripts/setup-tunnel.sh when ready."
fi
