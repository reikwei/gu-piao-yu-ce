from __future__ import annotations

from .models import SyncResult
from .providers import DataProvider, ProviderError
from .storage import CandleStore, normalize_symbol


class DataSyncService:
    def __init__(self, store: CandleStore, providers: list[DataProvider]):
        self.store = store
        self.providers = providers

    def sync_symbol(self, symbol: str) -> SyncResult:
        normalized = normalize_symbol(symbol)
        errors: list[str] = []
        for provider in self.providers:
            try:
                candles = provider.fetch_daily(normalized)
                if not candles:
                    raise ProviderError("returned no rows")
                rows = self.store.upsert_many(normalized, candles)
                return SyncResult(symbol=normalized, provider=provider.name, rows=rows)
            except Exception as exc:
                errors.append(f"{provider.name}: {exc}")
        raise ProviderError("; ".join(errors) if errors else "no providers configured")
