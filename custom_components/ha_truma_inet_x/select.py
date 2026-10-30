"""Select entities (multi level enum parameters) of the Truma iNet X integration."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import TrumaConfigEntry, TrumaCoordinator
from .entity import TrumaParamEntity, async_add_dynamic_entities
from .state import TrumaState

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class TrumaSelectDescription(SelectEntityDescription):
    topic: str
    parameter: str
    min_options: int = 3  # 2 options are handled by switches


SELECTS: tuple[TrumaSelectDescription, ...] = (
    TrumaSelectDescription(
        key="electric_level",
        translation_key="electric_level",
        topic="EnergySrc",
        parameter="ElectricLevel",
    ),
    TrumaSelectDescription(
        key="diesel_level",
        translation_key="diesel_level",
        topic="EnergySrc",
        parameter="DieselLevel",
    ),
    TrumaSelectDescription(
        key="energy_priority",
        translation_key="energy_priority",
        topic="EnergySrc",
        parameter="EnergySourcePrio",
        min_options=2,
    ),
    TrumaSelectDescription(
        key="heating_mode",
        translation_key="heating_mode",
        topic="AirHeating",
        parameter="Mode",
        min_options=2,
    ),
    TrumaSelectDescription(
        key="water_mode",
        translation_key="water_mode",
        topic="WaterHeating",
        parameter="Mode",
        min_options=2,
        entity_registry_enabled_default=False,
    ),
)


def _exists(state: TrumaState, desc: TrumaSelectDescription) -> bool:
    info = state.get(desc.topic, desc.parameter)
    return info is not None and info.writable and len(info.enum) >= desc.min_options


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
            "select",
            [
                (
                    desc.key,
                    lambda state, d=desc: _exists(state, d),
                    lambda d=desc: TrumaSelect(coordinator, d),
                )
                for desc in SELECTS
            ],
        )
    )


class TrumaSelect(TrumaParamEntity, SelectEntity):
    entity_description: TrumaSelectDescription

    def __init__(
        self, coordinator: TrumaCoordinator, description: TrumaSelectDescription
    ) -> None:
        super().__init__(
            coordinator, description, description.topic, description.parameter
        )

    @property
    def options(self) -> list[str]:
        opts = self.state_model.enum_options(self.topic, self.parameter)
        names = [o.name for o in opts]
        current = self.current_option
        if current is not None and current not in names:
            names.append(current)
        return names

    @property
    def current_option(self) -> str | None:
        info = self.info
        if info is None or info.value is None:
            return None
        name = info.enum_name()
        return name if name is not None else str(info.value)

    async def async_select_option(self, option: str) -> None:
        for opt in self.state_model.enum_options(
            self.topic, self.parameter, only_available=False
        ):
            if opt.name == option:
                await self._write(self.topic, self.parameter, opt.value)
                return
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unsupported_option",
            translation_placeholders={"option": option},
        )
