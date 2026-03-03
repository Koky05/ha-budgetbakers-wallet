"""Config flow for BudgetBakers Wallet integration."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .api import WalletApiClient, WalletAuthError, WalletApiError
from .const import (
    CONF_API_TOKEN,
    CONF_INVESTMENT_ENTITY,
    CONF_MONITORED_ACCOUNTS,
    CONF_TRANSACTIONS_COUNT,
    CONF_UPDATE_INTERVAL,
    DEFAULT_TRANSACTIONS_COUNT,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    MAX_TRANSACTIONS_COUNT,
    MIN_TRANSACTIONS_COUNT,
    MIN_UPDATE_INTERVAL,
    MAX_UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

TOKEN_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_API_TOKEN): vol.All(str, vol.Length(min=10)),
    }
)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for BudgetBakers Wallet."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        super().__init__()
        self._token: str = ""
        self._accounts: list[dict[str, Any]] = []

    async def _fetch_accounts(self, token: str) -> list[dict[str, Any]]:
        """Fetch accounts using the provided token."""
        session = async_get_clientsession(self.hass)
        client = WalletApiClient(session, token)
        return await client.async_get_accounts()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Step 1: API token input and validation."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._token = user_input[CONF_API_TOKEN].strip()

            try:
                self._accounts = await self._fetch_accounts(self._token)
            except WalletAuthError:
                errors["base"] = "invalid_auth"
            except (WalletApiError, aiohttp.ClientError):
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception during setup")
                errors["base"] = "unknown"

            if not errors:
                return await self.async_step_accounts()

        return self.async_show_form(
            step_id="user",
            data_schema=TOKEN_SCHEMA,
            errors=errors,
        )

    async def async_step_accounts(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Step 2: Select which accounts to monitor."""
        if user_input is not None:
            await self.async_set_unique_id("budgetbakers_wallet")
            self._abort_if_unique_id_configured()

            selected = user_input.get(CONF_MONITORED_ACCOUNTS, [])

            return self.async_create_entry(
                title="BudgetBakers Wallet",
                data={CONF_API_TOKEN: self._token},
                options={
                    CONF_UPDATE_INTERVAL: DEFAULT_UPDATE_INTERVAL,
                    CONF_MONITORED_ACCOUNTS: selected,
                    CONF_TRANSACTIONS_COUNT: DEFAULT_TRANSACTIONS_COUNT,
                    CONF_INVESTMENT_ENTITY: "",
                },
            )

        active_accounts = [
            a for a in self._accounts if not a.get("archived", False)
        ]

        if not active_accounts:
            return self.async_abort(reason="no_accounts")

        account_options = [
            SelectOptionDict(
                value=acc["id"],
                label=f"{acc.get('name', 'Unknown')} ({acc.get('accountType', '')})",
            )
            for acc in active_accounts
        ]

        default_ids = [acc["id"] for acc in active_accounts]

        accounts_schema = vol.Schema(
            {
                vol.Required(
                    CONF_MONITORED_ACCOUNTS,
                    default=default_ids,
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=account_options,
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="accounts",
            data_schema=accounts_schema,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> config_entries.ConfigFlowResult:
        """Handle reauthorization when token expires."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle reauth token input."""
        errors: dict[str, str] = {}

        if user_input is not None:
            token = user_input[CONF_API_TOKEN].strip()

            try:
                await self._fetch_accounts(token)
            except WalletAuthError:
                errors["base"] = "invalid_auth"
            except (WalletApiError, aiohttp.ClientError):
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception during reauth")
                errors["base"] = "unknown"

            if not errors:
                reauth_entry = self._get_reauth_entry()
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data={CONF_API_TOKEN: token},
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=TOKEN_SCHEMA,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Get the options flow handler."""
        return WalletOptionsFlowHandler()


class WalletOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options for BudgetBakers Wallet."""

    def __init__(self) -> None:
        """Initialize the options flow."""
        super().__init__()
        self._accounts: list[dict[str, Any]] = []
        self._update_interval: int = DEFAULT_UPDATE_INTERVAL
        self._transactions_count: int = DEFAULT_TRANSACTIONS_COUNT
        self._investment_entity: str = ""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Step 1: Update interval, transaction count, then account selection."""
        if user_input is not None:
            self._update_interval = user_input[CONF_UPDATE_INTERVAL]
            self._transactions_count = user_input[CONF_TRANSACTIONS_COUNT]
            self._investment_entity = user_input.get(CONF_INVESTMENT_ENTITY, "")
            # Fetch accounts for the next step
            token = self.config_entry.data[CONF_API_TOKEN]
            session = async_get_clientsession(self.hass)
            try:
                client = WalletApiClient(session, token)
                self._accounts = await client.async_get_accounts()
            except Exception:
                _LOGGER.exception("Failed to fetch accounts for options")
                self._accounts = []

            return await self.async_step_accounts()

        current_interval = self.config_entry.options.get(
            CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL
        )
        current_tx_count = self.config_entry.options.get(
            CONF_TRANSACTIONS_COUNT, DEFAULT_TRANSACTIONS_COUNT
        )
        current_investment_entity = self.config_entry.options.get(
            CONF_INVESTMENT_ENTITY, ""
        )

        options_schema = vol.Schema(
            {
                vol.Required(
                    CONF_UPDATE_INTERVAL,
                    default=current_interval,
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_UPDATE_INTERVAL, max=MAX_UPDATE_INTERVAL),
                ),
                vol.Required(
                    CONF_TRANSACTIONS_COUNT,
                    default=current_tx_count,
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_TRANSACTIONS_COUNT, max=MAX_TRANSACTIONS_COUNT),
                ),
                vol.Optional(
                    CONF_INVESTMENT_ENTITY,
                    default=current_investment_entity,
                ): EntitySelector(
                    EntitySelectorConfig(domain="sensor")
                ),
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=options_schema,
        )

    async def async_step_accounts(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Step 2: Reconfigure monitored accounts."""
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    CONF_UPDATE_INTERVAL: self._update_interval,
                    CONF_TRANSACTIONS_COUNT: self._transactions_count,
                    CONF_INVESTMENT_ENTITY: self._investment_entity,
                    CONF_MONITORED_ACCOUNTS: user_input.get(
                        CONF_MONITORED_ACCOUNTS, []
                    ),
                },
            )

        active_accounts = [
            a for a in self._accounts if not a.get("archived", False)
        ]

        current_monitored = self.config_entry.options.get(
            CONF_MONITORED_ACCOUNTS, [acc["id"] for acc in active_accounts]
        )

        account_options = [
            SelectOptionDict(
                value=acc["id"],
                label=f"{acc.get('name', 'Unknown')} ({acc.get('accountType', '')})",
            )
            for acc in active_accounts
        ]

        if not account_options:
            return self.async_create_entry(
                title="",
                data={
                    CONF_UPDATE_INTERVAL: self._update_interval,
                    CONF_TRANSACTIONS_COUNT: self._transactions_count,
                    CONF_INVESTMENT_ENTITY: self._investment_entity,
                    CONF_MONITORED_ACCOUNTS: [],
                },
            )

        accounts_schema = vol.Schema(
            {
                vol.Required(
                    CONF_MONITORED_ACCOUNTS,
                    default=current_monitored,
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=account_options,
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="accounts",
            data_schema=accounts_schema,
        )
