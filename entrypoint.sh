#!/bin/sh
# Starts the schedule. With arguments, runs that command instead,
# e.g.: docker compose run --rm kp2bw sync.sh --force
set -eu

if [ "$#" -gt 0 ]; then
  exec "$@"
fi

: "${BW_SERVER:?BW_SERVER is not set}"
: "${KEEPASS_FILE:?KEEPASS_FILE is not set}"
: "${CRON_SCHEDULE:?CRON_SCHEDULE is not set}"

mkdir -p /tmp/state

if [ "${RUN_ON_START:-true}" = "true" ]; then
  # leading newline: bw error messages end without one
  /usr/local/bin/sync.sh || printf '\n%s\n' "Initial run failed, the schedule keeps running" >&2
fi

# Runs never overlap: a slot that comes up while a sync is running is skipped
exec python3 /usr/local/bin/scheduler.py "$CRON_SCHEDULE" /usr/local/bin/sync.sh
