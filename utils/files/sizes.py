"""Rendering byte counts for people rather than for machines."""


def format_size_limit(max_bytes):
    """Render a byte ceiling the way someone would say it out loud.

    Integer-dividing by a megabyte — ``f"{max_bytes // (1024 * 1024)} MB"`` — reads
    ``At most 0 MB per file.`` for any limit under 1 MB. That is exactly the message a
    tenant sees when an administrator has *tightened* the limit, which is the moment the
    real number matters most.
    """
    if max_bytes >= 1024 * 1024:
        megabytes = max_bytes / (1024 * 1024)
        rendered = int(megabytes) if megabytes.is_integer() else round(megabytes, 1)
        return f"{rendered} MB"
    if max_bytes >= 1024:
        kilobytes = max_bytes / 1024
        rendered = int(kilobytes) if kilobytes.is_integer() else round(kilobytes, 1)
        return f"{rendered} KB"
    return f"{max_bytes} bytes"
