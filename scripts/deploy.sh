#!/usr/bin/env bash
# Deploy a fresh pull of /opt/cpe-cloud on the server.
#
# Usage:
#   sudo -u cpecloud /opt/cpe-cloud/scripts/deploy.sh
#
# This is a one-shot updater: pull, install new deps, run migrations, restart
# the service. Run as the cpecloud user (the service account) so ownership
# stays clean. Safe to re-run — every step is idempotent.
set -eu

APP_DIR="/opt/cpe-cloud"
cd "$APP_DIR"

echo "==> whoami: $(whoami)"
if [[ "$(whoami)" != "cpecloud" ]]; then
    echo "!! must run as 'cpecloud' user (use: sudo -u cpecloud $0)" >&2
    exit 1
fi

echo "==> git status (should be clean before pull)"
git status --short
if [[ -n "$(git status --porcelain)" ]]; then
    echo "!! working tree has local changes — commit or stash them first" >&2
    exit 1
fi

echo "==> git pull"
git pull --ff-only

echo "==> pip install"
# shellcheck source=/dev/null
source .venv/bin/activate
pip install -q -r requirements.txt

echo "==> alembic upgrade head"
alembic upgrade head

echo "==> restart systemd unit"
# cpecloud has a sudoers drop-in for JUST this restart command (INSTALL.md §10)
sudo -n systemctl restart cpe-cloud.service

echo "==> systemctl status"
systemctl is-active cpe-cloud.service
echo "==> deploy complete"
