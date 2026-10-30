"""Hot water (Truma Combi boiler) as water_heater entity."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from homeassistant.components.water_heater import (
    STATE_OFF,
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ACTIVE_OFF,
    ACTIVE_ON,
    DOMAIN,
    HOT_WATER_DEFAULT_ENUM,
    HOT_WATER_NAME_MAP,
    HOT_WATER_TARGET_TEMP,
    MODE_VENTING,
    TOPIC_AIR_CIRCULATION,
    TOPIC_ROOM_CLIMATE,
    TOPIC_WATER_HEATING,
    WRITE_SEQUENCE_DELAY,
)
from .coordinator import TrumaConfigEntry, TrumaCoordinator
from .entity import TrumaEntity

PARALLEL_UPDATES = 0


def operation_key(name: str) -> str:
    """Panel enum name (e.g. '60' or 'Comfort') -> HA operation key."""
    normalized = name.strip().lower().replace("°c", "").replace("°", "").strip()
    if normalized in HOT_WATER_NAME_MAP:
        return HOT_WATER_NAME_MAP[normalized]
    slug = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")
    return slug or "mode"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TrumaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([TrumaWaterHeater(entry.runtime_data)])


class TrumaWaterHeater(TrumaEntity, WaterHeaterEntity):
    """WaterHeating topic."""

    _attr_translation_key = "hot_water"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_min_temp = 40
    _attr_max_temp = 70

    def __init__(self, coordinator: TrumaCoordinator) -> None:
        super().__init__(coordinator, "water_heater")

    def _modes(self) -> dict[str, int]:
        options = self.state_model.enum_options(
            TOPIC_WATER_HEATING, "Mode", fallback=HOT_WATER_DEFAULT_ENUM
        )
        return {operation_key(o.name): o.value for o in options}

    @property
    def available(self) -> bool:
        return super().available and self.state_model.has_topic(TOPIC_WATER_HEATING)

    @property
    def supported_features(self) -> WaterHeaterEntityFeature:
        return WaterHeaterEntityFeature.OPERATION_MODE | WaterHeaterEntityFeature.ON_OFF

    @property
    def operation_list(self) -> list[str]:
        return [STATE_OFF, *self._modes()]

    @property
    def current_operation(self) -> str | None:
        active = self.pval(TOPIC_WATER_HEATING, "Active")
        if active is None:
            return None
        if active == ACTIVE_OFF:
            return STATE_OFF
        info = self.param(TOPIC_WATER_HEATING, "Mode")
        if info is None or info.value is None:
            return None
        name = info.enum_name()
        if name is None:
            for value, default_name in HOT_WATER_DEFAULT_ENUM:
                if value == info.value:
                    name = default_name
        return operation_key(name) if name is not None else str(info.value)

    @property
    def current_temperature(self) -> float | None:
        value = self.pval(TOPIC_WATER_HEATING, "Temp")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return round(value / 10, 1)
        return None

    @property
    def target_temperature(self) -> float | None:
        op = self.current_operation
        if op is None or op == STATE_OFF:
            return None
        return HOT_WATER_TARGET_TEMP.get(op)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {}
        active = self.pval(TOPIC_WATER_HEATING, "Active")
        if active is not None:
            attrs["heating_state"] = {0: "off", 1: "active", 2: "idle"}.get(
                active, active
            )
        for name, attr in (
            ("BoostMode", "boost"),
            ("FasterHeatingMode", "faster_heating"),
            ("FasterHeatingModeTime", "faster_heating_time"),
            ("WaterCountDownTime", "count_down_time"),
        ):
            value = self.pval(TOPIC_WATER_HEATING, name)
            if value is not None:
                attrs[attr] = value
        return attrs

    # ------------------------------------------------------------------
    async def async_set_operation_mode(self, operation_mode: str) -> None:
        if operation_mode == STATE_OFF:
            await self.async_turn_off()
            return
        modes = self._modes()
        if operation_mode not in modes:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unsupported_operation",
                translation_placeholders={"mode": operation_mode},
            )
        value = modes[operation_mode]
        active = self.pval(TOPIC_WATER_HEATING, "Active")
        if active in (None, ACTIVE_OFF):
            await self._switch_on()
            await asyncio.sleep(WRITE_SEQUENCE_DELAY * 2)
        await self._write(TOPIC_WATER_HEATING, "Mode", value)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._switch_on()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._write(TOPIC_WATER_HEATING, "Active", ACTIVE_OFF)

    async def _switch_on(self) -> None:
        # app: SwitchWarmWaterHeatingModeUseCase.switchONWarmWaterHeating
        writes: list[tuple[str, str, Any]] = []
        if self.pval(
            TOPIC_ROOM_CLIMATE, "Mode"
        ) == MODE_VENTING and self.state_model.has(TOPIC_AIR_CIRCULATION, "FanLevel"):
            writes.append((TOPIC_AIR_CIRCULATION, "FanLevel", 0))
            writes.append((TOPIC_AIR_CIRCULATION, "Active", ACTIVE_OFF))
        writes.append((TOPIC_WATER_HEATING, "Active", ACTIVE_ON))
        await self._write_many(writes)
