from __future__ import annotations

from datetime import timedelta

from .models import SyncResult
from .providers import DataProvider, ProviderError
from .instruments import normalize_instrument_symbol
from .storage import CandleStore


class DataSyncService:
    def __init__(self, store: CandleStore, providers: list[DataProvider]):
        self.store = store
        self.providers = providers

    def sync_symbol(self, symbol: str, full_refresh: bool = False) -> SyncResult:
        normalized = normalize_instrument_symbol(symbol)
        earliest_cached_date = self.store.get_earliest_date(normalized) if full_refresh else None
        latest_date = None if full_refresh else self.store.get_latest_date(normalized)
        start_date = None if latest_date is None else latest_date + timedelta(days=1)
        errors: list[str] = []
        for provider in self.providers:
            try:
                candles = provider.fetch_daily(normalized, start_date=start_date)
                if start_date is not None:
                    candles = [candle for candle in candles if candle.date >= start_date]
                if not candles:
                    if start_date is not None:
                        return SyncResult(symbol=normalized, provider=provider.name, rows=0)
                    raise ProviderError("returned no rows")
                if full_refresh:
                    earliest_fetched_date = min(candle.date for candle in candles)
                    if earliest_cached_date is not None and earliest_fetched_date > earliest_cached_date:
                        raise ProviderError(
                            "full refresh returned partial history: "
                            f"cached_start={earliest_cached_date.isoformat()}, fetched_start={earliest_fetched_date.isoformat()}"
                        )
                    rows = self.store.replace_symbol_history(normalized, candles)
                else:
                    rows = self.store.upsert_many(normalized, candles)
                return SyncResult(symbol=normalized, provider=provider.name, rows=rows)
            except Exception as exc:
                errors.append(f"{provider.name}: {exc}")
        raise ProviderError("; ".join(errors) if errors else "no providers configured")
