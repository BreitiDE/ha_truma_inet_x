"""Minimal, dependency free CBOR (RFC 8949) encoder/decoder.

The Truma iNet X panel exchanges small CBOR maps (topic / parameter / value).
Shipping our own codec avoids an extra pip requirement on Home Assistant OS.

Supported:
  * unsigned / negative integers (incl. 64 bit)
  * byte strings, text strings (definite + indefinite length when decoding)
  * arrays, maps (definite + indefinite length when decoding)
  * false / true / null / undefined
  * half / single / double precision floats
  * tags (decoded transparently - the tag number is dropped)
"""

from __future__ import annotations

import math
import struct
from typing import Any

__all__ = ["dumps", "loads", "loads_prefix", "CBORDecodeError"]


class CBORDecodeError(ValueError):
    """Raised when a byte sequence is not valid CBOR."""


_BREAK = object()


# --------------------------------------------------------------------------
# Encoder
# --------------------------------------------------------------------------
def _encode_head(major: int, value: int) -> bytes:
    mt = major << 5
    if value < 24:
        return bytes([mt | value])
    if value < 0x100:
        return bytes([mt | 24, value])
    if value < 0x10000:
        return bytes([mt | 25]) + struct.pack(">H", value)
    if value < 0x100000000:
        return bytes([mt | 26]) + struct.pack(">I", value)
    if value < 0x10000000000000000:
        return bytes([mt | 27]) + struct.pack(">Q", value)
    raise ValueError("integer too large for CBOR")


def _encode(obj: Any, out: bytearray) -> None:
    if obj is None:
        out.append(0xF6)
    elif obj is True:
        out.append(0xF5)
    elif obj is False:
        out.append(0xF4)
    elif isinstance(obj, int):
        if obj >= 0:
            out += _encode_head(0, obj)
        else:
            out += _encode_head(1, -1 - obj)
    elif isinstance(obj, float):
        if (
            math.isnan(obj)
            or math.isinf(obj)
            or struct.unpack(">f", struct.pack(">f", obj))[0] == obj
        ):
            out.append(0xFA)
            out += struct.pack(">f", obj)
        else:
            out.append(0xFB)
            out += struct.pack(">d", obj)
    elif isinstance(obj, (bytes, bytearray, memoryview)):
        data = bytes(obj)
        out += _encode_head(2, len(data))
        out += data
    elif isinstance(obj, str):
        data = obj.encode("utf-8")
        out += _encode_head(3, len(data))
        out += data
    elif isinstance(obj, (list, tuple)):
        out += _encode_head(4, len(obj))
        for item in obj:
            _encode(item, out)
    elif isinstance(obj, dict):
        out += _encode_head(5, len(obj))
        for key, value in obj.items():
            _encode(key, out)
            _encode(value, out)
    else:
        raise TypeError(f"cannot CBOR-encode object of type {type(obj).__name__}")


def dumps(obj: Any) -> bytes:
    """Encode ``obj`` as CBOR."""
    out = bytearray()
    _encode(obj, out)
    return bytes(out)


# --------------------------------------------------------------------------
# Decoder
# --------------------------------------------------------------------------
def _half_to_float(value: int) -> float:
    exp = (value >> 10) & 0x1F
    mant = value & 0x3FF
    if exp == 0:
        val = math.ldexp(mant, -24)
    elif exp != 31:
        val = math.ldexp(mant + 1024, exp - 25)
    else:
        val = math.inf if mant == 0 else math.nan
    return -val if value & 0x8000 else val


class _Decoder:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def _take(self, count: int) -> bytes:
        end = self.pos + count
        if end > len(self.data):
            raise CBORDecodeError("unexpected end of data")
        chunk = self.data[self.pos : end]
        self.pos = end
        return chunk

    def _read_arg(self, info: int) -> int | None:
        if info < 24:
            return info
        if info == 24:
            return self._take(1)[0]
        if info == 25:
            return struct.unpack(">H", self._take(2))[0]
        if info == 26:
            return struct.unpack(">I", self._take(4))[0]
        if info == 27:
            return struct.unpack(">Q", self._take(8))[0]
        if info == 31:
            return None  # indefinite length
        raise CBORDecodeError(f"invalid additional info {info}")

    def decode(self) -> Any:
        initial = self._take(1)[0]
        major = initial >> 5
        info = initial & 0x1F

        if major == 7:
            if info == 20:
                return False
            if info == 21:
                return True
            if info in (22, 23):
                return None
            if info == 24:
                return self._take(1)[0]  # simple value
            if info == 25:
                return _half_to_float(struct.unpack(">H", self._take(2))[0])
            if info == 26:
                return struct.unpack(">f", self._take(4))[0]
            if info == 27:
                return struct.unpack(">d", self._take(8))[0]
            if info == 31:
                return _BREAK
            if info < 20:
                return info  # unassigned simple value
            raise CBORDecodeError(f"invalid simple value {info}")

        arg = self._read_arg(info)

        if major == 0:
            if arg is None:
                raise CBORDecodeError("indefinite integer")
            return arg
        if major == 1:
            if arg is None:
                raise CBORDecodeError("indefinite integer")
            return -1 - arg
        if major in (2, 3):
            if arg is None:
                parts = []
                while True:
                    item = self.decode()
                    if item is _BREAK:
                        break
                    parts.append(item)
                if major == 2:
                    return b"".join(parts)
                return "".join(parts)
            raw = self._take(arg)
            if major == 2:
                return bytes(raw)
            return raw.decode("utf-8", errors="replace")
        if major == 4:
            items = []
            if arg is None:
                while True:
                    item = self.decode()
                    if item is _BREAK:
                        break
                    items.append(item)
            else:
                for _ in range(arg):
                    items.append(self.decode())
            return items
        if major == 5:
            result: dict[Any, Any] = {}
            if arg is None:
                while True:
                    key = self.decode()
                    if key is _BREAK:
                        break
                    result[_hashable(key)] = self.decode()
            else:
                for _ in range(arg):
                    key = self.decode()
                    result[_hashable(key)] = self.decode()
            return result
        if major == 6:
            # Tag: decode the tagged item and drop the tag.
            return self.decode()
        raise CBORDecodeError(f"unknown major type {major}")  # pragma: no cover


def _hashable(key: Any) -> Any:
    if isinstance(key, list):
        return tuple(_hashable(k) for k in key)
    if isinstance(key, dict):
        return tuple(sorted((k, _hashable(v)) for k, v in key.items()))
    return key


def loads_prefix(data: bytes) -> tuple[Any, int]:
    """Decode the first CBOR item of ``data``; return (item, consumed_bytes)."""
    decoder = _Decoder(bytes(data))
    item = decoder.decode()
    if item is _BREAK:
        raise CBORDecodeError("unexpected break")
    return item, decoder.pos


def loads(data: bytes) -> Any:
    """Decode a single CBOR item (trailing bytes are ignored)."""
    item, _ = loads_prefix(data)
    return item
