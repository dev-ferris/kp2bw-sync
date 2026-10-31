#!/usr/bin/env python3
"""Checks the Dockerfile's pinned inputs for updates and rewrites it in place.

Checked:
  ARG KP2BW_VERSION     latest release on PyPI (kp2bw)
  ARG BW_CLI_VERSION    latest release on npm (@bitwarden/cli)
  ARG ESBUILD_VERSION   latest release on npm (esbuild)
  FROM <image>:<tag>    current digest of the tag (base image rebuilds,
                        e.g. security updates); pinned as <image>:<tag>@sha256:…

The image tags themselves (python:3.14-alpine, node:22-alpine) are not changed –
moving to a new Python or Node major version stays a deliberate edit.

Usage: check_updates.py [Dockerfile]
Writes "changed=true|false" and a multi-line "summary" to $GITHUB_OUTPUT.
"""
import json
import os
import re
import subprocess
import sys
import urllib.request

ARG_SOURCES = {
    "KP2BW_VERSION": ("PyPI", "https://pypi.org/pypi/kp2bw/json", lambda d: d["info"]["version"]),
    "BW_CLI_VERSION": ("npm", "https://registry.npmjs.org/@bitwarden/cli/latest", lambda d: d["version"]),
    "ESBUILD_VERSION": ("npm", "https://registry.npmjs.org/esbuild/latest", lambda d: d["version"]),
}
ARG_RE = re.compile(r"^(ARG[ \t]+(\w+)=)(\S+)[ \t]*$", re.M)
FROM_RE = re.compile(r"^(FROM[ \t]+)([^\s@]+:[^\s@]+)(@sha256:[0-9a-f]{64})?([ \t]+AS[ \t]+\S+)?[ \t]*$", re.M | re.I)


def fetch_json(url):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def image_digest(ref):
    """Digest of the multi-arch index behind a tag, via docker buildx imagetools."""
    out = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", ref, "--format", "{{json .Manifest}}"],
        check=True, capture_output=True, text=True,
    ).stdout
    return json.loads(out)["digest"]


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "Dockerfile"
    text = original = open(path, encoding="utf-8").read()
    changes = []

    def bump_arg(match):
        prefix, name, current = match.group(1), match.group(2), match.group(3)
        if name not in ARG_SOURCES:
            return match.group(0)
        source, url, pick = ARG_SOURCES[name]
        latest = pick(fetch_json(url))
        if latest != current:
            changes.append(f"{name}: {current} → {latest} ({source})")
            return f"{prefix}{latest}"
        print(f"{name}: {current} is current")
        return match.group(0)

    text = ARG_RE.sub(bump_arg, text)

    def bump_from(match):
        prefix, ref, pinned, alias = match.group(1), match.group(2), match.group(3), match.group(4) or ""
        digest = image_digest(ref)
        if pinned != f"@{digest}":
            old = pinned[1:20] + "…" if pinned else "unpinned"
            changes.append(f"{ref}: {old} → {digest[:19]}…")
            return f"{prefix}{ref}@{digest}{alias}"
        print(f"{ref}: digest is current")
        return match.group(0)

    text = FROM_RE.sub(bump_from, text)

    if text != original:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    for line in changes:
        print("update:", line)

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"changed={'true' if changes else 'false'}\n")
            fh.write("summary<<EOF\n" + "\n".join(changes) + "\nEOF\n")


if __name__ == "__main__":
    main()
