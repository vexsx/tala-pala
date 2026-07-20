"""Provider abstraction.

Every provider fetches datapoints and returns :class:`Observation` records
that carry BOTH the raw provider value (value/unit/currency exactly as quoted)
and the normalized canonical value, so conversions stay auditable.

Network behaviour:

* honest User-Agent (``IranGoldPredictor/1.0 (+self-hosted analytics)``);
* timeout from config;
* a small courtesy delay before each outbound request (rate-limit politeness);
* retry with exponential backoff, max 3 attempts, only for transient errors
  (network failures, 5xx, 429).  Auth walls / captchas (401/403) are never
  retried or bypassed — they fail fast with a clear error.
"""
from __future__ import annotations

import abc
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

USER_AGENT = "IranGoldPredictor/1.0 (+self-hosted analytics)"
MAX_ATTEMPTS = 3


class ProviderError(Exception):
    """Raised when a provider cannot deliver observations."""


@dataclass(frozen=True)
class Observation:
    """One datapoint: raw provider quote + normalized canonical value."""

    provider_code: str
    symbol: str                # canonical symbol, e.g. 'IR_GOLD_18K'
    raw_value: float           # exactly as the provider quoted it
    raw_unit: str              # e.g. 'IRR/gram'
    raw_currency: str          # 'IRR' | 'IRT' | 'USD' | 'INDEX' | 'PCT'
    value: float               # normalized (IRT for Iranian symbols, etc.)
    currency: str              # normalized currency
    unit: str                  # normalized unit
    observed_at: datetime      # aware UTC
    raw_payload: Optional[dict] = field(default=None, hash=False)


class Provider(abc.ABC):
    """Base class for all market-data providers."""

    code: str = "base"
    category: str = ""

    def __init__(
        self,
        timeout: float = 15.0,
        courtesy_delay: float = 1.0,
        backoff_base: float = 0.75,
    ) -> None:
        self.timeout = timeout
        self.courtesy_delay = courtesy_delay
        self.backoff_base = backoff_base

    # -- public API ----------------------------------------------------------

    @abc.abstractmethod
    def fetch(self) -> list[Observation]:
        """Fetch current observations.  Raises ProviderError on total failure."""

    # -- HTTP helpers --------------------------------------------------------

    def _client(self) -> httpx.Client:
        return httpx.Client(
            timeout=self.timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
            follow_redirects=True,
        )

    def _request(
        self,
        url: str,
        params: Optional[dict] = None,
        headers: Optional[dict] = None,
    ) -> httpx.Response:
        """GET with courtesy delay + bounded exponential-backoff retry.

        ``headers`` are merged over the client defaults (e.g. a Bearer
        ``Authorization`` header for keyed providers).
        """
        last_exc: Optional[Exception] = None
        for attempt in range(MAX_ATTEMPTS):
            if self.courtesy_delay > 0:
                time.sleep(self.courtesy_delay)
            try:
                with self._client() as client:
                    resp = client.get(url, params=params, headers=headers)
                if resp.status_code in (401, 403):
                    # Auth wall or bot-block: never retry, never bypass.
                    raise ProviderError(
                        f"{self.code}: access denied by {url} "
                        f"(HTTP {resp.status_code}); not retrying"
                    )
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"transient HTTP {resp.status_code}",
                        request=resp.request,
                        response=resp,
                    )
                resp.raise_for_status()
                return resp
            except ProviderError:
                raise
            except (httpx.HTTPError, OSError) as exc:
                last_exc = exc
                if attempt < MAX_ATTEMPTS - 1 and self.backoff_base > 0:
                    time.sleep(self.backoff_base * (2**attempt))
        raise ProviderError(f"{self.code}: request to {url} failed after "
                            f"{MAX_ATTEMPTS} attempts: {last_exc}")

    def _get_json(
        self, url: str, params: Optional[dict] = None, headers: Optional[dict] = None
    ) -> Any:
        resp = self._request(url, params=params, headers=headers)
        try:
            return resp.json()
        except ValueError as exc:
            raise ProviderError(f"{self.code}: non-JSON response from {url}: {exc}") from exc

    def _get_text(
        self, url: str, params: Optional[dict] = None, headers: Optional[dict] = None
    ) -> str:
        return self._request(url, params=params, headers=headers).text
