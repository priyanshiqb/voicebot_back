"""
Identify a file from its leading bytes, and say whether that contradicts its extension.

Every upload flow trusts the client twice: for the declared ``content_type``, which is
signed into the ticket but never checked against anything, and for the filename's
extension, which is allowlisted but is just a string. Neither says what is actually in
the file. A ``.png`` holding a PDF passes both.

The rule this module implements is deliberately asymmetric:

* If the bytes positively identify a type that **contradicts** the extension, reject.
* If the bytes identify nothing we know, **allow**.

Being unable to recognise a file is not evidence against it. Plain text and CSV have no
signature at all, and a strict allowlist would reject every legitimate one — so the
check only fires when it can name what the file really is. That keeps false rejections
at roughly zero while still catching the case that matters: a file pretending to be
something it is not.

This is not malware scanning and does not replace it. It answers "is this the kind of
file you said it was", not "is this file safe".
"""

#: Families that share a container format and cannot be told apart from their first
#: bytes, so a match on any member satisfies all of them.
#:
#: ``docx``/``xlsx`` are ZIP archives — the difference is a path inside the archive, and
#: opening it here would mean parsing untrusted zip structure to answer a question the
#: extension already answers well enough. ``mp4``/``mov`` share the ISO base media
#: container and differ only in an optional brand.
_EQUIVALENT = {
    "zip": {"zip", "docx", "xlsx"},
    "docx": {"zip", "docx", "xlsx"},
    "xlsx": {"zip", "docx", "xlsx"},
    "mp4": {"mp4", "mov"},
    "mov": {"mp4", "mov"},
}

#: (offset, magic bytes, canonical name). Order matters: the first match wins, so more
#: specific signatures must precede the containers they sit inside.
_SIGNATURES = [
    # --- images ---
    (0, b"\xff\xd8\xff", "jpg"),
    (0, b"\x89PNG\r\n\x1a\n", "png"),
    (0, b"GIF87a", "gif"),
    (0, b"GIF89a", "gif"),
    (0, b"BM", "bmp"),
    (0, b"II*\x00", "tiff"),
    (0, b"MM\x00*", "tiff"),
    # --- documents ---
    (0, b"%PDF-", "pdf"),
    (0, b"{\\rtf", "rtf"),
    # Legacy Office (.doc/.xls) and other OLE2 compound files.
    (0, b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "ole2"),
    # --- audio / video ---
    (0, b"ID3", "mp3"),
    (0, b"\x1aE\xdf\xa3", "webm"),
    (0, b"OggS", "ogg"),
    (0, b"fLaC", "flac"),
    # --- archives ---
    (0, b"PK\x03\x04", "zip"),
    (0, b"PK\x05\x06", "zip"),  # empty archive
    (0, b"PK\x07\x08", "zip"),  # spanned archive
    (0, b"Rar!\x1a\x07", "rar"),
    (0, b"\x1f\x8b", "gzip"),
    (0, b"7z\xbc\xaf'\x1c", "7z"),
    # --- executables and scripts: never a legitimate upload here ---
    (0, b"MZ", "executable"),
    (0, b"\x7fELF", "executable"),
    (0, b"\xca\xfe\xba\xbe", "executable"),
    (0, b"#!", "script"),
]

#: Which canonical names an extension may legitimately resolve to.
_EXTENSION_EXPECTS = {
    ".jpg": {"jpg"},
    ".jpeg": {"jpg"},
    ".png": {"png"},
    ".gif": {"gif"},
    ".webp": {"webp"},
    ".avif": {"avif"},
    ".bmp": {"bmp"},
    ".tiff": {"tiff"},
    ".mp4": {"mp4", "mov"},
    ".mov": {"mp4", "mov"},
    ".webm": {"webm"},
    ".mp3": {"mp3"},
    ".pdf": {"pdf"},
    ".docx": {"zip", "docx", "xlsx"},
    ".xlsx": {"zip", "docx", "xlsx"},
    # No signature. Verification cannot say anything about these, which is precisely
    # why "unidentified" must not mean "rejected".
    ".txt": set(),
    ".csv": set(),
}

#: How many leading bytes are enough. The furthest-in check is the ISO base media
#: brand at offset 8, so this is generous.
HEAD_BYTES = 64


def _is_riff(head, fourcc):
    """RIFF containers name their payload at offset 8, not offset 0."""
    return len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == fourcc


def _is_iso_media(head):
    """ISO base media (mp4/mov/avif/heic) puts ``ftyp`` at offset 4."""
    return len(head) >= 12 and head[4:8] == b"ftyp"


def _iso_brand(head):
    return head[8:12] if len(head) >= 12 else b""


def _is_mpeg_audio_frame(head):
    """A frameless MP3 with no ID3 tag starts at an MPEG audio sync word."""
    if len(head) < 2 or head[0] != 0xFF:
        return False
    # 111x xxxx — sync bits, with a layer that is not the reserved value.
    return (head[1] & 0xE0) == 0xE0 and (head[1] & 0x06) != 0x00


def sniff(head):
    """Return a canonical type name for ``head``, or ``None`` if unrecognised.

    ``head`` is the first :data:`HEAD_BYTES` of the file. Passing fewer is fine; the
    checks are length-guarded and simply match less.
    """
    if not head:
        return None
    head = bytes(head)

    # Container formats first — their outer bytes would otherwise match nothing, or in
    # the ISO case be missed entirely because the marker is not at offset 0.
    if _is_riff(head, b"WEBP"):
        return "webp"
    if _is_riff(head, b"AVI "):
        return "avi"
    if _is_riff(head, b"WAVE"):
        return "wav"
    if _is_iso_media(head):
        brand = _iso_brand(head)
        if brand.startswith(b"avif") or brand.startswith(b"avis"):
            return "avif"
        if brand.startswith(b"heic") or brand.startswith(b"heix"):
            return "heic"
        if brand.startswith(b"qt"):
            return "mov"
        return "mp4"

    for offset, magic, name in _SIGNATURES:
        if head[offset : offset + len(magic)] == magic:
            return name

    # Checked after the table so an ID3 tag wins, and because the sync-word test is
    # loose enough to collide with binary formats that have a real signature.
    if _is_mpeg_audio_frame(head):
        return "mp3"

    return None


def contradicts_extension(head, extension):
    """``(is_mismatch, detected)`` for a file's leading bytes against its extension.

    ``is_mismatch`` is True only when the bytes positively identify something the
    extension does not allow. Unknown content, an unknown extension, or an extension
    with no signature to check against all return False — silence is not evidence.
    """
    extension = (extension or "").strip().lower()
    if not extension.startswith(".") and extension:
        extension = f".{extension}"

    detected = sniff(head)
    if detected is None:
        return False, None

    expected = _EXTENSION_EXPECTS.get(extension)
    if not expected:
        # Either an extension we do not police, or one with no signature (.txt, .csv).
        # Both mean this check has nothing to say.
        return False, detected

    allowed = set(expected)
    for name in expected:
        allowed |= _EQUIVALENT.get(name, set())

    return detected not in allowed, detected
