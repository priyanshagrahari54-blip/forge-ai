"""Bounded, dependency-free image structure parsing (A39).

Real structural analysis of image bytes without any third-party
imaging library: format sniffing, dimensions for PNG/JPEG/BMP/GIF,
and a real PNG chunk walker that surfaces embedded text chunks
(tEXt/iTXt/zTXt) — which is exactly where malicious instructions tend
to hide. Everything is bounded; malformed or oversized input fails
closed with :class:`ImageFormatError`.
"""
from __future__ import annotations

import struct
import zlib

from forge.vision.base import ImageFormatError

MAX_IMAGE_BYTES = 5_000_000
MAX_CHUNKS = 512
MAX_CHUNK_TEXT = 2000

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def validate_image_bytes(image: bytes) -> None:
    if not isinstance(image, bytes):
        raise ImageFormatError("Image must be raw bytes")
    if not image:
        raise ImageFormatError("Image is empty")
    if len(image) > MAX_IMAGE_BYTES:
        raise ImageFormatError(
            f"Image exceeds {MAX_IMAGE_BYTES} bytes")


def sniff_image(image: bytes) -> tuple[str, int | None, int | None]:
    """Return (format, width, height) for a supported image format."""
    validate_image_bytes(image)
    if image.startswith(PNG_SIGNATURE):
        return "png", *_png_dimensions(image)
    if image.startswith(b"\xff\xd8"):
        return "jpeg", *_jpeg_dimensions(image)
    if image.startswith(b"BM"):
        return "bmp", *_bmp_dimensions(image)
    if image.startswith((b"GIF87a", b"GIF89a")):
        return "gif", *_gif_dimensions(image)
    raise ImageFormatError(
        "Unsupported or unrecognized image format "
        "(supported: png, jpeg, bmp, gif)")


def _png_dimensions(image: bytes) -> tuple[int, int]:
    if len(image) < 24:
        raise ImageFormatError("PNG header truncated")
    if image[12:16] != b"IHDR":
        raise ImageFormatError("PNG missing IHDR chunk")
    width, height = struct.unpack(">II", image[16:24])
    if width < 1 or height < 1 or width > 1_000_000 or height > 1_000_000:
        raise ImageFormatError("PNG dimensions out of bounds")
    return width, height


def _jpeg_dimensions(image: bytes) -> tuple[int, int]:
    # Walk JPEG markers (bounded) until a SOFn frame header is found.
    offset = 2
    limit = min(len(image), 1_000_000)
    while offset + 4 <= limit:
        if image[offset] != 0xFF:
            offset += 1
            continue
        marker = image[offset + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            offset += 2
            continue
        if offset + 4 > limit:
            break
        (length,) = struct.unpack(">H", image[offset + 2:offset + 4])
        if length < 2:
            break
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if offset + 9 <= limit:
                height, width = struct.unpack(
                    ">HH", image[offset + 5:offset + 9])
                if 0 < width <= 1_000_000 and 0 < height <= 1_000_000:
                    return width, height
            break
        offset += 2 + length
    raise ImageFormatError("JPEG has no decodable frame header")


def _bmp_dimensions(image: bytes) -> tuple[int, int]:
    if len(image) < 26:
        raise ImageFormatError("BMP header truncated")
    width, height = struct.unpack("<ii", image[18:26])
    if width < 1 or height < 1 or abs(width) > 1_000_000 \
            or abs(height) > 1_000_000:
        raise ImageFormatError("BMP dimensions out of bounds")
    return width, abs(height)


def _gif_dimensions(image: bytes) -> tuple[int, int]:
    if len(image) < 10:
        raise ImageFormatError("GIF header truncated")
    width, height = struct.unpack("<HH", image[6:10])
    if width < 1 or height < 1:
        raise ImageFormatError("GIF dimensions out of bounds")
    return width, height


def extract_png_text_chunks(image: bytes) -> list[str]:
    """Return embedded PNG text chunks (tEXt/iTXt/zTXt), bounded.

    Real chunk-walked data: these strings live inside the file and are
    reported verbatim as untrusted image content.
    """
    if not image.startswith(PNG_SIGNATURE):
        return []
    offset = 8
    chunks: list[str] = []
    while offset + 12 <= len(image) and len(chunks) < MAX_CHUNKS:
        (length,) = struct.unpack(">I", image[offset:offset + 4])
        if length > MAX_IMAGE_BYTES:
            break
        chunk_type = image[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > len(image):
            break
        payload = image[offset + 8:offset + 8 + length]
        if chunk_type == b"tEXt":
            parts = payload.split(b"\x00", 1)
            chunks.append(parts[1][:MAX_CHUNK_TEXT].decode(
                "utf-8", errors="replace") if len(parts) > 1 else "")
        elif chunk_type == b"iTXt":
            parts = payload.split(b"\x00", 4)
            if len(parts) >= 5:
                chunks.append(parts[4][:MAX_CHUNK_TEXT].decode(
                    "utf-8", errors="replace"))
        elif chunk_type == b"zTXt":
            parts = payload.split(b"\x00", 2)
            if len(parts) >= 3:
                try:
                    chunks.append(zlib.decompress(
                        parts[2])[:MAX_CHUNK_TEXT].decode(
                            "utf-8", errors="replace"))
                except Exception:
                    pass
        if chunk_type == b"IEND":
            break
        offset = end
    return chunks


DANGER_MARKERS = (
    "delete everything", "rm -rf", "format c:", "shutdown",
    "approve all", "approve everything", "ignore previous",
    "ignore all instructions", "disable security", "run as root",
    "sudo ", "give me your password", "reveal your secrets",
)
