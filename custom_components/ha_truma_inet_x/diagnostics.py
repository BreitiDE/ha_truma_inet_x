"""Diagnostics support (Download diagnostics in the device page)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_MUID, CONF_UUID
from .coordinator import TrumaConfigEntry

TO_REDACT = {CONF_MUID, CONF_UUID, "SerialNr", "UniqueID", "Imei", "Iccid", "ConnectID"}


def _redact_params(data: Any) -> Any:
    if isinstance(data, dict):
        result = {}
        for key, value in data.items():
            if (
                key in TO_REDACT
                and isinstance(value, dict)
                and "value" in value
                or key in ("Muid", "Uuid")
                and isinstance(value, dict)
            ):
                value = {**value, "value": "**REDACTED**"}
            result[key] = _redact_params(value)
        return result
    return data


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: TrumaConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    client = coordinator.client
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "connection": {
            "connected": client.connected,
            "ready": client.ready,
            "connect_count": client.connect_count,
            "last_error": client.last_error,
            "transport": client.transport_stats,
        },
        "state": _redact_params(coordinator.state.as_dict()),
    }
