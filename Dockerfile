# syntax=docker/dockerfile:1
# kp2bw needs Python >= 3.14, the Bitwarden CLI needs Node.js >= 22

ARG BW_CLI_VERSION=2026.10.0

# ---------------------------------------------------------------------------
# Stage 1: install the Bitwarden CLI and bundle it into a few files with esbuild.
# Instead of ~8,000 files in node_modules, only bw.js, two small chunks, the
# WebAssembly module and the locale files remain.
# BW_BUNDLE=false copies the CLI unbundled (fallback in case a new bw version
# cannot be bundled).
# ---------------------------------------------------------------------------
FROM node:22-alpine AS bw
ARG BW_CLI_VERSION
ARG ESBUILD_VERSION=0.28.2
ARG BW_BUNDLE=true

RUN npm install -g --omit=dev "@bitwarden/cli@${BW_CLI_VERSION}" \
 && npm install --no-save --prefix /tools "esbuild@${ESBUILD_VERSION}"

RUN set -eu; \
    C=/usr/local/lib/node_modules/@bitwarden/cli; \
    # Clean up first: because of require("./" + chunk), esbuild pulls in every
    # file in the build folder and would otherwise fail on the .map files
    find "$C" -type f \( -name '*.map' -o -name '*.md' -o -name '*.d.ts' -o -name '*.ts' \) -delete; \
    if [ "$BW_BUNDLE" != "true" ]; then \
      cp -a "$C" /opt/bw; \
      exit 0; \
    fi; \
    mkdir -p /opt/bw/build; \
    cp "$C/package.json" /opt/bw/; \
    cp -r "$C/build/locales" /opt/bw/build/; \
    cp "$C"/build/*.wasm /opt/bw/build/; \
    # jsdom resolves this file via require.resolve at load time, it must sit next to the bundle
    cp "$C/node_modules/jsdom/lib/jsdom/living/xhr/xhr-sync-worker.js" /opt/bw/build/; \
    cd "$C/build"; \
    for f in bw.js $(ls [0-9]*.js 2>/dev/null); do \
      /tools/node_modules/.bin/esbuild "$f" --bundle --platform=node --target=node22 \
        --format=cjs --minify --log-level=error \
        --loader:.map=empty --loader:.txt=empty \
        --external:'*.wasm' --external:./xhr-sync-worker.js --external:'./[0-9]*.js' \
        --alias:punycode="$C/node_modules/punycode/punycode.js" \
        --outfile="/opt/bw/build/$f"; \
    done; \
    chmod 755 /opt/bw/build/bw.js; \
    # Smoke test: the bundle must start without node_modules. The proxy pointing to a
    # closed port keeps bw from contacting api.bitwarden.com during the build.
    BITWARDENCLI_APPDATA_DIR=/tmp/bwtest HTTPS_PROXY=http://127.0.0.1:9 \
      node /opt/bw/build/bw.js --version 2>/dev/null

# ---------------------------------------------------------------------------
# Stage 2: runtime image
# ---------------------------------------------------------------------------
FROM python:3.14-alpine

ARG KP2BW_VERSION=3.7.1

# Node.js runtime only (no npm); the base image already ships tzdata.
# --no-compile: no .pyc files (saves ~12 MB); the root filesystem is read-only
# at runtime anyway.
RUN set -eu; \
    apk add --no-cache nodejs; \
    node -e 'process.exit(+process.versions.node.split(".")[0] >= 22 ? 0 : 1)' \
      || { echo "Node.js >= 22 required, found: $(node --version)" >&2; exit 1; }; \
    pip install --no-cache-dir --no-compile "kp2bw==${KP2BW_VERSION}"

COPY --from=bw /opt/bw /opt/bw
RUN ln -s /opt/bw/build/bw.js /usr/local/bin/bw

COPY --chmod=755 entrypoint.sh sync.sh scheduler.py deref.py /usr/local/bin/

# bw session and state live in tmpfs under /tmp
ENV HOME=/tmp \
    BITWARDENCLI_APPDATA_DIR=/tmp/bw \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NODE_OPTIONS=--no-deprecation \
    HEALTH_MAX_MINUTES=60

# Healthy as long as the last run (synced or skipped) is not older than
# HEALTH_MAX_MINUTES; the variable is evaluated at runtime
HEALTHCHECK --interval=5m --timeout=10s --start-period=10m --retries=1 \
  CMD find /tmp/state/last_ok -mmin -"$HEALTH_MAX_MINUTES" | grep -q .

USER 1000:1000
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
