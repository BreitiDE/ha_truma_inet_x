"""Switches of the Truma iNet X integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import BOOL_OFF, BOOL_ON
from .coordinator import TrumaConfigEntry, TrumaCoordinator
from .entity import TrumaParamEntity, async_add_dynamic_entities
from .state import TrumaState

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class TrumaSwitchDescription(SwitchEntityDescription):
    topic: str
    parameter: str
    on_value: int = BOOL_ON
    off_value: int = BOOL_OFF
    require_enum: bool = False  # multi level parameters: only on/off variants


def _binary_enum(state: TrumaState, desc: TrumaSwitchDescription) -> bool:
    info = state.get(desc.topic, desc.parameter)
    if info is None or not info.writable:
        return False
    if desc.require_enum:
        return len(info.enum) == 2
    return len(info.enum) <= 2


SWITCHES: tuple[TrumaSwitchDescription, ...] = (
    TrumaSwitchDescription(
        key="water_boost",
        translation_key="water_boost",
        topic="WaterHeating",
        parameter="BoostMode",
    ),
    TrumaSwitchDescription(
        key="water_faster_heating",
        translation_key="water_faster_heating",
        topic="WaterHeating",
        parameter="FasterHeatingMode",
    ),
    TrumaSwitchDescription(
        key="gas",
        translation_key="gas",
        topic="EnergySrc",
        parameter="GasLevel",
    ),
    TrumaSwitchDescription(
        key="diesel",
        translation_key="diesel",
        topic="EnergySrc",
        parameter="DieselLevel",
        require_enum=True,
    ),
    TrumaSwitchDescription(
        key="electric",
        translation_key="electric",
        topic="EnergySrc",
        parameter="ElectricLevel",
        require_enum=True,
    ),
    TrumaSwitchDescription(
        key="ambient_light",
        translation_key="ambient_light",
        topic="AmbientLight",
        parameter="Active",
    ),
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
            "switch",
            [
                (
                    desc.key,
                    lambda state, d=desc: _binary_enum(state, d),
                    lambda d=desc: TrumaSwitch(coordinator, d),
                )
                for desc in SWITCHES
            ],
        )
    )


class TrumaSwitch(TrumaParamEntity, SwitchEntity):
    entity_description: TrumaSwitchDescription

    def __init__(
        self, coordinator: TrumaCoordinator, description: TrumaSwitchDescription
    ) -> None:
        super().__init__(
            coordinator, description, description.topic, description.parameter
        )

    @property
    def is_on(self) -> bool | None:
        info = self.info
        if info is None or info.value is None:
            return None
        return info.value != self.entity_description.off_value

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._write(self.topic, self.parameter, self._on_value())

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._write(self.topic, self.parameter, self.entity_description.off_value)

    def _on_value(self) -> int:
        info = self.info
        if info is not None and info.enum:
            others = [
                o.value
                for o in info.enum
                if o.value != self.entity_description.off_value
            ]
            if others:
                return others[0]
        return self.entity_description.on_value
