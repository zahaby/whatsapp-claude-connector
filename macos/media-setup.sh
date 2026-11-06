#!/usr/bin/env bash
# VIDEO / VOICE-NOTE TRANSCRIPTION (local, on this Mac).
#   macos/media-setup.sh on      install the speech model (~1.5 GB once), switch on (verified)
#   macos/media-setup.sh off     switch off (verified); nothing is uninstalled
#   macos/media-setup.sh status
# Links and YouTube captions work without this.
source "$(dirname "$0")/common.sh"
VERIFY=("$PY" "$REPO/connector/verify.py")
action="${1:-status}"

apply_and_verify() {   # apply_and_verify on|off
  local want="$1" uc; uc="$(printf %s "$want" | tr a-z A-Z)"
  if svc_loaded com.wa.connector; then
    if ! ( restart_connector && "${VERIFY[@]}" media --expect "$want" ); then
      echo "ERROR: the running connector was NOT confirmed with transcription $uc." >&2
      exit 1
    fi
    echo "Verified: the running connector has transcription $uc."
  else
    echo "Connector not running; the setting applies on macos/start.sh."
  fi
}

case "$action" in
  on)
    echo "==> Installing the transcription packages"
    ( cd "$REPO/connector" && uv sync -q --extra media ) || die "could not install the transcription packages"
    [ -n "$(env_get WA_WHISPER_MODEL)" ] || env_set WA_WHISPER_MODEL medium
    "$PY" "$REPO/connector/media.py" download-model "$(env_get WA_WHISPER_MODEL)" || die "could not download the speech model"
    env_set WA_MEDIA true
    apply_and_verify on ;;
  off)
    env_set WA_MEDIA false
    apply_and_verify off ;;
  status)
    echo "configured: WA_MEDIA=$(env_get WA_MEDIA) model=$(env_get WA_WHISPER_MODEL)"
    if svc_loaded com.wa.connector; then
      if "${VERIFY[@]}" media --expect on 2>/dev/null; then echo "running connector: transcription ON"
      else echo "running connector: transcription OFF"; fi
    else echo "connector: not running"; fi ;;
  *) echo "usage: $0 on | off | status"; exit 2 ;;
esac
