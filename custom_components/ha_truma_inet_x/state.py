"""In-memory model of everything the panel told us (TopicRegistry of the app).

Several bus devices may publish a topic with the same name (e.g. every device
has an ``Identify`` topic, a system may have two ``AirHeating`` units), so the
registry is keyed by (device, topic, parameter).  Look-ups by (topic, parameter)
resolve to the *primary* device of that topic.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from .const import FALLBACK_TOPIC_DEVICE
from .protocol import EnumOption, ParameterInfo

Key = tuple[int, str, str]


def _is_phone(device: int) -> bool:
    return (device >> 8) in (0x05, 0x06)


class TrumaState:
    """Topic registry."""

    def __init__(self) -> None:
        self.params: dict[Key, ParameterInfo] = {}
        self.topic_devices: dict[str, list[int]] = {}
        self._discovered_topics: dict[str, int] = {}
        self.devices: list[int] = []
        self.restricted_devices: list[int] = []
        self.disconnected_devices: list[int] = []
        self.discovered_devices: set[int] = set()
        self.subscribed_topics: list[str] = []
        self.address: int | None = None
        self.protocol_version: tuple[int, int] | None = None
        self.software_revision: str | None = None
        self.last_update: float = 0.0
        self._listeners: list[Callable[[set[tuple[str, str]]], None]] = []

    # ------------------------------------------------------------------
    # listeners
    # ------------------------------------------------------------------
    def add_listener(
        self, listener: Callable[[set[tuple[str, str]]], None]
    ) -> Callable[[], None]:
        self._listeners.append(listener)

        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    def _notify(self, changed: set[tuple[str, str]]) -> None:
        if not changed:
            return
        for listener in list(self._listeners):
            listener(changed)

    # ------------------------------------------------------------------
    # updates
    # ------------------------------------------------------------------
    def _register_topic_device(self, topic: str, device: int) -> None:
        devices = self.topic_devices.setdefault(topic, [])
        if device not in devices:
            devices.append(device)

    def apply_info(self, info: ParameterInfo, source: int) -> bool:
        """Apply an INFO message (value + whatever metadata is present)."""
        key = (source, info.topic, info.name)
        existing = self.params.get(key)
        now = time.time()
        if existing is None:
            info.device = source
            info.updated = now
            self.params[key] = info
            changed = True
        else:
            changed = (
                existing.value != info.value or existing.available != info.available
            )
            existing.value = info.value
            existing.available = info.available
            if info.value_type is not None:
                existing.value_type = info.value_type
            if info.permission is not None:
                existing.permission = info.permission
            if info.minimum is not None:
                existing.minimum = info.minimum
            if info.maximum is not None:
                existing.maximum = info.maximum
            if info.enum:
                changed = changed or existing.enum != info.enum
                existing.enum = info.enum
            existing.updated = now
        self._register_topic_device(info.topic, source)
        self.last_update = now
        if changed:
            self._notify({(info.topic, info.name)})
        return changed

    def apply_discovery(self, infos: list[ParameterInfo], source: int) -> None:
        """Apply the parameters of a ParameterDiscoveryResponse (full metadata)."""
        changed: set[tuple[str, str]] = set()
        now = time.time()
        for info in infos:
            key = (source, info.topic, info.name)
            info.device = source
            info.updated = now
            old = self.params.get(key)
            if (
                old is None
                or old.value != info.value
                or old.enum != info.enum
                or old.available != info.available
            ):
                changed.add((info.topic, info.name))
            self.params[key] = info
            self._register_topic_device(info.topic, source)
            self._discovered_topics.setdefault(info.topic, source)
        self.last_update = now
        self._notify(changed)

    # ------------------------------------------------------------------
    # look-ups
    # ------------------------------------------------------------------
    def primary_device(self, topic: str) -> int | None:
        """Device that 'owns' a topic (PD source preferred, phones ignored)."""
        if topic in self._discovered_topics:
            return self._discovered_topics[topic]
        devices = self.topic_devices.get(topic) or []
        for dev in devices:
            if not _is_phone(dev) and dev != self.address:
                return dev
        return None

    def get(self, topic: str, name: str) -> ParameterInfo | None:
        dev = self.primary_device(topic)
        if dev is not None:
            info = self.params.get((dev, topic, name))
            if info is not None:
                return info
        # parameter only published by another device of the same topic
        for dev in self.topic_devices.get(topic, []):
            if _is_phone(dev) or dev == self.address:
                continue
            info = self.params.get((dev, topic, name))
            if info is not None:
                return info
        return None

    def value(self, topic: str, name: str, default: Any = None) -> Any:
        info = self.get(topic, name)
        if info is None or info.value is None:
            return default
        return info.value

    def has(self, topic: str, name: str) -> bool:
        return self.get(topic, name) is not None

    def has_topic(self, topic: str) -> bool:
        return self.primary_device(topic) is not None

    def device_params(self, device: int, topic: str) -> dict[str, ParameterInfo]:
        return {
            name: info
            for (dev, tn, name), info in self.params.items()
            if dev == device and tn == topic
        }

    def enum_options(
        self,
        topic: str,
        name: str,
        fallback: tuple[tuple[int, str], ...] = (),
        only_available: bool = True,
    ) -> list[EnumOption]:
        info = self.get(topic, name)
        if info is not None and info.enum:
            options = list(info.enum)
            if only_available:
                available = [o for o in options if o.available]
                options = available or options
            return options
        return [EnumOption(value=v, name=n) for v, n in fallback]

    def device_for_topic(self, topic: str) -> int | None:
        dev = self.primary_device(topic)
        if dev is not None:
            return dev
        return FALLBACK_TOPIC_DEVICE.get(topic)

    # ------------------------------------------------------------------
    def as_dict(self) -> dict[str, Any]:
        devices: dict[str, dict[str, dict[str, Any]]] = {}
        for (dev, topic, name), info in sorted(self.params.items()):
            devices.setdefault(f"0x{dev:04X}", {}).setdefault(topic, {})[name] = (
                info.as_dict()
            )

        def _hex(values) -> list[str]:
            return [f"0x{d:04X}" for d in values]

        return {
            "address": f"0x{self.address:04X}" if self.address is not None else None,
            "protocol_version": self.protocol_version,
            "software_revision": self.software_revision,
            "devices": _hex(self.devices),
            "restricted_devices": _hex(self.restricted_devices),
            "disconnected_devices": _hex(self.disconnected_devices),
            "discovered_devices": _hex(sorted(self.discovered_devices)),
            "topic_owner": {
                t: f"0x{d:04X}"
                for t in sorted(self.topic_devices)
                if (d := self.primary_device(t)) is not None
            },
            "subscribed_topics": self.subscribed_topics,
            "parameters": devices,
        }
