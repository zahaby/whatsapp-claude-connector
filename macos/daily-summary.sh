#!/usr/bin/env bash
# DAILY GROUP SUMMARY: a separate, minimal connector an unattended scheduled
# task uses to read ONE group and post one summary a day into it.
#
#   macos/daily-summary.sh set "<group name>"   choose the group (exact chat name)
#   macos/daily-summary.sh on | off             switch the daily endpoint (verified)
#   macos/daily-summary.sh url                  copy its connector URL (--print to show)
#   macos/daily-summary.sh rotate               new secret for its URL (verified)
#   macos/daily-summary.sh status               group, switch, last post
#   macos/daily-summary.sh reset-limit          allow the next post now (for testing)
source "$(dirname "$0")/common.sh"
DAILY=("$PY" "$REPO/connector/daily.py")
VERIFY=("$PY" "$REPO/connector/verify.py")
action="${1:-status}"

new_daily_token() { env_set MCP_DAILY_TOKEN "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"; }

apply_and_verify() {   # apply_and_verify on|off
  local want="$1" uc; uc="$(printf %s "$want" | tr a-z A-Z)"
  if svc_loaded com.wa.connector; then
    if ! ( restart_connector && "${VERIFY[@]}" autopost --expect "$want" ); then
      echo "ERROR: the running connector was NOT confirmed with the daily summary $uc." >&2
      [ "$want" = off ] && echo "       It may STILL BE ON. Run macos/stop.sh to be safe." >&2
      exit 1
    fi
    echo "Verified: the running connector has the daily summary $uc."
  else
    echo "Connector not running; the setting applies on macos/start.sh."
  fi
}

case "$action" in
  set)
    name="${2:?usage: $0 set \"<group name>\"}"
    line="$("${DAILY[@]}" resolve "$name")" || exit 1
    jid="${line%%$'\t'*}"; label="${line#*$'\t'}"
    env_set WA_AUTOPOST_CHAT "$jid"
    echo "Daily summary group: $label"
    if [ "$(env_get WHATSAPP_AUTOPOST)" = true ]; then apply_and_verify on; fi ;;
  on)
    [ -n "$(env_get WA_AUTOPOST_CHAT)" ] || die "choose the group first: $0 set \"<group name>\""
    [ -n "$(env_get MCP_DAILY_TOKEN)" ] || new_daily_token
    env_set WHATSAPP_AUTOPOST true
    apply_and_verify on
    echo "Next: $0 url  (then add it in claude.ai as a separate connector, e.g. 'WhatsApp Daily')." ;;
  off)
    env_set WHATSAPP_AUTOPOST false
    apply_and_verify off ;;
  rotate)
    new_daily_token
    if [ "$(env_get WHATSAPP_AUTOPOST)" = true ]; then apply_and_verify on; fi
    echo "New daily URL created; the old one no longer works. Update it in claude.ai: $0 url" ;;
  url)
    host="$(env_get MCP_PUBLIC_HOSTNAME)"; tok="$(env_get MCP_DAILY_TOKEN)"
    [ -n "$host" ] || die "MCP_PUBLIC_HOSTNAME not set; run macos/setup-tunnel.sh"
    [ -n "$tok" ] || die "daily summary not set up yet: $0 set \"<group>\" then $0 on"
    url="https://$host/daily/$tok"
    if [ "${2:-}" = "--print" ]; then echo "$url"
    else printf %s "$url" | pbcopy && echo "Daily-summary connector URL copied to clipboard (treat it like a password)."; fi ;;
  status)
    "${DAILY[@]}" status
    if svc_loaded com.wa.connector; then
      if "${VERIFY[@]}" autopost --expect on 2>/dev/null; then echo "running connector: daily summary ON"
      else echo "running connector: daily summary OFF"; fi
    else echo "connector: not running"; fi ;;
  reset-limit)
    "${DAILY[@]}" reset-limit ;;
  *) echo "usage: $0 set \"<group>\" | on | off | url [--print] | rotate | status | reset-limit"; exit 2 ;;
esac
