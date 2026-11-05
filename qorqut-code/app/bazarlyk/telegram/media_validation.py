"""Caption, file name and sealed-snapshot validation for outbound Telegram files and photos.

Pure checks on values and bytes; nothing here opens a path, reads the database or talks to Telegram. Every failure is a
``DomainError`` raised before any delivery is queued, so a caller can classify it as a pre-send refusal:

``validation_error``  a caption, file name, mode or declared size the contract does not accept (``details.field`` names it)
``file_empty``        the snapshot has no bytes
``file_too_large``    more than 50,000,000 bytes as a document or 10,000,000 bytes as a photo
``media_invalid``     the bytes are not a static JPEG or PNG within the photo bounds (corrupt, another format, animated,
                      multiple frames, a dimension or decompression-bomb refusal)
``operation_unavailable``  Pillow is not installed in this environment

Photo bytes are verified (container integrity) and decoded only to be checked; they are never re-encoded, resized or
modified. Documents keep their bytes and are checked for size alone. Pillow is imported lazily so that importing this module
never requires it.
"""

from __future__ import annotations

import mimetypes
import os
import unicodedata

from qorqut_operations.contracts import DomainError

DOCUMENT_MAX_BYTES = 50_000_000
PHOTO_MAX_BYTES = 10_000_000
CAPTION_MAX_CHARS = 1024
CAPTION_MAX_UTF16 = 1024
FILENAME_MAX_BYTES = 255
PHOTO_MAX_DIMENSION_SUM = 10_000
PHOTO_MAX_ASPECT_RATIO = 20
SEND_AS = ("document", "photo")
PHOTO_MIME = {"JPEG": "image/jpeg", "PNG": "image/png"}
DEFAULT_MIME = "application/octet-stream"

# Caption control characters that are ordinary text: a line feed, a carriage return and a tab.
_CAPTION_CONTROLS = frozenset("\n\r\t")
# Explicit embedding, override and isolate controls: invisible, and able to disguise an extension in a displayed name. The
# multipart file name contract does not refuse them; workspace source paths do (media_sources.normalize_workspace_path).
BIDI_CONTROLS = frozenset("‪‫‬‭‮⁦⁧⁨⁩")
# A private mapping that never reads system MIME files, so the same name always gets the same type.
_MIME_TYPES = mimetypes.MimeTypes()


def invalid(message, field=None, *, code="validation_error"):
    return DomainError(code, message, details={"field": field} if field else None)


def media_invalid(message, reason=None):
    return DomainError("media_invalid", message, details={"reason": reason} if reason else None)


def utf16_units(text):
    """UTF-16 code units of ``text`` (an astral character is two); ``text`` must be valid Unicode."""
    return len(text.encode("utf-16-le")) // 2


def byte_limit(send_as):
    return DOCUMENT_MAX_BYTES if _send_as(send_as) == "document" else PHOTO_MAX_BYTES


def _send_as(value):
    if value not in SEND_AS:
        raise invalid("send_as must be document or photo", "send_as")
    return value


def validate_caption(text):
    """The caption unchanged when it is plain text within both limits; ``""`` is a caption-less file.

    At most 1024 characters and 1024 UTF-16 code units, valid Unicode (no lone surrogate), and no control character (Unicode
    category Cc, which includes NUL) other than line feed, carriage return and tab. Nothing is trimmed or normalized, so the
    text the owner reads is exactly what was accepted."""
    if not isinstance(text, str):
        raise invalid("caption must be text", "caption")
    if len(text) > CAPTION_MAX_CHARS:
        raise invalid(f"caption exceeds {CAPTION_MAX_CHARS} characters", "caption")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise invalid("caption must be valid Unicode text", "caption") from None
    if utf16_units(text) > CAPTION_MAX_UTF16:
        raise invalid(f"caption exceeds {CAPTION_MAX_UTF16} UTF-16 code units", "caption")
    for char in text:
        if unicodedata.category(char) == "Cc" and char not in _CAPTION_CONTROLS:
            raise invalid("caption contains a control character; only line feed, carriage return and tab are allowed", "caption")
    return text


def validate_filename(name):
    """The file name unchanged when it is valid for the multipart upload, otherwise ``validation_error``.

    This is exactly the transport's header-safe contract: valid Unicode (a lone surrogate, Unicode category Cs, is refused),
    1 to 255 UTF-8 bytes, not ``.`` or ``..``, and none of a forward slash, a backslash, an ASCII double quote or a character
    of category Cc (control), Zl (line separator) or Zp (paragraph separator). A double quote is refused rather than escaped
    or renamed, so the name queued is the name uploaded. Everything else is preserved exactly and never normalized. Workspace
    source paths apply stronger rules of their own before this check."""
    if not isinstance(name, str) or not name:
        raise invalid("A file name is required", "filename")
    try:
        encoded = name.encode("utf-8")
    except UnicodeEncodeError:
        raise invalid("The file name must be valid Unicode text", "filename") from None
    if len(encoded) > FILENAME_MAX_BYTES:
        raise invalid(f"The file name exceeds {FILENAME_MAX_BYTES} UTF-8 bytes", "filename")
    if name in {".", ".."}:
        raise invalid("The file name must be a plain name, not . or ..", "filename")
    for char in name:
        if char in '/\\"' or unicodedata.category(char) in {"Cc", "Zl", "Zp"}:
            raise invalid("The file name contains a separator, quote or control character", "filename")
    return name


def validate_sha256(value, field="expected_sha256"):
    """A lowercase 64-character hexadecimal SHA-256 digest, unchanged."""
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise invalid(f"{field} must be exactly 64 lowercase hexadecimal characters", field)
    return value


def document_mime(filename):
    """The declared content type of a document: from its name's extension, or ``application/octet-stream``."""
    return _MIME_TYPES.guess_type(filename, strict=True)[0] or DEFAULT_MIME


def validate_snapshot(stream, *, send_as, size, filename):
    """Plain metadata of one sealed snapshot, or a refusal. ``stream`` must be seekable and is rewound on return.

    ``size`` is the byte count the snapshot store sealed; the stream must hold exactly that many bytes. The empty and
    byte-limit checks run first and independently of the content. A document then needs nothing more. A photo must be a
    static JPEG or PNG that Pillow verifies (container integrity) and then decodes completely, with positive dimensions,
    width plus height of at most 10,000, a long side at most 20 times the short side and a single frame. Returns
    ``{"send_as", "filename", "size", "mime"}`` plus ``{"format", "width", "height"}`` for a photo; the MIME type is that of
    the actual image format."""
    mode = _send_as(send_as)
    name = validate_filename(filename)
    if type(size) is not int:
        raise invalid("size must be an integer byte count", "size")
    if size < 1:
        raise DomainError("file_empty", "The file is empty", details={"field": "size"})
    if size > byte_limit(mode):
        raise DomainError("file_too_large", f"The file exceeds the {byte_limit(mode):,}-byte limit for a {mode}",
                          details={"field": "size", "limit": byte_limit(mode)})
    if not (hasattr(stream, "seekable") and stream.seekable()):
        raise ValueError("A snapshot stream must be seekable")
    try:
        if stream.seek(0, os.SEEK_END) != size:
            raise invalid("The snapshot holds a different number of bytes than its declared size", "size")
        stream.seek(0)
        metadata = {"send_as": mode, "filename": name, "size": size}
        if mode == "document":
            return {**metadata, "mime": document_mime(name)}
        return {**metadata, **_inspect_photo(stream)}
    finally:
        stream.seek(0)


class _Borrowed:
    """Lends a caller's stream to Pillow, whose ``Image.close`` would otherwise close the stream it was given."""

    def __init__(self, stream):
        self._stream = stream
        self.read, self.seek, self.tell = stream.read, stream.seek, stream.tell

    def readable(self):
        return True

    def seekable(self):
        return True

    def close(self):
        pass


def _open_checked(Image, stream):
    """The image opened from the start of ``stream``, after every header-only bound has been checked.

    Nothing is decoded here, so a refusal costs no pixel work. The caller closes the returned image."""
    stream.seek(0)
    # Only the two accepted decoders are tried, so another format is never decoded at all.
    image = Image.open(_Borrowed(stream), formats=tuple(PHOTO_MIME))
    try:
        if image.format not in PHOTO_MIME:
            # A multi-picture JPEG opens as MPO.
            raise media_invalid("A photo must be a static JPEG or PNG image", "format")
        width, height = image.size
        if width < 1 or height < 1:
            raise media_invalid("The image dimensions must be positive", "dimensions")
        if width + height > PHOTO_MAX_DIMENSION_SUM:
            raise media_invalid(f"The image width plus height exceeds {PHOTO_MAX_DIMENSION_SUM} pixels", "dimensions")
        if max(width, height) > PHOTO_MAX_ASPECT_RATIO * min(width, height):
            raise media_invalid(f"The image is more than {PHOTO_MAX_ASPECT_RATIO} times longer than it is wide", "aspect_ratio")
        if getattr(image, "is_animated", False) or getattr(image, "n_frames", 1) > 1:
            raise media_invalid("An animated or multi-frame image cannot be sent as a photo", "frames")
    except BaseException:
        image.close()
        raise
    return image


def _inspect_photo(stream):
    try:
        from PIL import Image
    except ImportError as exc:
        raise DomainError("operation_unavailable", "Photo validation is not available in this installation") from exc
    image = None
    try:
        # Container integrity first: Pillow's own ``verify`` walks the chunk structure of the whole file (a PNG's chunk
        # checksums up to its end marker, truncation) and invalidates the image it is called on. It is not a guarantee about
        # every byte: a damaged final IEND checksum, for one, is not noticed. Its reads are bounded by the snapshot's byte limit.
        checked = _open_checked(Image, stream)
        try:
            checked.verify()
        finally:
            checked.close()
        # Then a fresh image over the same unmodified bytes, whose bounds are checked again, is decoded completely. The
        # declared size is bounded above (at most 5000 x 5000 pixels), so the full decode is bounded too.
        image = _open_checked(Image, stream)
        image.load()
        return {"mime": PHOTO_MIME[image.format], "format": image.format, "width": image.width, "height": image.height}
    except DomainError:
        raise
    except Exception as exc:  # Pillow reports corrupt, truncated or bomb input through many exception types.
        raise media_invalid("The file is not a valid static JPEG or PNG image", type(exc).__name__) from None
    finally:
        if image is not None:
            image.close()
