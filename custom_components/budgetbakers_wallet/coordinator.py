"""DataUpdateCoordinator for BudgetBakers Wallet."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    WalletApiClient,
    WalletAuthError,
    WalletApiError,
    WalletRateLimitError,
    WalletSyncError,
)
from .const import API_MAX_DATE_RANGE_DAYS, DOMAIN

_LOGGER = logging.getLogger(__name__)


@dataclass
class WalletData:
    """Data class holding all fetched Wallet data."""

    accounts: list[dict[str, Any]] = field(default_factory=list)
    records_current_month: list[dict[str, Any]] = field(default_factory=list)
    categories: list[dict[str, Any]] = field(default_factory=list)
    budgets: list[dict[str, Any]] = field(default_factory=list)
    standing_orders: list[dict[str, Any]] = field(default_factory=list)
    last_full_update: datetime | None = None
    categories_map: dict[str, dict[str, Any]] = field(default_factory=dict)
    account_balances: dict[str, float] = field(default_factory=dict)


class WalletCoordinator(DataUpdateCoordinator[WalletData]):
    """Coordinator to fetch data from the BudgetBakers Wallet API."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: WalletApiClient,
        update_interval_minutes: int,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=update_interval_minutes),
        )
        self.client = client
        self._categories_last_fetched: datetime | None = None
        self._historical_balances: dict[str, float] = {}
        self._history_month: str = ""
        self._history_loaded: bool = False

    async def _fetch_all_records_in_range(
        self, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        """Fetch all records in a date range, splitting into safe windows."""
        all_records: list[dict[str, Any]] = []
        window_start = start

        while window_start < end:
            window_end = min(
                window_start + timedelta(days=API_MAX_DATE_RANGE_DAYS),
                end,
            )
            _LOGGER.debug(
                "Fetching records from %s to %s",
                window_start.isoformat(),
                window_end.isoformat(),
            )
            records = await self.client.async_get_records(
                record_date_gte=window_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                record_date_lt=window_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
            all_records.extend(records)
            window_start = window_end

        return all_records

    async def _async_update_data(self) -> WalletData:
        """Fetch data from the Wallet API."""
        try:
            now = datetime.now(timezone.utc)

            # Always fetch accounts
            accounts = await self.client.async_get_accounts()

            # Fetch categories (cache for 24h)
            if (
                self._categories_last_fetched is None
                or (now - self._categories_last_fetched) > timedelta(hours=24)
                or self.data is None
            ):
                categories = await self.client.async_get_categories()
                self._categories_last_fetched = now
            else:
                categories = self.data.categories

            categories_map = {c["id"]: c for c in categories if "id" in c}

            # Current month boundaries
            month_start = now.replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            )
            if month_start.month == 12:
                month_end = month_start.replace(
                    year=month_start.year + 1, month=1
                )
            else:
                month_end = month_start.replace(month=month_start.month + 1)

            current_month_key = month_start.strftime("%Y-%m")

            # Fetch current month records (always)
            current_month_records = await self.client.async_get_records(
                record_date_gte=month_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                record_date_lt=month_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )

            # Detect new accounts that need history fetch
            current_acc_ids = {acc["id"] for acc in accounts}
            cached_acc_ids = set(self._historical_balances.keys())
            new_accounts = current_acc_ids - cached_acc_ids

            # Fetch full history on first load, month change, or new accounts
            if (
                not self._history_loaded
                or self._history_month != current_month_key
                or new_accounts
            ):
                _LOGGER.info(
                    "Loading full transaction history for balance computation"
                )
                # Find earliest record date across all accounts
                earliest = None
                for acc in accounts:
                    stats = acc.get("recordStats")
                    if stats and stats.get("recordDate"):
                        min_date = stats["recordDate"].get("min")
                        if min_date:
                            try:
                                dt = datetime.fromisoformat(
                                    min_date.replace("Z", "+00:00")
                                )
                                if earliest is None or dt < earliest:
                                    earliest = dt
                            except (ValueError, TypeError):
                                pass

                if earliest is None:
                    earliest = month_start - timedelta(days=365)

                # Fetch all records from earliest to start of current month
                historical_records: list[dict[str, Any]] = []
                if earliest < month_start:
                    historical_records = await self._fetch_all_records_in_range(
                        earliest, month_start
                    )

                # Cache the historical per-account sums
                self._historical_balances = {}
                for acc in accounts:
                    self._historical_balances[acc["id"]] = 0.0

                for record in historical_records:
                    acc_id = record.get("accountId")
                    if acc_id not in self._historical_balances:
                        continue
                    amount_data = record.get("amount") or record.get("baseAmount")
                    if amount_data:
                        self._historical_balances[acc_id] += amount_data.get("value", 0.0)

                self._history_month = current_month_key
                self._history_loaded = True
                _LOGGER.info(
                    "History loaded: %d historical records across %d accounts",
                    len(historical_records),
                    len(self._historical_balances),
                )

            # Pre-aggregate current month sums by account (O(n) instead of O(n*m))
            current_sums: dict[str, float] = {}
            for record in current_month_records:
                acc_id = record.get("accountId")
                if not acc_id:
                    continue
                amount_data = record.get("amount") or record.get("baseAmount")
                if amount_data:
                    current_sums[acc_id] = (
                        current_sums.get(acc_id, 0.0) + amount_data.get("value", 0.0)
                    )

            # Compute final balances: initialBalance*100 + historical + currentMonth
            account_balances: dict[str, float] = {}
            for acc in accounts:
                acc_id = acc["id"]
                balance_data = acc.get("initialBalance")
                if balance_data is None:
                    balance_data = acc.get("initialBaseBalance")
                # API returns initialBalance divided by 100
                initial = (balance_data.get("value", 0.0) * 100) if balance_data else 0.0
                historical = self._historical_balances.get(acc_id, 0.0)
                current = current_sums.get(acc_id, 0.0)

                account_balances[acc_id] = round(
                    initial + historical + current, 2
                )

            # Fetch budgets and standing orders
            budgets = await self.client.async_get_budgets()
            standing_orders = await self.client.async_get_standing_orders()

            return WalletData(
                accounts=accounts,
                records_current_month=current_month_records,
                categories=categories,
                budgets=budgets,
                standing_orders=standing_orders,
                categories_map=categories_map,
                account_balances=account_balances,
                last_full_update=now,
            )

        except WalletAuthError as err:
            raise ConfigEntryAuthFailed(
                "Invalid or expired API token"
            ) from err
        except WalletSyncError as err:
            _LOGGER.debug("Wallet sync detail: %s", err)
            raise UpdateFailed("Wallet sync in progress") from None
        except WalletRateLimitError as err:
            _LOGGER.debug("Rate limit detail: %s", err)
            raise UpdateFailed("API rate limit exceeded") from None
        except WalletApiError as err:
            _LOGGER.debug("API error detail: %s", err)
            raise UpdateFailed(
                "Error communicating with Wallet API"
            ) from None
