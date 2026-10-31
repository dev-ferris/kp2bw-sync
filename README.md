# kp2bw-sync

A hardened container that keeps a [Vaultwarden](https://github.com/dani-garcia/vaultwarden)
vault in sync with a KeePass database (`.kdbx`) on a schedule.

Sync is **one-way: KeePass → Vaultwarden**. KeePass stays the source of truth;
Vaultwarden gives you the Bitwarden apps and browser extensions on top. The heavy
lifting is done by [kp2bw](https://github.com/kjanat/kp2bw) and the Bitwarden CLI;
this image wraps them for unattended operation.

## Contents

- [Features](#features)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Complete docker-compose.yml](#complete-docker-composeyml)
- [Secrets](#secrets)
- [Configuration reference](#configuration-reference)
- [Network setup](#network-setup)
- [Custom CA certificate](#custom-ca-certificate)
- [Operation](#operation)
- [How kp2bw handles entries](#how-kp2bw-handles-entries)
- [Troubleshooting](#troubleshooting)
- [Files](#files)
- [Related projects](#related-projects)
- [Acknowledgements](#acknowledgements)

## Features

- **Built-in scheduler** (cron syntax). Runs never overlap.
- **Change detection.** A run is skipped if the `.kdbx` has not changed since the
  last successful sync, so Vaultwarden only sees traffic when there is something to do.
- **Consistent reads.** The database is copied into a RAM-backed snapshot and
  compared with the original. If it is currently being written (KeePassXC saving,
  Remote Sync uploading), the run waits and retries instead of reading a torn file.
- **References and placeholders.** kp2bw itself only resolves `{REF:U@…}` and
  `{REF:P@…}` in username and password. Before the import, `deref.py` resolves
  title, URL, notes and custom fields as KeePass/KeePassXC would:
  `{REF:…}` (all search modes), `{TITLE}`, `{USERNAME}`, `{PASSWORD}`, `{URL}`,
  `{NOTES}`, `{UUID}`, `{S:field}`, `{T-REPLACE-RX:…}`, `{T-CONV:…}`.
  Entry UUIDs are preserved, so kp2bw keeps matching entries on later runs.
- **Hardened.** Runs as UID 1000 with `cap_drop: ALL` and
  `no-new-privileges`; state lives in tmpfs only. Optionally `read_only` (with
  file-based secrets, see [Secrets](#secrets)). Passwords are passed only to the two programs that need
  them, never to the container environment; the Bitwarden CLI is locked after
  every run.
- **Small image.** Alpine base, Bitwarden CLI bundled with esbuild, available
  for `linux/amd64` and `linux/arm64`.
- **Healthcheck.** Reports `unhealthy` if no run has succeeded for
  `HEALTH_MAX_MINUTES`.

## How it works

Each scheduled run (`sync.sh`):

1. takes a stable snapshot of the `.kdbx` into tmpfs,
2. skips the run if the snapshot's hash matches the last successful run,
3. logs the Bitwarden CLI in with the API key (once per container start),
4. writes an encrypted copy with resolved references/placeholders (`deref.py`),
5. runs kp2bw against that copy, which unlocks the vault with the master password
   and creates/updates items through a local `bw serve`,
6. locks the CLI again and records the hash.

## Requirements

- **Vaultwarden reachable over HTTPS.** The Bitwarden CLI refuses plain HTTP,
  even inside a Docker network. Put Vaultwarden behind your reverse proxy and let
  the container talk to the proxy (see [Network setup](#network-setup)).
- **A Vaultwarden account with an API key** (web vault → *Settings → Security →
  Keys → View API key*) and its master password.
- **The KeePass database password** (and key file, if you use one).
- If Vaultwarden uses a certificate from your own CA: the CA's **root
  certificate** as a PEM file.
- Docker Compose with support for `secrets.<name>.environment`.

## Quick start

```bash
git clone https://github.com/dev-ferris/kp2bw-sync.git
cd kp2bw-sync
cp .env.example .env    # fill in the values
docker login ghcr.io    # once – the package is private (token with read:packages)
docker compose up -d
docker compose logs -f kp2bw
```

The compose file uses the image built by the GitHub Actions workflow,
`ghcr.io/dev-ferris/kp2bw-sync` (`linux/amd64` and `linux/arm64`; tags `latest`,
`kp2bw-<version>`, `sha-<commit>`). It is also published to Docker Hub as
`<user>/kp2bw-sync`. Use `KP2BW_IMAGE` to pick another tag or registry.
With `pull_policy: always`, `docker compose up -d` fetches the newest image.

A successful first run ends with `Sync completed successfully`.

## Complete docker-compose.yml

This is a complete, self-contained example. It includes the secrets, the
network and an optional CA certificate. The `docker-compose.yml` in this
repository is the same file with `${…}` defaults for every value.

```yaml
name: kp2bw

services:
  kp2bw:
    image: ghcr.io/dev-ferris/kp2bw-sync:latest
    pull_policy: always
    container_name: kp2bw
    restart: unless-stopped
    init: true
    user: "1000:1000"                          # must be able to read the .kdbx

    environment:
      BW_SERVER: https://vault.example.lan     # HTTPS URL of Vaultwarden
      KEEPASS_FILE: db.kdbx                    # file name inside KEEPASS_DIR
      CRON_SCHEDULE: "*/15 * * * *"
      RUN_ON_START: "true"
      KP2BW_ARGS: "-v --skip-expired --no-include-recycle-bin"
      KP2BW_HTTP_TIMEOUT: "300"
      RESOLVE_REFS: "true"
      SETTLE_SECONDS: "3"
      HEALTH_MAX_MINUTES: "60"
      TZ: UTC

    volumes:
      # The directory, not the file: KeePassXC replaces the file on save
      - /path/to/keepass:/data:ro
      # Optional: root certificate of your own CA (remove if Vaultwarden
      # uses a publicly trusted certificate)
      - /path/to/root_ca.crt:/certs/root_ca.crt:ro

    secrets:
      - keepass_password
      - bw_master_password
      - bw_clientid
      - bw_clientsecret
      # - keepass_keyfile

    # read_only: true is not possible with secrets from variables –
    # see "Read-only root filesystem" under Secrets
    tmpfs:
      - /tmp:uid=1000,gid=1000,mode=0700
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true

    networks:
      - vaultwarden

networks:
  vaultwarden:
    external: true
    name: vaultwarden_internal   # network shared with Vaultwarden and the reverse proxy

secrets:
  # Read from variables of the same name in .env or your stack manager
  keepass_password:
    environment: KEEPASS_PASSWORD
  bw_master_password:
    environment: BW_MASTER_PASSWORD
  bw_clientid:
    environment: BW_CLIENTID
  bw_clientsecret:
    environment: BW_CLIENTSECRET
  # A key file is binary and has to be a file
  # keepass_keyfile:
  #   file: ./secrets/keepass.keyx
```

With the matching `.env` next to it:

```dotenv
KEEPASS_PASSWORD='your-keepass-password'
BW_MASTER_PASSWORD='your-vaultwarden-master-password'
BW_CLIENTID='user.xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx'
BW_CLIENTSECRET='your-client-secret'
```

## Secrets

The container expects four secrets as files under `/run/secrets/`:

| File | Content |
|---|---|
| `keepass_password` | Password of the KeePass database |
| `bw_master_password` | Master password of the Vaultwarden account |
| `bw_clientid` | API key `client_id` (starts with `user.`) |
| `bw_clientsecret` | API key `client_secret` |
| `keepass_keyfile` | *optional* – KeePass key file |

How Compose fills them is up to you:

**From variables** (as shown above). The value comes from `.env` or from your
stack manager (Portainer, Dockhand, …), where it can usually be marked as
secret. Inside the container it appears only as a file – not in the
environment and not in `docker inspect`. Use single quotes in `.env` so `$`
and `#` are not interpreted, and reference the **variable name** without
`${…}`:

```yaml
secrets:
  bw_master_password:
    environment: BW_MASTER_PASSWORD     # correct
  # environment: ${BW_MASTER_PASSWORD}  # wrong – Compose would look for a
  #                                       variable named after your password
```

**From files** on the host:

```yaml
secrets:
  bw_master_password:
    file: ./secrets/bw_master_password
```

```bash
mkdir -p secrets && chmod 700 secrets
printf '%s' 'your-password' > secrets/bw_master_password
chmod 600 secrets/*            # owned by the UID the container runs as
```

### Read-only root filesystem

Compose provides secrets sourced from **variables** by copying them into the
container's filesystem when the container is created. That is not possible on a
read-only root filesystem, so Compose rejects the combination:

```
cannot create secret "…" in read-only service kp2bw: `file` is the sole supported option
```

Pick one:

| | Secrets from variables | Secrets from files |
|---|---|---|
| `read_only: true` | not possible | possible |
| Values managed in | `.env` / stack manager (can be masked) | files on the host (`chmod 600`) |
| Plaintext at rest | copy in the container layer on the host (root-only) | the files on the host |

The default `docker-compose.yml` uses secrets from variables without
`read_only`. The container still runs as an unprivileged user with all
capabilities dropped, so without `read_only` it can only write to
world-writable paths and its own tmpfs – not to the image's system files.

Read-only variant with file-based secrets:

```yaml
services:
  kp2bw:
    # … as above …
    read_only: true
    secrets:
      - keepass_password
      - bw_master_password
      - bw_clientid
      - bw_clientsecret

secrets:
  keepass_password:
    file: ./secrets/keepass_password
  bw_master_password:
    file: ./secrets/bw_master_password
  bw_clientid:
    file: ./secrets/bw_clientid
  bw_clientsecret:
    file: ./secrets/bw_clientsecret
```

The files are bind-mounted as they are, so they must be readable by the UID the
container runs as (`PUID`, default 1000). `uid`, `gid` and `mode` on secrets only
take effect in Swarm mode and are ignored by plain Compose.

**Do not** pass the passwords as plain `environment:` entries of the service
(e.g. `BW_MASTER_PASSWORD: ${BW_MASTER_PASSWORD}`). That puts them into the
container configuration, where they are visible in `docker inspect` even if
your stack manager masks them in its UI.

## Configuration reference

### Required

| Variable | Description |
|---|---|
| `BW_SERVER` | HTTPS URL of Vaultwarden |
| `KEEPASS_DIR` | Directory on the host containing the `.kdbx` (mounted read-only to `/data`) |
| `KEEPASS_FILE` | File name of the `.kdbx` |
| `KEEPASS_PASSWORD` | KeePass password (secret) |
| `BW_MASTER_PASSWORD` | Vaultwarden master password (secret) |
| `BW_CLIENTID` / `BW_CLIENTSECRET` | Vaultwarden API key (secrets) |

### Optional

| Variable | Default | Description |
|---|---|---|
| `CA_CERT` | – | Path to the root certificate (PEM) of your own CA on the host; if unset, only public CAs are trusted |
| `CRON_SCHEDULE` | `*/15 * * * *` | Sync schedule |
| `RUN_ON_START` | `true` | Sync once right after start |
| `HEALTH_MAX_MINUTES` | `60` | Healthcheck threshold; must be longer than the schedule interval |
| `KP2BW_ARGS` | `-v --skip-expired --no-include-recycle-bin` | Options passed to kp2bw |
| `KP2BW_HTTP_TIMEOUT` | `300` | Timeout for requests to Vaultwarden (seconds) |
| `RESOLVE_REFS` | `true` | Resolve references/placeholders before the import |
| `SETTLE_SECONDS` | `3` | Delay used to detect a `.kdbx` that is being written |
| `VAULTWARDEN_NETWORK` | `vaultwarden_internal` | External network shared with Vaultwarden and the reverse proxy |
| `PUID` / `PGID` | `1000` | User the container runs as; must be able to read the `.kdbx` |
| `TZ` | `UTC` | Time zone for logs and schedule |
| `KP2BW_IMAGE` | `ghcr.io/dev-ferris/kp2bw-sync:latest` | Image to run, e.g. a fixed tag `kp2bw-3.7.1` or the Docker Hub image |

Useful kp2bw options for `KP2BW_ARGS`: `--skip-expired`,
`--no-include-recycle-bin`, `-t tag1,tag2` (only entries with these tags),
`--force-update` (KeePass wins over edits made in Vaultwarden).

## Network setup

The Bitwarden CLI requires HTTPS, so the container cannot talk to Vaultwarden's
plain HTTP port directly. The recommended layout keeps everything inside Docker:

- Vaultwarden and kp2bw share an internal network (`internal: true`).
- The reverse proxy is attached to the same network and has the **Vaultwarden
  hostname as a network alias** there.

kp2bw then resolves the hostname to the proxy inside Docker, gets a certificate
that matches the name, and needs no route to the LAN or the internet.

Example for the reverse proxy (Caddy shown, any proxy works):

```yaml
services:
  caddy:
    networks:
      vaultwarden_internal:
        aliases:
          - vault.example.lan    # exactly the hostname from BW_SERVER
      # … other networks of the proxy, also in map form

networks:
  vaultwarden_internal:
    external: true
```

The internal network itself is easiest to create once, independent of all
stacks:

```bash
docker network create --internal vaultwarden_internal
```

Notes:

- The alias must match the hostname in `BW_SERVER` **exactly**.
- As soon as one network of a service has options such as `aliases`, all of
  that service's networks must use the map form (`name:` / `name: {}`).
- Aliases only take effect after the proxy container is recreated.
- If the hostname resolves to an address outside the internal network (e.g. a
  macvlan IP of the proxy), the connection fails with `EHOSTUNREACH`.

## Custom CA certificate

If Vaultwarden's certificate is issued by your own CA (step-ca, an internal PKI,
…), mount the CA's **root** certificate as PEM:

```yaml
volumes:
  - /path/to/root_ca.crt:/certs/root_ca.crt:ro
```

or set `CA_CERT=/path/to/root_ca.crt` when using the repository's compose file.

Every `*.crt` and `*.pem` file in `/certs` inside the container is trusted in
addition to the public CAs, whatever its name. So instead of a single file you
can also mount a directory or a volume there – e.g. the directory of your CA
that holds the public certificates:

```yaml
volumes:
  - type: volume
    source: ca_certs          # external volume with the public certificates
    target: /certs
    read_only: true
    volume:
      subpath: certs          # optional: only this sub-directory (Docker ≥ 26)
```

In the repository's `docker-compose.yml`, replace the `${CA_CERT:-/dev/null}`
line with such a mount. Only public certificates are needed – never mount a
directory that also contains the CA's private keys.

The log lists every certificate that is used, e.g.
`Additionally trusting /certs/root_ca.crt`. If the path in `CA_CERT` does not
exist, Docker silently creates an empty directory in its place; the container
then reports `/certs/root_ca.crt is a directory – CA_CERT points to a file that
does not exist`.

## Operation

```bash
docker exec kp2bw sync.sh --force               # sync now, even without changes
docker compose logs kp2bw | grep -E "WARNING|ERROR"
docker exec kp2bw sh -c 'wc -c /run/secrets/*'  # secrets present? (length only)
```

kp2bw's detailed debug log lives in tmpfs at `/tmp/.local/state/kp2bw/logs/`
and is gone after a restart. Copy it out before restarting if you need it:

```bash
docker cp kp2bw:/tmp/.local/state/kp2bw/logs/ .
```

**KeePassXC Remote Sync.** If you sync the `.kdbx` to a share with KeePassXC's
Remote Sync, upload atomically so readers never see a half-written file:

```sh
sh -c 'cp {TEMP_DATABASE} /share/db.kdbx.tmp && mv /share/db.kdbx.tmp /share/db.kdbx'
```

## How kp2bw handles entries

- Entries are matched by their KeePass UUID (`KP2BW_ID` field). Changed entries
  are updated in place; no duplicates are created.
- Updates are based on content (name, notes, fields, login), not timestamps.
  Unchanged entries cause no writes.
- Items edited manually in Vaultwarden are not overwritten (`KP2BW_SYNC`
  signature). Add `--force-update` to `KP2BW_ARGS` once to let KeePass win.
- **Deletions are not synced.**
- Entries that share credentials via `{REF:U@…}`/`{REF:P@…}` are merged into a
  single Vaultwarden item with multiple URIs.
- Expired entries and the recycle bin are skipped with the default `KP2BW_ARGS`.

## Troubleshooting

| Message | Cause / fix |
|---|---|
| `Insecure URL not allowed` | `BW_SERVER` uses HTTP – the CLI requires HTTPS |
| `getaddrinfo ENOTFOUND <host>` | Hostname cannot be resolved; check `BW_SERVER` and the proxy alias |
| `connect EHOSTUNREACH` | kp2bw is not in the network where the proxy has the alias, or the alias differs from the hostname |
| `unable to get local issuer certificate` | Root certificate missing or wrong; the log must show `Additionally trusting /certs/…` |
| `… is a directory – CA_CERT points to a file that does not exist` | Wrong path in `CA_CERT` – Docker created an empty directory instead |
| `api.bitwarden.com … EAI_AGAIN` / `ETIMEDOUT` | Harmless – the CLI queries its default server once before the custom server is set |
| `invalid_client` | `BW_CLIENTID` / `BW_CLIENTSECRET` wrong or rotated |
| `secret '…' is missing or not readable` | Secret not provided, or owned by a different UID than the container |
| `wrong KeePass password or key file` | Check `KEEPASS_PASSWORD` / key file |
| `KeePass file content is incomplete or corrupted` | The file was read while being written; use atomic uploads (see above). Otherwise check it in KeePassXC |
| `field '…': no entry with UUID …` | A reference points to an entry that no longer exists |
| `environment variable "…" required by secret … is not set` | Variable missing, or `${…}` used under `secrets:` instead of the plain name |
| `file is the sole supported option` | `read_only: true` together with secrets from variables – see [Read-only root filesystem](#read-only-root-filesystem) |

## Files

| File | Purpose |
|---|---|
| `Dockerfile` | Multi-stage build: bundled Bitwarden CLI + kp2bw on Python Alpine |
| `docker-compose.yml` | Service, network, secrets, all values configurable via `.env` |
| `.env.example` | Template for `.env` |
| `entrypoint.sh` | Initial run, then hands over to the scheduler |
| `scheduler.py` | Minimal cron scheduler without overlapping runs |
| `sync.sh` | One run: snapshot, change detection, login, deref, kp2bw |
| `deref.py` | Resolves references/placeholders into an encrypted copy |
| `.github/workflows/docker.yml` | Multi-arch build and publish |
| `.github/workflows/dockerhub-readme.yml` | Publishes this README as the Docker Hub overview |
| `.github/workflows/check-updates.yml` | Weekly check for new kp2bw/Bitwarden CLI/esbuild versions and base image updates; updates the Dockerfile and starts the build |
| `.github/scripts/check_updates.py` | Update check used by that workflow |

## Related projects

- [kp2bw](https://github.com/kjanat/kp2bw) – the KeePass → Bitwarden migration
  tool this container runs
- [KeePassXC](https://github.com/keepassxreboot/keepassxc) – cross-platform
  KeePass client, e.g. to maintain the source database
- [Vaultwarden](https://github.com/dani-garcia/vaultwarden) – the
  Bitwarden-compatible server the entries are synced to
- [Bitwarden CLI](https://github.com/bitwarden/clients) – used by kp2bw to talk
  to Vaultwarden
- [pykeepass](https://github.com/libkeepass/pykeepass) – used by `deref.py` to
  read and write the KeePass database

## Acknowledgements

This repository – the container setup, scripts, workflow and documentation –
was created with the assistance of [Claude](https://claude.ai) by Anthropic.
