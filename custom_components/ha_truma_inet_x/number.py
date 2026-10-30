"""Number entities of the Truma iNet X integration."""

from __future__ import annotations

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .client import TrumaError
from .coordinator import TrumaConfigEntry, TrumaCoordinator
from .entity import TrumaParamEntity, async_add_dynamic_entities

PARALLEL_UPDATES = 0

FAN_LEVEL = NumberEntityDescription(
    key="ventilation_level",
    translation_key="ventilation_level",
    native_step=1,
    mode=NumberMode.SLIDER,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TrumaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entry.async_on_unload(
        async_add_dynamic_entities(
            coordinator,
            async_add_entities,
            "number",
            [
                (
                    FAN_LEVEL.key,
                    lambda state: (
                        (info := state.get("AirCirculation", "FanLevel")) is not None
                        and info.writable
                    ),
                    lambda: TrumaFanLevel(coordinator),
                )
            ],
        )
    )


class TrumaFanLevel(TrumaParamEntity, NumberEntity):
    """AirCirculation.FanLevel (ventilation / fan speed)."""

    def __init__(self, coordinator: TrumaCoordinator) -> None:
        super().__init__(coordinator, FAN_LEVEL, "AirCirculation", "FanLevel")

    @property
    def native_min_value(self) -> float:
        info = self.info
        return float(info.minimum) if info and info.minimum is not None else 0.0

    @property
    def native_max_value(self) -> float:
        info = self.info
        return float(info.maximum) if info and info.maximum is not None else 10.0

    @property
    def native_value(self) -> float | None:
        info = self.info
        if info is None or not isinstance(info.value, (int, float)):
            return None
        return float(info.value)

    async def async_set_native_value(self, value: float) -> None:
        try:
            await self.coordinator.client.set_air_circulation(int(round(value)))
        except TrumaError as err:
            from homeassistant.exceptions import HomeAssistantError

            raise HomeAssistantError(f"Truma: {err}") from err
