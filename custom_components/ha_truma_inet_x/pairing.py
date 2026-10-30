"""BLE bonding with the iNet X panel.

The Truma app simply calls ``BluetoothDevice.createBond()`` – Android then asks
for the passkey shown on the panel.  BlueZ needs a registered *agent* that
answers the passkey request; Home Assistant does not provide one, therefore we
register a tiny agent via ``dbus-fast`` (already shipped with Home Assistant on
Linux) for the duration of the pairing.

For remote adapters (ESPHome Bluetooth proxies) we fall back to bleak's
``pair()`` which only supports "Just Works" pairing.
"""

import asyncio
import contextlib
import logging
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

BLUEZ_SERVICE = "org.bluez"
AGENT_PATH = "/ha_truma_inet_x/agent"


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


def _build_agent(passkey: int | None):
    from dbus_fast import DBusError
    from dbus_fast.service import ServiceInterface, method

    class _Agent(ServiceInterface):  # pragma: no cover - needs a real BlueZ
        def __init__(self) -> None:
            super().__init__("org.bluez.Agent1")
            self.passkey_requested = False

        @method()
        def Release(self):  # noqa: N802
            _LOGGER.debug("Agent released")

        @method()
        def RequestPinCode(self, device: "o") -> "s":  # noqa: N802,F821
            self.passkey_requested = True
            if passkey is None:
                raise DBusError("org.bluez.Error.Rejected", "passkey required")
            return f"{passkey:06d}"

        @method()
        def DisplayPinCode(self, device: "o", pincode: "s"):  # noqa: N802,F821
            _LOGGER.debug("DisplayPinCode %s", pincode)

        @method()
        def RequestPasskey(self, device: "o") -> "u":  # noqa: N802,F821
            self.passkey_requested = True
            if passkey is None:
                raise DBusError("org.bluez.Error.Rejected", "passkey required")
            return passkey

        @method()
        def DisplayPasskey(self, device: "o", passkey_: "u", entered: "q"):  # noqa: N802,F821
            _LOGGER.debug("DisplayPasskey %06d", passkey_)

        @method()
        def RequestConfirmation(self, device: "o", passkey_: "u"):  # noqa: N802,F821
            _LOGGER.debug("RequestConfirmation %06d -> accepted", passkey_)

        @method()
        def RequestAuthorization(self, device: "o"):  # noqa: N802,F821
            _LOGGER.debug("RequestAuthorization -> accepted")

        @method()
        def AuthorizeService(self, device: "o", uuid: "s"):  # noqa: N802,F821
            _LOGGER.debug("AuthorizeService %s -> accepted", uuid)

        @method()
        def Cancel(self):  # noqa: N802
            _LOGGER.debug("Pairing cancelled by BlueZ")

    return _Agent()


async def async_pair_bluez(
    address: str,
    passkey: int | None,
    adapter_hint: str | None = None,
    timeout: float = 45.0,
) -> PairingResult:
    """Bond with ``address`` through the local BlueZ daemon."""
    try:
        from dbus_fast import BusType, Message, MessageType, Variant
        from dbus_fast.aio import MessageBus
    except ImportError as err:  # pragma: no cover
        raise PairingError("dbus_unavailable", str(err)) from err

    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    except Exception as err:  # noqa: BLE001
        raise PairingError("dbus_unavailable", str(err)) from err

    try:
        reply = await bus.call(
            Message(
                destination=BLUEZ_SERVICE,
                path="/",
                interface="org.freedesktop.DBus.ObjectManager",
                member="GetManagedObjects",
            )
        )
        if reply.message_type == MessageType.ERROR:
            raise PairingError("dbus_unavailable", str(reply.body))
        objects = reply.body[0]

        adapters: dict[str, str] = {}
        for path, ifaces in objects.items():
            if "org.bluez.Adapter1" in ifaces:
                addr = ifaces["org.bluez.Adapter1"].get("Address")
                adapters[path] = (addr.value if addr else "").upper()

        candidates: list[tuple[str, dict]] = []
        for path, ifaces in objects.items():
            dev = ifaces.get("org.bluez.Device1")
            if not dev:
                continue
            dev_addr = dev.get("Address")
            if dev_addr and dev_addr.value.upper() == address.upper():
                candidates.append((path, dev))
        if not candidates:
            raise PairingError("device_not_local", address)

        def _score(item: tuple[str, dict]) -> int:
            path, _dev = item
            if adapter_hint:
                for apath, aaddr in adapters.items():
                    if path.startswith(apath + "/") and aaddr == adapter_hint.upper():
                        return 0
                    if path.startswith(apath + "/") and apath.endswith(adapter_hint):
                        return 0
            return 1

        candidates.sort(key=_score)
        dev_path, dev_props = candidates[0]
        paired = dev_props.get("Paired")
        if paired is not None and paired.value:
            return PairingResult(paired=True, already_paired=True, method="bluez")

        agent = _build_agent(passkey)
        bus.export(AGENT_PATH, agent)
        reply = await bus.call(
            Message(
                destination=BLUEZ_SERVICE,
                path="/org/bluez",
                interface="org.bluez.AgentManager1",
                member="RegisterAgent",
                signature="os",
                body=[AGENT_PATH, "KeyboardDisplay"],
            )
        )
        if reply.message_type == MessageType.ERROR and "AlreadyExists" not in str(
            reply.error_name
        ):
            raise PairingError("agent_failed", str(reply.body))

        try:
            await bus.call(
                Message(
                    destination=BLUEZ_SERVICE,
                    path=dev_path,
                    interface="org.freedesktop.DBus.Properties",
                    member="Set",
                    signature="ssv",
                    body=["org.bluez.Device1", "Trusted", Variant("b", True)],
                )
            )
            try:
                reply = await asyncio.wait_for(
                    bus.call(
                        Message(
                            destination=BLUEZ_SERVICE,
                            path=dev_path,
                            interface="org.bluez.Device1",
                            member="Pair",
                        )
                    ),
                    timeout,
                )
            except TimeoutError as err:
                with contextlib.suppress(Exception):
                    await bus.call(
                        Message(
                            destination=BLUEZ_SERVICE,
                            path=dev_path,
                            interface="org.bluez.Device1",
                            member="CancelPairing",
                        )
                    )
                raise PairingError("pairing_timeout") from err

            if reply.message_type == MessageType.ERROR:
                name = reply.error_name or ""
                detail = f"{name}: {reply.body}"
                _LOGGER.debug("BlueZ Pair() failed: %s", detail)
                if name.endswith("AlreadyExists"):
                    return PairingResult(
                        paired=True, already_paired=True, method="bluez"
                    )
                if getattr(agent, "passkey_requested", False) and passkey is None:
                    raise PairingError("passkey_required", detail)
                if name.endswith("AuthenticationFailed") or name.endswith(
                    "AuthenticationRejected"
                ):
                    raise PairingError("wrong_passkey", detail)
                raise PairingError("pairing_failed", detail)
            return PairingResult(paired=True, method="bluez")
        finally:
            with contextlib.suppress(Exception):
                await bus.call(
                    Message(
                        destination=BLUEZ_SERVICE,
                        path="/org/bluez",
                        interface="org.bluez.AgentManager1",
                        member="UnregisterAgent",
                        signature="o",
                        body=[AGENT_PATH],
                    )
                )
            with contextlib.suppress(Exception):
                bus.unexport(AGENT_PATH)
    finally:
        with contextlib.suppress(Exception):
            bus.disconnect()


async def async_remove_bond_bluez(address: str) -> bool:
    """Remove an existing bond (used when the user wants to re-pair)."""
    try:
        from dbus_fast import BusType, Message, MessageType
        from dbus_fast.aio import MessageBus
    except ImportError:  # pragma: no cover
        return False
    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    except Exception:  # noqa: BLE001
        return False
    try:
        reply = await bus.call(
            Message(
                destination=BLUEZ_SERVICE,
                path="/",
                interface="org.freedesktop.DBus.ObjectManager",
                member="GetManagedObjects",
            )
        )
        if reply.message_type == MessageType.ERROR:
            return False
        removed = False
        for path, ifaces in reply.body[0].items():
            dev = ifaces.get("org.bluez.Device1")
            if not dev:
                continue
            dev_addr = dev.get("Address")
            if not dev_addr or dev_addr.value.upper() != address.upper():
                continue
            adapter_path = path.rsplit("/", 1)[0]
            res = await bus.call(
                Message(
                    destination=BLUEZ_SERVICE,
                    path=adapter_path,
                    interface="org.bluez.Adapter1",
                    member="RemoveDevice",
                    signature="o",
                    body=[path],
                )
            )
            removed = removed or res.message_type != MessageType.ERROR
        return removed
    finally:
        with contextlib.suppress(Exception):
            bus.disconnect()
