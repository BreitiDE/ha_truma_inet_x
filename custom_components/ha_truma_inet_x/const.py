"""Constants for the Truma iNet X (BLE) integration.

All protocol values were taken from the decompiled Truma iNet X Android app
(com.truma.inetx 3.5.0) and cross-checked against
https://github.com/daaaaan/truma-inetx-ble.
"""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "ha_truma_inet_x"
MANUFACTURER: Final = "Truma"

# --------------------------------------------------------------------------
# Config entry keys
# --------------------------------------------------------------------------
CONF_MUID: Final = "muid"
CONF_UUID: Final = "uuid"
CONF_USERNAME: Final = "username"
CONF_PASSKEY: Final = "passkey"
CONF_CMD_NOTIFY: Final = "cmd_notify_characteristic"

CMD_NOTIFY_AUTO: Final = "auto"
CMD_NOTIFY_4001: Final = "fc314001"
CMD_NOTIFY_4004: Final = "fc314004"

DEFAULT_USERNAME: Final = "Home Assistant"

# --------------------------------------------------------------------------
# BLE
# --------------------------------------------------------------------------
TRUMA_MANUFACTURER_ID: Final = 3187  # 0x0C73

_UUID_BASE = "-f3b2-11e8-8eb2-f2801f1b9fd1"

SERVICE_UUID_LEGACY: Final = "fc310000" + _UUID_BASE
SERVICE_UUID_MASTER_PANEL: Final = "fc310001" + _UUID_BASE
SERVICE_UUID_ROOM_CLIMATE_PANEL: Final = "fc310002" + _UUID_BASE
SERVICE_UUID_INTERFACE_AC: Final = "fc310003" + _UUID_BASE
SERVICE_UUID_INTERFACE: Final = "fc310004" + _UUID_BASE
SERVICE_UUID_MASTER_FACELIFT_PANEL: Final = "fc310005" + _UUID_BASE
SERVICE_UUID_ROOM_CLIMATE_FACELIFT_PANEL: Final = "fc310006" + _UUID_BASE
SERVICE_UUID_FILTER: Final = "fc316029" + _UUID_BASE

ADVERTISED_SERVICE_UUIDS: Final = (
    SERVICE_UUID_LEGACY,
    SERVICE_UUID_MASTER_PANEL,
    SERVICE_UUID_ROOM_CLIMATE_PANEL,
    SERVICE_UUID_INTERFACE_AC,
    SERVICE_UUID_INTERFACE,
    SERVICE_UUID_MASTER_FACELIFT_PANEL,
    SERVICE_UUID_ROOM_CLIMATE_FACELIFT_PANEL,
    SERVICE_UUID_FILTER,
)

PANEL_TYPES: Final = {
    SERVICE_UUID_LEGACY: "iNet X",
    SERVICE_UUID_MASTER_PANEL: "iNet X Panel",
    SERVICE_UUID_ROOM_CLIMATE_PANEL: "iNet X Room Climate Panel",
    SERVICE_UUID_INTERFACE_AC: "iNet X Interface AC",
    SERVICE_UUID_INTERFACE: "iNet X Interface",
    SERVICE_UUID_MASTER_FACELIFT_PANEL: "iNet X Panel (Facelift)",
    SERVICE_UUID_ROOM_CLIMATE_FACELIFT_PANEL: "iNet X Room Climate Panel (Facelift)",
}

DATA_SERVICE_UUID: Final = "f47bbbac" + _UUID_BASE
CHAR_CMD: Final = "fc314001" + _UUID_BASE  # write (with response) + notify
CHAR_DATA_WRITE: Final = "fc314002" + _UUID_BASE  # write without response
CHAR_DATA_READ: Final = "fc314003" + _UUID_BASE  # notify
CHAR_CMD_ALT: Final = "fc314004" + _UUID_BASE  # alternative cmd notify
CHAR_SOFTWARE_REVISION: Final = "00002a28-0000-1000-8000-00805f9b34fb"

# Pairing flag = last byte of the manufacturer specific advertisement data
PAIRING_FLAGS: Final = {
    0x01: "pairing_enabled",
    0x00: "pairing_disabled",
    0xF0: "pairing_failed",
    0xF1: "pairing_not_possible_multiple_connections",
}

# --------------------------------------------------------------------------
# Timing (milliseconds in the app, seconds here)
# --------------------------------------------------------------------------
TRANSPORT_TIMEOUT: Final = 10.0  # ListExtension.waitForFirst / waitForDataLengthOf
MESSAGE_RESPONSE_TIMEOUT: Final = 8.0  # AppConstants.MESSAGE_RESPONSE_TIME_OUT
SUBSCRIBE_BATCH_SIZE: Final = 10
SUBSCRIBE_BATCH_DELAY: Final = 0.25
WRITE_MIN_INTERVAL: Final = 0.1  # RateLimitingTruMessageLayerWriter: 10 msg/s
WRITE_SEQUENCE_DELAY: Final = 0.4  # pause between multi-parameter writes
RECONNECT_MIN_DELAY: Final = 5.0
RECONNECT_MAX_DELAY: Final = 120.0
READY_TIMEOUT: Final = 60.0

# --------------------------------------------------------------------------
# Topics (MessagePayloadConstants.LIST_OF_ALL_TOPICS, same order as the app)
# --------------------------------------------------------------------------
ALL_TOPICS: Final = (
    "Identify",
    "Transfer",
    "Install",
    "DeviceManagement",
    "AirHeating",
    "WaterHeating",
    "AirCirculation",
    "EnergySrc",
    "PowerSupply",
    "PowerMgmt",
    "GasControl",
    "Switches",
    "Temperature",
    "ErrorReset",
    "VBat",
    "L1Bat",
    "L2Bat",
    "LinePower",
    "FreshWater",
    "GreyWater",
    "RoomClimate",
    "AirCooling",
    "AmbientLight",
    "GasBtl",
    "BleDeviceManagement",
    "Panel",
    "BatteryMngmt",
    "MobileIdentity",
    "Connect",
    "TimerConfig",
    "BluetoothDevice",
    "System",
    "Resources",
    "Fridge",
    "SubDevices",
)

TOPIC_ROOM_CLIMATE: Final = "RoomClimate"
TOPIC_AIR_HEATING: Final = "AirHeating"
TOPIC_AIR_COOLING: Final = "AirCooling"
TOPIC_AIR_CIRCULATION: Final = "AirCirculation"
TOPIC_WATER_HEATING: Final = "WaterHeating"
TOPIC_ENERGY_SRC: Final = "EnergySrc"
TOPIC_TEMPERATURE: Final = "Temperature"
TOPIC_ERROR_RESET: Final = "ErrorReset"
TOPIC_LINE_POWER: Final = "LinePower"
TOPIC_VBAT: Final = "VBat"
TOPIC_IDENTIFY: Final = "Identify"
TOPIC_SYSTEM: Final = "System"
TOPIC_PANEL: Final = "Panel"

# --------------------------------------------------------------------------
# Device addresses (TruDevice)
# --------------------------------------------------------------------------
ADDR_MESSAGE_BROKER: Final = 0x0000
ADDR_PANEL: Final = 0x0101
ADDR_HEATER_FALLBACK: Final = 0x0201  # TIN bus device #1 (Combi heater)
ADDR_BROADCAST: Final = 0xFFFF

# Fallback routing when the publishing device of a topic is still unknown
# (taken from the reference implementation, validated on a Combi D 4 E).
FALLBACK_TOPIC_DEVICE: Final = {
    TOPIC_ROOM_CLIMATE: ADDR_PANEL,
    TOPIC_AIR_HEATING: ADDR_HEATER_FALLBACK,
    TOPIC_WATER_HEATING: ADDR_HEATER_FALLBACK,
    TOPIC_AIR_CIRCULATION: ADDR_HEATER_FALLBACK,
    TOPIC_AIR_COOLING: ADDR_HEATER_FALLBACK,
    TOPIC_ENERGY_SRC: ADDR_HEATER_FALLBACK,
}

# --------------------------------------------------------------------------
# Enumerations (core/model/enums)
# --------------------------------------------------------------------------
# RoomClimate.Mode  (ModeType)
MODE_OFF: Final = 0
MODE_ACC: Final = 1  # automatic climate control
MODE_COOLING: Final = 2
MODE_HEATING: Final = 3
MODE_HEATING_AC: Final = 4
MODE_VENTING: Final = 5
MODE_DEHUMIDIFYING: Final = 6

# *.Active  (ControlLoopStateEnum)
ACTIVE_OFF: Final = 0
ACTIVE_ON: Final = 1
ACTIVE_IDLE: Final = 2

# BooleanEnum
BOOL_OFF: Final = 0
BOOL_ON: Final = 1

# ParameterValueType (type field of a ParameterRead)
TYPE_NUMBER: Final = 1
TYPE_ENUMERATION: Final = 2
TYPE_BOOLEAN: Final = 3
TYPE_STRING: Final = 4
TYPE_TEMPERATURE: Final = 10
TYPE_VOLTAGE: Final = 11
TYPE_CURRENT: Final = 12
TYPE_PERCENTAGE: Final = 15
TYPE_TIME: Final = 18

# Water heating mode names as delivered by the panel (HotWaterModeType)
HOT_WATER_NAME_MAP: Final = {
    "40": "eco",
    "eco": "eco",
    "60": "comfort",
    "comfort": "comfort",
    "70": "hot",
    "hot": "hot",
    "away": "away",
    "night": "night",
    "shower": "shower",
}
HOT_WATER_DEFAULT_ENUM: Final = ((0, "40"), (1, "60"), (2, "70"))
HOT_WATER_TARGET_TEMP: Final = {"eco": 40, "comfort": 60, "hot": 70}

SIGNAL_DATA = f"{DOMAIN}_data"
