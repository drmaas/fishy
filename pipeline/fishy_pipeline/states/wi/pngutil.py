# SPDX-License-Identifier: GPL-3.0-only
"""Tiny PNG reader and writer (8-bit gray, RGB, or RGBA, no interlace).

The georef stage uses this so unit tests stay on the standard library.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@dataclass(frozen=True)
class RasterImage:
    width: int
    height: int
    rgb: bytes

    def __post_init__(self) -> None:
        expected = self.width * self.height * 3
        if len(self.rgb) != expected:
            raise ValueError(f"rgb length {len(self.rgb)} != {expected}")


def _chunk(tag: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(tag + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)


def write_png(path: Path, image: RasterImage) -> None:
    """Write an 8-bit RGB PNG with filter None."""
    raw = bytearray()
    stride = image.width * 3
    for y in range(image.height):
        raw.append(0)
        start = y * stride
        raw.extend(image.rgb[start : start + stride])
    ihdr = struct.pack(">IIBBBBB", image.width, image.height, 8, 2, 0, 0, 0)
    payload = _SIGNATURE + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    payload += _chunk(b"IEND", b"")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _paeth(left: int, up: int, upper_left: int) -> int:
    estimate = left + up - upper_left
    dist_left = abs(estimate - left)
    dist_up = abs(estimate - up)
    dist_upper_left = abs(estimate - upper_left)
    if dist_left <= dist_up and dist_left <= dist_upper_left:
        return left
    if dist_up <= dist_upper_left:
        return up
    return upper_left


def _unfilter(raw: bytes, width: int, height: int, channels: int) -> bytes:
    stride = width * channels
    expected = height * (stride + 1)
    if len(raw) < expected:
        raise ValueError("PNG image data is truncated")
    out = bytearray(height * stride)
    previous = bytearray(stride)
    cursor = 0
    for y in range(height):
        filter_type = raw[cursor]
        cursor += 1
        row = bytearray(raw[cursor : cursor + stride])
        cursor += stride
        if filter_type == 0:
            pass
        elif filter_type == 1:
            for index in range(stride):
                left = row[index - channels] if index >= channels else 0
                row[index] = (row[index] + left) & 255
        elif filter_type == 2:
            for index in range(stride):
                row[index] = (row[index] + previous[index]) & 255
        elif filter_type == 3:
            for index in range(stride):
                left = row[index - channels] if index >= channels else 0
                row[index] = (row[index] + ((left + previous[index]) // 2)) & 255
        elif filter_type == 4:
            for index in range(stride):
                left = row[index - channels] if index >= channels else 0
                up = previous[index]
                upper_left = previous[index - channels] if index >= channels else 0
                row[index] = (row[index] + _paeth(left, up, upper_left)) & 255
        else:
            raise ValueError(f"unsupported PNG filter {filter_type}")
        out[y * stride : (y + 1) * stride] = row
        previous = row
    return bytes(out)


def _to_rgb(samples: bytes, width: int, height: int, color_type: int) -> bytes:
    count = width * height
    if color_type == 2:
        return samples
    rgb = bytearray(count * 3)
    if color_type == 0:
        for index in range(count):
            value = samples[index]
            rgb[index * 3 : index * 3 + 3] = bytes((value, value, value))
        return bytes(rgb)
    if color_type == 4:
        for index in range(count):
            value = samples[index * 2]
            rgb[index * 3 : index * 3 + 3] = bytes((value, value, value))
        return bytes(rgb)
    if color_type == 6:
        for index in range(count):
            src = index * 4
            rgb[index * 3 : index * 3 + 3] = samples[src : src + 3]
        return bytes(rgb)
    raise ValueError(f"unsupported PNG color type {color_type}")


def read_png(path: Path) -> RasterImage:
    """Read an 8-bit non-interlaced PNG into RGB bytes."""
    data = path.read_bytes()
    if not data.startswith(_SIGNATURE):
        raise ValueError(f"not a PNG: {path}")
    offset = len(_SIGNATURE)
    width = height = color_type = None
    idat = bytearray()
    while offset + 8 <= len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        offset += 4
        tag = data[offset : offset + 4]
        offset += 4
        chunk = data[offset : offset + length]
        offset += length + 4  # skip CRC
        if tag == b"IHDR":
            width, height, bit_depth, color_type, _compression, _filter, interlace = struct.unpack(
                ">IIBBBBB", chunk
            )
            if bit_depth != 8 or interlace != 0:
                raise ValueError("PNG must be 8-bit and non-interlaced")
            if color_type not in {0, 2, 4, 6}:
                raise ValueError(f"unsupported PNG color type {color_type}")
        elif tag == b"IDAT":
            idat.extend(chunk)
        elif tag == b"IEND":
            break
    if width is None or height is None or color_type is None or not idat:
        raise ValueError(f"incomplete PNG: {path}")
    channels = {0: 1, 2: 3, 4: 2, 6: 4}[color_type]
    samples = _unfilter(zlib.decompress(bytes(idat)), width, height, channels)
    return RasterImage(width=width, height=height, rgb=_to_rgb(samples, width, height, color_type))
