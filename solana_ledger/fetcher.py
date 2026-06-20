"""
Helius API fetcher.

Two-phase approach:
  Phase 1 – getSignaturesForAddress (RPC, 1000/page, fast)
  Phase 2 – Enhanced Transactions API (100/batch, returns parsed data)

On incremental runs, Phase 1 stops as soon as an entire page of signatures is
already in the local cache, so only truly new signatures are fetched.
"""

import time
from typing import Dict, Generator, List, Optional, Set

import requests

HELIUS_RPC = "https://mainnet.helius-rpc.com/"
HELIUS_TXN_URL = "https://api.helius.xyz/v0/transactions"

_MAX_RETRIES = 5
_BACKOFF_BASE = 2.0  # seconds


class RateLimiter:
    def __init__(self, calls_per_second: float = 5.0):
        self._interval = 1.0 / max(calls_per_second, 0.01)
        self._last = 0.0

    def wait(self):
        now = time.monotonic()
        gap = self._interval - (now - self._last)
        if gap > 0:
            time.sleep(gap)
        self._last = time.monotonic()


def _post_with_retry(url: str, **kwargs) -> requests.Response:
    for attempt in range(_MAX_RETRIES):
        try:
            resp = requests.post(url, timeout=60, **kwargs)
            if resp.status_code == 429:
                wait = _BACKOFF_BASE ** (attempt + 1)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            if attempt == _MAX_RETRIES - 1:
                raise
            time.sleep(_BACKOFF_BASE ** (attempt + 1))
    raise RuntimeError("Max retries exceeded")


def fetch_signatures(
    address: str,
    api_key: str,
    known_sigs: Set[str],
    rate_limiter: Optional[RateLimiter] = None,
) -> Generator[List[Dict], None, None]:
    """
    Yield pages of raw signature dicts from getSignaturesForAddress.

    Stops pagination early when an entire page is already in known_sigs,
    which makes incremental runs very fast.
    """
    if rate_limiter is None:
        rate_limiter = RateLimiter(5.0)

    before: Optional[str] = None

    while True:
        rate_limiter.wait()

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getSignaturesForAddress",
            "params": [
                address,
                {
                    "limit": 1000,
                    **({"before": before} if before else {}),
                },
            ],
        }

        resp = _post_with_retry(f"{HELIUS_RPC}?api-key={api_key}", json=payload)
        data = resp.json()

        if "error" in data:
            raise RuntimeError(f"RPC error for {address}: {data['error']}")

        results: List[Dict] = data.get("result") or []
        if not results:
            break

        yield results

        # If every sig in this page is already cached we've caught up
        if all(r["signature"] in known_sigs for r in results):
            break

        if len(results) < 1000:
            break

        before = results[-1]["signature"]


def fetch_transactions(
    signatures: List[str],
    api_key: str,
    rate_limiter: Optional[RateLimiter] = None,
    batch_size: int = 100,
) -> Generator[List[Dict], None, None]:
    """
    Yield batches of Helius-enhanced transaction objects.

    Null entries (transactions Helius cannot parse) are silently dropped.
    """
    if rate_limiter is None:
        rate_limiter = RateLimiter(3.0)

    for i in range(0, len(signatures), batch_size):
        batch = signatures[i : i + batch_size]
        rate_limiter.wait()

        resp = _post_with_retry(
            f"{HELIUS_TXN_URL}?api-key={api_key}",
            json={"transactions": batch},
        )
        data = resp.json()

        if not isinstance(data, list):
            raise RuntimeError(f"Unexpected response from Helius: {data}")

        yield [tx for tx in data if tx is not None]
