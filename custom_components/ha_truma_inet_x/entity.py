"""Base entity + helpers for the Truma iNet X integration."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity import Entity, EntityDescription
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .client import TrumaError
from .const import DOMAIN, MANUFACTURER, TYPE_CURRENT, TYPE_TEMPERATURE, TYPE_VOLTAGE
from .coordinator import TrumaCoordinator
from .protocol import ParameterInfo
from .state import TrumaState


class TrumaEntity(CoordinatorEntity[TrumaCoordinator]):
    """Common base class."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: TrumaCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.address)},
            connections={(CONNECTION_BLUETOOTH, coordinator.address)},
            manufacturer=MANUFACTURER,
            name=coordinator.config_entry.title,
        )

    @property
    def state_model(self) -> TrumaState:
        return self.coordinator.state

    @property
    def available(self) -> bool:
        return (
            self.coordinator.connected
            and self.coordinator.client.state.address is not None
        )

    def param(self, topic: str, name: str) -> ParameterInfo | None:
        return self.coordinator.state.get(topic, name)

    def pval(self, topic: str, name: str, default: Any = None) -> Any:
        return self.coordinator.state.value(topic, name, default)

    async def _write(self, topic: str, name: str, value: Any) -> None:
        try:
            await self.coordinator.async_write(topic, name, value)
        except TrumaError as err:
            raise HomeAssistantError(f"Truma: {err}") from err

    async def _write_many(self, writes: list[tuple[str, str, Any]]) -> None:
        try:
            await self.coordinator.async_write_many(writes)
        except TrumaError as err:
            raise HomeAssistantError(f"Truma: {err}") from err


class TrumaParamEntity(TrumaEntity):
    """Entity bound to exactly one (topic, parameter)."""

    topic: str
    parameter: str

    def __init__(
        self,
        coordinator: TrumaCoordinator,
        description: EntityDescription,
        topic: str,
        parameter: str,
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self.topic = topic
        self.parameter = parameter

    @property
    def info(self) -> ParameterInfo | None:
        return self.param(self.topic, self.parameter)

    @property
    def available(self) -> bool:
        info = self.info
        return (
            super().available
            and info is not None
            and info.available
            and info.value is not None
        )


def scale_value(info: ParameterInfo | None, default_scale: float = 1.0) -> float | None:
    """Convert a raw panel value into engineering units."""
    if (
        info is None
        or not isinstance(info.value, (int, float))
        or isinstance(info.value, bool)
    ):
        return None
    scale = default_scale
    if info.value_type == TYPE_TEMPERATURE or info.value_type == TYPE_VOLTAGE:
        scale = 0.1
    elif info.value_type == TYPE_CURRENT:
        scale = 0.001
    value = info.value * scale
    return round(value, 3)


def async_add_dynamic_entities(
    coordinator: TrumaCoordinator,
    async_add_entities: AddEntitiesCallback,
    platform_domain: str,
    candidates: Iterable[
        tuple[str, Callable[[TrumaState], bool], Callable[[], Entity]]
    ],
) -> Callable[[], None]:
    """Add entities as soon as the panel reported the underlying parameters.

    ``candidates``: (unique key, exists(state) -> bool, factory)
    Entities that already exist in the entity registry (known from an earlier
    run) are added immediately so they do not show up as "not provided".
    """
    pending = list(candidates)
    added: set[str] = set()
    registry = er.async_get(coordinator.hass)

    def _known(key: str) -> bool:
        return (
            registry.async_get_entity_id(
                platform_domain, DOMAIN, f"{coordinator.address}_{key}"
            )
            is not None
        )

    @callback
    def _check() -> None:
        new: list[Entity] = []
        for key, exists, factory in pending:
            if key in added:
                continue
            if exists(coordinator.state) or (not added_once and _known(key)):
                added.add(key)
                new.append(factory())
        if new:
            async_add_entities(new)

    added_once = False
    _check()
    added_once = True

    _check()
    return coordinator.async_add_listener(_check)
