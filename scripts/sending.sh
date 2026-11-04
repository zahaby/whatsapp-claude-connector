#!/usr/bin/env bash
# THE SEND SWITCH.   scripts/sending.sh on | off | status
# on/off only report success after the RUNNING remote connector is observed
# exposing (or not exposing) the send tool.
source "$(dirname "$0")/common.sh"
VERIFY=("$PY" "$REPO/connector/verify.py")
want="${1:-status}"
WANT_UC="$(printf %s "$want" | tr a-z A-Z)"   # bash 3.2 (macOS) has no ${x^^}

case "$want" in
  on)  env_set WHATSAPP_ALLOW_SEND true ;;
  off) env_set WHATSAPP_ALLOW_SEND false ;;
  status)
    echo "configured: WHATSAPP_ALLOW_SEND=$(env_get WHATSAPP_ALLOW_SEND)"
    if svc_loaded com.wa.connector; then
      if "${VERIFY[@]}" sending --expect on 2>/dev/null; then echo "running remote connector: sending ON"
      elif "${VERIFY[@]}" sending --expect off 2>/dev/null; then echo "running remote connector: sending OFF"
      else echo "running remote connector: UNKNOWN (not reachable)"; fi
    else
      echo "remote connector: not running"
    fi
    exit 0 ;;
  *) echo "usage: $0 on|off|status"; exit 2 ;;
esac

echo "configured: WHATSAPP_ALLOW_SEND=$(env_get WHATSAPP_ALLOW_SEND)"
if svc_loaded com.wa.connector; then
  if ! ( restart_connector && "${VERIFY[@]}" sending --expect "$want" ); then
    echo "ERROR: the running remote connector was NOT confirmed as sending $WANT_UC." >&2
    [ "$want" = off ] && echo "       Sending may STILL BE ENABLED. Run scripts/stop.sh to be safe." >&2
    exit 1
  fi
  echo "Verified: running remote connector has sending $WANT_UC."
else
  echo "Remote connector not running; the setting applies on scripts/start.sh."
fi
echo "Claude Desktop (local): quit and reopen it to apply. Claude app: start a new chat."
