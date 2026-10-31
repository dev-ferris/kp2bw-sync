#!/usr/bin/env python3
"""Resolves KeePass references and placeholders that kp2bw does not resolve.

kp2bw only resolves {REF:U@…} and {REF:P@…} in username and password.
References and placeholders in title, URL, notes and custom fields would
otherwise end up verbatim in Vaultwarden. This script writes a copy of the
database in which these fields are resolved. Username and password are left
untouched so that kp2bw keeps merging entries that share credentials.

The copy is encrypted with the same credentials; UUIDs are preserved, so kp2bw
keeps matching entries correctly on later runs.

Supported (as in KeePass/KeePassXC):
  {REF:<field>@<search>:<text>}
      field:  T title, U username, P password, A URL, N notes, I UUID
      search: I UUID, T/U/P/A/N field content, O custom fields
              (text search: case-insensitive, substring, first match wins)
  {TITLE} {USERNAME} {PASSWORD} {URL} {NOTES} {UUID} {S:<custom field>}
  {T-REPLACE-RX:/text/pattern/replacement/}   (any separator character)
  {T-CONV:/text/upper/}  {T-CONV:/text/lower/}
Placeholders are evaluated recursively. An empty target field becomes an empty
string. Unknown placeholders are kept verbatim, as KeePass does. If a reference
fails, the whole field is left unchanged and the reason is reported.

Usage: deref.py <source.kdbx> <target.kdbx>
Credentials: KP2BW_KEEPASS_PASSWORD, optionally KP2BW_KEEPASS_KEYFILE
"""
import os
import re
import sys
import uuid

from pykeepass import PyKeePass
from pykeepass.exceptions import CredentialsError, HeaderChecksumError, PayloadChecksumError

FIELD = {"T": "title", "U": "username", "P": "password", "A": "url", "N": "notes"}
FIELD_NAME = {"T": "title", "U": "username", "P": "password", "A": "URL", "N": "notes",
              "I": "UUID", "O": "custom fields"}
SIMPLE = {"TITLE": "title", "USERNAME": "username", "PASSWORD": "password",
          "URL": "url", "NOTES": "notes"}
STANDARD_FIELDS = ("title", "url", "notes")
# Standard fields that pykeepass may also list under custom_properties
SKIP_PROPERTIES = {"Title", "UserName", "Password", "URL", "Notes"}
REF_RE = re.compile(r"^REF:([TUPANI])@([TUPANIO]):(.*)$", re.IGNORECASE | re.DOTALL)
MAX_DEPTH = 15


class ResolveError(Exception):
    pass


def find_target(kp, search_in, text):
    if search_in == "I":
        try:
            wanted = uuid.UUID(text.strip())
        except ValueError:
            raise ResolveError(f"invalid UUID '{text}'")
        entry = kp.find_entries(uuid=wanted, first=True)
        if entry is None:
            raise ResolveError(f"no entry with UUID {wanted.hex.upper()}")
        return entry
    needle = text.lower()
    for entry in kp.entries:
        if search_in == "O":
            values = [v for k, v in entry.custom_properties.items() if k not in SKIP_PROPERTIES]
        else:
            values = [getattr(entry, FIELD[search_in])]
        if any(v and needle in str(v).lower() for v in values):
            return entry
    raise ResolveError(f"no entry whose {FIELD_NAME[search_in]} contains '{text}'")


def split_args(raw, count):
    """Splits '/a/b/c/' using the first character as the separator.

    Only splits at the top level, not inside nested {…} placeholders, so that
    e.g. an inserted URL containing '//' does not break the arguments.
    """
    if not raw:
        raise ResolveError("empty parameters")
    sep, parts, current, depth = raw[0], [], [], 0
    for ch in raw[1:]:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if len(parts) < count:
        raise ResolveError(f"expected {count} parameters, found {len(parts)}")
    return parts[:count]


def dotnet_replacement(repl):
    """KeePass uses .NET regex: $1 / ${name} -> Python syntax."""
    repl = repl.replace("\\", "\\\\")
    repl = re.sub(r"\$\{(\w+)\}", r"\\g<\1>", repl)
    return re.sub(r"\$(\d+)", r"\\g<\1>", repl)


class Resolver:
    def __init__(self, kp):
        self.kp = kp
        self.unsupported = set()

    def field(self, entry, attr, depth):
        return self.expand(entry, getattr(entry, attr) or "", depth + 1)

    def placeholder(self, entry, token, depth):
        upper = token.upper()
        if upper in SIMPLE:
            return self.field(entry, SIMPLE[upper], depth)
        if upper == "UUID":
            return entry.uuid.hex.upper()
        if upper.startswith("S:"):
            return self.expand(entry, entry.get_custom_property(token[2:]) or "", depth + 1)

        ref = REF_RE.match(token)
        if ref:
            wanted, search_in = ref.group(1).upper(), ref.group(2).upper()
            text = self.expand(entry, ref.group(3), depth + 1)
            target = find_target(self.kp, search_in, text)
            if wanted == "I":
                return target.uuid.hex.upper()
            # The referenced value is evaluated in the context of the target entry
            return self.field(target, FIELD[wanted], depth)

        if upper.startswith("T-REPLACE-RX:"):
            text, pattern, repl = split_args(token[len("T-REPLACE-RX:"):], 3)
            text = self.expand(entry, text, depth + 1)
            pattern = self.expand(entry, pattern, depth + 1)
            repl = self.expand(entry, repl, depth + 1)
            try:
                return re.sub(pattern, dotnet_replacement(repl), text)
            except re.error as exc:
                raise ResolveError(f"invalid pattern '{pattern}': {exc}")

        if upper.startswith("T-CONV:"):
            text, kind = split_args(token[len("T-CONV:"):], 2)
            text = self.expand(entry, text, depth + 1)
            kind = kind.lower()
            if kind in ("u", "upper"):
                return text.upper()
            if kind in ("l", "lower"):
                return text.lower()
            raise ResolveError(f"T-CONV type '{kind}' not supported")

        # Unknown (or not a placeholder at all, e.g. JSON in notes): keep it
        # verbatim like KeePass does and report it once at the end
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*(:.*)?", token, re.DOTALL):
            self.unsupported.add("{" + token.split(":", 1)[0].upper() + "}")
        return "{" + token + "}"

    def expand(self, entry, text, depth=0):
        if depth > MAX_DEPTH:
            raise ResolveError("reference chain too long or circular")
        if not text or "{" not in text:
            return text
        out, i = [], 0
        while i < len(text):
            ch = text[i]
            if ch != "{":
                out.append(ch)
                i += 1
                continue
            # find the matching closing brace (nested)
            level, j = 0, i
            while j < len(text):
                if text[j] == "{":
                    level += 1
                elif text[j] == "}":
                    level -= 1
                    if level == 0:
                        break
                j += 1
            if j >= len(text):
                out.append(text[i:])  # no closing brace: keep verbatim
                break
            out.append(self.placeholder(entry, text[i + 1:j], depth))
            i = j + 1
        return "".join(out)


def describe(entry):
    path = "/".join(p for p in (entry.group.path if entry.group is not None else []) if p)
    return f"{path}/[{entry.uuid.hex.upper()}]" if path else f"[{entry.uuid.hex.upper()}]"


def main():
    if len(sys.argv) != 3:
        sys.exit("usage: deref.py <source.kdbx> <target.kdbx>")
    source, target = sys.argv[1], sys.argv[2]

    try:
        kp = PyKeePass(
            source,
            password=os.environ.get("KP2BW_KEEPASS_PASSWORD") or None,
            keyfile=os.environ.get("KP2BW_KEEPASS_KEYFILE") or None,
        )
    except CredentialsError:
        sys.exit("ERROR: wrong KeePass password or key file")
    except PayloadChecksumError:
        sys.exit("ERROR: KeePass file content is incomplete or corrupted "
                 "(checksum mismatch, credentials are correct). Usually the file was "
                 "being written while it was read; otherwise open it in KeePassXC and check.")
    except HeaderChecksumError as exc:
        sys.exit(f"ERROR: not a valid KeePass file ({exc})")
    resolver = Resolver(kp)

    # Compute all new values first, then write: this way every reference sees
    # the original data, regardless of the order of the entries.
    updates, failed = [], 0
    for entry in kp.entries:
        fields = [(f, getattr(entry, f), None) for f in STANDARD_FIELDS]
        fields += [(k, v, entry.is_custom_property_protected(k))
                   for k, v in entry.custom_properties.items() if k not in SKIP_PROPERTIES]
        for name, raw, protected in fields:
            if not raw or "{" not in raw:
                continue
            try:
                new = resolver.expand(entry, raw)
            except ResolveError as exc:
                failed += 1
                print(f"WARNING: {describe(entry)} field '{name}': {exc}", file=sys.stderr)
                continue
            if new != raw:
                updates.append((entry, name, new, protected))

    for entry, name, new, protected in updates:
        if protected is None:
            setattr(entry, name, new)
        else:
            entry.set_custom_property(name, new, protect=protected)

    kp.save(target)
    print(f"References/placeholders resolved: {len(updates)} fields, unresolvable: {failed}")
    if resolver.unsupported:
        print("Note: kept verbatim: " + ", ".join(sorted(resolver.unsupported)))


if __name__ == "__main__":
    main()
