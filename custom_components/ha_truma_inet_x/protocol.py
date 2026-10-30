"""Truma iNet X message layer (TruMessageV3 / V5 payload encoding).

Layout reverse engineered from com.truma.inetx 3.5.0
(communication/messagelayer/TruMessageV3.java, TruV5MessageEncoder.java,
TruV5MessageDecoder.java):

TruMessageV3 (16 byte header, little endian)
    0..1   destination device id
    2..3   source device id
    4..5   packet size = len(payload) + 9
    6      control type  (TruControlType)
    7..15  segmentation header (all zero for normal messages)
    16..   payload

Payload for control type 0x01 (registration)   : [type, correlation, CBOR...]
Payload for control type 0x02 (device discovery): [type, reserved, CBOR...]
Payload for control type 0x03 (message broker)  : [mbp type, correlation, CBOR...]

This module has no Home Assistant dependencies so it can be unit tested.
"""

from __future__ import annotations

import random
import struct
import time
from dataclasses import dataclass, field
from typing import Any

from . import cbor
from .const import (
    ADDR_BROADCAST,
    ADDR_MESSAGE_BROKER,
    TYPE_NUMBER,
    TYPE_STRING,
    TYPE_TIME,
)

HEADER_SIZE = 16
SEGMENTATION_HEADER_SIZE = 9

# TruControlType
CTRL_DEVICE_REGISTRATION = 0x01
CTRL_DEVICE_DISCOVERY = 0x02
CTRL_MESSAGE_BROKER = 0x03
CTRL_FILE_MANAGER = 0x04
CTRL_SECURITY = 0x05
CTRL_FIRMWARE_UPDATE = 0x06
CTRL_NONE = 0x0A

# TruMessageType (registration / device discovery)
MSG_REQUEST = 0x01
MSG_RESPONSE = 0x02
MSG_DEREGISTER = 0x03

# TruMessageBrokerType
MBP_INFO = 0x00
MBP_WRITE = 0x01
MBP_SUBSCRIBE = 0x02
MBP_BINARY = 0x03
MBP_PARAMETER_DISCOVERY = 0x04
MBP_SUBSCRIBE_RESPONSE = 0x82
MBP_PARAMETER_DISCOVERY_RESPONSE = 0x84

# Segmentation header flags (byte 7)
SEG_IS_SEGMENTED = 0x01


@dataclass(slots=True)
class Frame:
    """A decoded TruMessageV3 frame."""

    destination: int
    source: int
    control: int
    segmentation: bytes
    payload: bytes
    packet_size: int = 0

    @property
    def is_segmented(self) -> bool:
        return bool(self.segmentation and self.segmentation[0] & SEG_IS_SEGMENTED)


@dataclass(slots=True)
class Message:
    """A decoded higher level message."""

    frame: Frame
    kind: str  # registration | deregistration | device_discovery | mbp | other
    sub_type: int | None = None
    correlation: int | None = None
    data: Any = None
    raw_payload: bytes = b""
    error: str | None = None

    @property
    def source(self) -> int:
        return self.frame.source

    @property
    def destination(self) -> int:
        return self.frame.destination


# --------------------------------------------------------------------------
# Frame encoding / decoding
# --------------------------------------------------------------------------
def build_frame(
    destination: int,
    source: int,
    control: int,
    payload: bytes,
    segmentation: bytes | None = None,
) -> bytes:
    """Serialise a TruMessageV3 frame."""
    seg = segmentation if segmentation is not None else bytes(SEGMENTATION_HEADER_SIZE)
    if len(seg) != SEGMENTATION_HEADER_SIZE:
        raise ValueError("segmentation header must be 9 bytes")
    return (
        struct.pack(
            "<HHHB",
            destination & 0xFFFF,
            source & 0xFFFF,
            (len(payload) + SEGMENTATION_HEADER_SIZE) & 0xFFFF,
            control & 0xFF,
        )
        + seg
        + bytes(payload)
    )


def parse_frame(data: bytes) -> Frame | None:
    """Parse a TruMessageV3 frame (returns None if too short)."""
    if len(data) < HEADER_SIZE:
        return None
    destination, source, packet_size, control = struct.unpack_from("<HHHB", data, 0)
    return Frame(
        destination=destination,
        source=source,
        control=control,
        segmentation=bytes(data[7:16]),
        payload=bytes(data[16:]),
        packet_size=packet_size,
    )


def _decode_cbor(raw: bytes) -> tuple[Any, str | None]:
    if not raw:
        return None, None
    try:
        return cbor.loads(raw), None
    except Exception as err:  # noqa: BLE001 - we never want to crash on bad data
        return None, str(err)


def decode_message(data: bytes) -> Message | None:
    """Decode a complete message as received from the transport layer."""
    frame = parse_frame(data)
    if frame is None:
        return None

    payload = frame.payload
    if frame.control in (
        CTRL_DEVICE_REGISTRATION,
        CTRL_DEVICE_DISCOVERY,
        CTRL_MESSAGE_BROKER,
    ):
        if len(payload) < 2 or frame.is_segmented:
            return Message(frame=frame, kind="other", raw_payload=payload)
        sub_type = payload[0]
        second = payload[1]
        body, error = _decode_cbor(payload[2:])
        if frame.control == CTRL_DEVICE_REGISTRATION:
            kind = "deregistration" if sub_type == MSG_DEREGISTER else "registration"
        elif frame.control == CTRL_DEVICE_DISCOVERY:
            kind = "device_discovery"
        else:
            kind = "mbp"
        return Message(
            frame=frame,
            kind=kind,
            sub_type=sub_type,
            correlation=second,
            data=body,
            raw_payload=payload,
            error=error,
        )
    return Message(frame=frame, kind="other", raw_payload=payload)


# --------------------------------------------------------------------------
# Message builders (TruV5MessageEncoder)
# --------------------------------------------------------------------------
def random_correlation_id() -> int:
    return random.randint(0, 255)


def build_registration_request(
    protocol_version: tuple[int, int], correlation: int | None = None
) -> bytes:
    """Registration request: broadcast -> message broker, {"pv": [major, minor]}."""
    corr = random_correlation_id() if correlation is None else correlation
    body = cbor.dumps({"pv": [protocol_version[0], protocol_version[1]]})
    return build_frame(
        ADDR_MESSAGE_BROKER,
        ADDR_BROADCAST,
        CTRL_DEVICE_REGISTRATION,
        bytes([MSG_REQUEST, corr & 0xFF]) + body,
    )


def build_device_discovery_request(source: int) -> bytes:
    return build_frame(
        ADDR_MESSAGE_BROKER,
        source,
        CTRL_DEVICE_DISCOVERY,
        bytes([MSG_REQUEST, 0x00]),
    )


def _mbp(mbp_type: int, payload: bytes, correlation: int = 0) -> bytes:
    return bytes([mbp_type & 0xFF, correlation & 0xFF]) + payload


def build_subscribe(source: int, topics: list[str]) -> bytes:
    return build_frame(
        ADDR_MESSAGE_BROKER,
        source,
        CTRL_MESSAGE_BROKER,
        _mbp(MBP_SUBSCRIBE, cbor.dumps({"tn": list(topics)})),
    )


def build_parameter_discovery_request(source: int, destination: int) -> bytes:
    return build_frame(
        destination,
        source,
        CTRL_MESSAGE_BROKER,
        _mbp(MBP_PARAMETER_DISCOVERY, b""),
    )


def build_write(
    source: int, destination: int, topic: str, param: str, value: Any
) -> bytes:
    """ParameterWrite {"tn", "pn", "v"} as MBP WRITE_MESSAGE."""
    body = cbor.dumps({"tn": topic, "pn": param, "v": value})
    return build_frame(destination, source, CTRL_MESSAGE_BROKER, _mbp(MBP_WRITE, body))


def build_parameter_discovery_response(
    source: int, destination: int, body: dict[str, Any]
) -> bytes:
    return build_frame(
        destination,
        source,
        CTRL_MESSAGE_BROKER,
        _mbp(MBP_PARAMETER_DISCOVERY_RESPONSE, cbor.dumps(body)),
    )


def build_broadcast_info(source: int, body: dict[str, Any]) -> bytes:
    return build_frame(
        ADDR_BROADCAST,
        source,
        CTRL_MESSAGE_BROKER,
        _mbp(MBP_INFO, cbor.dumps(body)),
    )


def _param(topic: str, name: str, value_type: int, value: Any) -> dict[str, Any]:
    # Same field order as Jackson writes ParameterRead (tn, pn, type, v)
    return {"tn": topic, "pn": name, "type": value_type, "v": value}


def utc_offset_minutes(now: float | None = None) -> int:
    now = time.time() if now is None else now
    local = time.localtime(now)
    return int((local.tm_gmtoff or 0) // 60)


@dataclass(slots=True)
class Identity:
    """Identity the panel uses to recognise this 'smartphone'."""

    muid: str
    uuid: str
    username: str


def build_identity_messages(
    source: int,
    destination: int,
    identity: Identity,
    now: float | None = None,
) -> list[bytes]:
    """Messages the app sends when the panel asks for its parameters.

    Mirrors TruvanSubscribeForParameterDiscoveryRequestService.parameterDiscoveryRequests:
    4 PARAMETER_DISCOVERY_RESPONSE messages followed by 4 broadcast INFO messages.
    """
    now = time.time() if now is None else now
    ts = int(now)
    lot = utc_offset_minutes(now)
    msgs = [
        build_parameter_discovery_response(
            source,
            destination,
            {
                "avail": 1,
                "topics": [
                    {
                        "tn": "MobileIdentity",
                        "parameters": [
                            _param(
                                "MobileIdentity", "Muid", TYPE_STRING, identity.muid
                            ),
                            _param(
                                "MobileIdentity", "Uuid", TYPE_STRING, identity.uuid
                            ),
                        ],
                    }
                ],
            },
        ),
        build_parameter_discovery_response(
            source,
            destination,
            {
                "avail": 1,
                "topics": [
                    {
                        "tn": "MobileIdentity",
                        "parameters": [
                            _param(
                                "MobileIdentity",
                                "UserName",
                                TYPE_STRING,
                                identity.username[:64],
                            )
                        ],
                    }
                ],
            },
        ),
        build_parameter_discovery_response(
            source,
            destination,
            {
                "avail": 1,
                "topics": [
                    {
                        "tn": "SystemTime",
                        "parameters": [
                            _param("SystemTime", "Time", TYPE_TIME, ts),
                            _param("SystemTime", "Lot", TYPE_NUMBER, lot),
                        ],
                    }
                ],
            },
        ),
        build_parameter_discovery_response(source, destination, {"LastMessage": 1}),
        build_broadcast_info(source, _param("SystemTime", "Time", TYPE_TIME, ts)),
        build_broadcast_info(source, _param("SystemTime", "Lot", TYPE_NUMBER, lot)),
        build_broadcast_info(
            source, _param("MobileIdentity", "Muid", TYPE_STRING, identity.muid)
        ),
        build_broadcast_info(
            source, _param("MobileIdentity", "Uuid", TYPE_STRING, identity.uuid)
        ),
    ]
    return msgs


# --------------------------------------------------------------------------
# Payload helpers
# --------------------------------------------------------------------------
def parse_protocol_version(software_revision: str | None) -> tuple[int, int]:
    """TruProtocolVersion.fromSoftwareRevision (defaults to the latest: 5.1)."""
    if not software_revision:
        return (5, 1)
    text = software_revision.strip("\x00 \t\r\n")
    parts: list[int] = []
    for chunk in text.replace("-", ".").split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    if not parts:
        return (5, 1)
    major = parts[0]
    minor = parts[1] if len(parts) > 1 else 0
    if major == 5 and minor in (0, 1):
        return (5, minor)
    if major in (2, 3, 4):
        return (major, 0)
    return (5, 1)


@dataclass(slots=True)
class EnumOption:
    value: int
    name: str
    available: bool = True


@dataclass(slots=True)
class ParameterInfo:
    """Metadata + current value of one topic parameter (ParameterRead)."""

    topic: str
    name: str
    value: Any = None
    value_type: int | None = None
    permission: int | None = None
    available: bool = True
    minimum: int | None = None
    maximum: int | None = None
    enum: list[EnumOption] = field(default_factory=list)
    description: str | None = None
    device: int | None = None
    updated: float = 0.0

    @property
    def writable(self) -> bool:
        # PermissionType: READ_ONLY=0, WRITE_ONLY=1, READ_WRITE_ONLY=2
        if self.permission is None:
            return True
        return self.permission in (1, 2)

    def enum_name(self, value: Any | None = None) -> str | None:
        val = self.value if value is None else value
        for opt in self.enum:
            if opt.value == val:
                return opt.name
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "name": self.name,
            "value": self.value,
            "type": self.value_type,
            "perm": self.permission,
            "avail": self.available,
            "min": self.minimum,
            "max": self.maximum,
            "enum": [{"v": o.value, "n": o.name, "a": o.available} for o in self.enum],
            "desc": self.description,
            "device": f"0x{self.device:04X}" if self.device is not None else None,
        }


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return default


def parse_parameter_read(
    raw: dict[str, Any], topic: str | None = None
) -> ParameterInfo | None:
    """Turn a CBOR ParameterRead map into a ParameterInfo."""
    if not isinstance(raw, dict):
        return None
    tn = raw.get("tn") or topic
    pn = raw.get("pn")
    if not isinstance(tn, str) or not isinstance(pn, str):
        return None
    enum_opts: list[EnumOption] = []
    raw_enum = raw.get("enum")
    if isinstance(raw_enum, list):
        for item in raw_enum:
            if isinstance(item, dict) and isinstance(item.get("v"), int):
                enum_opts.append(
                    EnumOption(
                        value=item["v"],
                        name=str(item.get("n", item["v"])),
                        available=_as_bool(item.get("a"), True),
                    )
                )
    vtype = raw.get("type")
    perm = raw.get("perm")
    minimum = raw.get("min")
    maximum = raw.get("max")
    return ParameterInfo(
        topic=tn,
        name=pn,
        value=raw.get("v"),
        value_type=vtype if isinstance(vtype, int) else None,
        permission=perm if isinstance(perm, int) else None,
        available=_as_bool(raw.get("avail"), True),
        minimum=minimum if isinstance(minimum, (int, float)) else None,
        maximum=maximum if isinstance(maximum, (int, float)) else None,
        enum=enum_opts,
        description=raw.get("desc") if isinstance(raw.get("desc"), str) else None,
    )


def iter_discovery_parameters(body: Any):
    """Yield ParameterInfo objects contained in a ParameterDiscoveryResponse."""
    if not isinstance(body, dict):
        return
    topics = body.get("topics")
    if not isinstance(topics, list):
        return
    for topic in topics:
        if not isinstance(topic, dict):
            continue
        tn = topic.get("tn")
        params = topic.get("parameters")
        if not isinstance(params, list):
            continue
        for raw in params:
            info = parse_parameter_read(raw, tn)
            if info is not None:
                yield info


def is_last_message(body: Any) -> bool:
    return isinstance(body, dict) and _as_bool(body.get("LastMessage"), False)
