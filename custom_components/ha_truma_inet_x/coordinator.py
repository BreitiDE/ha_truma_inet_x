"""Home Assistant glue around :class:`TrumaInetXClient`."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .client import TrumaInetXClient
from .const import (
    CMD_NOTIFY_AUTO,
    CONF_CMD_NOTIFY,
    CONF_MUID,
    CONF_USERNAME,
    CONF_UUID,
    DEFAULT_USERNAME,
    DOMAIN,
    TOPIC_IDENTIFY,
)
from .protocol import Identity
from .state import TrumaState

_LOGGER = logging.getLogger(__name__)

UPDATE_DEBOUNCE = 0.25

type TrumaConfigEntry = ConfigEntry[TrumaCoordinator]


class TrumaCoordinator(DataUpdateCoordinator[None]):
    """Push based coordinator: the client notifies us about every change."""

    config_entry: TrumaConfigEntry

    def __init__(self, hass: HomeAssistant, entry: TrumaConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{entry.data[CONF_ADDRESS]}",
            update_interval=None,
            always_update=True,
        )
        self.address: str = entry.data[CONF_ADDRESS]
        identity = Identity(
            muid=entry.data[CONF_MUID],
            uuid=entry.data[CONF_UUID],
            username=entry.options.get(
                CONF_USERNAME, entry.data.get(CONF_USERNAME, DEFAULT_USERNAME)
            ),
        )
        self.client = TrumaInetXClient(
            address=self.address,
            identity=identity,
            ble_device_callback=self._ble_device,
            name=entry.title,
            cmd_notify=entry.options.get(CONF_CMD_NOTIFY, CMD_NOTIFY_AUTO),
            on_connection_change=self._connection_changed,
            on_ready=self._ready,
        )
        self._unsub_state: CALLBACK_TYPE | None = None
        self._debounce_handle = None
        self._device_info_done = False
        self.data = None

    # ------------------------------------------------------------------
    @property
    def state(self) -> TrumaState:
        return self.client.state

    @property
    def connected(self) -> bool:
        return self.client.connected

    def _ble_device(self):
        return bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )

    async def _async_update_data(self) -> None:
        """Nothing to poll - all data is pushed by the panel."""
        return None

    # ------------------------------------------------------------------
    async def async_start(self) -> None:
        self._unsub_state = self.state.add_listener(self._state_changed)
        self.client.start()

    async def async_shutdown(self) -> None:
        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None
        if self._debounce_handle:
            self._debounce_handle.cancel()
            self._debounce_handle = None
        await self.client.stop()
        await super().async_shutdown()

    # ------------------------------------------------------------------
    @callback
    def _state_changed(self, _changed: set[tuple[str, str]]) -> None:
        if self._debounce_handle is None:
            self._debounce_handle = self.hass.loop.call_later(
                UPDATE_DEBOUNCE, self._flush_update
            )

    @callback
    def _flush_update(self) -> None:
        self._debounce_handle = None
        if self.client.ready and not self._device_info_done:
            self._update_device_registry()
        self.async_set_updated_data(None)

    @callback
    def _connection_changed(self, connected: bool) -> None:
        _LOGGER.debug("%s connected: %s", self.address, connected)
        if not connected:
            self._device_info_done = False
        self.async_set_updated_data(None)

    @callback
    def _ready(self) -> None:
        self._update_device_registry()
        self.async_set_updated_data(None)

    # ------------------------------------------------------------------
    def _identify(self, device: int | None) -> dict[str, Any]:
        if device is None:
            return {}
        return {
            name: info.value
            for name, info in self.state.device_params(device, TOPIC_IDENTIFY).items()
        }

    def panel_identify(self) -> dict[str, Any]:
        return self._identify(0x0101) or self._identify(
            self.state.primary_device(TOPIC_IDENTIFY)
        )

    def heater_identify(self) -> dict[str, Any]:
        return self._identify(self.state.device_for_topic("AirHeating"))

    @staticmethod
    def _version(ident: dict[str, Any]) -> str | None:
        parts = [ident.get("SwMaj"), ident.get("SwMin"), ident.get("SwBgFx")]
        if parts[0] is None:
            return None
        return ".".join(str(p) for p in parts if p is not None)

    @callback
    def _update_device_registry(self) -> None:
        registry = dr.async_get(self.hass)
        device = registry.async_get_device(identifiers={(DOMAIN, self.address)})
        if device is None:
            return
        panel = self.panel_identify()
        heater = self.heater_identify()
        model = heater.get("Name") or panel.get("Name")
        updates: dict[str, Any] = {}
        if model and isinstance(model, str):
            updates["model"] = model
        if (version := self._version(panel)) is not None:
            updates["sw_version"] = version
        hw = panel.get("HwMaj")
        if hw is not None:
            updates["hw_version"] = f"{hw}.{panel.get('HwMin', 0)}"
        serial = panel.get("SerialNr")
        if isinstance(serial, str) and serial:
            updates["serial_number"] = serial
        if updates:
            registry.async_update_device(device.id, **updates)
        self._device_info_done = bool(panel or heater)

    # ------------------------------------------------------------------
    async def async_write(self, topic: str, param: str, value: Any) -> None:
        await self.client.write(topic, param, value)

    async def async_write_many(self, writes: list[tuple[str, str, Any]]) -> None:
        await self.client.write_many(writes)
