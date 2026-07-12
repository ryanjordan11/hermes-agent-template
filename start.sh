#!/bin/bash
set -e

# Mirror dashboard-ref-only's startup: create every directory OneHub expects
# and seed a default config.yaml if the volume is empty. Without these,
# `hermes dashboard` endpoints that hit logs/, sessions/, cron/, etc. can fail
# with opaque errors even though no auth is actually involved.
# NOTE (OneHub >= v2026.7.1): several dirs were consolidated and are now
# resolved via get_hermes_dir("<new>", "<old>"), which returns the NEW path
# unless the OLD one already has *content*. Seeding an empty legacy stub no
# longer "claims" it — OneHub ignores empty stubs and writes to the new path
# (upstream #27602). So we seed the NEW paths: pairing -> platforms/pairing,
# image_cache -> cache/images, audio_cache -> cache/audio. A populated legacy
# dir from a pre-v2026.7.1 deploy still wins on both sides, so no migration is
# needed. server.py:_resolve_pairing_dir() mirrors this same rule for the
# admin panel's Users tab — keep the two in sync on future bumps.
mkdir -p /data/.onehub/cron /data/.onehub/sessions /data/.onehub/logs \
         /data/.onehub/memories /data/.onehub/skills /data/.onehub/platforms/pairing \
         /data/.onehub/hooks /data/.onehub/cache/images /data/.onehub/cache/audio \
         /data/.onehub/workspace /data/.onehub/skins /data/.onehub/plans \
         /data/.onehub/home

# Stamp the install method as "docker" so OneHub treats this as an immutable
# container image, not a pip checkout. OneHub's detect_install_method() reads
# $ONEHUB_HOME/.install_method FIRST (before any .git / pip fallback). Without
# this stamp the template falls through to "pip" — because the Dockerfile strips
# /opt/onehub-agent/.git — and the dashboard's "Update OneHub" button then runs
# a real `hermes update` (PyPI pip-upgrade) INSIDE the running container. That
# upgrade is ephemeral (reverts on the next redeploy) and can desync the Python
# package from the image's pre-built web_dist/ui-tui bundles. Stamping "docker"
# makes that button correctly refuse with "pull a fresh image / redeploy", which
# matches the real upgrade path here (bump ONEHUB_REF in Railway + redeploy).
# Written unconditionally each boot so it stays correct and self-heals.
printf 'docker\n' > /data/.onehub/.install_method

if [ ! -f /data/.onehub/config.yaml ] && [ -f /opt/onehub-agent/cli-config.yaml.example ]; then
  cp /opt/onehub-agent/cli-config.yaml.example /data/.onehub/config.yaml
fi

[ ! -f /data/.onehub/.env ] && touch /data/.onehub/.env

# Bootstrap OAuth tokens from env var (e.g. xAI Grok SuperGrok).
# Set ONEHUB_AUTH_JSON_BOOTSTRAP to the contents of a locally-generated
# ~/.onehub/auth.json. Written only once — subsequent token refreshes update
# the file in place on the persistent volume.
if [ ! -f /data/.onehub/auth.json ] && [ -n "${ONEHUB_AUTH_JSON_BOOTSTRAP}" ]; then
  printf '%s' "${ONEHUB_AUTH_JSON_BOOTSTRAP}" > /data/.onehub/auth.json
  chmod 600 /data/.onehub/auth.json
fi

# Clear any stale gateway PID file left over from the previous container.
# `hermes gateway` writes /data/.onehub/gateway.pid on start but does not
# remove it on SIGTERM. Since /data is a persistent volume, the file
# survives container restarts and causes every subsequent boot to exit with
# "ERROR gateway.run: PID file race lost to another gateway instance".
# No OneHub process can be running at this point (we're pre-exec in a fresh
# container), so removing the file unconditionally is safe.
rm -f /data/.onehub/gateway.pid

exec python /app/server.py
