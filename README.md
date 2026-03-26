# BudgetBakers Wallet for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

Custom Home Assistant integration for [BudgetBakers Wallet](https://budgetbakers.com/) — track your financial accounts, spending, transactions, budgets, and standing orders directly in Home Assistant.

## Features

- **Account balances** — real-time balance for each financial account (bank, cash, credit card, investment, loan, mortgage)
- **Monthly spending** — total expenses with breakdown by category
- **Recent transactions** — configurable number of transactions per account (0-100)
- **Budget progress** — percentage used with remaining amount
- **Standing orders** — recurring payments with frequency, payee, and category
- **Investment accounts** — link to external sensors (e.g., Avanza Stock) for live market values
- **Multi-language** — English, Slovak, Czech translations

## Requirements

- BudgetBakers Wallet **Premium** subscription (API access)
- API token from [BudgetBakers Settings](https://web.budgetbakers.com/settings/apiTokens)

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Click the three dots in the top right corner
3. Select **Custom repositories**
4. Add `https://github.com/Koky05/ha-budgetbakers-wallet` as **Integration**
5. Click **Install**
6. Restart Home Assistant

### Manual

1. Copy `custom_components/budgetbakers_wallet/` to your HA `config/custom_components/` directory
2. Restart Home Assistant

## Configuration

1. Go to **Settings** > **Devices & Services** > **Add Integration**
2. Search for **BudgetBakers Wallet**
3. Enter your API token
4. Select which accounts to monitor
5. Done!

### Options

| Option | Default | Description |
|--------|---------|-------------|
| Update interval | 30 min | How often to poll the API (5-1440 minutes) |
| Transactions count | 10 | Recent transactions per account in attributes (0-100) |
| Monitored accounts | All | Which accounts to create sensors for |
| Investment entity | — | HA sensor entity for investment account balance (e.g., Avanza Stock) |

### Adding New Accounts

When you add a new bank account in the Wallet app, go to **Configure** in the integration options. The account list will be refreshed automatically on the accounts selection page.

## Sensors

### Per Account (as separate devices)

| Sensor | Type | Description |
|--------|------|-------------|
| Balance | monetary | Current account balance |
| Standing orders | monetary | Recurring payment amounts with frequency |

### Global (Wallet service device)

| Sensor | Type | Description |
|--------|------|-------------|
| Monthly Spending | monetary | Total expenses this month with category breakdown |
| Recent Transactions | count | Transaction count with last 10 in attributes |
| Budget progress | percentage | Budget usage with spent/remaining amounts |

## API Notes

- **Read-only API** — all GET endpoints, no modifications
- **Rate limit** — 500 requests/hour
- **Balance computation** — `initialBalance * 100 + sum(all records)` (API returns initialBalance divided by 100)
- **Record amounts** — already signed (negative for expenses, positive for income)
- **Max date range** — 370 days per request, uses 365-day windows

## Compatibility

- Home Assistant **2026.02+**
- Forward-compatible with **2026.03** (follows new error handling patterns)

## License

MIT
