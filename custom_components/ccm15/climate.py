"""
Support for Midea's CCM-15 thermostats.
Author: github.com/ohsc
configuration.yaml
climate:
  - platform: ccm15
    name: ccm15
    host: IP_ADDRESS
    port: 80
    scan_interval: 10
"""
import logging
import json
import voluptuous as vol
import asyncio
import async_timeout

from homeassistant.components.climate import ClimateEntity, PLATFORM_SCHEMA
from homeassistant.components.climate.const import (
    ATTR_HVAC_MODE, 
    HVACMode,
    FAN_AUTO,
    FAN_LOW,
    FAN_MEDIUM,
    FAN_HIGH,
    FAN_OFF,
    ClimateEntityFeature,
)
from homeassistant.const import (
    CONF_NAME, 
    CONF_HOST, 
    CONF_PORT,
    UnitOfTemperature,
    ATTR_TEMPERATURE,
)
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import xmltodict

DOMAIN = 'ccm15'
_LOGGER = logging.getLogger(__name__)
DEFAULT_NAME = 'Midea Thermostat'
DEFAULT_TIMEOUT = 5
BASE_URL = 'http://{0}:{1}{2}'
CONF_URL_STATUS = '/status.xml'
CONF_URL_CTRL = '/ctrl.xml'
ATTR_MODE = 'mode'

# Define fan modes
CONST_MODE_FAN_AUTO = FAN_AUTO
CONST_MODE_FAN_LOW = FAN_LOW
CONST_MODE_FAN_MIDDLE = FAN_MEDIUM
CONST_MODE_FAN_HIGH = FAN_HIGH
CONST_MODE_FAN_OFF = FAN_OFF

# State to command mapping
CONST_STATE_CMD_MAP = {
    HVACMode.COOL: 0,
    HVACMode.HEAT: 1,
    HVACMode.DRY: 2,
    HVACMode.FAN_ONLY: 3,
    HVACMode.OFF: 4,
    HVACMode.AUTO: 5
}
CONST_CMD_STATE_MAP = {v: k for k, v in CONST_STATE_CMD_MAP.items()}

# Fan mode to command mapping
CONST_FAN_CMD_MAP = {
    CONST_MODE_FAN_AUTO: 0,
    CONST_MODE_FAN_LOW: 2,
    CONST_MODE_FAN_MIDDLE: 3,
    CONST_MODE_FAN_HIGH: 4,
    CONST_MODE_FAN_OFF: 5
}
CONST_CMD_FAN_MAP = {v: k for k, v in CONST_FAN_CMD_MAP.items()}

# Supported HVAC modes
SUPPORT_HVAC = [
    HVACMode.COOL,
    HVACMode.HEAT,
    HVACMode.DRY,
    HVACMode.FAN_ONLY,
    HVACMode.OFF,
    HVACMode.AUTO
]

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    vol.Optional(CONF_HOST, default='192.168.1.200'): cv.string,
    vol.Optional(CONF_PORT, default=80): cv.positive_int,
})

# parse data from ccm bytes
def get_status_from(s):
    locked_wind = 0
    locked_mode = 0
    locked_cold_temp = 0
    locked_hot_temp = 0
    ml = 0
    fl = 0
    ctl = 0
    htl = 0
    rml = 0
    mode = 0
    fan = 0
    temp = 0
    err = 0
    settemp = 0
    if s == '-':
        return None
    
    bytesarr = bytes.fromhex(s.strip(','))
    buf = bytesarr[0]
    is_degreeF = (buf >> 0) & 1
    ctl = (buf >> 3) & 0x1f
    
    buf = bytesarr[1]
    htl = (buf >> 0) & 0x1f
    locked_wind = (buf >> 5) & 7
    
    buf = bytesarr[2]
    locked_mode = (buf >> 0) & 3
    err = (buf >> 2) & 0x3f
    if locked_mode == 1:
        locked_mode = 0
    elif locked_mode == 2:
        locked_mode = 1
    else:
        locked_mode = -1
    
    buf = bytesarr[3]
    mode = (buf >> 2) & 7
    fan = (buf >> 5) & 7
    buf = (buf >> 1) & 1
    if buf != 0:
        ml = 1
    
    buf = bytesarr[4]
    settemp = (buf >> 3) & 0x1f
    DEGREE = "℃"
    if is_degreeF:
        settemp += 62
        ctl += 62
        htl += 62
        DEGREE = "℉"
    
    buf = bytesarr[5]
    if ((buf >> 3) & 1) == 0:
        ctl = 0
    if ((buf >> 4) & 1) == 0:
        htl = 0
    fl = 0 if ((buf >> 5) & 1) == 0 else 1
    if ((buf >> 6) & 1) != 0:
        rml = 1
    
    buf = bytesarr[6]
    temp = buf if buf < 128 else buf - 256
    
    ac = {}
    ac['ac_mode'] = mode
    ac['fan'] = fan
    ac['temp'] = temp
    ac['settemp'] = settemp
    ac['err'] = err
    ac['locked'] = 0
    if ml == 1 or fl == 1 or ctl > 0 or htl > 0 or rml == 1:  # 修复了语法错误
        ac['locked'] = 1
    ac['l_rm'] = rml
    ac['l_mode'] = 10 if ml == 0 else locked_mode
    ac['l_wind'] = 10 if fl == 0 else locked_wind
    ac['l_cool_temp'] = ctl
    ac['l_heat_temp'] = htl
    return ac

async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the Midea thermostats."""
    host = config.get(CONF_HOST)
    port = config.get(CONF_PORT)
    name = config.get(CONF_NAME)
    
    try:
        acs = await _async_poll_status(hass, host, port)
        devices = []
        
        for ac_name, ac_state in acs.items():
            devices.append(Thermostat(name, ac_name, host, port, ac_state))
        
        async_add_entities(devices)
    except Exception as e:
        _LOGGER.error(f"Failed to set up CCM15 climate: {e}")

async def _async_poll_status(hass, host, port):
    """Poll the AC status using non-blocking aiohttp client."""
    resource = BASE_URL.format(host, port, CONF_URL_STATUS)
    session = async_get_clientsession(hass)
    
    try:
        async with async_timeout.timeout(10):
            response = await session.get(resource)
            text = await response.text()
            doc = xmltodict.parse(text)
            data = doc['response']
            
            acs = {}
            for ac_name, ac_binary in data.items():
                if len(ac_binary) > 1:
                    ac_state = get_status_from(ac_binary)
                    if ac_state:
                        acs[ac_name] = ac_state
            return acs
    except asyncio.TimeoutError:
        _LOGGER.error("Timeout while connecting to %s", resource)
        return {}
    except Exception as e:
        _LOGGER.error("Error getting status from %s: %s", resource, e)
        return {}

class Thermostat(ClimateEntity):
    """Representation of a Midea thermostat."""

    def __init__(self, name, ac_name, host, port, acdata):
        """Initialize the thermostat."""
        self._name = f'{name}_{ac_name}'
        self._acdata = ''
        self._ac_name = ac_name
        self._ac_id = 2 ** (int(ac_name.strip('a')))
        self._host = host
        self._port = port
        self._current_temp = None
        self._current_settemp = None
        self._current_state = None
        self._current_fan = None
        self._current_setfan = CONST_MODE_FAN_AUTO
        self._hvac_list = SUPPORT_HVAC
        self._fan_list = [
            CONST_MODE_FAN_OFF,
            CONST_MODE_FAN_AUTO,
            CONST_MODE_FAN_LOW,
            CONST_MODE_FAN_MIDDLE,
            CONST_MODE_FAN_HIGH
        ]
        self.updateWithAcdata(acdata)
        _LOGGER.debug("Init called")

    @property
    def supported_features(self):
        """Return the list of supported features."""
        return ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.FAN_MODE

    @property
    def should_poll(self):
        """Polling needed for thermostat."""
        _LOGGER.debug("Should_Poll called")
        return True

    def updateWithAcdata(self, acdata):
        """Update self data with acdata"""
        self._current_temp = acdata['temp']
        self._current_settemp = acdata['settemp']
        self._current_state = CONST_CMD_STATE_MAP[acdata['ac_mode']]
        self._current_fan = CONST_CMD_FAN_MAP[acdata['fan']]
        if self._current_fan != CONST_MODE_FAN_OFF:
            self._current_setfan = self._current_fan

    async def async_update(self):
        """Update the data from the thermostat."""
        try:
            acs = await _async_poll_status(self.hass, self._host, self._port)
            if self._ac_name in acs:
                acdata = acs[self._ac_name]
                if self._acdata == acdata:
                    return
                self._acdata = acdata
                self.updateWithAcdata(acdata)
        except Exception as e:
            _LOGGER.error(f"Error updating thermostat: {e}")

    async def async_set_states(self):
        """Set new target states."""
        state_cmd = CONST_STATE_CMD_MAP[self._current_state]
        fan_cmd = CONST_FAN_CMD_MAP[self._current_fan]
        url = BASE_URL.format(
            self._host,
            self._port,
            CONF_URL_CTRL +
            f'?ac0={self._ac_id}'
            '&ac1=0'
            f'&mode={state_cmd}'
            f'&fan={fan_cmd}'
            f'&temp={self._current_settemp}'
        )
        _LOGGER.info("Set state=%s", url)
        
        session = async_get_clientsession(self.hass)
        try:
            async with async_timeout.timeout(DEFAULT_TIMEOUT):
                response = await session.get(url)
                if response.status != 200:
                    _LOGGER.error("Error with API request: %s", response.status)
                else:
                    _LOGGER.debug("API request ok %d", response.status)
        except Exception as e:
            _LOGGER.error(f"Failed to set states: {e}")

    @property
    def unique_id(self):
        """Return the unique_id of the thermostat."""
        from homeassistant.util import slugify
        return 'ccm15' + slugify(self.name)

    @property
    def name(self):
        """Return the name of the thermostat."""
        return self._name

    @property
    def extra_state_attributes(self):
        """Return the device specific state attributes."""
        return {
            ATTR_MODE: self._current_state
        }

    @property
    def temperature_unit(self):
        """Return the unit of measurement."""
        return UnitOfTemperature.CELSIUS

    @property
    def current_temperature(self):
        """Return the current temperature."""
        return self._current_temp

    @property
    def target_temperature(self):
        """Return the temperature we try to reach."""
        return self._current_settemp

    async def async_set_temperature(self, **kwargs):
        """Set new target temperature."""
        import math
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        else:
            self._current_settemp = int(math.ceil(temperature)) if temperature > self._current_settemp else int(math.floor(temperature))
            await self.async_set_states()

    @property
    def hvac_mode(self):
        """Return the current state of the thermostat."""
        return self._current_state

    async def async_set_hvac_mode(self, hvac_mode):
        """Set hvac mode."""
        if hvac_mode not in CONST_STATE_CMD_MAP:
            hvac_mode = HVACMode.OFF
        if self._current_state != hvac_mode and self._current_fan == CONST_MODE_FAN_OFF:
            self._current_fan = self._current_setfan
        self._current_state = hvac_mode
        await self.async_set_states()

    @property
    def hvac_modes(self):
        """List of available hvac modes."""
        return self._hvac_list

    @property
    def fan_mode(self):
        """Return the fan setting."""
        return self._current_fan

    async def async_set_fan_mode(self, fan):
        """Set new target fan mode."""
        if self._current_state == HVACMode.OFF:
            return
        if fan not in CONST_FAN_CMD_MAP:
            fan = CONST_MODE_FAN_AUTO
        if fan == CONST_MODE_FAN_OFF:
            self._current_state = HVACMode.OFF
        self._current_fan = fan
        if self._current_fan != CONST_MODE_FAN_OFF:
            self._current_setfan = self._current_fan
        await self.async_set_states()

    @property
    def fan_modes(self):
        """Return the list of available fan modes."""
        return self._fan_list
