"""Sensor platform for BudgetBakers Wallet."""

from __future__ import annotations

import heapq
import html
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import WalletConfigEntry
from .const import (
    ATTR_ACCOUNT_TYPE,
    ATTR_BANK_ACCOUNT_NUMBER,
    ATTR_RECORD_COUNT,
    CONF_INVESTMENT_ENTITY,
    CONF_MONITORED_ACCOUNTS,
    CONF_TRANSACTIONS_COUNT,
    DEFAULT_TRANSACTIONS_COUNT,
    DOMAIN,
)
from .coordinator import WalletCoordinator, WalletData

_LOGGER = logging.getLogger(__name__)

ACCOUNT_TYPE_ICONS = {
    "CurrentAccount": "mdi:bank",
    "Cash": "mdi:cash",
    "CreditCard": "mdi:credit-card",
    "SavingAccount": "mdi:piggy-bank",
    "Investment": "mdi:chart-line",
    "Loan": "mdi:hand-coin",
    "Mortgage": "mdi:home-city",
    "Insurance": "mdi:shield-check",
    "General": "mdi:wallet",
    "Bonus": "mdi:gift",
    "Overdraft": "mdi:bank-minus",
}


def _mask_bank_number(bank_num: str) -> str:
    """Mask a bank account number, showing only last 4 characters."""
    if len(bank_num) > 4:
        return "****" + bank_num[-4:]
    return "****"


def _format_transaction(record: dict[str, Any]) -> dict[str, Any]:
    """Format a record into a transaction dict for attributes."""
    amount_data = record.get("baseAmount") or record.get("amount")
    cat = record.get("category")
    tx: dict[str, Any] = {
        "date": record.get("recordDate", "")[:10],
        "type": record.get("recordType", ""),
        "amount": round(amount_data.get("value", 0.0), 2) if amount_data else 0,
        "currency": amount_data.get("currencyCode", "EUR") if amount_data else "EUR",
    }
    if record.get("recordType") == "expense":
        payee = record.get("payee")
        if payee:
            tx["payee"] = html.escape(payee[:100])
    else:
        payer = record.get("payer")
        if payer:
            tx["payer"] = html.escape(payer[:100])
    if cat and cat.get("name"):
        tx["category"] = cat["name"]
    note = record.get("note")
    if note:
        tx["note"] = html.escape(note[:100])
    return tx


def _compute_budget_spent(
    budget: dict[str, Any],
    records: list[dict[str, Any]],
) -> float:
    """Compute total spending for a budget's categories/accounts."""
    category_ids = set(budget.get("categoryIds", []))
    account_ids = set(budget.get("accountIds", []))
    spent = 0.0
    for record in records:
        if record.get("recordType") != "expense":
            continue
        if category_ids:
            cat = record.get("category")
            if not cat or cat.get("id") not in category_ids:
                continue
        if account_ids:
            if record.get("accountId") not in account_ids:
                continue
        amount_data = record.get("baseAmount") or record.get("amount")
        if amount_data:
            spent += abs(amount_data.get("value", 0.0))
    return spent


def _wallet_device_info(entry_id: str) -> DeviceInfo:
    """Return shared DeviceInfo for the Wallet service."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
        name="BudgetBakers Wallet",
        manufacturer="BudgetBakers",
        configuration_url="https://web.budgetbakers.com/settings/apiTokens",
        entry_type=DeviceEntryType.SERVICE,
    )


def _account_device_info(entry_id: str, account: dict[str, Any]) -> DeviceInfo:
    """Return DeviceInfo for a specific account."""
    account_name = account.get("name", "Unknown")
    account_type = account.get("accountType", "General")
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry_id}_{account['id']}")},
        name=f"Wallet: {account_name}",
        manufacturer="BudgetBakers",
        model=account_type,
        via_device=(DOMAIN, entry_id),
        configuration_url="https://web.budgetbakers.com",
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WalletConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Wallet sensors from a config entry."""
    coordinator = entry.runtime_data
    entry_id = entry.entry_id

    entities: list[SensorEntity] = []

    monitored_ids = set(
        entry.options.get(CONF_MONITORED_ACCOUNTS, [])
    )
    tx_count = entry.options.get(CONF_TRANSACTIONS_COUNT, DEFAULT_TRANSACTIONS_COUNT)
    investment_entity = entry.options.get(CONF_INVESTMENT_ENTITY, "")

    if coordinator.data:
        for account in coordinator.data.accounts:
            if account.get("archived", False):
                continue
            if monitored_ids and account["id"] not in monitored_ids:
                continue
            inv_entity = investment_entity if account.get("accountType") == "Investment" else ""
            entities.append(
                WalletAccountBalanceSensor(coordinator, account, entry_id, tx_count, inv_entity)
            )

        entities.append(WalletMonthlySpendingSensor(coordinator, entry_id))
        entities.append(WalletRecentTransactionsSensor(coordinator, entry_id))

        for budget in coordinator.data.budgets:
            entities.append(WalletBudgetProgressSensor(coordinator, budget, entry_id))

        for order in coordinator.data.standing_orders:
            order_acc_id = order.get("accountId")
            if monitored_ids and order_acc_id and order_acc_id not in monitored_ids:
                continue
            account = next(
                (a for a in coordinator.data.accounts if a["id"] == order_acc_id),
                None,
            )
            entities.append(
                WalletStandingOrderSensor(coordinator, order, entry_id, account)
            )

    async_add_entities(entities)


class WalletAccountBalanceSensor(CoordinatorEntity[WalletCoordinator], SensorEntity):
    """Sensor showing account balance."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(
        self,
        coordinator: WalletCoordinator,
        account: dict[str, Any],
        entry_id: str,
        transactions_count: int = 10,
        investment_entity: str = "",
    ) -> None:
        """Initialize the account balance sensor."""
        super().__init__(coordinator)
        self._account_id = account["id"]
        self._transactions_count = transactions_count
        self._investment_entity = investment_entity
        self._attr_unique_id = f"wallet_{self._account_id}_balance"
        self._attr_name = "Balance"
        self._attr_device_info = _account_device_info(entry_id, account)

        balance_data = account.get("initialBaseBalance") or account.get("initialBalance")
        if balance_data:
            self._attr_native_unit_of_measurement = balance_data.get("currencyCode", "EUR")
        else:
            self._attr_native_unit_of_measurement = "EUR"

        account_type = account.get("accountType", "General")
        self._attr_icon = ACCOUNT_TYPE_ICONS.get(account_type, "mdi:wallet")
        self._is_investment = account_type == "Investment"

    @property
    def _account(self) -> dict[str, Any] | None:
        """Find current account in coordinator data."""
        if not self.coordinator.data:
            return None
        for acc in self.coordinator.data.accounts:
            if acc["id"] == self._account_id:
                return acc
        return None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        """Return the account balance."""
        if self._is_investment and self._investment_entity:
            state = self.hass.states.get(self._investment_entity)
            if state and state.state not in ("unknown", "unavailable"):
                try:
                    return round(float(state.state), 2)
                except (ValueError, TypeError):
                    pass

        if not self.coordinator.data:
            return None
        return self.coordinator.data.account_balances.get(self._account_id)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes including recent transactions."""
        account = self._account
        if not account:
            return {}

        attrs: dict[str, Any] = {
            ATTR_ACCOUNT_TYPE: account.get("accountType"),
        }
        if self._is_investment:
            if self._investment_entity:
                attrs["source"] = self._investment_entity
                attrs["note"] = "Balance from linked investment sensor (live market value)"
            else:
                attrs["approximate"] = True
                attrs["note"] = "Investment balance excludes market value changes (API limitation)"

        bank_num = account.get("bankAccountNumber")
        if bank_num:
            attrs[ATTR_BANK_ACCOUNT_NUMBER] = _mask_bank_number(bank_num)

        stats = account.get("recordStats")
        if stats:
            attrs[ATTR_RECORD_COUNT] = stats.get("recordCount", 0)

        if self._transactions_count > 0 and self.coordinator.data:
            account_records = self.coordinator.data.records_by_account.get(
                self._account_id, []
            )
            sorted_records = heapq.nlargest(
                self._transactions_count,
                account_records,
                key=lambda r: r.get("recordDate", ""),
            )

            attrs["transactions"] = [_format_transaction(r) for r in sorted_records]
            attrs["transactions_this_month"] = len(account_records)

        return attrs

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.coordinator.last_update_success and self._account is not None


class WalletMonthlySpendingSensor(CoordinatorEntity[WalletCoordinator], SensorEntity):
    """Sensor showing total monthly spending."""

    _attr_has_entity_name = True
    _attr_name = "Monthly Spending"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "EUR"
    _attr_icon = "mdi:chart-pie"

    def __init__(self, coordinator: WalletCoordinator, entry_id: str) -> None:
        """Initialize the monthly spending sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"wallet_{entry_id}_monthly_spending"
        self._attr_device_info = _wallet_device_info(entry_id)

    @property
    def native_value(self) -> float | None:
        """Return total spending this month."""
        if not self.coordinator.data:
            return None

        total = 0.0
        for record in self.coordinator.data.records_current_month:
            if record.get("recordType") != "expense":
                continue
            amount_data = record.get("baseAmount") or record.get("amount")
            if amount_data:
                total += abs(amount_data.get("value", 0.0))

        return round(total, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return spending breakdown by category."""
        if not self.coordinator.data:
            return {}

        by_category: dict[str, float] = {}

        for record in self.coordinator.data.records_current_month:
            if record.get("recordType") != "expense":
                continue
            amount_data = record.get("baseAmount") or record.get("amount")
            if not amount_data:
                continue
            value = abs(amount_data.get("value", 0.0))

            cat = record.get("category")
            cat_name = (cat.get("name") if cat else None) or "Uncategorized"

            by_category[cat_name] = round(
                by_category.get(cat_name, 0.0) + value, 2
            )

        sorted_cats = dict(
            sorted(by_category.items(), key=lambda x: x[1], reverse=True)
        )

        return {
            "categories": sorted_cats,
            "transaction_count": sum(
                1
                for r in self.coordinator.data.records_current_month
                if r.get("recordType") == "expense"
            ),
        }


class WalletRecentTransactionsSensor(CoordinatorEntity[WalletCoordinator], SensorEntity):
    """Sensor showing recent transactions."""

    _attr_has_entity_name = True
    _attr_name = "Recent Transactions"
    _attr_icon = "mdi:format-list-bulleted"

    def __init__(self, coordinator: WalletCoordinator, entry_id: str) -> None:
        """Initialize the recent transactions sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"wallet_{entry_id}_recent_transactions"
        self._attr_device_info = _wallet_device_info(entry_id)

    @property
    def native_value(self) -> int | None:
        """Return the count of transactions this month."""
        if not self.coordinator.data:
            return None
        return len(self.coordinator.data.records_current_month)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return last 10 transactions as attributes."""
        if not self.coordinator.data:
            return {}

        sorted_records = heapq.nlargest(
            10,
            self.coordinator.data.records_current_month,
            key=lambda r: r.get("recordDate", ""),
        )

        return {"transactions": [_format_transaction(r) for r in sorted_records]}


class WalletBudgetProgressSensor(CoordinatorEntity[WalletCoordinator], SensorEntity):
    """Sensor showing budget progress as percentage."""

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "%"
    _attr_icon = "mdi:target"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: WalletCoordinator,
        budget: dict[str, Any],
        entry_id: str,
    ) -> None:
        """Initialize the budget progress sensor."""
        super().__init__(coordinator)
        self._budget_id = budget["id"]
        budget_name = budget.get("name", "Unknown")
        self._attr_unique_id = f"wallet_budget_{self._budget_id}"
        self._attr_name = f"Budget: {budget_name}"
        self._attr_device_info = _wallet_device_info(entry_id)

    @property
    def _budget(self) -> dict[str, Any] | None:
        """Find current budget in coordinator data."""
        if not self.coordinator.data:
            return None
        for b in self.coordinator.data.budgets:
            if b["id"] == self._budget_id:
                return b
        return None

    @property
    def native_value(self) -> float | None:
        """Return budget usage percentage."""
        budget = self._budget
        if not budget or not self.coordinator.data:
            return None

        try:
            budget_amount = float(budget.get("amount", 0))
        except (ValueError, TypeError):
            return None

        if budget_amount <= 0:
            return None

        spent = _compute_budget_spent(
            budget, self.coordinator.data.records_current_month
        )
        percentage = (spent / budget_amount) * 100
        return round(min(percentage, 999.9), 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return budget details."""
        budget = self._budget
        if not budget:
            return {}

        try:
            budget_amount = float(budget.get("amount", 0))
        except (ValueError, TypeError):
            budget_amount = 0

        spent = 0.0
        if self.coordinator.data:
            spent = _compute_budget_spent(
                budget, self.coordinator.data.records_current_month
            )

        remaining = max(budget_amount - spent, 0)

        attrs: dict[str, Any] = {
            "budget_amount": round(budget_amount, 2),
            "spent_amount": round(spent, 2),
            "remaining": round(remaining, 2),
            "currency": budget.get("currencyCode", "EUR"),
        }

        if budget.get("startDate"):
            attrs["start_date"] = budget["startDate"]
        if budget.get("endDate"):
            attrs["end_date"] = budget["endDate"]

        category_ids = set(budget.get("categoryIds", []))
        if category_ids and self.coordinator.data:
            cat_names = []
            for cid in category_ids:
                cat = self.coordinator.data.categories_map.get(cid)
                if cat and cat.get("name"):
                    cat_names.append(cat["name"])
            if cat_names:
                attrs["categories"] = cat_names

        return attrs

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.coordinator.last_update_success and self._budget is not None


def _parse_rrule(rrule: str) -> str:
    """Convert RRULE string to human-readable frequency."""
    if not rrule:
        return "unknown"
    parts = {}
    for part in rrule.split(";"):
        if "=" in part:
            key, val = part.split("=", 1)
            parts[key] = val

    freq = parts.get("FREQ", "")
    try:
        interval = int(parts.get("INTERVAL", "1"))
    except (ValueError, TypeError):
        interval = 1

    if freq == "DAILY":
        return f"every {interval} days" if interval > 1 else "daily"
    if freq == "WEEKLY":
        return f"every {interval} weeks" if interval > 1 else "weekly"
    if freq == "MONTHLY":
        if interval == 1:
            return "monthly"
        if interval == 3:
            return "quarterly"
        if interval == 6:
            return "semi-annually"
        if interval == 12:
            return "annually"
        return f"every {interval} months"
    if freq == "YEARLY":
        return f"every {interval} years" if interval > 1 else "annually"
    return rrule


class WalletStandingOrderSensor(CoordinatorEntity[WalletCoordinator], SensorEntity):
    """Sensor showing a standing order (recurring payment)."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: WalletCoordinator,
        order: dict[str, Any],
        entry_id: str,
        account: dict[str, Any] | None,
    ) -> None:
        """Initialize the standing order sensor."""
        super().__init__(coordinator)
        self._order_id = order["id"]
        order_name = order.get("name", "Unknown")
        self._attr_unique_id = f"wallet_standing_{self._order_id}"
        self._attr_name = f"Standing: {order_name}"
        self._attr_native_unit_of_measurement = order.get("currencyCode", "EUR")

        order_type = order.get("type", "expense")
        self._attr_icon = "mdi:cash-plus" if order_type == "income" else "mdi:cash-minus"

        if account:
            self._attr_device_info = _account_device_info(entry_id, account)
        else:
            self._attr_device_info = _wallet_device_info(entry_id)

    @property
    def _order(self) -> dict[str, Any] | None:
        """Find current standing order in coordinator data."""
        if not self.coordinator.data:
            return None
        for o in self.coordinator.data.standing_orders:
            if o["id"] == self._order_id:
                return o
        return None

    @property
    def native_value(self) -> float | None:
        """Return the standing order amount."""
        order = self._order
        if not order:
            return None
        try:
            return round(float(order.get("amount", 0)), 2)
        except (ValueError, TypeError):
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return standing order details."""
        order = self._order
        if not order:
            return {}

        attrs: dict[str, Any] = {
            "type": order.get("type", ""),
            "frequency": _parse_rrule(order.get("recurrenceRule", "")),
            "recurrence_rule": order.get("recurrenceRule", ""),
            "payment_type": order.get("paymentType", ""),
        }

        payee = order.get("payee")
        if payee:
            attrs["payee"] = html.escape(payee[:100])
        payer = order.get("payer")
        if payer:
            attrs["payer"] = html.escape(payer[:100])
        note = order.get("note")
        if note:
            attrs["note"] = html.escape(note[:100])

        cat_id = order.get("categoryId")
        if cat_id and self.coordinator.data:
            cat = self.coordinator.data.categories_map.get(cat_id)
            if cat and cat.get("name"):
                attrs["category"] = cat["name"]

        generate_from = order.get("generateFromDate")
        if generate_from:
            attrs["active_since"] = generate_from[:10]

        rrule = order.get("recurrenceRule", "")
        if "UNTIL=" in rrule:
            until = rrule.split("UNTIL=")[1].split(";")[0]
            attrs["ends_at"] = until[:10] if len(until) >= 10 else until

        return attrs

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.coordinator.last_update_success and self._order is not None
