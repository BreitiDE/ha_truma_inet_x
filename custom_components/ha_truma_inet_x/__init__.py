"""Truma iNet X (Bluetooth) integration for Home Assistant.

Controls a Truma heater (e.g. Combi 6 E) through the Truma iNet X panel via
Bluetooth Low Energy – the same way the Truma iNet X smartphone app does.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components import bluetooth
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import (
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import config_validation as cv

from .client import TrumaError
from .const import DOMAIN
from .coordinator import TrumaConfigEntry, TrumaCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.WATER_HEATER,
]

SERVICE_WRITE_PARAMETER = "write_parameter"
SERVICE_REDISCOVER = "rediscover"
SERVICE_DUMP = "dump_parameters"
SERVICE_RELEASE = "release_bluetooth"
ATTR_MINUTES = "minutes"

ATTR_ENTRY_ID = "config_entry_id"
ATTR_TOPIC = "topic"
ATTR_PARAMETER = "parameter"
ATTR_VALUE = "value"
ATTR_DEVICE = "device"

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

WRITE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTRY_ID): cv.string,
        vol.Required(ATTR_TOPIC): cv.string,
        vol.Required(ATTR_PARAMETER): cv.string,
        vol.Required(ATTR_VALUE): vol.Any(int, float, str, bool),
        vol.Optional(ATTR_DEVICE): cv.string,
    }
)
ENTRY_SCHEMA = vol.Schema({vol.Optional(ATTR_ENTRY_ID): cv.string})
RELEASE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTRY_ID): cv.string,
        vol.Optional(ATTR_MINUTES): vol.All(
            vol.Coerce(float), vol.Range(min=0, max=1440)
        ),
    }
)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Register the integration services."""

    def _coordinator(call: ServiceCall) -> TrumaCoordinator:
        entry_id = call.data.get(ATTR_ENTRY_ID)
        entries = [
            e
            for e in hass.config_entries.async_loaded_entries(DOMAIN)
            if entry_id in (None, e.entry_id)
        ]
        if not entries:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="no_entry"
            )
        return entries[0].runtime_data

    async def _write(call: ServiceCall) -> None:
        coordinator = _coordinator(call)
        device = call.data.get(ATTR_DEVICE)
        dest: int | None = None
        if device:
            try:
                dest = int(str(device), 0)
            except ValueError as err:
                raise ServiceValidationError(
                    f"invalid device address {device}"
                ) from err
        try:
            await coordinator.client.write(
                call.data[ATTR_TOPIC],
                call.data[ATTR_PARAMETER],
                call.data[ATTR_VALUE],
                dest,
            )
        except TrumaError as err:
            raise HomeAssistantError(f"Truma: {err}") from err

    async def _rediscover(call: ServiceCall) -> None:
        coordinator = _coordinator(call)
        try:
            await coordinator.client.discover_devices()
            await coordinator.client.discover_parameters()
        except TrumaError as err:
            raise HomeAssistantError(f"Truma: {err}") from err

    async def _release(call: ServiceCall) -> None:
        coordinator = _coordinator(call)
        minutes = call.data.get(ATTR_MINUTES)
        if minutes == 0:
            coordinator.async_resume()
        else:
            await coordinator.async_release(minutes)

    async def _dump(call: ServiceCall) -> ServiceResponse:
        return _coordinator(call).state.as_dict()

    hass.services.async_register(
        DOMAIN, SERVICE_WRITE_PARAMETER, _write, schema=WRITE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_REDISCOVER, _rediscover, schema=ENTRY_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RELEASE, _release, schema=RELEASE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_DUMP,
        _dump,
        schema=ENTRY_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: TrumaConfigEntry) -> bool:
    """Set up Truma iNet X from a config entry."""
    address: str = entry.data[CONF_ADDRESS]
    if not bluetooth.async_ble_device_from_address(hass, address, connectable=True):
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="device_not_found",
            translation_placeholders={"address": address},
        )

    coordinator = TrumaCoordinator(hass, entry)
    entry.runtime_data = coordinator
    await coordinator.async_start()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: TrumaConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: TrumaConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_shutdown()
    return unload_ok
