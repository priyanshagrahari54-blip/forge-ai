"""Shared image fixtures for the A39 vision suites (no PIL needed)."""
from __future__ import annotations

import base64
import struct
import zlib


def png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + chunk_type + data
            + struct.pack(">I", zlib.crc32(chunk_type + data)))


def make_png(width: int = 320, height: int = 200,
             text_chunks: tuple[str, ...] = (),
             extra_chunks: tuple[tuple[bytes, bytes], ...] = ()) -> bytes:
    """Build a well-formed PNG (RGBA-ish, uncompressed rows)."""
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + bytes([0x7F]) * (width * 3)
    raw = row * height
    image = (b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", header)
             + png_chunk(b"IDAT", zlib.compress(raw)))
    for text in text_chunks:
        image += png_chunk(b"tEXt", b"prompt\x00" + text.encode("utf-8"))
    for chunk_type, data in extra_chunks:
        image += png_chunk(chunk_type, data)
    return image + png_chunk(b"IEND", b"")


def make_jpeg(width: int = 64, height: int = 48) -> bytes:
    """Minimal decodable JPEG: SOI + APP0 + SOF0 + EOI."""
    app0 = (b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00"
            + struct.pack(">BB", 1, 1) + struct.pack(">B", 0)
            + struct.pack(">HH", 1, 1) + struct.pack(">BB", 0, 0))
    sof0 = b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" \
        + struct.pack(">HHB", height, width, 3) \
        + bytes([1, 0x11, 0x00, 3, 0x11, 0x00, 3, 0x11, 0x00])
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


def b64(image: bytes) -> str:
    return base64.b64encode(image).decode("ascii")
