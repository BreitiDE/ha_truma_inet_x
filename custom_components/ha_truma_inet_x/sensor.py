"""Sensors of the Truma iNet X integration."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import TrumaConfigEntry, TrumaCoordinator
from .entity import TrumaParamEntity, async_add_dynamic_entities, scale_value

PARALLEL_UPDATES = 0

ACTIVE_STATES = {0: "off", 1: "active", 2: "idle"}


@dataclass(frozen=True, kw_only=True)
class TrumaSensorDescription(SensorEntityDescription):
    topic: str
    parameter: str
    scale: float = 1.0
    enum_states: dict[int, str] | None = None
    use_panel_enum: bool = False


SENSORS: tuple[TrumaSensorDescription, ...] = (
    TrumaSensorDescription(
        key="room_temperature",
        translation_key="room_temperature",
        topic="AirHeating",
        parameter="Temp",
        scale=0.1,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
    ),
    TrumaSensorDescription(
        key="target_room_temperature",
        translation_key="target_room_temperature",
        topic="AirHeating",
        parameter="TgtTemp",
        scale=0.1,
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        entity_registry_enabled_default=False,
    ),
    TrumaSensorDescription(
        key="water_temperature",
        translation_key="water_temperature",
        topic="WaterHeating",
        parameter="Temp",
        scale=0.1,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
    ),
    TrumaSensorDescription(
        key="internal_temperature",
        translation_key="internal_temperature",
        topic="Temperature",
        parameter="Internal",
        scale=0.1,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
    ),
    TrumaSensorDescription(
        key="external_temperature",
        translation_key="external_temperature",
        topic="Temperature",
        parameter="External",
        scale=0.1,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
    ),
    TrumaSensorDescription(
        key="air_heating_state",
        translation_key="air_heating_state",
        topic="AirHeating",
        parameter="Active",
        device_class=SensorDeviceClass.ENUM,
        options=list(ACTIVE_STATES.values()),
        enum_states=ACTIVE_STATES,
    ),
    TrumaSensorDescription(
        key="water_heating_state",
        translation_key="water_heating_state",
        topic="WaterHeating",
        parameter="Active",
        device_class=SensorDeviceClass.ENUM,
        options=list(ACTIVE_STATES.values()),
        enum_states=ACTIVE_STATES,
    ),
    TrumaSensorDescription(
        key="room_climate_mode",
        translation_key="room_climate_mode",
        topic="RoomClimate",
        parameter="Mode",
        use_panel_enum=True,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    TrumaSensorDescription(
        key="fan_level",
        translation_key="fan_level",
        topic="AirCirculation",
        parameter="FanLevel",
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
    ),
    TrumaSensorDescription(
        key="vehicle_battery_voltage",
        translation_key="vehicle_battery_voltage",
        topic="VBat",
        parameter="Voltage",
        scale=0.1,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
    ),
    TrumaSensorDescription(
        key="leisure_battery_voltage",
        translation_key="leisure_battery_voltage",
        topic="L1Bat",
        parameter="Voltage",
        scale=0.1,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
    ),
    TrumaSensorDescription(
        key="leisure_battery_current",
        translation_key="leisure_battery_current",
        topic="L1Bat",
        parameter="Current",
        scale=0.001,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=2,
    ),
    TrumaSensorDescription(
        key="leisure_battery_soc",
        translation_key="leisure_battery_soc",
        topic="L1Bat",
        parameter="SoC",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
    ),
    TrumaSensorDescription(
        key="error_code",
        translation_key="error_code",
        topic="ErrorReset",
        parameter="ErrCode",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    TrumaSensorDescription(
        key="power_mode",
        translation_key="power_mode",
        topic="PowerMgmt",
        parameter="PwrMode",
        use_panel_enum=True,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    TrumaSensorDescription(
        key="energy_source_priority",
        translation_key="energy_source_priority",
        topic="EnergySrc",
        parameter="EnergySourcePrio",
        use_panel_enum=True,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
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
            "sensor",
            [
                (
                    desc.key,
                    lambda state, d=desc: state.has(d.topic, d.parameter),
                    lambda d=desc: TrumaSensor(coordinator, d),
                )
                for desc in SENSORS
            ],
        )
    )


class TrumaSensor(TrumaParamEntity, SensorEntity):
    entity_description: TrumaSensorDescription

    def __init__(
        self, coordinator: TrumaCoordinator, description: TrumaSensorDescription
    ) -> None:
        super().__init__(
            coordinator, description, description.topic, description.parameter
        )

    @property
    def native_value(self):
        info = self.info
        if info is None or info.value is None:
            return None
        desc = self.entity_description
        if desc.enum_states is not None:
            return desc.enum_states.get(info.value)
        if desc.use_panel_enum:
            name = info.enum_name()
            return name if name is not None else info.value
        if desc.native_unit_of_measurement is not None and (
            not isinstance(info.value, (int, float)) or isinstance(info.value, bool)
        ):
            return None
        if isinstance(info.value, (int, float)) and not isinstance(info.value, bool):
            if desc.scale != 1.0:
                return round(info.value * desc.scale, 3)
            return scale_value(info)
        if isinstance(info.value, list):
            return ", ".join(str(v) for v in info.value) or "0"
        return info.value
