"""
Helpers for building and authorizing tenant-scoped S3 object keys.

Presigned-upload flows hand an object key to the client and later accept one back.
Without these checks a caller can name a key belonging to another tenant, which
allows both cross-tenant reads (confirm someone else's object into your own record)
and cross-tenant deletes (oversize rejection paths purge the key they were given).

Every flow that builds or accepts an object key must go through this module.
"""

import os
import re
import unicodedata
import uuid

# Anything outside this allowlist is replaced, so a filename can never alter the
# shape of the key it is interpolated into.
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def sanitize_filename(filename, max_length=120, default="file"):
    """
    Reduce an arbitrary client-supplied filename to a safe basename.

    Strips directory components (both separators, so Windows clients are covered),
    control characters, and unicode trickery. `../../t-victim/x.pdf` becomes `x.pdf`.
    """
    if not filename:
        return default

    name = unicodedata.normalize("NFKD", str(filename))
    # Drop every path component before sanitizing, so traversal cannot survive.
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = _CONTROL_CHARS.sub("", name)
    name = _UNSAFE_CHARS.sub("_", name)
    name = name.strip("._")

    if not name:
        return default

    if len(name) > max_length:
        root, ext = os.path.splitext(name)
        root = root[: max(1, max_length - len(ext))]
        name = f"{root}{ext}"

    return name


def tenant_prefix(tenant_id):
    """The key prefix every object belonging to a tenant must sit under."""
    return f"t-{tenant_id}/"


def build_tenant_object_key(tenant_id, folder, filename, unique_id=None):
    """
    Build a collision-free, tenant-scoped object key with a sanitized filename.
    """
    unique_id = unique_id or uuid.uuid4()
    return f"{tenant_prefix(tenant_id)}{folder}/{unique_id}_{sanitize_filename(filename)}"


def is_object_key_owned_by_tenant(object_key, tenant_id, folder=None):
    """
    True only if `object_key` is a well-formed key inside this tenant's namespace.

    Callers must treat a False result as "not found" rather than "forbidden" —
    a 403 would confirm that another tenant's key exists.
    """
    if not object_key or not isinstance(object_key, str):
        return False

    if _CONTROL_CHARS.search(object_key):
        return False

    # Reject anything that could resolve to a different prefix on an S3 backend
    # that normalizes paths (MinIO on a filesystem backend does).
    if "\\" in object_key or "//" in object_key:
        return False

    if any(segment == ".." for segment in object_key.split("/")):
        return False

    expected = tenant_prefix(tenant_id)
    if folder:
        expected = f"{expected}{folder}/"

    return object_key.startswith(expected)
