#!/usr/bin/env bash
# THE SEND SWITCH.   scripts/sending.sh on | off | status
source "$(dirname "$0")/common.sh"
case "${1:-status}" in
  on)  env_set WHATSAPP_ALLOW_SEND true ;;
  off) env_set WHATSAPP_ALLOW_SEND false ;;
  status) ;;
  *) echo "usage: $0 on|off|status"; exit 2 ;;
esac
echo "WHATSAPP_ALLOW_SEND=$(env_get WHATSAPP_ALLOW_SEND)"
if [ "${1:-status}" != status ]; then
  svc_restart com.wa.connector
  echo "Remote connector restarted. Quit and reopen Claude Desktop to apply locally."
  echo "In the Claude app, open a new chat so the tool list refreshes."
fi
