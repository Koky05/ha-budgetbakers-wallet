"""DataUpdateCoordinator for BudgetBakers Wallet."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
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

CHECKPOINT_FILENAME = "budgetbakers_wallet_checkpoint.json"


# --- Utility functions ---


def _safe_float(value: Any) -> float:
    """Safely convert a value to float, returning 0.0 on failure."""
    try:
        return float(value)
    except (ValueError, TypeError):
        return 0.0


def _get_record_value(record: dict[str, Any]) -> float:
    """Extract the numeric amount from a record safely."""
    amount_data = record.get("amount") or record.get("baseAmount")
    if not amount_data:
        return 0.0
    return _safe_float(amount_data.get("value", 0))


# --- Data model ---


@dataclass
class WalletData:
    """Data class holding all fetched Wallet data."""

    accounts: list[dict[str, Any]] = field(default_factory=list)
    records_current_month: list[dict[str, Any]] = field(default_factory=list)
    records_by_account: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    categories: list[dict[str, Any]] = field(default_factory=list)
    budgets: list[dict[str, Any]] = field(default_factory=list)
    standing_orders: list[dict[str, Any]] = field(default_factory=list)
    last_full_update: datetime | None = None
    categories_map: dict[str, dict[str, Any]] = field(default_factory=dict)
    account_balances: dict[str, float] = field(default_factory=dict)
    account_map: dict[str, dict[str, Any]] = field(default_factory=dict)
    budget_map: dict[str, dict[str, Any]] = field(default_factory=dict)
    standing_order_map: dict[str, dict[str, Any]] = field(default_factory=dict)


# --- Checkpoint persistence ---


class BalanceCheckpoint:
    """Persistent storage for historical balance sums."""

    def __init__(self, storage_path: Path) -> None:
        self._path = storage_path / CHECKPOINT_FILENAME

    def load(self) -> tuple[dict[str, float], str]:
        """Load checkpoint from disk. Returns (balances, month_key)."""
        if not self._path.exists():
            return {}, ""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            balances = {
                k: _safe_float(v)
                for k, v in data.get("balances", {}).items()
            }
            month = data.get("month", "")
            _LOGGER.debug(
                "Loaded balance checkpoint: %d accounts, month=%s",
                len(balances), month,
            )
            return balances, month
        except (json.JSONDecodeError, OSError) as err:
            _LOGGER.warning("Failed to load balance checkpoint: %s", err)
            return {}, ""

    def save(self, balances: dict[str, float], month_key: str) -> None:
        """Save checkpoint to disk. Only stores account IDs and sums."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "month": month_key,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "balances": {k: round(v, 4) for k, v in balances.items()},
            }
            self._path.write_text(
                json.dumps(data, indent=2),
                encoding="utf-8",
            )
            _LOGGER.debug("Saved balance checkpoint: %d accounts", len(balances))
        except OSError as err:
            _LOGGER.warning("Failed to save balance checkpoint: %s", err)

    def delete(self) -> None:
        """Remove checkpoint file."""
        try:
            self._path.unlink(missing_ok=True)
        except OSError:
            pass


# --- Coordinator ---


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
        self._checkpoint = BalanceCheckpoint(
            Path(hass.config.path(".storage"))
        )

    async def _aggregate_history_streaming(
        self,
        accounts: list[dict[str, Any]],
        earliest: datetime,
        month_start: datetime,
    ) -> None:
        """Fetch historical records and aggregate sums per-window (low memory).

        Processes records window by window so peak memory is O(records_per_window)
        instead of O(total_records). Each window's list is freed after aggregation.
        """
        self._historical_balances = {
            acc.get("id", ""): 0.0 for acc in accounts if acc.get("id")
        }

        window_start = earliest
        total_records = 0

        while window_start < month_start:
            window_end = min(
                window_start + timedelta(days=API_MAX_DATE_RANGE_DAYS),
                month_start,
            )
            _LOGGER.debug(
                "Fetching history window %s to %s",
                window_start.isoformat(),
                window_end.isoformat(),
            )
            records = await self.client.async_get_records(
                record_date_gte=window_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                record_date_lt=window_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
            for record in records:
                acc_id = record.get("accountId")
                if acc_id in self._historical_balances:
                    self._historical_balances[acc_id] += _get_record_value(record)
            total_records += len(records)
            window_start = window_end

        _LOGGER.info(
            "History loaded: %d records across %d accounts",
            total_records,
            len(self._historical_balances),
        )

    def _find_earliest_record_date(
        self, accounts: list[dict[str, Any]], fallback: datetime
    ) -> datetime:
        """Find the earliest record date across all accounts."""
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
        return earliest if earliest is not None else fallback

    async def _ensure_historical_balances(
        self,
        accounts: list[dict[str, Any]],
        month_start: datetime,
        current_month_key: str,
    ) -> None:
        """Load or fetch historical balances, using checkpoint when possible."""
        current_acc_ids = {acc.get("id") for acc in accounts if acc.get("id")}
        cached_acc_ids = set(self._historical_balances.keys())

        # Prune removed accounts
        for removed_id in cached_acc_ids - current_acc_ids:
            self._historical_balances.pop(removed_id, None)

        new_accounts = current_acc_ids - cached_acc_ids

        # Try loading from checkpoint on first startup
        if not self._history_loaded:
            saved_balances, saved_month = self._checkpoint.load()
            if saved_month == current_month_key and saved_balances:
                saved_acc_ids = set(saved_balances.keys())
                if current_acc_ids.issubset(saved_acc_ids):
                    self._historical_balances = saved_balances
                    self._history_month = saved_month
                    self._history_loaded = True
                    _LOGGER.info(
                        "Restored balance checkpoint: %d accounts, month=%s",
                        len(saved_balances), saved_month,
                    )
                    return

        # Full re-fetch needed: first load, month change, or new accounts
        if (
            not self._history_loaded
            or self._history_month != current_month_key
            or new_accounts
        ):
            earliest = self._find_earliest_record_date(
                accounts, month_start - timedelta(days=365)
            )
            if earliest < month_start:
                await self._aggregate_history_streaming(
                    accounts, earliest, month_start
                )

            self._history_month = current_month_key
            self._history_loaded = True

            # Persist checkpoint for fast restart
            self._checkpoint.save(
                self._historical_balances, current_month_key
            )

    async def _async_update_data(self) -> WalletData:
        """Fetch data from the Wallet API."""
        try:
            now = datetime.now(timezone.utc)

            # --- Fetch accounts ---
            accounts = await self.client.async_get_accounts()

            # --- Fetch categories (cache for 24h) ---
            if (
                self._categories_last_fetched is None
                or (now - self._categories_last_fetched) > timedelta(hours=24)
                or self.data is None
            ):
                categories = await self.client.async_get_categories()
                self._categories_last_fetched = now
            else:
                categories = self.data.categories

            categories_map = {
                c["id"]: c for c in categories if c.get("id")
            }

            # --- Current month boundaries ---
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

            # --- Fetch current month records ---
            current_month_records = await self.client.async_get_records(
                record_date_gte=month_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                record_date_lt=month_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )

            # --- Ensure historical balances (checkpoint or API fetch) ---
            await self._ensure_historical_balances(
                accounts, month_start, current_month_key
            )

            # --- Pre-index current month records by account (O(1) lookup) ---
            records_by_account: dict[str, list[dict[str, Any]]] = {}
            current_sums: dict[str, float] = {}
            for record in current_month_records:
                acc_id = record.get("accountId")
                if not acc_id:
                    continue
                records_by_account.setdefault(acc_id, []).append(record)
                current_sums[acc_id] = (
                    current_sums.get(acc_id, 0.0) + _get_record_value(record)
                )

            # --- Compute final balances ---
            account_balances: dict[str, float] = {}
            for acc in accounts:
                acc_id = acc.get("id")
                if not acc_id:
                    continue
                balance_data = acc.get("initialBalance")
                if balance_data is None:
                    balance_data = acc.get("initialBaseBalance")
                initial = (
                    _safe_float(balance_data.get("value", 0)) * 100
                ) if balance_data else 0.0
                historical = self._historical_balances.get(acc_id, 0.0)
                current = current_sums.get(acc_id, 0.0)
                account_balances[acc_id] = round(
                    initial + historical + current, 2
                )

            # --- Fetch budgets and standing orders ---
            budgets = await self.client.async_get_budgets()
            standing_orders = await self.client.async_get_standing_orders()

            # --- Build lookup maps for O(1) access in sensors ---
            account_map = {
                acc["id"]: acc for acc in accounts if acc.get("id")
            }
            budget_map = {
                b["id"]: b for b in budgets if b.get("id")
            }
            standing_order_map = {
                o["id"]: o for o in standing_orders if o.get("id")
            }

            return WalletData(
                accounts=accounts,
                records_current_month=current_month_records,
                records_by_account=records_by_account,
                categories=categories,
                budgets=budgets,
                standing_orders=standing_orders,
                categories_map=categories_map,
                account_balances=account_balances,
                account_map=account_map,
                budget_map=budget_map,
                standing_order_map=standing_order_map,
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
            _LOGGER.warning("API rate limit exceeded: %s", err)
            raise UpdateFailed("API rate limit exceeded") from None
        except WalletApiError as err:
            _LOGGER.debug("API error detail: %s", err)
            raise UpdateFailed(
                "Error communicating with Wallet API"
            ) from None
