"""Minimal BlueZ management (mgmt) API client.

Used only during pairing to reproduce what ``btmgmt sc off`` / ``btmgmt io-cap 3``
do: the iNet X panel bonds with "LE legacy pairing / Just Works" (no code is
shown).  With Secure Connections enabled BlueZ may pair, but the panel does not
accept the connection as an app connection afterwards
(see node-red-contrib-truma-inetx, docs/reverse-engineering/initial-findings.md).

The socket is opened through ``btsocket`` (shipped with Home Assistant, used
by bluetooth-auto-recovery) and every call runs in an executor.
"""

from __future__ import annotations

import logging
import socket
import struct
import time
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

OP_READ_INFO = 0x0004
OP_SET_IO_CAPABILITY = 0x0018
OP_SET_SECURE_CONN = 0x002D

EV_CMD_COMPLETE = 0x0001
EV_CMD_STATUS = 0x0002

SETTING_POWERED = 1 << 0
SETTING_SECURE_CONN = 1 << 11

IO_CAP_NO_INPUT_NO_OUTPUT = 0x03


class MgmtError(Exception):
    """mgmt socket not usable or command failed."""


@dataclass(slots=True)
class ControllerInfo:
    index: int
    address: str
    current_settings: int

    @property
    def secure_connections(self) -> bool:
        return bool(self.current_settings & SETTING_SECURE_CONN)


def _open_socket() -> socket.socket:
    try:
        from btsocket import btmgmt_socket
    except ImportError as err:  # pragma: no cover
        raise MgmtError(f"btsocket not available: {err}") from err
    try:
        sock = btmgmt_socket.open()
    except BaseException as err:  # btsocket raises BaseException subclasses
        raise MgmtError(f"cannot open mgmt socket: {err}") from err
    sock.settimeout(3.0)
    return sock


def _close_socket(sock: socket.socket) -> None:
    try:
        from btsocket import btmgmt_socket

        btmgmt_socket.close(sock)
    except BaseException:  # noqa: BLE001
        try:
            sock.close()
        except OSError:
            pass


def _command(
    sock: socket.socket, opcode: int, index: int, params: bytes = b""
) -> bytes:
    sock.send(struct.pack("<HHH", opcode, index, len(params)) + params)
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            data = sock.recv(1024)
        except TimeoutError as err:
            raise MgmtError(f"timeout waiting for mgmt opcode 0x{opcode:04X}") from err
        if len(data) < 6:
            continue
        event, ev_index, length = struct.unpack_from("<HHH", data, 0)
        payload = data[6 : 6 + length]
        if event not in (EV_CMD_COMPLETE, EV_CMD_STATUS) or len(payload) < 3:
            continue
        ev_opcode, status = struct.unpack_from("<HB", payload, 0)
        if ev_opcode != opcode or ev_index != index:
            continue
        if status != 0:
            raise MgmtError(
                f"mgmt opcode 0x{opcode:04X} failed with status 0x{status:02X}"
            )
        return payload[3:]
    raise MgmtError(f"no answer for mgmt opcode 0x{opcode:04X}")


def read_info(index: int) -> ControllerInfo:
    sock = _open_socket()
    try:
        data = _command(sock, OP_READ_INFO, index)
    finally:
        _close_socket(sock)
    if len(data) < 17:
        raise MgmtError("short controller info")
    address = ":".join(f"{b:02X}" for b in reversed(data[0:6]))
    current = struct.unpack_from("<I", data, 13)[0]
    return ControllerInfo(index=index, address=address, current_settings=current)


def set_secure_connections(index: int, enabled: bool) -> None:
    sock = _open_socket()
    try:
        _command(sock, OP_SET_SECURE_CONN, index, bytes([1 if enabled else 0]))
    finally:
        _close_socket(sock)


def set_io_capability(index: int, capability: int) -> None:
    sock = _open_socket()
    try:
        _command(sock, OP_SET_IO_CAPABILITY, index, bytes([capability]))
    finally:
        _close_socket(sock)


class LegacyPairingSettings:
    """Context: Secure Connections off + NoInputNoOutput, restored afterwards."""

    def __init__(self, index: int) -> None:
        self.index = index
        self.applied = False
        self._restore_sc = False

    def apply(self) -> None:
        info = read_info(self.index)
        _LOGGER.debug(
            "hci%d settings before pairing: 0x%08X", self.index, info.current_settings
        )
        if info.secure_connections:
            set_secure_connections(self.index, False)
            self._restore_sc = True
        try:
            set_io_capability(self.index, IO_CAP_NO_INPUT_NO_OUTPUT)
        except MgmtError as err:
            _LOGGER.debug("Could not set IO capability: %s", err)
        self.applied = True

    def restore(self) -> None:
        if self._restore_sc:
            try:
                set_secure_connections(self.index, True)
            except MgmtError as err:
                _LOGGER.warning(
                    "Could not re-enable Bluetooth Secure Connections on hci%d: %s",
                    self.index,
                    err,
                )
            self._restore_sc = False
        self.applied = False
