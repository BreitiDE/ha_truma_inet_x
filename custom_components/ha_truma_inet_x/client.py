"""BLE client for the Truma iNet X panel.

Connection + session handling modelled after the Truma app
(TruvanBluetoothCore, BTCommunicationManager, TruMessageBroker):

 1. connect, (MTU), subscribe CMD + DATA_READ notifications
 2. read Software Revision (0x2A28) -> protocol version (5.1 = latest)
 3. registration request  {"pv": [5, 1]}            -> {"addr": <our address>}
 4. answer the panel's parameter discovery request with our identity
    (MobileIdentity Muid/Uuid/UserName + SystemTime)
 5. subscribe all topics in batches of 10 (wait for every SubscribeResponse)
 6. device discovery                                -> list of bus devices
 7. parameter discovery for every registered device -> topic registry
 8. keep the connection: INFO messages update the registry, writes are sent
    to the device that publishes the topic
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import Any

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    establish_connection,
)

from .const import (
    ACTIVE_ON,
    ADDR_BROADCAST,
    ADDR_MESSAGE_BROKER,
    ALL_TOPICS,
    CHAR_CMD,
    CHAR_CMD_ALT,
    CHAR_DATA_READ,
    CHAR_DATA_WRITE,
    CHAR_SOFTWARE_REVISION,
    CMD_NOTIFY_4001,
    CMD_NOTIFY_4004,
    CMD_NOTIFY_AUTO,
    MESSAGE_RESPONSE_TIMEOUT,
    RECONNECT_MAX_DELAY,
    RECONNECT_MIN_DELAY,
    SUBSCRIBE_BATCH_DELAY,
    SUBSCRIBE_BATCH_SIZE,
    WRITE_MIN_INTERVAL,
    WRITE_SEQUENCE_DELAY,
)
from .protocol import (
    MBP_BINARY,
    MBP_INFO,
    MBP_PARAMETER_DISCOVERY,
    MBP_PARAMETER_DISCOVERY_RESPONSE,
    MBP_SUBSCRIBE_RESPONSE,
    MBP_WRITE,
    MSG_RESPONSE,
    Identity,
    Message,
    build_device_discovery_request,
    build_identity_messages,
    build_parameter_discovery_request,
    build_registration_request,
    build_subscribe,
    build_write,
    decode_message,
    is_last_message,
    iter_discovery_parameters,
    parse_parameter_read,
    parse_protocol_version,
)
from .state import TrumaState
from .transport import TransportError, TrumaTransport

_LOGGER = logging.getLogger(__name__)

IDENTITY_FALLBACK_DELAY = 8.0
PARAMETER_DISCOVERY_TIMEOUT = 20.0


class TrumaError(Exception):
    """Generic error talking to the panel."""


class RegistrationError(TrumaError):
    """The panel refused our registration."""


def _uuid_eq(a: str, b: str) -> bool:
    return a.lower() == b.lower()


class TrumaInetXClient:
    """Keeps a persistent BLE session with an iNet X panel."""

    def __init__(
        self,
        address: str,
        identity: Identity,
        ble_device_callback: Callable[[], BLEDevice | None],
        name: str = "Truma iNet X",
        cmd_notify: str = CMD_NOTIFY_AUTO,
        on_connection_change: Callable[[bool], None] | None = None,
        on_ready: Callable[[], None] | None = None,
        on_pause_change: Callable[[bool], None] | None = None,
    ) -> None:
        self.address = address
        self.identity = identity
        self.name = name
        self.cmd_notify = cmd_notify
        self.state = TrumaState()
        self._ble_device_callback = ble_device_callback
        self._on_connection_change = on_connection_change
        self._on_ready = on_ready
        self._on_pause_change = on_pause_change
        self._paused_until: float | None = None
        self._resume_event = asyncio.Event()

        self._client: BleakClientWithServiceCache | None = None
        self._transport: TrumaTransport | None = None
        self._chunk_size = 20
        self._connected = False
        self._ready = asyncio.Event()
        self._disconnected = asyncio.Event()
        self._run_task: asyncio.Task | None = None
        self._running = False
        self._tasks: set[asyncio.Task] = set()

        # request / response bookkeeping
        self._registration_future: asyncio.Future[dict[str, Any]] | None = None
        self._subscribe_future: asyncio.Future[Any] | None = None
        self._device_discovery_future: asyncio.Future[None] | None = None
        self._pd_futures: dict[int, asyncio.Future[None]] = {}
        self._identity_sent = False
        self._write_lock = asyncio.Lock()
        self._last_write = 0.0
        self.last_error: str | None = None
        self.connect_count = 0

    # ------------------------------------------------------------------
    # properties
    # ------------------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    @property
    def transport_stats(self) -> dict[str, int]:
        return dict(self._transport.stats) if self._transport else {}

    async def wait_ready(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
        except TimeoutError:
            return False
        return True

    # ------------------------------------------------------------------
    # life cycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._run_task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        self._running = False
        if self._run_task:
            self._run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._run_task
            self._run_task = None
        await self._disconnect()

    async def _run(self) -> None:
        delay = RECONNECT_MIN_DELAY
        while self._running:
            if await self._wait_while_paused():
                delay = RECONNECT_MIN_DELAY
                continue
            try:
                await self.connect_and_setup()
                delay = RECONNECT_MIN_DELAY
                await self._disconnected.wait()
                if not self.paused:
                    _LOGGER.info("%s: connection lost", self.name)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                if not self.paused:
                    self.last_error = str(err)
                    _LOGGER.warning("%s: connection failed: %s", self.name, err)
                    _LOGGER.debug("connection failure details", exc_info=True)
            await self._disconnect()
            if not self._running:
                break
            if self.paused:
                continue
            _LOGGER.debug("%s: reconnecting in %.0fs", self.name, delay)
            self._resume_event.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._resume_event.wait(), delay)
            delay = min(delay * 2, RECONNECT_MAX_DELAY)

    # ------------------------------------------------------------------
    # releasing Bluetooth for the smartphone app
    # ------------------------------------------------------------------
    @property
    def paused(self) -> bool:
        return self._paused_until is not None and time.monotonic() < self._paused_until

    @property
    def pause_remaining(self) -> float:
        if not self.paused:
            return 0.0
        return max(0.0, (self._paused_until or 0) - time.monotonic())

    async def _wait_while_paused(self) -> bool:
        """Block while paused; True if a pause was waited for."""
        if self._paused_until is None:
            return False
        waited = False
        while self._running and self.paused:
            waited = True
            self._resume_event.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._resume_event.wait(), self.pause_remaining)
        self._paused_until = None
        _LOGGER.info("%s: Bluetooth release ended, reconnecting", self.name)
        if self._on_pause_change:
            self._on_pause_change(False)
        return waited

    async def release(self, seconds: float) -> None:
        """Disconnect and stay away so the smartphone app can connect."""
        self._paused_until = time.monotonic() + max(1.0, seconds)
        _LOGGER.info("%s: releasing Bluetooth for %.0f s", self.name, seconds)
        if self._on_pause_change:
            self._on_pause_change(True)
        self._resume_event.set()
        client = self._client
        if client is not None:
            with contextlib.suppress(Exception):
                await client.disconnect()
        self._disconnected.set()

    def resume(self) -> None:
        """End a Bluetooth release early."""
        if self._paused_until is not None:
            self._paused_until = time.monotonic()
        self._resume_event.set()

    # ------------------------------------------------------------------
    # connection
    # ------------------------------------------------------------------
    async def connect_and_setup(self) -> None:
        """Connect, register and run the complete discovery."""
        await self.connect()
        await self.setup_session()

    async def connect(self) -> None:
        device = self._ble_device_callback()
        if device is None:
            raise TrumaError(f"{self.address} is not reachable via Bluetooth")

        self._ready.clear()
        self._disconnected.clear()
        self._identity_sent = False

        client = await establish_connection(
            BleakClientWithServiceCache,
            device,
            self.name,
            disconnected_callback=self._on_disconnected,
            ble_device_callback=self._ble_device_callback,
            max_attempts=3,
        )
        self._client = client
        try:
            await self._setup_gatt(client)
        except Exception:
            with contextlib.suppress(Exception):
                await client.clear_cache()
            await self._disconnect()
            raise
        self._connected = True
        self.connect_count += 1
        if self._on_connection_change:
            self._on_connection_change(True)

    async def _setup_gatt(self, client: BleakClientWithServiceCache) -> None:
        services = client.services
        cmd = services.get_characteristic(CHAR_CMD)
        data_w = services.get_characteristic(CHAR_DATA_WRITE)
        data_r = services.get_characteristic(CHAR_DATA_READ)
        alt = services.get_characteristic(CHAR_CMD_ALT)
        if cmd is None or data_w is None or data_r is None:
            raise TrumaError("Truma GATT characteristics not found")

        # MTU (BlueZ needs an explicit acquire to learn the negotiated MTU)
        mtu = 23
        backend = getattr(client, "_backend", None)
        acquire = getattr(backend, "_acquire_mtu", None)
        if acquire is not None:
            with contextlib.suppress(Exception):
                await acquire()
        with contextlib.suppress(Exception):
            mtu = int(client.mtu_size)
        self._chunk_size = max(20, min(mtu - 3, 512))
        _LOGGER.info("%s: connected, MTU %s", self.name, mtu)

        self._transport = TrumaTransport(
            write_cmd=lambda data: client.write_gatt_char(cmd, data, response=True),
            write_data=lambda data: client.write_gatt_char(
                data_w, data, response=False
            ),
            on_message=self._handle_message,
            chunk_size=lambda: self._chunk_size,
        )

        # Which characteristic delivers the CMD notifications?
        # The app uses FC314004 when present, otherwise FC314001.
        # The reference implementation found FC314004 to break the transport
        # on a Combi D 4 E, therefore "auto" prefers FC314001.
        notify_char = cmd
        if self.cmd_notify == CMD_NOTIFY_4004 and alt is not None:
            notify_char = alt
        elif self.cmd_notify in (CMD_NOTIFY_AUTO, CMD_NOTIFY_4001):
            props = {p.lower() for p in cmd.properties}
            if not props & {"notify", "indicate"} and alt is not None:
                notify_char = alt
        _LOGGER.debug("%s: CMD notifications on %s", self.name, notify_char.uuid)

        transport = self._transport
        await client.start_notify(
            notify_char, lambda _c, data: transport.handle_cmd_notification(bytes(data))
        )
        await client.start_notify(
            data_r, lambda _c, data: transport.handle_data_notification(bytes(data))
        )

        revision: str | None = None
        rev_char = services.get_characteristic(CHAR_SOFTWARE_REVISION)
        if rev_char is not None:
            try:
                raw = await client.read_gatt_char(rev_char)
                revision = bytes(raw).decode("utf-8", errors="ignore").strip("\x00 ")
            except (TimeoutError, BleakError, OSError) as err:
                _LOGGER.debug("%s: cannot read software revision: %s", self.name, err)
        self.state.software_revision = revision
        self.state.protocol_version = parse_protocol_version(revision)
        _LOGGER.debug(
            "%s: software revision %r -> protocol %s",
            self.name,
            revision,
            self.state.protocol_version,
        )

    def _on_disconnected(self, _client: Any) -> None:
        was_connected = self._connected
        self._connected = False
        self._ready.clear()
        if self._transport:
            self._transport.close()
        self._fail_pending(TrumaError("disconnected"))
        self._disconnected.set()
        if was_connected and self._on_connection_change:
            self._on_connection_change(False)

    async def _disconnect(self) -> None:
        client = self._client
        self._client = None
        if self._transport:
            self._transport.close()
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        if client is not None:
            with contextlib.suppress(Exception):
                await client.disconnect()
        if self._connected:
            self._connected = False
            if self._on_connection_change:
                self._on_connection_change(False)
        self._ready.clear()

    def _fail_pending(self, err: Exception) -> None:
        futures: list[asyncio.Future | None] = [
            self._registration_future,
            self._subscribe_future,
            self._device_discovery_future,
            *self._pd_futures.values(),
        ]
        for fut in futures:
            if fut is not None and not fut.done():
                fut.set_exception(err)
                fut.exception()  # mark as retrieved

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------
    # session
    # ------------------------------------------------------------------
    async def _send(self, frame: bytes) -> None:
        if self._transport is None or not self._connected:
            raise TrumaError("not connected")
        try:
            await self._transport.send(frame)
        except TransportError as err:
            raise TrumaError(str(err)) from err

    async def setup_session(self) -> None:
        await self.register()
        # The panel normally asks for our parameters right after registration.
        # If it does not, send the identity anyway (same content, to the broker).
        self._spawn(self._identity_fallback())
        await self.subscribe_topics()
        await self.discover_devices()
        await self.discover_parameters()
        self._ready.set()
        _LOGGER.info(
            "%s: ready (address 0x%04X, %d parameters)",
            self.name,
            self.state.address or 0,
            len(self.state.params),
        )
        if self._on_ready:
            self._on_ready()

    async def register(self, attempts: int = 3) -> int:
        version = self.state.protocol_version or (5, 1)
        last_err: Exception | None = None
        for attempt in range(attempts):
            loop = asyncio.get_running_loop()
            self._registration_future = loop.create_future()
            try:
                await self._send(build_registration_request(version))
                response = await asyncio.wait_for(
                    self._registration_future, MESSAGE_RESPONSE_TIMEOUT
                )
            except (TimeoutError, TrumaError) as err:
                last_err = err
                _LOGGER.warning(
                    "%s: registration attempt %d failed: %s",
                    self.name,
                    attempt + 1,
                    err,
                )
                if not self._connected:
                    break
                continue
            finally:
                self._registration_future = None
            error = response.get("err")
            if error:
                raise RegistrationError(f"registration refused: {error}")
            address = response.get("addr")
            if not isinstance(address, int):
                raise RegistrationError(f"invalid registration response {response}")
            self.state.address = address & 0xFFFF
            _LOGGER.info(
                "%s: registered at the panel as 0x%04X", self.name, self.state.address
            )
            return self.state.address
        raise RegistrationError(f"no registration response ({last_err})")

    async def _identity_fallback(self) -> None:
        await asyncio.sleep(IDENTITY_FALLBACK_DELAY)
        if not self._identity_sent and self._connected:
            _LOGGER.debug("%s: panel did not ask for identity, sending it", self.name)
            await self.send_identity(ADDR_MESSAGE_BROKER)

    async def send_identity(self, destination: int) -> None:
        if self.state.address is None:
            return
        self._identity_sent = True
        for frame in build_identity_messages(
            self.state.address, destination, self.identity
        ):
            try:
                await self._send(frame)
            except TrumaError as err:
                _LOGGER.debug("Sending identity failed: %s", err)
                return

    async def subscribe_topics(self) -> None:
        assert self.state.address is not None
        acknowledged: list[str] = []
        topics = list(ALL_TOPICS)
        for start in range(0, len(topics), SUBSCRIBE_BATCH_SIZE):
            batch = topics[start : start + SUBSCRIBE_BATCH_SIZE]
            loop = asyncio.get_running_loop()
            self._subscribe_future = loop.create_future()
            try:
                await self._send(build_subscribe(self.state.address, batch))
                response = await asyncio.wait_for(
                    self._subscribe_future, MESSAGE_RESPONSE_TIMEOUT
                )
                if isinstance(response, dict):
                    if response.get("err"):
                        _LOGGER.debug("Subscription error: %s", response.get("err"))
                    names = response.get("tn")
                    if isinstance(names, list):
                        acknowledged.extend(str(n) for n in names)
            except TimeoutError:
                _LOGGER.debug("No subscribe response for %s", batch)
            finally:
                self._subscribe_future = None
            await asyncio.sleep(SUBSCRIBE_BATCH_DELAY)
        self.state.subscribed_topics = acknowledged
        _LOGGER.debug("%s: subscribed %d topics", self.name, len(acknowledged))

    async def discover_devices(self) -> None:
        assert self.state.address is not None
        loop = asyncio.get_running_loop()
        self._device_discovery_future = loop.create_future()
        try:
            await self._send(build_device_discovery_request(self.state.address))
            await asyncio.wait_for(
                self._device_discovery_future, MESSAGE_RESPONSE_TIMEOUT
            )
        except TimeoutError:
            _LOGGER.debug("%s: device discovery incomplete", self.name)
        finally:
            self._device_discovery_future = None

    def _discovery_targets(self) -> list[int]:
        own = self.state.address
        targets = []
        for dev in self.state.devices:
            if dev == own or dev in (ADDR_BROADCAST, ADDR_MESSAGE_BROKER):
                continue
            if (dev >> 8) in (0x05, 0x06):  # other smartphones / BLE controllers
                continue
            targets.append(dev)
        if not targets:
            targets = [0x0101, 0x0201]
        return targets

    async def discover_parameters(self, devices: list[int] | None = None) -> None:
        """Ask every bus device for its topics (like the app: all requests at once)."""
        assert self.state.address is not None
        loop = asyncio.get_running_loop()
        pending: dict[int, asyncio.Future[None]] = {}
        for device in devices or self._discovery_targets():
            if device in self._pd_futures:
                continue
            fut = loop.create_future()
            self._pd_futures[device] = fut
            pending[device] = fut
            try:
                await self._send(
                    build_parameter_discovery_request(self.state.address, device)
                )
            except TrumaError as err:
                _LOGGER.debug("Parameter discovery 0x%04X failed: %s", device, err)
                self._pd_futures.pop(device, None)
                pending.pop(device, None)
                if not self._connected:
                    raise
        if not pending:
            return
        try:
            await asyncio.wait(pending.values(), timeout=PARAMETER_DISCOVERY_TIMEOUT)
        finally:
            for device, fut in pending.items():
                self._pd_futures.pop(device, None)
                if fut.done() and not fut.cancelled() and fut.exception() is None:
                    self.state.discovered_devices.add(device)
                elif not fut.done():
                    fut.cancel()
                    _LOGGER.debug(
                        "%s: no parameter discovery from 0x%04X", self.name, device
                    )
        if not self._connected:
            raise TrumaError("disconnected during parameter discovery")

    # ------------------------------------------------------------------
    # incoming messages
    # ------------------------------------------------------------------
    def _handle_message(self, raw: bytes) -> None:
        msg = decode_message(raw)
        if msg is None:
            _LOGGER.debug("Undecodable message %s", raw.hex())
            return
        if msg.error:
            _LOGGER.debug("CBOR error %s in %s", msg.error, raw.hex())
        _LOGGER.debug(
            "RX 0x%04X -> 0x%04X %s/%s: %s",
            msg.source,
            msg.destination,
            msg.kind,
            msg.sub_type,
            msg.data,
        )
        if msg.kind == "registration":
            self._handle_registration(msg)
        elif msg.kind == "deregistration":
            self._handle_deregistration(msg)
        elif msg.kind == "device_discovery":
            self._handle_device_discovery(msg)
        elif msg.kind == "mbp":
            self._handle_mbp(msg)

    def _handle_registration(self, msg: Message) -> None:
        if msg.sub_type != MSG_RESPONSE or not isinstance(msg.data, dict):
            return
        fut = self._registration_future
        if fut is not None and not fut.done():
            fut.set_result(msg.data)

    def _handle_deregistration(self, msg: Message) -> None:
        removed = msg.data.get("dereg") if isinstance(msg.data, dict) else None
        if isinstance(removed, int):
            removed = [removed]
        if not isinstance(removed, list):
            return
        _LOGGER.debug("%s: devices deregistered: %s", self.name, removed)
        if self.state.address is not None and self.state.address in removed:
            _LOGGER.warning("%s: panel deregistered us, reconnecting", self.name)
            self._spawn(self._force_reconnect())
            return
        for dev in removed:
            if dev in self.state.devices:
                self.state.devices.remove(dev)

    async def _force_reconnect(self) -> None:
        client = self._client
        if client is not None:
            with contextlib.suppress(Exception):
                await client.disconnect()

    def _handle_device_discovery(self, msg: Message) -> None:
        if msg.sub_type != MSG_RESPONSE or not isinstance(msg.data, dict):
            return
        data = msg.data

        def _ints(key: str) -> list[int] | None:
            val = data.get(key)
            if isinstance(val, list):
                return [int(x) for x in val if isinstance(x, int)]
            return None

        devices = _ints("Devices")
        restricted = _ints("Restricted")
        disconnected = _ints("Disc")
        new_devices: list[int] = []
        if devices is not None:
            for dev in devices:
                if dev not in self.state.devices:
                    self.state.devices.append(dev)
                    new_devices.append(dev)
        if restricted is not None:
            self.state.restricted_devices = restricted
        if disconnected is not None:
            self.state.disconnected_devices = disconnected
        fut = self._device_discovery_future
        if fut is not None:
            if is_last_message(data) and not fut.done():
                fut.set_result(None)
        elif self._ready.is_set() and new_devices:
            # a device appeared later on (e.g. heater woke up)
            targets = [d for d in new_devices if d in self._discovery_targets()]
            if targets:
                self._spawn(self.discover_parameters(targets))

    def _handle_mbp(self, msg: Message) -> None:
        sub = msg.sub_type
        if sub == MBP_INFO:
            info = parse_parameter_read(msg.data)
            if info is not None:
                self.state.apply_info(info, msg.source)
        elif sub == MBP_PARAMETER_DISCOVERY_RESPONSE:
            infos = list(iter_discovery_parameters(msg.data))
            if infos:
                self.state.apply_discovery(infos, msg.source)
            if is_last_message(msg.data):
                fut = self._pd_futures.get(msg.source)
                if fut is not None and not fut.done():
                    fut.set_result(None)
        elif sub == MBP_PARAMETER_DISCOVERY:
            # The panel (message broker) asks for our parameters -> identity
            if self.state.address is not None:
                self._spawn(self.send_identity(msg.source))
        elif sub == MBP_SUBSCRIBE_RESPONSE:
            fut = self._subscribe_future
            if fut is not None and not fut.done():
                fut.set_result(msg.data)
        elif sub in (MBP_BINARY, MBP_WRITE):
            _LOGGER.debug("Ignoring MBP type 0x%02X from 0x%04X", sub, msg.source)

    # ------------------------------------------------------------------
    # writing
    # ------------------------------------------------------------------
    async def write(
        self, topic: str, param: str, value: Any, device: int | None = None
    ) -> None:
        """Write one parameter to the device that owns ``topic``."""
        if self.state.address is None or not self._connected:
            raise TrumaError("not connected to the panel")
        dest = device if device is not None else self.state.device_for_topic(topic)
        if dest is None:
            raise TrumaError(f"unknown device for topic {topic}")
        async with self._write_lock:
            wait = WRITE_MIN_INTERVAL - (time.monotonic() - self._last_write)
            if wait > 0:
                await asyncio.sleep(wait)
            _LOGGER.debug(
                "%s: write %s.%s=%r -> 0x%04X", self.name, topic, param, value, dest
            )
            await self._send(build_write(self.state.address, dest, topic, param, value))
            self._last_write = time.monotonic()

    async def write_many(
        self, writes: list[tuple[str, str, Any]], delay: float = WRITE_SEQUENCE_DELAY
    ) -> None:
        for index, (topic, param, value) in enumerate(writes):
            if index:
                await asyncio.sleep(delay)
            await self.write(topic, param, value)

    # convenience wrappers --------------------------------------------------
    async def set_air_circulation(self, level: int) -> None:
        if level <= 0:
            await self.write_many([("AirCirculation", "FanLevel", 0)])
        else:
            await self.write_many(
                [
                    ("AirCirculation", "Active", ACTIVE_ON),
                    ("AirCirculation", "FanLevel", level),
                ]
            )
