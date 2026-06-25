"""BudgetBakers Wallet integration for Home Assistant."""

from __future__ import annotations

import hashlib
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import WalletApiClient
from .const import (
    CONF_API_TOKEN,
    CONF_INVESTMENT_ENTITIES,
    CONF_MONITORED_ACCOUNTS,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    PLATFORMS,
)
from .coordinator import WalletCoordinator

_LOGGER = logging.getLogger(__name__)

WalletConfigEntry = ConfigEntry[WalletCoordinator]


def _token_unique_id(token: str) -> str:
    """Return a stable non-secret identifier for this API token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


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
    if entry.unique_id == "budgetbakers_wallet":
        hass.config_entries.async_update_entry(
            entry, unique_id=_token_unique_id(token)
        )

    update_interval = entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
    monitored_accounts = entry.options.get(CONF_MONITORED_ACCOUNTS, [])

    session = async_get_clientsession(hass)
    client = WalletApiClient(session, token)

    coordinator = WalletCoordinator(
        hass=hass,
        client=client,
        entry_id=entry.entry_id,
        update_interval_minutes=update_interval,
        monitored_account_ids=monitored_accounts,
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
