"""BLE bonding with the iNet X panel.

How the panel pairs (Truma app 3.5.0 + node-red-contrib-truma-inetx captures):

* On the panel: main menu -> "App" enables Bluetooth pairing.  The last byte of
  the advertised manufacturer data switches to 0x01 ("Pairing aktiv").
* The client connects, the protected CMD characteristic answers with
  "Insufficient Authentication" and SMP pairing starts.  The panel uses
  LE legacy pairing / Just Works - **no code is shown**.
* On Linux this works reliably with a BlueZ agent "NoInputNoOutput" and
  Secure Connections temporarily disabled (``btmgmt sc off``).

Home Assistant does not provide a BlueZ agent, so we register our own via
``dbus-fast`` for the duration of the pairing and call ``Device1.Pair()``.
A passkey is still supported for panels that show one.

For remote adapters (ESPHome Bluetooth proxies) we fall back to bleak's
``pair()``.
"""

import asyncio
import contextlib
import logging
from dataclasses import dataclass

from .mgmt import LegacyPairingSettings, MgmtError

_LOGGER = logging.getLogger(__name__)

BLUEZ_SERVICE = "org.bluez"
AGENT_PATH = "/ha_truma_inet_x/agent"
DISCOVERY_TIMEOUT = 30.0
PAIR_TIMEOUT = 45.0
BOND_SETTLE_TIME = 3.0


class PairingError(Exception):
    """Raised when bonding failed.  ``reason`` is a translation key."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


@dataclass(slots=True)
class PairingResult:
    paired: bool
    already_paired: bool = False
    method: str = ""
    legacy_settings: bool = False


def _build_agent(passkey: int | None, device_path: str):
    from dbus_fast import DBusError
    from dbus_fast.service import ServiceInterface, method

    def _check(device: str) -> None:
        if device != device_path:
            raise DBusError("org.bluez.Error.Rejected", "unexpected device")

    class _Agent(ServiceInterface):  # pragma: no cover - needs a real BlueZ
        def __init__(self) -> None:
            super().__init__("org.bluez.Agent1")
            self.passkey_requested = False

        @method()
        def Release(self):  # noqa: N802
            _LOGGER.debug("Agent released")

        @method()
        def RequestPinCode(self, device: "o") -> "s":  # noqa: N802,F821
            _check(device)
            self.passkey_requested = True
            if passkey is None:
                raise DBusError("org.bluez.Error.Rejected", "passkey required")
            return f"{passkey:06d}"

        @method()
        def DisplayPinCode(self, device: "o", pincode: "s"):  # noqa: N802,F821
            _LOGGER.debug("DisplayPinCode %s", pincode)

        @method()
        def RequestPasskey(self, device: "o") -> "u":  # noqa: N802,F821
            _check(device)
            self.passkey_requested = True
            _LOGGER.info("Agent: panel requests a passkey")
            if passkey is None:
                raise DBusError("org.bluez.Error.Rejected", "passkey required")
            return passkey

        @method()
        def DisplayPasskey(self, device: "o", passkey_: "u", entered: "q"):  # noqa: N802,F821
            _LOGGER.debug("DisplayPasskey %06d", passkey_)

        @method()
        def RequestConfirmation(self, device: "o", passkey_: "u"):  # noqa: N802,F821
            _check(device)
            if passkey is not None and passkey != passkey_:
                raise DBusError("org.bluez.Error.Rejected", "passkey mismatch")
            _LOGGER.info("Agent: RequestConfirmation %06d -> accepted", passkey_)

        @method()
        def RequestAuthorization(self, device: "o"):  # noqa: N802,F821
            _check(device)
            _LOGGER.info("Agent: RequestAuthorization -> accepted")

        @method()
        def AuthorizeService(self, device: "o", uuid: "s"):  # noqa: N802,F821
            _LOGGER.debug("AuthorizeService %s -> accepted", uuid)

        @method()
        def Cancel(self):  # noqa: N802
            _LOGGER.info("Agent: Pairing cancelled by BlueZ")

    return _Agent()


class _Bus:
    """Small helper around a dbus-fast MessageBus."""

    def __init__(self, bus) -> None:
        from dbus_fast import Message, MessageType, Variant

        self.bus = bus
        self.Message = Message
        self.MessageType = MessageType
        self.Variant = Variant

    async def call(
        self, path: str, interface: str, member: str, signature: str = "", body=None
    ):
        reply = await self.bus.call(
            self.Message(
                destination=BLUEZ_SERVICE,
                path=path,
                interface=interface,
                member=member,
                signature=signature,
                body=body or [],
            )
        )
        return reply

    def is_error(self, reply) -> bool:
        return reply.message_type == self.MessageType.ERROR

    async def objects(self) -> dict:
        reply = await self.call(
            "/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects"
        )
        if self.is_error(reply):
            raise PairingError("dbus_unavailable", str(reply.body))
        return reply.body[0]

    async def get_bool(self, path: str, prop: str) -> bool | None:
        reply = await self.call(
            path,
            "org.freedesktop.DBus.Properties",
            "Get",
            "ss",
            ["org.bluez.Device1", prop],
        )
        if self.is_error(reply):
            return None
        return bool(reply.body[0].value)

    async def set_bool(self, path: str, prop: str, value: bool) -> None:
        await self.call(
            path,
            "org.freedesktop.DBus.Properties",
            "Set",
            "ssv",
            ["org.bluez.Device1", prop, self.Variant("b", value)],
        )


def _adapters(objects: dict) -> dict[str, str]:
    result = {}
    for path, ifaces in objects.items():
        if "org.bluez.Adapter1" in ifaces:
            addr = ifaces["org.bluez.Adapter1"].get("Address")
            result[path] = (addr.value if addr else "").upper()
    return result


def _find_device(objects: dict, address: str, adapter_path: str | None) -> str | None:
    for path, ifaces in objects.items():
        dev = ifaces.get("org.bluez.Device1")
        if not dev:
            continue
        if adapter_path and not path.startswith(adapter_path + "/"):
            continue
        dev_addr = dev.get("Address")
        if dev_addr and dev_addr.value.upper() == address.upper():
            return path
    return None


def _select_adapter(adapters: dict[str, str], hint: str | None) -> str | None:
    if hint:
        for path, addr in adapters.items():
            if addr == hint.upper() or path.endswith("/" + hint):
                return path
    return None


async def async_pair_bluez(
    address: str,
    passkey: int | None,
    adapter_hint: str | None = None,
    legacy: bool = True,
    timeout: float = PAIR_TIMEOUT,
) -> PairingResult:
    """Bond with ``address`` through the local BlueZ daemon."""
    try:
        from dbus_fast import BusType
        from dbus_fast.aio import MessageBus
    except ImportError as err:  # pragma: no cover
        raise PairingError("dbus_unavailable", str(err)) from err

    try:
        raw_bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    except Exception as err:  # noqa: BLE001
        raise PairingError("dbus_unavailable", str(err)) from err
    bus = _Bus(raw_bus)

    try:
        objects = await bus.objects()
        adapters = _adapters(objects)
        if not adapters:
            raise PairingError("device_not_local", "no local Bluetooth adapter")
        adapter_path = _select_adapter(adapters, adapter_hint)

        dev_path = _find_device(objects, address, adapter_path) or _find_device(
            objects, address, None
        )
        if dev_path is None:
            dev_path = await _discover(
                bus, address, adapter_path or next(iter(adapters))
            )
        if dev_path is None:
            raise PairingError("device_not_local", address)
        adapter_path = dev_path.rsplit("/", 1)[0]
        _LOGGER.info(
            "Pairing %s: BlueZ device %s (adapters: %s, hint %s)",
            address,
            dev_path,
            ", ".join(f"{p}={a}" for p, a in adapters.items()),
            adapter_hint,
        )

        if await bus.get_bool(dev_path, "Paired"):
            _LOGGER.info("Pairing %s: already paired in BlueZ", address)
            await bus.set_bool(dev_path, "Trusted", True)
            return PairingResult(paired=True, already_paired=True, method="bluez")

        legacy_settings: LegacyPairingSettings | None = None
        if legacy and passkey is None:
            try:
                index = int(adapter_path.rsplit("hci", 1)[1])
                legacy_settings = LegacyPairingSettings(index)
                await asyncio.get_running_loop().run_in_executor(
                    None, legacy_settings.apply
                )
                _LOGGER.info(
                    "Pairing %s: hci%d switched to legacy pairing (Secure Connections off)",
                    address,
                    index,
                )
            except (MgmtError, ValueError, IndexError) as err:
                _LOGGER.warning(
                    "Could not switch the Bluetooth adapter to legacy pairing (%s); "
                    "trying anyway",
                    err,
                )
                legacy_settings = None

        try:
            return await _pair(bus, dev_path, passkey, timeout, legacy_settings)
        finally:
            if legacy_settings is not None:
                await asyncio.get_running_loop().run_in_executor(
                    None, legacy_settings.restore
                )
    finally:
        with contextlib.suppress(Exception):
            raw_bus.disconnect()


async def _discover(bus: _Bus, address: str, adapter_path: str) -> str | None:
    """Let BlueZ discover the panel so a Device1 object exists."""
    _LOGGER.info(
        "Pairing %s: not known to BlueZ, discovering on %s", address, adapter_path
    )
    with contextlib.suppress(Exception):
        await bus.call(
            adapter_path,
            "org.bluez.Adapter1",
            "SetDiscoveryFilter",
            "a{sv}",
            [{"Transport": bus.Variant("s", "le")}],
        )
    reply = await bus.call(adapter_path, "org.bluez.Adapter1", "StartDiscovery")
    started = not bus.is_error(reply)
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + DISCOVERY_TIMEOUT
        while loop.time() < deadline:
            path = _find_device(await bus.objects(), address, None)
            if path:
                return path
            await asyncio.sleep(1)
        return None
    finally:
        if started:
            with contextlib.suppress(Exception):
                await bus.call(adapter_path, "org.bluez.Adapter1", "StopDiscovery")


async def _pair(
    bus: _Bus,
    dev_path: str,
    passkey: int | None,
    timeout: float,
    legacy_settings: LegacyPairingSettings | None,
) -> PairingResult:
    capability = "NoInputNoOutput" if passkey is None else "KeyboardDisplay"
    agent = _build_agent(passkey, dev_path)
    bus.bus.export(AGENT_PATH, agent)
    reply = await bus.call(
        "/org/bluez",
        "org.bluez.AgentManager1",
        "RegisterAgent",
        "os",
        [AGENT_PATH, capability],
    )
    if bus.is_error(reply) and "AlreadyExists" not in str(reply.error_name):
        bus.bus.unexport(AGENT_PATH)
        raise PairingError("agent_failed", str(reply.body))
    _LOGGER.info(
        "Pairing %s: agent %s registered, calling Device1.Pair()", dev_path, capability
    )

    paired = False
    try:
        pair_task = asyncio.ensure_future(
            bus.call(dev_path, "org.bluez.Device1", "Pair")
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        error_reply = None
        while loop.time() < deadline:
            if await bus.get_bool(dev_path, "Paired"):
                paired = True
                break
            if pair_task.done():
                reply = pair_task.result()
                if not bus.is_error(reply):
                    paired = True
                    break
                error_reply = reply
                # Pair() may report an error although the bond was created
                await asyncio.sleep(1)
                paired = bool(await bus.get_bool(dev_path, "Paired"))
                break
            await asyncio.sleep(0.5)

        if not pair_task.done():
            pair_task.cancel()

        if not paired:
            if error_reply is None:
                _LOGGER.warning(
                    "Pairing %s: no result after %.0f s (agent asked for passkey: %s)",
                    dev_path,
                    timeout,
                    getattr(agent, "passkey_requested", False),
                )
                raise PairingError("pairing_timeout")
            name = error_reply.error_name or ""
            detail = f"{name}: {error_reply.body}"
            _LOGGER.warning("Pairing %s: BlueZ Pair() failed: %s", dev_path, detail)
            if name.endswith("AlreadyExists"):
                return PairingResult(paired=True, already_paired=True, method="bluez")
            if getattr(agent, "passkey_requested", False) and passkey is None:
                raise PairingError("passkey_required", detail)
            if name.endswith(("AuthenticationFailed", "AuthenticationRejected")):
                raise PairingError(
                    "wrong_passkey" if passkey is not None else "pairing_rejected",
                    detail,
                )
            if name.endswith(("ConnectionAttemptFailed", "AuthenticationTimeout")):
                raise PairingError("cannot_connect", detail)
            raise PairingError("pairing_failed", detail)

        _LOGGER.info("Pairing %s: paired, marking as trusted", dev_path)
        await bus.set_bool(dev_path, "Trusted", True)
        # give bluetoothd time to store the bond keys
        await asyncio.sleep(BOND_SETTLE_TIME)
        return PairingResult(
            paired=True, method="bluez", legacy_settings=legacy_settings is not None
        )
    finally:
        if not paired:
            with contextlib.suppress(Exception):
                await bus.call(dev_path, "org.bluez.Device1", "CancelPairing")
        # leave the link free for the protocol session
        with contextlib.suppress(Exception):
            await bus.call(dev_path, "org.bluez.Device1", "Disconnect")
        with contextlib.suppress(Exception):
            await bus.call(
                "/org/bluez",
                "org.bluez.AgentManager1",
                "UnregisterAgent",
                "o",
                [AGENT_PATH],
            )
        with contextlib.suppress(Exception):
            bus.bus.unexport(AGENT_PATH)


async def async_remove_bond_bluez(address: str) -> bool:
    """Remove an existing bond (used when the user wants to re-pair)."""
    try:
        from dbus_fast import BusType
        from dbus_fast.aio import MessageBus
    except ImportError:  # pragma: no cover
        return False
    try:
        raw_bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    except Exception:  # noqa: BLE001
        return False
    bus = _Bus(raw_bus)
    try:
        removed = False
        for path, ifaces in (await bus.objects()).items():
            dev = ifaces.get("org.bluez.Device1")
            if not dev:
                continue
            dev_addr = dev.get("Address")
            if not dev_addr or dev_addr.value.upper() != address.upper():
                continue
            res = await bus.call(
                path.rsplit("/", 1)[0],
                "org.bluez.Adapter1",
                "RemoveDevice",
                "o",
                [path],
            )
            removed = removed or not bus.is_error(res)
        return removed
    finally:
        with contextlib.suppress(Exception):
            raw_bus.disconnect()
