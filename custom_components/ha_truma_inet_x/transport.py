"""Truma iNet X BLE transport layer.

Re-implementation of com.truma.inetx.panel.manager.transport.TrumaTransport
(TrumaTransportWriteFSM / TrumaTransportReadFSM).

CMD characteristic (FC314001, write with response + notify):

    app -> panel  0x01 <len LE16>       InitDataTransfer   (we want to send len bytes)
    panel -> app  0x81 <ready>          InitDataWriteTransferResponse (0x00 = READY)
    app -> panel  ... payload on DATA_WRITE (FC314002, write without response,
                      sliced into MTU-3 sized chunks)
    panel -> app  0xF0 <status>         AckDataTransfer (0x01 = TRANSFER_COMPLETED)

    panel -> app  0x83 <len LE16>       Announcement       (panel wants to send len bytes)
    app -> panel  0x03 <ready>          AckDataTransmit    (0x00 = READY)
    panel -> app  ... len bytes on DATA_READ (FC314003, notify, may be several chunks)
    app -> panel  0xF0 <status>         AckDataTransfer (0x01 = TRANSFER_COMPLETED)

    panel -> app  0x79 abort / 0xFC protocol error / 0xFE invalid parameter
                  -> reset transport
    panel -> app  0xFF invalid opcode -> logged

Sending and receiving run independently (full duplex), exactly like the app.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable

from .const import TRANSPORT_TIMEOUT

_LOGGER = logging.getLogger(__name__)

OP_INIT_DATA_TRANSFER = 0x01
OP_ACK_DATA_TRANSMIT = 0x03
OP_INIT_DATA_TRANSFER_RESPONSE = 0x81
OP_ANNOUNCEMENT = 0x83
OP_ACK_DATA_TRANSFER = 0xF0
OP_ABORT_DATA_TRANSFER = 0xF9  # command (app -> panel)
OP_ABORT_RESPONSE = 0x79
OP_PROTOCOL_ERROR = 0xFC
OP_INVALID_PARAMETER = 0xFE
OP_INVALID_OPCODE = 0xFF

READY = 0x00
NOT_READY = 0x01

ACK_TRANSFER_COMPLETED = 0x01
ACK_NOT_READY = 0x02
ACK_TOO_MUCH_DATA = 0x03
ACK_TIMEOUT_ON_SEND = 0x04
ACK_INTERNAL_ERROR = 0x05
ACK_TIMEOUT_ON_RECEIVE = 0x06


class TransportError(Exception):
    """Raised when a transfer to the panel failed."""


WriteFunc = Callable[[bytes], Awaitable[None]]


class TrumaTransport:
    """Full duplex packet transport on top of the CMD / DATA characteristics."""

    def __init__(
        self,
        write_cmd: WriteFunc,
        write_data: WriteFunc,
        on_message: Callable[[bytes], None],
        chunk_size: Callable[[], int],
        timeout: float = TRANSPORT_TIMEOUT,
    ) -> None:
        self._write_cmd_raw = write_cmd
        self._write_data_raw = write_data
        self._on_message = on_message
        self._chunk_size = chunk_size
        self._timeout = timeout

        self._send_lock = asyncio.Lock()
        self._gatt_lock = asyncio.Lock()
        self._send_waiter: asyncio.Future[bytes] | None = None

        self._rx_expected: int | None = None
        self._rx_buffer = bytearray()
        self._rx_pending: deque[int] = deque()
        self._rx_timeout: asyncio.TimerHandle | None = None
        self._tasks: set[asyncio.Task] = set()
        self._closed = False

        self.stats = {
            "sent": 0,
            "received": 0,
            "send_errors": 0,
            "receive_errors": 0,
            "resets": 0,
        }

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _spawn(self, coro: Awaitable) -> None:
        if self._closed:
            if asyncio.iscoroutine(coro):
                coro.close()
            return
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            _LOGGER.debug("Transport task failed: %s", task.exception())

    async def _write_cmd(self, data: bytes) -> None:
        async with self._gatt_lock:
            _LOGGER.debug("TX CMD  %s", data.hex())
            await self._write_cmd_raw(data)

    async def _write_data(self, data: bytes) -> None:
        size = max(20, self._chunk_size())
        async with self._gatt_lock:
            for offset in range(0, len(data), size):
                chunk = data[offset : offset + size]
                _LOGGER.debug("TX DATA %s", chunk.hex())
                await self._write_data_raw(chunk)

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def close(self) -> None:
        """Stop all activity (called on disconnect)."""
        self._closed = True
        self._cancel_rx_timeout()
        self._fail_send_waiter(TransportError("transport closed"))
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()

    async def send(self, payload: bytes) -> None:
        """Send one complete message to the panel (raises TransportError)."""
        if len(payload) > 0xFFFF:
            raise TransportError("message too large")
        async with self._send_lock:
            if self._closed:
                raise TransportError("transport closed")
            loop = asyncio.get_running_loop()

            # 1) InitDataTransfer -> wait for 0x81
            self._send_waiter = loop.create_future()
            try:
                await self._write_cmd(
                    bytes([OP_INIT_DATA_TRANSFER]) + len(payload).to_bytes(2, "little")
                )
                response = await asyncio.wait_for(self._send_waiter, self._timeout)
            except TimeoutError as err:
                self.stats["send_errors"] += 1
                self._spawn(
                    self._write_cmd(bytes([OP_ACK_DATA_TRANSFER, ACK_TIMEOUT_ON_SEND]))
                )
                raise TransportError("panel did not answer InitDataTransfer") from err
            finally:
                self._send_waiter = None

            if response[0] != OP_INIT_DATA_TRANSFER_RESPONSE:
                self.stats["send_errors"] += 1
                raise TransportError(f"unexpected response {response.hex()}")
            if len(response) > 1 and response[1] != READY:
                self.stats["send_errors"] += 1
                raise TransportError("panel not ready to receive")

            # 2) payload -> wait for 0xF0 0x01
            self._send_waiter = loop.create_future()
            try:
                await self._write_data(payload)
                ack = await asyncio.wait_for(self._send_waiter, self._timeout)
            except TimeoutError as err:
                self.stats["send_errors"] += 1
                self._spawn(
                    self._write_cmd(bytes([OP_ACK_DATA_TRANSFER, ACK_TIMEOUT_ON_SEND]))
                )
                raise TransportError("panel did not acknowledge data") from err
            finally:
                self._send_waiter = None

            if ack[0] != OP_ACK_DATA_TRANSFER or (
                len(ack) > 1 and ack[1] != ACK_TRANSFER_COMPLETED
            ):
                self.stats["send_errors"] += 1
                raise TransportError(f"panel rejected data: {ack.hex()}")
            self.stats["sent"] += 1

    # ------------------------------------------------------------------
    # notifications
    # ------------------------------------------------------------------
    def handle_cmd_notification(self, data: bytes) -> None:
        """Feed a notification of the CMD characteristic."""
        if not data or self._closed:
            return
        data = bytes(data)
        _LOGGER.debug("RX CMD  %s", data.hex())
        opcode = data[0]

        if opcode == OP_INIT_DATA_TRANSFER_RESPONSE:
            self._resolve_send_waiter(data)
        elif opcode == OP_ACK_DATA_TRANSFER:
            if len(data) > 1 and data[1] == ACK_TIMEOUT_ON_SEND:
                # The panel gave up sending to us -> reset the read path
                _LOGGER.debug("Panel reported TIMEOUT_ON_SEND, resetting receive path")
                self._reset_rx()
                self._start_next_rx()
            else:
                self._resolve_send_waiter(data)
        elif opcode == OP_ANNOUNCEMENT:
            if len(data) < 3:
                _LOGGER.debug("Malformed announcement %s", data.hex())
                return
            length = int.from_bytes(data[1:3], "little")
            if self._rx_expected is None:
                self._start_rx(length)
            else:
                self._rx_pending.append(length)
        elif opcode in (OP_ABORT_RESPONSE, OP_PROTOCOL_ERROR, OP_INVALID_PARAMETER):
            _LOGGER.debug("Panel reported transport error %s, resetting", data.hex())
            self.stats["resets"] += 1
            self._fail_send_waiter(
                TransportError(f"transfer aborted by panel ({data.hex()})")
            )
            self._reset_rx()
            self._rx_pending.clear()
        elif opcode == OP_INVALID_OPCODE:
            _LOGGER.debug("Panel reported invalid opcode: %s", data.hex())
        else:
            _LOGGER.debug("Unhandled CMD notification %s", data.hex())

    def handle_data_notification(self, data: bytes) -> None:
        """Feed a notification of the DATA_READ characteristic."""
        if not data or self._closed:
            return
        _LOGGER.debug("RX DATA %s", bytes(data).hex())
        self._rx_buffer += data
        self._check_rx_complete()

    # ------------------------------------------------------------------
    # receive path
    # ------------------------------------------------------------------
    def _fail_send_waiter(self, err: Exception) -> None:
        fut = self._send_waiter
        if fut is not None and not fut.done():
            fut.set_exception(err)
            fut.exception()  # mark as retrieved, the sender may be gone already

    def _resolve_send_waiter(self, data: bytes) -> None:
        if self._send_waiter and not self._send_waiter.done():
            self._send_waiter.set_result(data)
        else:
            _LOGGER.debug(
                "Unexpected CMD response %s (no transfer pending)", data.hex()
            )

    def _start_rx(self, length: int) -> None:
        self._rx_expected = length
        loop = asyncio.get_running_loop()
        self._cancel_rx_timeout()
        self._rx_timeout = loop.call_later(self._timeout, self._rx_timed_out)
        self._spawn(self._write_cmd(bytes([OP_ACK_DATA_TRANSMIT, READY])))
        # data might already be buffered (it never should, but be tolerant)
        self._check_rx_complete()

    def _start_next_rx(self) -> None:
        if self._rx_expected is None and self._rx_pending:
            self._start_rx(self._rx_pending.popleft())

    def _check_rx_complete(self) -> None:
        if self._rx_expected is None:
            return
        if len(self._rx_buffer) < self._rx_expected:
            return
        message = bytes(self._rx_buffer[: self._rx_expected])
        del self._rx_buffer[: self._rx_expected]
        if self._rx_buffer:
            _LOGGER.debug(
                "Dropping %d surplus bytes after message", len(self._rx_buffer)
            )
            self._rx_buffer.clear()
        self._rx_expected = None
        self._cancel_rx_timeout()
        self.stats["received"] += 1
        self._spawn(
            self._write_cmd(bytes([OP_ACK_DATA_TRANSFER, ACK_TRANSFER_COMPLETED]))
        )
        try:
            self._on_message(message)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Error while handling message %s", message.hex())
        self._start_next_rx()

    def _rx_timed_out(self) -> None:
        self._rx_timeout = None
        if self._rx_expected is None:
            return
        _LOGGER.debug(
            "Timeout while receiving %d bytes (got %d)",
            self._rx_expected,
            len(self._rx_buffer),
        )
        self.stats["receive_errors"] += 1
        self._reset_rx()
        self._spawn(
            self._write_cmd(bytes([OP_ACK_DATA_TRANSFER, ACK_TIMEOUT_ON_RECEIVE]))
        )
        self._start_next_rx()

    def _cancel_rx_timeout(self) -> None:
        if self._rx_timeout is not None:
            self._rx_timeout.cancel()
            self._rx_timeout = None

    def _reset_rx(self) -> None:
        self._cancel_rx_timeout()
        self._rx_expected = None
        self._rx_buffer.clear()
