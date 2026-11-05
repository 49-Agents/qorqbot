"""Caption, file name and snapshot validation for outbound Telegram files and photos.

Pure value and byte checks: real Pillow decodes generated images, no database, service or network."""
import io
import random
import struct
import zlib

import pytest
from qorqut_operations.contracts import DomainError

from bazarlyk.telegram import media_validation as mv
from bazarlyk.telegram.media_validation import (CAPTION_MAX_UTF16, DOCUMENT_MAX_BYTES, PHOTO_MAX_BYTES, validate_caption,
                                                validate_filename, validate_snapshot)

Image = pytest.importorskip("PIL.Image")


def code(call, *args, **kwargs):
    with pytest.raises(DomainError) as caught:
        call(*args, **kwargs)
    return caught.value.code


def image_bytes(fmt="PNG", size=(30, 20), color=(200, 30, 30), **options):
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, fmt, **options)
    return out.getvalue()


def png_chunk(kind, body):
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def png_header_only(width, height):
    """A syntactically valid PNG header claiming a size; its pixel data is a single empty IDAT, never decodable."""
    return (b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + png_chunk(b"IDAT", zlib.compress(b"")) + png_chunk(b"IEND", b""))


def snapshot(data, send_as="photo", filename="photo.png"):
    stream = io.BytesIO(data)
    return validate_snapshot(stream, send_as=send_as, size=len(data), filename=filename), stream


# ----- constants and caption ------------------------------------------------------------------------------------------


def test_the_published_limits_are_the_contract_values():
    assert (DOCUMENT_MAX_BYTES, PHOTO_MAX_BYTES, CAPTION_MAX_UTF16) == (50_000_000, 10_000_000, 1024)
    assert mv.CAPTION_MAX_CHARS == 1024 and mv.FILENAME_MAX_BYTES == 255


def test_a_caption_is_accepted_unchanged_within_both_limits():
    for text in ("", "Hello", "Hesabat: Əhməd, Çiçək, Şəkər — 文件 📎\nSecond line\twith tab", "a" * 1024, "ا" * 1024,
                 "windows\r\nline\rbreaks"):
        assert validate_caption(text) == text
    assert "️" in validate_caption("☎️") and validate_caption("é") == "é"  # Never normalized.


def test_a_caption_obeys_the_character_limit_and_the_utf16_limit_independently():
    assert code(validate_caption, "a" * 1025) == "validation_error"
    # 512 astral characters are 1024 UTF-16 units (accepted); 513 are 1026 units while only 513 characters.
    assert validate_caption("😀" * 512) == "😀" * 512
    assert mv.utf16_units("😀" * 513) == 1026 and len("😀" * 513) == 513
    assert code(validate_caption, "😀" * 513) == "validation_error"
    # One astral character at the end: 1023 characters / 1024 units is accepted, 1024 characters / 1025 units is not.
    assert validate_caption("a" * 1022 + "😀") == "a" * 1022 + "😀"
    assert code(validate_caption, "a" * 1023 + "😀") == "validation_error"


def test_a_caption_allows_only_lf_cr_and_tab_among_the_unicode_control_characters():
    for allowed in ("\n", "\r", "\t", "a\r\nb", "a\n\rb", "tab\there\nnewline"):
        assert validate_caption(allowed) == allowed, repr(allowed)
    # Every other category Cc character is refused: the C0 range, DEL and the C1 range.
    for number in [*range(0, 32), *range(127, 160)]:
        char = chr(number)
        if char in "\n\r\t":
            continue
        assert code(validate_caption, f"a{char}b") == "validation_error", hex(number)
    for bad in ("\ud800", "a\udfffb", "\ud83d", "a\ude00"):  # Lone surrogates are not valid Unicode.
        assert code(validate_caption, bad) == "validation_error", repr(bad)
    for bad in (None, b"bytes", 12, ["x"]):
        assert code(validate_caption, bad) == "validation_error"
    # Other format and separator characters are plain text and stay as written.
    keep = "a\u200db\u2028c\u00a0d\u202ee"
    assert validate_caption(keep) == keep


# ----- file name -------------------------------------------------------------------------------------------------------


def test_ordinary_unicode_filenames_are_preserved_exactly():
    for name in ("report.pdf", "hesabat Əlavə (1).pdf", "тест файл.docx", "文件.png", "emoji 📎.txt", "no extension", "a.b.c.tar.gz",
                 ".hidden-name", "100% done.csv", "Ü.pdf", "é.txt", "x" * 255):
        assert validate_filename(name) == name


def test_filename_bounds_count_utf8_bytes():
    assert validate_filename("ə" * 127 + "a") and code(validate_filename, "ə" * 128) == "validation_error"
    assert code(validate_filename, "x" * 256) == "validation_error"


def test_filenames_refuse_exactly_the_transports_header_unsafe_names():
    for bad in ("", ".", "..", "a/b", "/a", "a/", "a\\b", 'a"b', '"', '"a.pdf"', "a\x00b", "a\nb", "a\r\nSet-Cookie: x=1", "a\tb",
                "a\x7fb", "a\x85b", "a\x9fb", "a\u2028b", "a\u2029b", "\ud800.txt", "a\udfff", "x" * 256, "ə" * 128, None, 5,
                b"a.pdf"):
        assert code(validate_filename, bad) == "validation_error", repr(bad)
    for number in [*range(0, 32), *range(127, 160)]:
        assert code(validate_filename, f"a{chr(number)}b.pdf") == "validation_error", hex(number)


def test_filenames_outside_that_contract_are_valid_and_preserved():
    # The transport accepts these, so early validation does not refuse or rewrite them; workspace paths are stricter.
    for name in (" leading.pdf", "trailing.pdf ", "a\u00a0b.pdf", "fdp.\u202eexe", "a\u2066b", "a\u200db", "it's.txt", "a`b$c;d&e.txt",
                 "a:b*c?d<e>f|g.txt", "%00.txt", "...", ".a", "a.", "ə" * 127 + "a"):
        assert validate_filename(name) == name, repr(name)


def test_sha256_digests_are_exactly_lowercase_hex():
    digest = "0123456789abcdef" * 4
    assert mv.validate_sha256(digest) == digest
    for bad in (digest.upper(), digest[:-1], digest + "0", "g" * 64, "", None, 7, "sha256:" + digest):
        assert code(mv.validate_sha256, bad) == "validation_error"


# ----- documents -------------------------------------------------------------------------------------------------------


def test_a_document_keeps_its_bytes_whatever_they_are_and_the_stream_is_rewound():
    data = bytes(range(256)) * 10
    metadata, stream = snapshot(data, "document", "blob.bin")
    assert metadata == {"send_as": "document", "filename": "blob.bin", "size": len(data), "mime": "application/octet-stream"}
    assert stream.tell() == 0 and stream.read() == data
    assert snapshot(b"%PDF-1.4 x", "document", "r.pdf")[0]["mime"] == "application/pdf"
    assert snapshot(b"x", "document", "weird.unknownext")[0]["mime"] == "application/octet-stream"
    # A document that is an image is still sent as a document, with its name's type, and is not decoded.
    assert snapshot(image_bytes(), "document", "scan.png")[0]["mime"] == "image/png"
    assert snapshot(b"not an image at all", "document", "scan.png")[0]["size"] == 19


def test_documents_check_emptiness_and_bytes_independently_of_content():
    assert code(validate_snapshot, io.BytesIO(b""), send_as="document", size=0, filename="a.txt") == "file_empty"
    assert code(validate_snapshot, io.BytesIO(b"x"), send_as="document", size=DOCUMENT_MAX_BYTES + 1, filename="a.txt") == "file_too_large"
    assert code(validate_snapshot, io.BytesIO(b"x"), send_as="photo", size=PHOTO_MAX_BYTES + 1, filename="a.png") == "file_too_large"
    # The photo limit is smaller than the document limit.
    assert code(validate_snapshot, io.BytesIO(b"x"), send_as="photo", size=PHOTO_MAX_BYTES + 1, filename="a.png") == "file_too_large"


def test_the_exact_byte_limits_are_accepted(tmp_path):
    big = tmp_path / "big.bin"
    with big.open("wb") as stream:
        stream.truncate(DOCUMENT_MAX_BYTES)
    with big.open("rb") as stream:
        assert validate_snapshot(stream, send_as="document", size=DOCUMENT_MAX_BYTES, filename="big.bin")["size"] == DOCUMENT_MAX_BYTES
        assert stream.tell() == 0
    # A valid PNG padded to exactly the photo limit still decodes (trailing bytes are ignored), and keeps its bytes.
    padded = image_bytes() + b"\0" * (PHOTO_MAX_BYTES - len(image_bytes()))
    assert len(padded) == PHOTO_MAX_BYTES
    metadata, stream = snapshot(padded)
    assert metadata["size"] == PHOTO_MAX_BYTES and metadata["mime"] == "image/png" and stream.read() == padded


def test_a_declared_size_must_match_the_stream_and_the_stream_must_seek():
    assert code(validate_snapshot, io.BytesIO(b"abc"), send_as="document", size=4, filename="a.txt") == "validation_error"
    assert code(validate_snapshot, io.BytesIO(b"abc"), send_as="document", size=2, filename="a.txt") == "validation_error"
    assert code(validate_snapshot, io.BytesIO(b"abc"), send_as="document", size=True, filename="a.txt") == "validation_error"
    assert code(validate_snapshot, io.BytesIO(b"abc"), send_as="video", size=3, filename="a.txt") == "validation_error"
    assert code(validate_snapshot, io.BytesIO(b"abc"), send_as="document", size=3, filename="a/b.txt") == "validation_error"

    class Pipe(io.RawIOBase):
        def seekable(self):
            return False

    with pytest.raises(ValueError):
        validate_snapshot(Pipe(), send_as="document", size=3, filename="a.txt")
    stream = io.BytesIO(b"abc")
    stream.seek(2)
    code(validate_snapshot, stream, send_as="document", size=9, filename="a.txt")
    assert stream.tell() == 0, "A refusal rewinds the stream too"


# ----- photos ----------------------------------------------------------------------------------------------------------


def test_static_jpeg_and_png_are_accepted_with_their_real_format_dimensions_and_untouched_bytes():
    png, jpeg = image_bytes("PNG", (30, 20)), image_bytes("JPEG", (64, 48))
    metadata, stream = snapshot(png)
    assert metadata == {"send_as": "photo", "filename": "photo.png", "size": len(png), "mime": "image/png",
                        "format": "PNG", "width": 30, "height": 20}
    assert stream.tell() == 0 and stream.read() == png and not stream.closed
    metadata, stream = snapshot(jpeg, filename="photo.jpg")
    assert (metadata["mime"], metadata["format"], metadata["width"], metadata["height"]) == ("image/jpeg", "JPEG", 64, 48)
    assert stream.read() == jpeg and not stream.closed
    # The actual format wins over the name: a JPEG called .png is a JPEG; a PNG called .jpg is a PNG.
    assert snapshot(jpeg, filename="really.png")[0]["mime"] == "image/jpeg"
    assert snapshot(png, filename="really.jpg")[0]["mime"] == "image/png"
    for mode, fmt in (("L", "PNG"), ("P", "PNG"), ("RGBA", "PNG"), ("1", "PNG"), ("L", "JPEG"), ("CMYK", "JPEG")):
        out = io.BytesIO()
        Image.new(mode, (10, 10)).save(out, fmt)
        assert snapshot(out.getvalue())[0]["format"] == fmt


def test_other_image_formats_text_and_garbage_are_not_photos():
    for fmt in ("GIF", "BMP", "WEBP", "TIFF", "ICO"):
        data = image_bytes(fmt, (16, 16))
        assert code(validate_snapshot, io.BytesIO(data), send_as="photo", size=len(data), filename="photo.png") == "media_invalid", fmt
    for data in (b"not an image", b"%PDF-1.4 \x00" * 5, b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"\x00" * 100, b"<svg/>"):
        assert code(validate_snapshot, io.BytesIO(data), send_as="photo", size=len(data), filename="a.png") == "media_invalid"


def test_animated_and_multi_frame_images_are_refused():
    frames = [Image.new("RGB", (20, 20), (value, 0, 0)) for value in (0, 100, 200)]
    animated_png = io.BytesIO()
    frames[0].save(animated_png, "PNG", save_all=True, append_images=frames[1:], duration=50)
    animated_gif = io.BytesIO()
    frames[0].save(animated_gif, "GIF", save_all=True, append_images=frames[1:])
    multi_picture = io.BytesIO()
    frames[0].save(multi_picture, "MPO", save_all=True, append_images=frames[1:])
    for data in (animated_png.getvalue(), animated_gif.getvalue(), multi_picture.getvalue()):
        metadata_code = code(validate_snapshot, io.BytesIO(data), send_as="photo", size=len(data), filename="a.png")
        assert metadata_code == "media_invalid"
    # The same bytes are fine as documents.
    assert snapshot(animated_png.getvalue(), "document", "a.png")[0]["size"] == len(animated_png.getvalue())
    # A single-frame image saved by the animated writer is still static.
    still = io.BytesIO()
    frames[0].save(still, "PNG", save_all=True)
    assert snapshot(still.getvalue())[0]["width"] == 20


def test_truncated_and_corrupted_images_are_refused_not_partially_accepted():
    png, jpeg = image_bytes("PNG", (200, 200), color=(10, 20, 30)), image_bytes("JPEG", (200, 200))
    noisy = io.BytesIO()
    Image.frombytes("RGB", (120, 120), bytes((i * 7 + j) % 251 for i in range(120) for j in range(360))).save(noisy, "PNG")
    noisy = noisy.getvalue()
    assert code(validate_snapshot, io.BytesIO(noisy[:len(noisy) // 2]), send_as="photo", size=len(noisy) // 2, filename="a.png") == "media_invalid"
    assert code(validate_snapshot, io.BytesIO(jpeg[:len(jpeg) // 2]), send_as="photo", size=len(jpeg) // 2, filename="a.jpg") == "media_invalid"
    broken = bytearray(png)
    idat = broken.index(b"IDAT") + 4
    broken[idat + 2] ^= 0xFF  # Corrupt compressed data; the chunk CRC no longer matches.
    assert code(validate_snapshot, io.BytesIO(bytes(broken)), send_as="photo", size=len(broken), filename="a.png") == "media_invalid"
    assert code(validate_snapshot, io.BytesIO(png[:20]), send_as="photo", size=20, filename="a.png") == "media_invalid"
    from PIL import ImageFile
    assert ImageFile.LOAD_TRUNCATED_IMAGES is False, "Truncated images must never be loaded"


def png_chunks(data):
    """``(type, start of its checksum)`` for each chunk of a PNG."""
    position, found = 8, []
    while position < len(data):
        (length,) = struct.unpack(">I", data[position:position + 4])
        found.append((data[position + 4:position + 8], position + 8 + length))
        position += 12 + length
    return found


def with_flipped_checksum(data, chunk_type, which=0):
    """``data`` with one bit of the checksum of the ``which``-th ``chunk_type`` chunk changed; no other byte is touched."""
    at = [crc for kind, crc in png_chunks(data) if kind == chunk_type][which]
    broken = bytearray(data)
    broken[at] ^= 0x01
    return bytes(broken)


def test_a_png_with_a_bad_chunk_checksum_is_refused_even_though_its_pixels_decode():
    from PIL.PngImagePlugin import PngInfo
    info = PngInfo()
    info.add_text("Comment", "ancillary metadata")
    out = io.BytesIO()
    Image.new("RGB", (10, 10), (200, 30, 30)).save(out, "PNG", pnginfo=info)
    small = out.getvalue()
    noise = random.Random(7).randbytes(300 * 300 * 3)  # Incompressible, so the encoder writes several data chunks.
    big = io.BytesIO()
    Image.frombytes("RGB", (300, 300), noise).save(big, "PNG")
    big = big.getvalue()
    assert [kind for kind, _ in png_chunks(big)].count(b"IDAT") > 1, "The fixture needs several data chunks"
    assert snapshot(small)[0]["format"] == "PNG" and snapshot(big)[0]["format"] == "PNG"
    cases = {"the only IDAT": with_flipped_checksum(small, b"IDAT"), "an ancillary tEXt chunk": with_flipped_checksum(small, b"tEXt"),
             "the first IDAT": with_flipped_checksum(big, b"IDAT", 0), "a later IDAT": with_flipped_checksum(big, b"IDAT", 1),
             "the last IDAT": with_flipped_checksum(big, b"IDAT", [k for k, _ in png_chunks(big)].count(b"IDAT") - 1),
             "the header": with_flipped_checksum(small, b"IHDR")}
    for label, damaged in cases.items():
        assert len(damaged) == len(small) or len(damaged) == len(big)
        stream = io.BytesIO(damaged)
        assert code(validate_snapshot, stream, send_as="photo", size=len(damaged), filename="a.png") == "media_invalid", label
        assert not stream.closed and stream.tell() == 0 and stream.getvalue() == damaged, label
    # The same damage is not a decoding problem: decoding the pixels alone accepts it, so only the checksum check refuses it.
    # (If a Pillow release ever notices the damage while decoding, this precondition fails and the test needs a new fixture.)
    for label in ("the only IDAT", "the last IDAT"):
        decoded = Image.open(io.BytesIO(cases[label]))
        decoded.load()
        assert decoded.size in {(10, 10), (300, 300)}
    # Documents are not inspected, so the same bytes are accepted as a document and stay byte for byte.
    metadata, stream = snapshot(cases["the only IDAT"], "document", "damaged.png")
    assert metadata["size"] == len(cases["the only IDAT"]) and stream.getvalue() == cases["the only IDAT"]


def test_verification_keeps_the_valid_bytes_and_the_stream_exactly_as_given():
    for fmt in ("PNG", "JPEG"):
        data = image_bytes(fmt, (64, 48))
        stream = io.BytesIO(data)
        metadata = validate_snapshot(stream, send_as="photo", size=len(data), filename="photo")
        assert (metadata["format"], metadata["width"], metadata["height"]) == (fmt, 64, 48)
        assert not stream.closed and stream.tell() == 0 and stream.getvalue() == data


def test_photo_dimension_and_aspect_ratio_bounds():
    for size in ((1, 1), (2000, 100), (100, 2000), (4000, 200)):
        data = image_bytes("PNG", size, color=(1, 2, 3))
        metadata = snapshot(data)[0]
        assert (metadata["width"], metadata["height"]) == size, size
    # The ratio bound is the long side over the short side: exactly 20 passes, 20.01 does not.
    for size in ((2001, 100), (100, 2001)):
        data = image_bytes("PNG", size)
        assert code(validate_snapshot, io.BytesIO(data), send_as="photo", size=len(data), filename="a.png") == "media_invalid", size
    # Width plus height above 10,000 is refused from the header alone, before any decode.
    for width, height in ((5001, 5000), (10001, 1), (9999, 2), (20000, 20000), (3, 10000)):
        data = png_header_only(width, height)
        assert code(validate_snapshot, io.BytesIO(data), send_as="photo", size=len(data), filename="a.png") == "media_invalid", (width, height)


def test_a_full_size_photo_within_bounds_decodes():
    data = image_bytes("PNG", (5000, 5000), color=(5, 6, 7))
    assert len(data) < PHOTO_MAX_BYTES
    metadata, stream = snapshot(data)
    assert (metadata["width"], metadata["height"]) == (5000, 5000) and stream.tell() == 0


@pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")
def test_decompression_bombs_and_zero_sized_headers_are_refused_before_decoding():
    for width, height in ((100_000, 100_000), (65_535, 65_535), (0, 10), (10, 0), (0, 0)):
        data = png_header_only(width, height)
        assert code(validate_snapshot, io.BytesIO(data), send_as="photo", size=len(data), filename="a.png") == "media_invalid", (width, height)
    # An image that Pillow only warns about (above its pixel soft limit) is still refused by the explicit dimension rule.
    data = png_header_only(10_000, 10_000)
    assert code(validate_snapshot, io.BytesIO(data), send_as="photo", size=len(data), filename="a.png") == "media_invalid"


def test_photo_validation_does_not_close_the_callers_stream(tmp_path):
    path = tmp_path / "a.png"
    data = image_bytes()
    path.write_bytes(data)
    with path.open("rb") as stream:
        validate_snapshot(stream, send_as="photo", size=len(data), filename="a.png")
        assert not stream.closed and stream.read() == data
