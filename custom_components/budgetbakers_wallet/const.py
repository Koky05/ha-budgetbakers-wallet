"""Constants for the BudgetBakers Wallet integration."""

from homeassistant.const import Platform

DOMAIN = "budgetbakers_wallet"
PLATFORMS = [Platform.SENSOR]

CONF_API_TOKEN = "api_token"
CONF_UPDATE_INTERVAL = "update_interval"
CONF_MONITORED_ACCOUNTS = "monitored_accounts"
CONF_TRANSACTIONS_COUNT = "transactions_count"
CONF_INVESTMENT_ENTITIES = "investment_entities"

DEFAULT_UPDATE_INTERVAL = 30  # minutes
MIN_UPDATE_INTERVAL = 5
MAX_UPDATE_INTERVAL = 1440  # 24 hours

DEFAULT_TRANSACTIONS_COUNT = 10
MIN_TRANSACTIONS_COUNT = 0
MAX_TRANSACTIONS_COUNT = 100

API_BASE_URL = "https://rest.budgetbakers.com/wallet"
API_MAX_LIMIT = 200
API_MAX_DATE_RANGE_DAYS = 365  # API rejects >370 days, using 365 for safety
API_MAX_PAGES = 50  # Safety limit: 50 pages * 200 items = 10,000 max

ATTR_ACCOUNT_TYPE = "account_type"
ATTR_BANK_ACCOUNT_NUMBER = "bank_account_number"
ATTR_RECORD_COUNT = "record_count"
