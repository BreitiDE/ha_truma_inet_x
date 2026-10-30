"""Config flow for the Truma iNet X (Bluetooth) integration."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from typing import Any

import voluptuous as vol

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from .client import RegistrationError, TrumaError, TrumaInetXClient
from .const import (
    ADVERTISED_SERVICE_UUIDS,
    CMD_NOTIFY_4001,
    CMD_NOTIFY_4004,
    CMD_NOTIFY_AUTO,
    CONF_CMD_NOTIFY,
    CONF_LEGACY_PAIRING,
    CONF_MUID,
    CONF_PASSKEY,
    CONF_RELEASE_MINUTES,
    CONF_USERNAME,
    CONF_UUID,
    DEFAULT_RELEASE_MINUTES,
    DEFAULT_USERNAME,
    DOMAIN,
    PAIRING_FLAG_DISABLED,
    PAIRING_FLAG_ENABLED,
    PAIRING_FLAG_MULTIPLE,
    PAIRING_FLAGS,
    PANEL_TYPES,
    TRUMA_MANUFACTURER_ID,
)
from .pairing import PairingError, async_pair_bluez
from .protocol import Identity

_LOGGER = logging.getLogger(__name__)

CONF_SKIP_PAIRING = "skip_pairing"
TEST_TIMEOUT = 75.0
PAIRING_WAIT = 30.0
INTEGRATION_VERSION = "1.0.2"


def is_truma_panel(info: BluetoothServiceInfoBleak) -> bool:
    if TRUMA_MANUFACTURER_ID in info.manufacturer_data:
        return True
    uuids = {u.lower() for u in info.service_uuids}
    return bool(uuids & set(ADVERTISED_SERVICE_UUIDS))


def pairing_flag(info: BluetoothServiceInfoBleak | None) -> int | None:
    if info is None:
        return None
    data = info.manufacturer_data.get(TRUMA_MANUFACTURER_ID)
    if not data:
        return None
    return data[-1]


def pairing_state(info: BluetoothServiceInfoBleak | None) -> str:
    flag = pairing_flag(info)
    if flag is None:
        return "unknown"
    return PAIRING_FLAGS.get(flag, "unknown")


def panel_type(info: BluetoothServiceInfoBleak) -> str:
    for service_uuid in info.service_uuids:
        if (name := PANEL_TYPES.get(service_uuid.lower())) is not None:
            return name
    return "iNet X"


class TrumaInetXConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovery: BluetoothServiceInfoBleak | None = None
        self._discovered: dict[str, BluetoothServiceInfoBleak] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return TrumaInetXOptionsFlow()

    # ------------------------------------------------------------------
    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovery = discovery_info
        self.context["title_placeholders"] = {
            "name": discovery_info.name or panel_type(discovery_info)
        }
        return await self.async_step_pair()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            self._discovery = self._discovered[address]
            return await self.async_step_pair()

        current = self._async_current_ids(include_ignore=False)
        for info in async_discovered_service_info(self.hass, connectable=True):
            if info.address in current or info.address in self._discovered:
                continue
            if is_truma_panel(info):
                self._discovered[info.address] = info

        if not self._discovered:
            return self.async_abort(reason="no_devices_found")

        options = {
            address: f"{info.name or panel_type(info)} ({address})"
            for address, info in self._discovered.items()
        }
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_ADDRESS): vol.In(options)}),
        )

    # ------------------------------------------------------------------
    async def async_step_pair(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._discovery is not None
        info = self._latest_info()
        errors: dict[str, str] = {}
        detail = ""

        if user_input is not None:
            passkey_raw = str(user_input.get(CONF_PASSKEY) or "").strip()
            passkey: int | None = None
            if passkey_raw:
                if not passkey_raw.isdigit() or len(passkey_raw) > 6:
                    errors[CONF_PASSKEY] = "invalid_passkey"
                else:
                    passkey = int(passkey_raw)

            skip = bool(user_input.get(CONF_SKIP_PAIRING))
            _LOGGER.info(
                "Setup %s: submitted (pairing state %s, passkey %s, legacy %s, skip %s)",
                info.address,
                pairing_state(info),
                "yes" if passkey is not None else "no",
                user_input.get(CONF_LEGACY_PAIRING, True),
                skip,
            )
            if not errors and not skip:
                flag_error = await self._async_wait_pairing_enabled()
                if flag_error:
                    _LOGGER.warning(
                        "Setup %s: panel does not allow pairing (%s, state %s)",
                        info.address,
                        flag_error,
                        pairing_state(self._latest_info()),
                    )
                    errors["base"] = flag_error
                else:
                    info = self._latest_info()
                    try:
                        await self._async_pair(
                            info,
                            passkey,
                            bool(user_input.get(CONF_LEGACY_PAIRING, True)),
                        )
                    except PairingError as err:
                        _LOGGER.warning("Pairing with %s failed: %s", info.address, err)
                        errors["base"] = err.reason
                        detail = err.detail

            if not errors:
                identity = Identity(
                    muid=str(uuid.uuid4()).upper(),
                    uuid=str(uuid.uuid4()).lower(),
                    username=user_input.get(CONF_USERNAME) or DEFAULT_USERNAME,
                )
                _LOGGER.info(
                    "Setup %s: connecting and registering at the panel", info.address
                )
                try:
                    await self._async_test_session(info, identity)
                except RegistrationError as err:
                    _LOGGER.warning("Registration at %s failed: %s", info.address, err)
                    errors["base"] = "no_registration"
                    detail = str(err)
                except (TrumaError, TimeoutError, Exception) as err:  # noqa: BLE001
                    _LOGGER.warning("Connecting to %s failed: %s", info.address, err)
                    _LOGGER.debug("details", exc_info=True)
                    errors["base"] = "cannot_connect"
                    detail = str(err)
                else:
                    _LOGGER.info("Setup %s: registration successful", info.address)
                    return self.async_create_entry(
                        title=info.name or panel_type(info),
                        data={
                            CONF_ADDRESS: info.address,
                            CONF_MUID: identity.muid,
                            CONF_UUID: identity.uuid,
                            CONF_USERNAME: identity.username,
                        },
                    )

        if user_input is None:
            _LOGGER.info(
                "Setup %s (%s): pairing form shown (integration %s), pairing state %s, source %s",
                info.address,
                info.name,
                INTEGRATION_VERSION,
                pairing_state(info),
                info.source,
            )
        defaults = user_input or {}
        return self.async_show_form(
            step_id="pair",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_USERNAME,
                        default=defaults.get(CONF_USERNAME, DEFAULT_USERNAME),
                    ): TextSelector(),
                    vol.Optional(
                        CONF_LEGACY_PAIRING,
                        default=defaults.get(CONF_LEGACY_PAIRING, True),
                    ): bool,
                    vol.Optional(CONF_PASSKEY, default=""): TextSelector(),
                    vol.Optional(
                        CONF_SKIP_PAIRING,
                        default=defaults.get(CONF_SKIP_PAIRING, False),
                    ): bool,
                }
            ),
            errors=errors,
            description_placeholders={
                "name": info.name or panel_type(info),
                "address": info.address,
                "pairing_state": pairing_state(info),
                "detail": detail or "-",
            },
        )

    def _latest_info(self) -> BluetoothServiceInfoBleak:
        """Most recent advertisement (the pairing flag changes at runtime)."""
        assert self._discovery is not None
        latest = bluetooth.async_last_service_info(
            self.hass, self._discovery.address, connectable=True
        )
        if latest is not None:
            self._discovery = latest
        return self._discovery

    async def _async_wait_pairing_enabled(self) -> str | None:
        """Wait (max. PAIRING_WAIT s) until the panel advertises 'pairing active'."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + PAIRING_WAIT
        flag: int | None = None
        _LOGGER.info(
            "Setup %s: waiting up to %.0f s for 'pairing active' (current state %s)",
            self._discovery.address if self._discovery else "?",
            PAIRING_WAIT,
            pairing_state(self._discovery),
        )
        while True:
            flag = pairing_flag(self._latest_info())
            if flag is None or flag == PAIRING_FLAG_ENABLED:
                return None
            if loop.time() >= deadline:
                break
            await asyncio.sleep(1)
        if flag == PAIRING_FLAG_MULTIPLE:
            return "other_phone_connected"
        if flag == PAIRING_FLAG_DISABLED:
            return "pairing_not_active"
        return None

    # ------------------------------------------------------------------
    async def _async_pair(
        self, info: BluetoothServiceInfoBleak, passkey: int | None, legacy: bool
    ) -> None:
        try:
            result = await async_pair_bluez(
                info.address, passkey, adapter_hint=info.source, legacy=legacy
            )
            _LOGGER.info("Setup %s: BlueZ pairing result %s", info.address, result)
            return
        except PairingError as err:
            if err.reason not in ("device_not_local", "dbus_unavailable"):
                raise
            _LOGGER.debug(
                "Local BlueZ pairing not possible (%s), trying bleak", err.reason
            )

        # Remote adapter (e.g. ESPHome Bluetooth proxy): "Just Works" pairing
        from bleak_retry_connector import (
            BleakClientWithServiceCache,
            establish_connection,
        )

        device = bluetooth.async_ble_device_from_address(
            self.hass, info.address, connectable=True
        )
        if device is None:
            raise PairingError("cannot_connect", "device not reachable")
        client = await establish_connection(
            BleakClientWithServiceCache,
            device,
            info.name or info.address,
            max_attempts=3,
        )
        try:
            await client.pair()
        except Exception as err:  # noqa: BLE001
            raise PairingError("pairing_failed", str(err)) from err
        finally:
            with contextlib.suppress(Exception):
                await client.disconnect()

    async def _async_test_session(
        self, info: BluetoothServiceInfoBleak, identity: Identity
    ) -> None:
        """Connect, register and introduce ourselves (= the app's first contact)."""
        client = TrumaInetXClient(
            address=info.address,
            identity=identity,
            ble_device_callback=lambda: bluetooth.async_ble_device_from_address(
                self.hass, info.address, connectable=True
            ),
            name=info.name or info.address,
        )

        async def _run() -> None:
            await client.connect()
            await client.register()
            # give the panel the chance to ask for our identity
            for _ in range(20):
                if client._identity_sent:  # noqa: SLF001
                    break
                await asyncio.sleep(0.5)
            if not client._identity_sent:  # noqa: SLF001
                await client.send_identity(0x0000)
            await asyncio.sleep(2)

        try:
            await asyncio.wait_for(_run(), TEST_TIMEOUT)
        finally:
            await client.stop()


class TrumaInetXOptionsFlow(OptionsFlow):
    """Options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_USERNAME,
                        default=options.get(
                            CONF_USERNAME,
                            self.config_entry.data.get(CONF_USERNAME, DEFAULT_USERNAME),
                        ),
                    ): TextSelector(),
                    vol.Optional(
                        CONF_RELEASE_MINUTES,
                        default=options.get(
                            CONF_RELEASE_MINUTES, DEFAULT_RELEASE_MINUTES
                        ),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=1,
                            max=120,
                            step=1,
                            mode=NumberSelectorMode.BOX,
                            unit_of_measurement="min",
                        )
                    ),
                    vol.Optional(
                        CONF_CMD_NOTIFY,
                        default=options.get(CONF_CMD_NOTIFY, CMD_NOTIFY_AUTO),
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[CMD_NOTIFY_AUTO, CMD_NOTIFY_4001, CMD_NOTIFY_4004],
                            mode=SelectSelectorMode.DROPDOWN,
                            translation_key="cmd_notify",
                        )
                    ),
                }
            ),
        )
