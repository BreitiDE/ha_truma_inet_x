"""Binary sensors of the Truma iNet X integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ACTIVE_ON
from .coordinator import TrumaConfigEntry, TrumaCoordinator
from .entity import TrumaEntity, TrumaParamEntity, async_add_dynamic_entities

PARALLEL_UPDATES = 0


def _error_active(value: Any) -> bool:
    if isinstance(value, list):
        return any(v not in (0, None) for v in value)
    return value not in (0, None, "", "0")


@dataclass(frozen=True, kw_only=True)
class TrumaBinarySensorDescription(BinarySensorEntityDescription):
    topic: str
    parameter: str
    is_on_fn: Callable[[Any], bool]


BINARY_SENSORS: tuple[TrumaBinarySensorDescription, ...] = (
    TrumaBinarySensorDescription(
        key="burner_active",
        translation_key="burner_active",
        topic="AirHeating",
        parameter="Active",
        device_class=BinarySensorDeviceClass.HEAT,
        is_on_fn=lambda v: v == ACTIVE_ON,
    ),
    TrumaBinarySensorDescription(
        key="water_heating_active",
        translation_key="water_heating_active",
        topic="WaterHeating",
        parameter="Active",
        device_class=BinarySensorDeviceClass.HEAT,
        is_on_fn=lambda v: v == ACTIVE_ON,
    ),
    TrumaBinarySensorDescription(
        key="line_power",
        translation_key="line_power",
        topic="LinePower",
        parameter="Plugged",
        device_class=BinarySensorDeviceClass.PLUG,
        is_on_fn=lambda v: bool(v),
    ),
    TrumaBinarySensorDescription(
        key="error",
        translation_key="error",
        topic="ErrorReset",
        parameter="ErrCode",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=_error_active,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TrumaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities([TrumaConnectedSensor(coordinator)])
    entry.async_on_unload(
        async_add_dynamic_entities(
            coordinator,
            async_add_entities,
            "binary_sensor",
            [
                (
                    desc.key,
                    lambda state, d=desc: state.has(d.topic, d.parameter),
                    lambda d=desc: TrumaBinarySensor(coordinator, d),
                )
                for desc in BINARY_SENSORS
            ],
        )
    )


class TrumaBinarySensor(TrumaParamEntity, BinarySensorEntity):
    entity_description: TrumaBinarySensorDescription

    def __init__(
        self, coordinator: TrumaCoordinator, description: TrumaBinarySensorDescription
    ) -> None:
        super().__init__(
            coordinator, description, description.topic, description.parameter
        )

    @property
    def is_on(self) -> bool | None:
        info = self.info
        if info is None or info.value is None:
            return None
        return self.entity_description.is_on_fn(info.value)


class TrumaConnectedSensor(TrumaEntity, BinarySensorEntity):
    """BLE session state."""

    _attr_translation_key = "connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: TrumaCoordinator) -> None:
        super().__init__(coordinator, "connected")

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.client.ready

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        client = self.coordinator.client
        state = client.state
        return {
            "ble_connected": client.connected,
            "address": f"0x{state.address:04X}" if state.address is not None else None,
            "protocol_version": ".".join(map(str, state.protocol_version))
            if state.protocol_version
            else None,
            "software_revision": state.software_revision,
            "bus_devices": [f"0x{d:04X}" for d in state.devices],
            "parameters": len(state.params),
            "connects": client.connect_count,
            "last_error": client.last_error,
            **{f"transport_{k}": v for k, v in client.transport_stats.items()},
        }
