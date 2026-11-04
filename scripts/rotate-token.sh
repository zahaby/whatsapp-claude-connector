#!/usr/bin/env bash
# Generate a new 256-bit token. The old connector URL stops working immediately.
source "$(dirname "$0")/common.sh"
env_set MCP_AUTH_TOKEN "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
chmod 600 "$ENV_FILE"
[ "${1:-}" = "--no-restart" ] || svc_restart com.wa.connector
echo "New token set. Update the custom connector URL in claude.ai:"
"$(dirname "$0")/connector-url.sh"
