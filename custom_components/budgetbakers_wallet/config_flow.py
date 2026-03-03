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
    CONF_INVESTMENT_ENTITIES,
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


def _build_investment_label(acc: dict[str, Any], index: int) -> str:
    """Build a unique label for an investment account field."""
    name = acc.get("name", "Investment")
    acc_type = acc.get("accountType", "")
    bank_num = acc.get("bankAccountNumber", "")
    # Ensure uniqueness by appending index if needed
    suffix = f" ({bank_num})" if bank_num else f" #{index + 1}"
    return f"{name}{suffix}"


def _validate_investment_entity(
    hass: Any, entity_id: str
) -> bool:
    """Validate that an investment entity exists and has numeric state."""
    if not entity_id:
        return False
    state = hass.states.get(entity_id)
    if state and state.state not in ("unknown", "unavailable"):
        try:
            float(state.state)
            return True
        except (ValueError, TypeError):
            _LOGGER.warning(
                "Investment entity %s has non-numeric state '%s', skipping",
                entity_id,
                state.state,
            )
            return False
    # Entity might not be ready yet — accept it
    return True


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for BudgetBakers Wallet."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        super().__init__()
        self._token: str = ""
        self._accounts: list[dict[str, Any]] = []
        self._selected_accounts: list[str] = []
        self._investment_entities: dict[str, str] = {}

    async def _fetch_accounts(self, token: str) -> list[dict[str, Any]]:
        """Fetch accounts using the provided token."""
        session = async_get_clientsession(self.hass)
        client = WalletApiClient(session, token)
        return await client.async_get_accounts()

    def _get_investment_accounts(
        self, selected_ids: list[str]
    ) -> list[dict[str, Any]]:
        """Return Investment-type accounts from the selected list."""
        return [
            a
            for a in self._accounts
            if a.get("id") in selected_ids
            and a.get("accountType") == "Investment"
        ]

    # --- Step 1: Token ---

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
            description_placeholders={
                "token_url": "https://web.budgetbakers.com/settings/apiTokens"
            },
        )

    # --- Step 2: Account selection ---

    async def async_step_accounts(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Step 2: Select which accounts to monitor."""
        if user_input is not None:
            self._selected_accounts = user_input.get(CONF_MONITORED_ACCOUNTS, [])

            investment_accounts = self._get_investment_accounts(
                self._selected_accounts
            )
            if investment_accounts:
                return await self.async_step_investments()
            return await self.async_step_settings()

        active_accounts = [
            a for a in self._accounts if not a.get("archived", False)
        ]

        if not active_accounts:
            return self.async_abort(reason="no_accounts")

        account_options = [
            SelectOptionDict(
                value=acc["id"],
                label=self._format_account_label(acc),
            )
            for acc in active_accounts
        ]

        default_ids = [acc["id"] for acc in active_accounts]

        return self.async_show_form(
            step_id="accounts",
            data_schema=vol.Schema(
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
            ),
        )

    # --- Step 3: Investment entity linking ---

    async def async_step_investments(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Step 3: Link investment accounts to HA sensors."""
        investment_accounts = self._get_investment_accounts(
            self._selected_accounts
        )

        # Build unique label→id mapping (handles name collisions)
        label_to_id: dict[str, str] = {}
        for idx, acc in enumerate(investment_accounts):
            label = _build_investment_label(acc, idx)
            label_to_id[label] = acc["id"]

        if user_input is not None:
            investment_entities: dict[str, str] = {}
            for label, acc_id in label_to_id.items():
                entity = user_input.get(label, "")
                if entity and _validate_investment_entity(self.hass, entity):
                    investment_entities[acc_id] = entity

            self._investment_entities = investment_entities
            return await self.async_step_settings()

        # Build schema — use descriptive label as field key
        schema_fields: dict[Any, Any] = {}
        for label in label_to_id:
            schema_fields[
                vol.Optional(label, default="")
            ] = EntitySelector(EntitySelectorConfig(domain="sensor"))

        return self.async_show_form(
            step_id="investments",
            data_schema=vol.Schema(schema_fields),
        )

    # --- Step 4: Settings ---

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Step 4: Configure update interval and transaction count."""
        if user_input is not None:
            await self.async_set_unique_id("budgetbakers_wallet")
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title="BudgetBakers Wallet",
                data={CONF_API_TOKEN: self._token},
                options={
                    CONF_UPDATE_INTERVAL: user_input[CONF_UPDATE_INTERVAL],
                    CONF_TRANSACTIONS_COUNT: user_input[CONF_TRANSACTIONS_COUNT],
                    CONF_MONITORED_ACCOUNTS: self._selected_accounts,
                    CONF_INVESTMENT_ENTITIES: self._investment_entities,
                },
            )

        return self.async_show_form(
            step_id="settings",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_UPDATE_INTERVAL,
                        default=DEFAULT_UPDATE_INTERVAL,
                    ): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=MIN_UPDATE_INTERVAL, max=MAX_UPDATE_INTERVAL),
                    ),
                    vol.Required(
                        CONF_TRANSACTIONS_COUNT,
                        default=DEFAULT_TRANSACTIONS_COUNT,
                    ): vol.All(
                        vol.Coerce(int),
                        vol.Range(
                            min=MIN_TRANSACTIONS_COUNT,
                            max=MAX_TRANSACTIONS_COUNT,
                        ),
                    ),
                }
            ),
        )

    # --- Reauth ---

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
            description_placeholders={
                "token_url": "https://web.budgetbakers.com/settings/apiTokens"
            },
        )

    # --- Options flow ---

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Get the options flow handler."""
        return WalletOptionsFlowHandler()

    # --- Helpers ---

    @staticmethod
    def _format_account_label(acc: dict[str, Any]) -> str:
        """Format an account for display in the selector."""
        name = acc.get("name", "Unknown")
        acc_type = acc.get("accountType", "")
        return f"{name} ({acc_type})"


class WalletOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options for BudgetBakers Wallet."""

    def __init__(self) -> None:
        """Initialize the options flow."""
        super().__init__()
        self._accounts: list[dict[str, Any]] = []
        self._selected_accounts: list[str] = []
        self._update_interval: int = DEFAULT_UPDATE_INTERVAL
        self._transactions_count: int = DEFAULT_TRANSACTIONS_COUNT

    # --- Step 1: Settings ---

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Step 1: Update interval and transaction count."""
        if user_input is not None:
            self._update_interval = user_input[CONF_UPDATE_INTERVAL]
            self._transactions_count = user_input[CONF_TRANSACTIONS_COUNT]

            # Fetch accounts for next steps
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

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
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
                        vol.Range(
                            min=MIN_TRANSACTIONS_COUNT,
                            max=MAX_TRANSACTIONS_COUNT,
                        ),
                    ),
                }
            ),
        )

    # --- Step 2: Account selection ---

    async def async_step_accounts(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Step 2: Reconfigure monitored accounts."""
        if user_input is not None:
            self._selected_accounts = user_input.get(
                CONF_MONITORED_ACCOUNTS, []
            )

            # Check for Investment accounts
            investment_accounts = [
                a
                for a in self._accounts
                if a.get("id") in self._selected_accounts
                and a.get("accountType") == "Investment"
            ]
            if investment_accounts:
                return await self.async_step_investments()
            return self._create_options_entry({})

        active_accounts = [
            a for a in self._accounts if not a.get("archived", False)
        ]

        current_monitored = self.config_entry.options.get(
            CONF_MONITORED_ACCOUNTS,
            [acc["id"] for acc in active_accounts],
        )

        if not active_accounts:
            return self._create_options_entry({})

        account_options = [
            SelectOptionDict(
                value=acc["id"],
                label=f"{acc.get('name', 'Unknown')} ({acc.get('accountType', '')})",
            )
            for acc in active_accounts
        ]

        return self.async_show_form(
            step_id="accounts",
            data_schema=vol.Schema(
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
            ),
        )

    # --- Step 3: Investment entity linking ---

    async def async_step_investments(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Step 3: Link investment accounts to HA sensors."""
        investment_accounts = [
            a
            for a in self._accounts
            if a.get("id") in self._selected_accounts
            and a.get("accountType") == "Investment"
        ]

        # Build unique label→id mapping
        label_to_id: dict[str, str] = {}
        for idx, acc in enumerate(investment_accounts):
            label = _build_investment_label(acc, idx)
            label_to_id[label] = acc["id"]

        if user_input is not None:
            investment_entities: dict[str, str] = {}
            for label, acc_id in label_to_id.items():
                entity = user_input.get(label, "")
                if entity and _validate_investment_entity(self.hass, entity):
                    investment_entities[acc_id] = entity

            return self._create_options_entry(investment_entities)

        # Build schema with current values
        current_entities = self.config_entry.options.get(
            CONF_INVESTMENT_ENTITIES, {}
        )

        schema_fields: dict[Any, Any] = {}
        for label, acc_id in label_to_id.items():
            current = current_entities.get(acc_id, "")
            schema_fields[
                vol.Optional(label, default=current)
            ] = EntitySelector(EntitySelectorConfig(domain="sensor"))

        return self.async_show_form(
            step_id="investments",
            data_schema=vol.Schema(schema_fields),
        )

    # --- Helpers ---

    def _create_options_entry(
        self, investment_entities: dict[str, str]
    ) -> config_entries.ConfigFlowResult:
        """Create the options entry with all collected data."""
        return self.async_create_entry(
            title="",
            data={
                CONF_UPDATE_INTERVAL: self._update_interval,
                CONF_TRANSACTIONS_COUNT: self._transactions_count,
                CONF_MONITORED_ACCOUNTS: self._selected_accounts,
                CONF_INVESTMENT_ENTITIES: investment_entities,
            },
        )
