"""BudgetBakers Wallet API client."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from .const import API_BASE_URL, API_MAX_LIMIT, API_MAX_PAGES

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)


class WalletAuthError(Exception):
    """Raised when API returns 401 (invalid/expired token)."""


class WalletRateLimitError(Exception):
    """Raised when API returns 429 (rate limit exceeded)."""


class WalletSyncError(Exception):
    """Raised when API returns 409 (initial sync in progress)."""


class WalletApiError(Exception):
    """Raised on other API errors (5xx, connection issues)."""


class WalletApiClient:
    """Async client for the BudgetBakers Wallet REST API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        token: str,
    ) -> None:
        self._session = session
        self._token = token
        self._base_url = API_BASE_URL
        self._rate_limit_remaining: int | None = None

    @property
    def rate_limit_remaining(self) -> int | None:
        """Return the number of remaining API requests."""
        return self._rate_limit_remaining

    def _headers(self) -> dict[str, str]:
        """Return auth headers."""
        return {"Authorization": f"Bearer {self._token}"}

    def _track_rate_limit(self, resp: aiohttp.ClientResponse) -> None:
        """Track rate limit from response headers."""
        remaining = resp.headers.get("X-RateLimit-Remaining")
        if remaining is not None:
            try:
                self._rate_limit_remaining = int(remaining)
            except (ValueError, TypeError):
                pass

    async def _request(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make a single API request."""
        url = f"{self._base_url}{path}"
        try:
            async with self._session.get(
                url, headers=self._headers(), params=params,
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                self._track_rate_limit(resp)

                if resp.status == 200:
                    try:
                        return await resp.json()
                    except (ValueError, aiohttp.ContentTypeError) as err:
                        raise WalletApiError("Invalid JSON response") from err
                if resp.status == 401:
                    raise WalletAuthError(
                        f"Authentication failed (HTTP {resp.status})"
                    )
                if resp.status == 409:
                    try:
                        data = await resp.json()
                        retry_minutes = data.get("retry_after_minutes", 5)
                    except Exception:
                        retry_minutes = 5
                    raise WalletSyncError(
                        f"Initial sync in progress, retry after {retry_minutes} minutes"
                    )
                if resp.status == 429:
                    retry_after = resp.headers.get("Retry-After", "60")
                    raise WalletRateLimitError(
                        f"Rate limit exceeded, retry after {retry_after}s"
                    )
                raise WalletApiError(f"API error (HTTP {resp.status})")
        except aiohttp.ClientError as err:
            raise WalletApiError(f"Connection error: {err}") from err

    async def _paginated_request(
        self,
        path: str,
        result_key: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch all pages of a paginated endpoint."""
        all_items: list[dict[str, Any]] = []
        request_params = dict(params) if params else {}
        request_params["limit"] = API_MAX_LIMIT
        request_params.setdefault("offset", 0)

        page_count = 0
        while True:
            page_count += 1
            if page_count > API_MAX_PAGES:
                _LOGGER.warning(
                    "Pagination limit reached for %s after %d pages", path, API_MAX_PAGES
                )
                break

            data = await self._request(path, request_params)
            items = data.get(result_key, [])
            all_items.extend(items)

            next_offset = data.get("nextOffset")
            if next_offset is None:
                break
            request_params["offset"] = next_offset

        return all_items

    async def async_test_connection(self) -> bool:
        """Test the API connection with a minimal request."""
        await self._request("/v1/api/accounts", {"limit": 1})
        return True

    async def async_get_accounts(self) -> list[dict[str, Any]]:
        """Get all financial accounts."""
        return await self._paginated_request(
            "/v1/api/accounts", "accounts"
        )

    async def async_get_records(
        self,
        record_date_gte: str | None = None,
        record_date_lt: str | None = None,
        account_id: str | None = None,
        category_id: str | None = None,
        sort_by: str = "-recordDate",
    ) -> list[dict[str, Any]]:
        """Get financial transaction records."""
        params: dict[str, Any] = {"sortBy": sort_by}

        date_filters: list[tuple[str, str]] = []
        if record_date_gte:
            date_filters.append(("recordDate", f"gte.{record_date_gte}"))
        if record_date_lt:
            date_filters.append(("recordDate", f"lt.{record_date_lt}"))
        if account_id:
            params["accountId"] = account_id
        if category_id:
            params["categoryId"] = category_id

        all_records: list[dict[str, Any]] = []
        params["limit"] = API_MAX_LIMIT
        params["offset"] = 0

        page_count = 0
        while True:
            page_count += 1
            if page_count > API_MAX_PAGES:
                _LOGGER.warning(
                    "Pagination limit reached for records after %d pages", API_MAX_PAGES
                )
                break

            url = f"{self._base_url}/v1/api/records"
            request_params = dict(params)

            async with self._session.get(
                url,
                headers=self._headers(),
                params=[*request_params.items(), *date_filters],
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                self._track_rate_limit(resp)

                if resp.status == 200:
                    try:
                        data = await resp.json()
                    except (ValueError, aiohttp.ContentTypeError) as err:
                        raise WalletApiError("Invalid JSON response") from err
                    records = data.get("records", [])
                    all_records.extend(records)
                    next_offset = data.get("nextOffset")
                elif resp.status == 401:
                    raise WalletAuthError("Authentication failed")
                elif resp.status == 429:
                    raise WalletRateLimitError("Rate limit exceeded")
                elif resp.status == 409:
                    raise WalletSyncError("Sync in progress")
                else:
                    raise WalletApiError(f"API error (HTTP {resp.status})")

            if resp.status != 200 or next_offset is None:
                break
            params["offset"] = next_offset

        return all_records

    async def async_get_categories(self) -> list[dict[str, Any]]:
        """Get all transaction categories."""
        return await self._paginated_request(
            "/v1/api/categories", "categories"
        )

    async def async_get_budgets(self) -> list[dict[str, Any]]:
        """Get all budgets."""
        return await self._paginated_request(
            "/v1/api/budgets", "budgets"
        )

    async def async_get_standing_orders(self) -> list[dict[str, Any]]:
        """Get all standing orders."""
        return await self._paginated_request(
            "/v1/api/standing-orders", "standingOrders"
        )
