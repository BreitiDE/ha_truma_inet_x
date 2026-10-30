"""Room climate (heating / ventilation) of the Truma system."""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ACTIVE_IDLE,
    ACTIVE_OFF,
    ACTIVE_ON,
    DOMAIN,
    MODE_ACC,
    MODE_COOLING,
    MODE_DEHUMIDIFYING,
    MODE_HEATING,
    MODE_HEATING_AC,
    MODE_OFF,
    MODE_VENTING,
    TOPIC_AIR_CIRCULATION,
    TOPIC_AIR_COOLING,
    TOPIC_AIR_HEATING,
    TOPIC_ROOM_CLIMATE,
    TOPIC_TEMPERATURE,
)
from .coordinator import TrumaConfigEntry, TrumaCoordinator
from .entity import TrumaEntity

PARALLEL_UPDATES = 0

MODE_TO_HVAC: dict[int, HVACMode] = {
    MODE_OFF: HVACMode.OFF,
    MODE_ACC: HVACMode.AUTO,
    MODE_COOLING: HVACMode.COOL,
    MODE_HEATING: HVACMode.HEAT,
    MODE_HEATING_AC: HVACMode.HEAT,
    MODE_VENTING: HVACMode.FAN_ONLY,
    MODE_DEHUMIDIFYING: HVACMode.DRY,
}
DEFAULT_MODES = (MODE_OFF, MODE_HEATING, MODE_VENTING)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TrumaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([TrumaClimate(entry.runtime_data)])


class TrumaClimate(TrumaEntity, ClimateEntity):
    """RoomClimate / AirHeating as one climate entity."""

    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 0.5

    def __init__(self, coordinator: TrumaCoordinator) -> None:
        super().__init__(coordinator, "climate")

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _mode_values(self) -> dict[HVACMode, int]:
        """HVAC mode -> RoomClimate.Mode value (only modes the system offers)."""
        options = self.state_model.enum_options(TOPIC_ROOM_CLIMATE, "Mode")
        values = [o.value for o in options] if options else list(DEFAULT_MODES)
        if not self.state_model.has(TOPIC_ROOM_CLIMATE, "Mode"):
            values = list(DEFAULT_MODES)
        result: dict[HVACMode, int] = {}
        for value in values:
            hvac = MODE_TO_HVAC.get(value)
            if hvac is None:
                continue
            # prefer pure heating over heating with AC
            if (
                hvac == HVACMode.HEAT
                and HVACMode.HEAT in result
                and value == MODE_HEATING_AC
            ):
                continue
            result[hvac] = value
        result.setdefault(HVACMode.OFF, MODE_OFF)
        return result

    def _room_mode(self) -> int | None:
        mode = self.pval(TOPIC_ROOM_CLIMATE, "Mode")
        if isinstance(mode, int):
            return mode
        # Systems without RoomClimate topic: derive from AirHeating.Active
        active = self.pval(TOPIC_AIR_HEATING, "Active")
        if isinstance(active, int):
            return MODE_OFF if active == ACTIVE_OFF else MODE_HEATING
        return None

    def _temp_param(self) -> tuple[str, str]:
        mode = self._room_mode()
        if mode == MODE_ACC and self.state_model.has(TOPIC_ROOM_CLIMATE, "TgtTemp"):
            return TOPIC_ROOM_CLIMATE, "TgtTemp"
        if mode == MODE_COOLING and self.state_model.has(TOPIC_AIR_COOLING, "TgtTemp"):
            return TOPIC_AIR_COOLING, "TgtTemp"
        if self.state_model.has(TOPIC_AIR_HEATING, "TgtTemp"):
            return TOPIC_AIR_HEATING, "TgtTemp"
        return TOPIC_ROOM_CLIMATE, "TgtTemp"

    # ------------------------------------------------------------------
    # state
    # ------------------------------------------------------------------
    @property
    def available(self) -> bool:
        return super().available and (
            self.state_model.has_topic(TOPIC_ROOM_CLIMATE)
            or self.state_model.has_topic(TOPIC_AIR_HEATING)
        )

    @property
    def supported_features(self) -> ClimateEntityFeature:
        features = (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.TURN_ON
            | ClimateEntityFeature.TURN_OFF
        )
        if self.preset_modes:
            features |= ClimateEntityFeature.PRESET_MODE
        return features

    @property
    def hvac_modes(self) -> list[HVACMode]:
        return list(self._mode_values())

    @property
    def hvac_mode(self) -> HVACMode | None:
        mode = self._room_mode()
        if mode is None:
            return None
        return MODE_TO_HVAC.get(mode, HVACMode.HEAT)

    @property
    def hvac_action(self) -> HVACAction | None:
        mode = self._room_mode()
        if mode is None:
            return None
        if mode == MODE_OFF:
            return HVACAction.OFF
        if mode == MODE_VENTING:
            return HVACAction.FAN
        if mode == MODE_DEHUMIDIFYING:
            return HVACAction.DRYING
        if mode == MODE_COOLING:
            active = self.pval(TOPIC_AIR_COOLING, "Active")
            return HVACAction.COOLING if active == ACTIVE_ON else HVACAction.IDLE
        active = self.pval(TOPIC_AIR_HEATING, "Active")
        if active == ACTIVE_ON:
            return HVACAction.HEATING
        if active in (ACTIVE_IDLE, ACTIVE_OFF):
            return HVACAction.IDLE
        return None

    @property
    def current_temperature(self) -> float | None:
        for topic, name in (
            (TOPIC_AIR_HEATING, "Temp"),
            (TOPIC_AIR_COOLING, "Temp"),
            (TOPIC_TEMPERATURE, "Internal"),
        ):
            value = self.pval(topic, name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return round(value / 10, 1)
        return None

    @property
    def target_temperature(self) -> float | None:
        value = self.pval(*self._temp_param())
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return round(value / 10, 1)
        return None

    @property
    def min_temp(self) -> float:
        info = self.param(*self._temp_param())
        if info is not None and info.minimum is not None:
            return info.minimum / 10
        return 5.0

    @property
    def max_temp(self) -> float:
        info = self.param(*self._temp_param())
        if info is not None and info.maximum is not None:
            return info.maximum / 10
        return 30.0

    @property
    def preset_modes(self) -> list[str] | None:
        if not self.state_model.has(TOPIC_AIR_HEATING, "Mode"):
            return None
        options = self.state_model.enum_options(TOPIC_AIR_HEATING, "Mode")
        names = [o.name for o in options]
        return names or None

    @property
    def preset_mode(self) -> str | None:
        info = self.param(TOPIC_AIR_HEATING, "Mode")
        if info is None:
            return None
        return info.enum_name()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {}
        mode = self.pval(TOPIC_ROOM_CLIMATE, "Mode")
        info = self.param(TOPIC_ROOM_CLIMATE, "Mode")
        if info is not None and mode is not None:
            attrs["room_climate_mode"] = info.enum_name() or mode
        fan = self.pval(TOPIC_AIR_CIRCULATION, "FanLevel")
        if fan is not None:
            attrs["fan_level"] = fan
        heating = self.param(TOPIC_AIR_HEATING, "Active")
        if heating is not None and heating.value is not None:
            attrs["air_heating_state"] = {0: "off", 1: "active", 2: "idle"}.get(
                heating.value, heating.value
            )
        return attrs

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------
    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        modes = self._mode_values()
        if hvac_mode not in modes:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unsupported_hvac_mode",
                translation_placeholders={"mode": str(hvac_mode)},
            )
        target = modes[hvac_mode]

        if not self.state_model.has(TOPIC_ROOM_CLIMATE, "Mode"):
            # Fallback for systems without RoomClimate topic
            if hvac_mode == HVACMode.OFF:
                await self._write(TOPIC_AIR_HEATING, "Active", ACTIVE_OFF)
            elif hvac_mode == HVACMode.FAN_ONLY:
                await self.coordinator.client.set_air_circulation(
                    int(self.pval(TOPIC_AIR_CIRCULATION, "FanLevel", 0) or 5)
                )
            else:
                await self._write(TOPIC_AIR_HEATING, "Active", ACTIVE_ON)
            return

        if hvac_mode == HVACMode.OFF and self._room_mode() == MODE_VENTING:
            # app: resetVentilationFanLevelsAndTurnOffRoomClimate
            writes: list[tuple[str, str, Any]] = []
            if self.state_model.has(TOPIC_AIR_CIRCULATION, "FanLevel"):
                writes.append((TOPIC_AIR_CIRCULATION, "FanLevel", 0))
            writes.append((TOPIC_ROOM_CLIMATE, "Mode", MODE_OFF))
            await self._write_many(writes)
            return
        await self._write(TOPIC_ROOM_CLIMATE, "Mode", target)

    async def async_turn_on(self) -> None:
        modes = self._mode_values()
        await self.async_set_hvac_mode(
            HVACMode.HEAT
            if HVACMode.HEAT in modes
            else next(iter(m for m in modes if m != HVACMode.OFF))
        )

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if (hvac_mode := kwargs.get("hvac_mode")) is not None:
            await self.async_set_hvac_mode(hvac_mode)
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        topic, name = self._temp_param()
        value = int(round(float(temperature) * 10))
        value = max(int(self.min_temp * 10), min(int(self.max_temp * 10), value))
        await self._write(topic, name, value)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        options = self.state_model.enum_options(TOPIC_AIR_HEATING, "Mode")
        for option in options:
            if option.name == preset_mode:
                await self._write(TOPIC_AIR_HEATING, "Mode", option.value)
                return
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unsupported_preset",
            translation_placeholders={"preset": preset_mode},
        )
