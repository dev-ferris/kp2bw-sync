#!/bin/sh
# One sync run KeePass -> Vaultwarden.
# Skipped if the .kdbx has not changed since the last successful run.
# Force a run: sync.sh --force
set -eu

SECRETS=/run/secrets
STATE=/tmp/state
KDBX="/data/${KEEPASS_FILE}"
# Certificates of your own CA: every *.crt / *.pem in this directory is trusted
# in addition to the public CAs (docker-compose.yml mounts CA_CERT here)
CERT_DIR=/certs

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }

secret() {
  if [ ! -r "$SECRETS/$1" ]; then
    log "ERROR: secret '$1' is missing or not readable (check file permissions / PUID)" >&2
    exit 1
  fi
  # $(...) strips trailing newlines
  cat "$SECRETS/$1"
}

mkdir -p "$STATE"

# Prevent concurrent runs (schedule + manual call via docker exec)
exec 9>"$STATE/lock"
if ! flock -n 9; then
  log "A run is already in progress, skipped"
  exit 0
fi

if [ ! -r "$KDBX" ]; then
  log "ERROR: $KDBX is not readable (check KEEPASS_DIR / KEEPASS_FILE / permissions)" >&2
  exit 1
fi

# Take a snapshot of the file into tmpfs and work only with that from here on.
# If the copy does not match the original after a short delay, the file is
# currently being written (KeePassXC saving, Remote Sync copying) – wait and
# retry instead of reading a half-written file.
SNAP="$STATE/source.kdbx"
WORK="$STATE/work.kdbx"
trap 'rm -f "$SNAP" "$WORK"' EXIT
STABLE=false
for attempt in 1 2 3 4 5; do
  cp "$KDBX" "$SNAP"
  sleep "${SETTLE_SECONDS:-3}"
  if cmp -s "$KDBX" "$SNAP"; then
    STABLE=true
    break
  fi
  log "KeePass file is being written, waiting (attempt $attempt/5)"
  sleep 10
done
if [ "$STABLE" != "true" ]; then
  log "ERROR: KeePass file keeps changing, run aborted" >&2
  exit 1
fi

HASH="$(sha256sum "$SNAP" | cut -d' ' -f1)"
if [ "${1:-}" != "--force" ] && [ -f "$STATE/last_hash" ] && [ "$(cat "$STATE/last_hash")" = "$HASH" ]; then
  log "KeePass file unchanged, skipped"
  touch "$STATE/last_ok"
  exit 0
fi

# Collect all certificates from CERT_DIR into one bundle (NODE_EXTRA_CA_CERTS
# accepts a single file). Without any, only the public CAs are trusted.
BUNDLE="$STATE/ca-bundle.pem"
: > "$BUNDLE"
for f in "$CERT_DIR"/*.crt "$CERT_DIR"/*.pem; do
  if [ -d "$f" ]; then
    # Docker creates an empty directory if the source file does not exist
    log "WARNING: $f is a directory – CA_CERT points to a file that does not exist" >&2
  elif [ -f "$f" ] && [ -s "$f" ]; then   # skips /dev/null (nothing configured)
    if grep -q -- "-----BEGIN CERTIFICATE-----" "$f"; then
      cat "$f" >> "$BUNDLE"
      echo >> "$BUNDLE"
      log "Additionally trusting $f"
    else
      log "WARNING: $f does not contain a PEM certificate, ignored" >&2
    fi
  fi
done
if [ -s "$BUNDLE" ]; then
  export NODE_EXTRA_CA_CERTS="$BUNDLE"
fi

# Log in the bw CLI if there is no session yet (first run after container start)
STATUS="$(bw status 2>/dev/null | sed -n 's/.*"status":"\([a-z]*\)".*/\1/p')"
if [ "$STATUS" = "unauthenticated" ] || [ -z "$STATUS" ]; then
  log "Logging in bw CLI to $BW_SERVER"
  bw config server "$BW_SERVER" >/dev/null
  BW_CLIENTID="$(secret bw_clientid)" \
  BW_CLIENTSECRET="$(secret bw_clientsecret)" \
    bw login --apikey >/dev/null
fi

export KP2BW_KEEPASS_FILE="$SNAP"
# Keep passwords in plain shell variables (not exported) and hand them only to
# the two programs that need them – not to every child process such as bw/node
KP_PW="$(secret keepass_password)"
BW_PW="$(secret bw_master_password)"
if [ -r "$SECRETS/keepass_keyfile" ]; then
  export KP2BW_KEEPASS_KEYFILE="$SECRETS/keepass_keyfile"
fi

# Resolve references in title, URL, notes and custom fields (kp2bw only handles
# username/password). The copy stays encrypted and lives in tmpfs.
if [ "${RESOLVE_REFS:-true}" = "true" ]; then
  if ! KP2BW_KEEPASS_PASSWORD="$KP_PW" python3 /usr/local/bin/deref.py "$SNAP" "$WORK"; then
    log "ERROR: preprocessing (deref.py) failed, run aborted" >&2
    exit 1
  fi
  export KP2BW_KEEPASS_FILE="$WORK"
fi

log "Starting kp2bw"
RC=0
# KP2BW_ARGS intentionally unquoted so that multiple options are split
# shellcheck disable=SC2086
KP2BW_KEEPASS_PASSWORD="$KP_PW" KP2BW_BITWARDEN_PASSWORD="$BW_PW" \
  kp2bw -y ${KP2BW_ARGS:-} || RC=$?
unset KP_PW BW_PW

bw lock >/dev/null 2>&1 || true

if [ "$RC" -ne 0 ]; then
  log "ERROR: kp2bw exited with status $RC" >&2
  exit "$RC"
fi

echo "$HASH" > "$STATE/last_hash"
touch "$STATE/last_ok"
log "Sync completed successfully"
