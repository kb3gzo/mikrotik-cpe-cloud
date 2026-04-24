#!/usr/bin/env bash
# Render app/templates/rsc/shelf-flash.rsc.j2 for a given fetch token.
#
# Usage:
#   scripts/render-shelf-flash.sh <TOKEN> [LABEL] [UPLINK_IF] [EXPIRES_AT]
#
# Prints the rendered RSC script to stdout. Pipe to a file and paste onto the
# router(s) at flash time.
#
# Example — mint a 10-year token and render a shelf-flash script using it:
#   sudo -u cpecloud bash -c 'cd /opt/cpe-cloud && source .venv/bin/activate && \
#     python -m app.cli fetch-tokens mint \
#       --label "shelf-flash batch 2026" --ttl-hours 87600'
#   # copy the printed Token value, then:
#   ./scripts/render-shelf-flash.sh \
#     <TOKEN_VALUE> \
#     "shelf-flash batch 2026" \
#     ether1 \
#     "2036-04-24T00:00:00Z" \
#     > /tmp/shelf-flash-2026.rsc
set -euo pipefail

TOKEN="${1:?token required (arg 1)}"
LABEL="${2:-shelf-flash batch}"
UPLINK_IF="${3:-ether1}"
TOKEN_EXPIRES_AT="${4:-see fetch-tokens list}"

# Pull the server FQDN from the deployed .env if we're running on the server;
# fall back to a sensible default for dev / local rendering.
SERVER_FQDN=""
if [[ -r /opt/cpe-cloud/.env ]]; then
    SERVER_FQDN="$(grep -E '^SERVER_FQDN=' /opt/cpe-cloud/.env | cut -d= -f2- | tr -d '"')"
fi
SERVER_FQDN="${SERVER_FQDN:-mcc.bradfordbroadband.com}"

GENERATED_AT="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$SCRIPT_DIR/../app/templates/rsc/shelf-flash.rsc.j2"

if [[ ! -r "$TEMPLATE" ]]; then
    echo "ERROR: template not found at $TEMPLATE" >&2
    exit 2
fi

# Use `|` as the sed delimiter — base64url tokens can contain `/` but never
# `|`. Replacement strings are plain text, no backref metachars in play.
sed -e "s|{{ fetch_token }}|${TOKEN}|g" \
    -e "s|{{ token_label }}|${LABEL}|g" \
    -e "s|{{ uplink_interface }}|${UPLINK_IF}|g" \
    -e "s|{{ server_fqdn }}|${SERVER_FQDN}|g" \
    -e "s|{{ generated_at }}|${GENERATED_AT}|g" \
    -e "s|{{ token_expires_at }}|${TOKEN_EXPIRES_AT}|g" \
    "$TEMPLATE"
