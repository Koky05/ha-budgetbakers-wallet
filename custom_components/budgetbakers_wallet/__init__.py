"""BudgetBakers Wallet integration for Home Assistant."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import WalletApiClient
from .const import (
    CONF_API_TOKEN,
    CONF_INVESTMENT_ENTITIES,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    PLATFORMS,
)
from .coordinator import WalletCoordinator

_LOGGER = logging.getLogger(__name__)

WalletConfigEntry = ConfigEntry[WalletCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: WalletConfigEntry) -> bool:
    """Set up BudgetBakers Wallet from a config entry."""
    # Migrate old investment_entity (string) to investment_entities (dict)
    investment_data = entry.options.get(CONF_INVESTMENT_ENTITIES)
    if isinstance(investment_data, str):
        _LOGGER.info("Migrating investment_entity from string to dict format")
        new_options = dict(entry.options)
        new_options[CONF_INVESTMENT_ENTITIES] = {}
        hass.config_entries.async_update_entry(entry, options=new_options)

    token = entry.data[CONF_API_TOKEN]
    update_interval = entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)

    session = async_get_clientsession(hass)
    client = WalletApiClient(session, token)

    coordinator = WalletCoordinator(
        hass=hass,
        client=client,
        update_interval_minutes=update_interval,
    )

    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: WalletConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_options_updated(
    hass: HomeAssistant, entry: WalletConfigEntry
) -> None:
    """Handle options update — reload the integration."""
    await hass.config_entries.async_reload(entry.entry_id)
