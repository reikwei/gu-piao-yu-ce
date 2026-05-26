from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .funds import DEFAULT_FUND_HISTORY_DAYS, FundFactorStore, FundFactorSyncService, build_default_fund_providers
from .predictors import KronosPredictor
from .providers import build_default_providers, list_a_share_symbols
from .relative_strength import DEFAULT_RELATIVE_HISTORY_DAYS, RelativeStrengthStore, RelativeStrengthSyncService
from .storage import CandleStore, normalize_symbol
from .sync import DataSyncService
from .accounts import AccountStore


_PROGRESS_VERSION = 1


load_dotenv()


def main() -> None:
    parser = argparse.ArgumentParser(description="A-share Kronos MVP")
    parser.add_argument("--db", default=os.getenv("KLINE_DB_PATH", "data/candles.db"))
    parser.add_argument("--fund-db", default=os.getenv("FUND_DB_PATH", "data/fund_factors.db"))
    parser.add_argument("--relative-db", default=os.getenv("RELATIVE_DB_PATH", "data/relative_strength.db"))
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync_parser = subparsers.add_parser("sync", help="sync daily K-line data")
    sync_parser.add_argument("symbols", nargs="*")
    sync_parser.add_argument("--all", action="store_true", dest="sync_all", help="sync all A-share symbols")
    sync_parser.add_argument(
        "--market",
        choices=["all", "sh", "sz", "bj"],
        default="all",
        help="limit all-market sync to one exchange shard",
    )
    sync_parser.add_argument(
        "--prefixes",
        help="comma-separated symbol prefixes used to split all-market sync into smaller shards",
    )
    sync_parser.add_argument("--progress-file", help="progress JSON file used to resume interrupted all-market syncs")
    sync_parser.add_argument("--max-retries", type=int, default=int(os.getenv("SYNC_MAX_RETRIES", "2")))
    sync_parser.add_argument("--reset-progress", action="store_true", help="discard saved progress and rebuild the queue")
    sync_parser.add_argument(
        "--full-refresh",
        action="store_true",
        help="re-fetch full history for matched symbols and overwrite existing cached rows",
    )

    sync_fund_parser = subparsers.add_parser("sync-funds", help="sync recent market-wide fund factors")
    sync_fund_parser.add_argument(
        "--history-days",
        type=int,
        default=int(os.getenv("FUND_SYNC_HISTORY_DAYS", str(DEFAULT_FUND_HISTORY_DAYS))),
        help="number of recent A-share trading sessions to keep incrementally in the fund cache",
    )

    sync_relative_parser = subparsers.add_parser("sync-relative", help="sync benchmark, industry mapping and relative strength cache")
    sync_relative_parser.add_argument("symbols", nargs="*")
    sync_relative_parser.add_argument(
        "--history-days",
        type=int,
        default=int(os.getenv("RELATIVE_SYNC_HISTORY_DAYS", str(DEFAULT_RELATIVE_HISTORY_DAYS))),
        help="number of recent calendar days used to seed benchmark and industry caches when empty",
    )
    sync_relative_parser.add_argument(
        "--refresh-mappings",
        action="store_true",
        help="force a fresh industry mapping pull instead of reusing a recent mapping cache",
    )

    predict_parser = subparsers.add_parser("predict", help="run Kronos prediction from local cache")
    predict_parser.add_argument("symbol")
    predict_parser.add_argument("--horizon", type=int, default=5)
    predict_parser.add_argument("--paths", type=int, default=3)
    predict_parser.add_argument("--lookback", type=int, default=512)

    serve_parser = subparsers.add_parser("serve", help="start FastAPI dev server")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8000)

    merge_parser = subparsers.add_parser("merge-db", help="merge multiple SQLite caches into one database")
    merge_parser.add_argument("target")
    merge_parser.add_argument("sources", nargs="+")

    admin_parser = subparsers.add_parser("create-admin", help="create the first admin account")
    admin_parser.add_argument("--username", default=os.getenv("ADMIN_USERNAME", "admin"))
    admin_parser.add_argument("--password", default=os.getenv("ADMIN_PASSWORD") or os.getenv("APP_ACCESS_PASSWORD"))
    admin_parser.add_argument("--app-db", default=os.getenv("APP_DB_PATH"))

    args = parser.parse_args()

    if args.command == "sync":
        _run_sync(parser, args)
    elif args.command == "sync-funds":
        _run_sync_funds(args)
    elif args.command == "sync-relative":
        _run_sync_relative(args)
    elif args.command == "predict":
        store = CandleStore(args.db)
        candles = store.get_latest(args.symbol, limit=args.lookback)
        predictor = KronosPredictor(
            model_name=os.getenv("KRONOS_MODEL", "NeoQuasar/Kronos-small"),
            tokenizer_name=os.getenv("KRONOS_TOKENIZER", "NeoQuasar/Kronos-Tokenizer-base"),
            device=os.getenv("KRONOS_DEVICE", "cpu"),
        )
        result = predictor.predict(args.symbol, candles, horizon=args.horizon, paths=args.paths)
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    elif args.command == "serve":
        import uvicorn

        uvicorn.run("kronos_mvp.api:create_app", host=args.host, port=args.port, reload=False, factory=True)
    elif args.command == "merge-db":
        _run_merge_db(args)
    elif args.command == "create-admin":
        _run_create_admin(args, args.db)


def _run_sync(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.max_retries < 0:
        parser.error("sync --max-retries must be >= 0")
    if args.sync_all and args.symbols:
        parser.error("sync --all cannot be combined with explicit symbols")
    if not args.sync_all and args.market != "all":
        parser.error("sync --market requires --all")
    if not args.sync_all and args.progress_file:
        parser.error("sync --progress-file requires --all")
    if not args.sync_all and args.prefixes:
        parser.error("sync --prefixes requires --all")

    prefixes = _parse_prefixes(args.prefixes)

    store = CandleStore(args.db)
    service = DataSyncService(store=store, providers=build_default_providers())

    if args.sync_all:
        progress_path = _resolve_progress_path(store.db_path, args.market, args.progress_file, prefixes)
        if args.reset_progress:
            _delete_progress_file(progress_path)

        state = _load_progress_state(
            progress_path,
            market=args.market,
            max_retries=args.max_retries,
            prefixes=prefixes,
            full_refresh=args.full_refresh,
        )
        resume_source = None
        if state is None:
            symbols = list_a_share_symbols(market=args.market)
            symbols = _filter_symbols_by_prefixes(symbols, prefixes)
            if not symbols:
                parser.error("sync --all resolved no symbols")
            state = _new_progress_state(
                symbols=symbols,
                market=args.market,
                max_retries=args.max_retries,
                prefixes=prefixes,
                full_refresh=args.full_refresh,
            )
            _save_progress_state(progress_path, state)
            resume_source = "fresh"
        else:
            resume_source = str(state.pop("resume_source", "progress"))

        print(
            json.dumps(
                {
                    "event": "sync_progress",
                    "mode": state["mode"],
                    "market": state["market"],
                    "progress_file": str(progress_path),
                    "resume": resume_source != "fresh",
                    "resume_source": resume_source,
                    "prefixes": state.get("prefixes", []),
                    "fullRefresh": bool(state.get("full_refresh")),
                    "pending": len(state["pending"]),
                    "total": state["summary"]["symbols"],
                },
                ensure_ascii=False,
            )
        )
        _run_all_market_sync(service, state, progress_path, args.max_retries)
        return

    symbols = _normalize_symbols(args.symbols)
    if not symbols:
        parser.error("sync requires one or more symbols or --all")
    _run_symbol_sync(service, symbols, full_refresh=args.full_refresh)


def _run_symbol_sync(service: DataSyncService, symbols: list[str], full_refresh: bool = False) -> None:
    succeeded = 0
    updated = 0
    total_rows = 0
    for symbol in symbols:
        result = service.sync_symbol(symbol, full_refresh=full_refresh)
        succeeded += 1
        updated += int(result.rows > 0)
        total_rows += result.rows
        print(json.dumps({"ok": True, **result.__dict__, "fullRefresh": full_refresh}, ensure_ascii=False))
    print(
        json.dumps(
            {
                "summary": {
                    "mode": "symbols",
                    "fullRefresh": full_refresh,
                    "symbols": len(symbols),
                    "succeeded": succeeded,
                    "failed": 0,
                    "updated": updated,
                    "rows": total_rows,
                }
            },
            ensure_ascii=False,
        )
    )


def _run_sync_funds(args: argparse.Namespace) -> None:
    store = FundFactorStore(args.fund_db)
    service = FundFactorSyncService(store=store, providers=build_default_fund_providers())
    result = service.sync_recent(history_days=args.history_days)
    print(json.dumps({"ok": True, **result.to_dict()}, ensure_ascii=False))
    print(
        json.dumps(
            {
                "summary": {
                    "mode": "funds",
                    "targetDate": result.target_date.isoformat(),
                    "requestedDays": result.requested_days,
                    "syncedDays": len(result.synced_trade_dates),
                    "skippedDays": len(result.skipped_trade_dates),
                    "providers": list(result.providers),
                    "rows": result.rows,
                }
            },
            ensure_ascii=False,
        )
    )


def _run_sync_relative(args: argparse.Namespace) -> None:
    store = RelativeStrengthStore(args.relative_db)
    service = RelativeStrengthSyncService(store=store)
    symbols = _normalize_symbols(args.symbols)
    if symbols:
        result = service.sync_market(
            history_days=args.history_days,
            symbols=symbols,
            force_refresh_mappings=bool(args.refresh_mappings),
        )
        mode = "symbols"
    else:
        result = service.sync_market(
            history_days=args.history_days,
            force_refresh_mappings=bool(args.refresh_mappings),
        )
        mode = "market"

    print(json.dumps({"ok": True, **result.to_dict()}, ensure_ascii=False))
    print(
        json.dumps(
            {
                "summary": {
                    "mode": "relative-strength",
                    "scope": mode,
                    "symbols": list(result.target_symbols),
                    "historyDays": args.history_days,
                    "forceRefreshMappings": bool(args.refresh_mappings),
                    "mappingRows": result.mapping_rows,
                    "benchmarkCount": len(result.benchmark_labels),
                    "industryCount": len(result.industry_names),
                    "rows": result.rows,
                    "warnings": list(result.warnings),
                }
            },
            ensure_ascii=False,
        )
    )


def _run_all_market_sync(
    service: DataSyncService,
    state: dict[str, Any],
    progress_path: Path,
    max_retries: int,
) -> None:
    while state["pending"]:
        symbol = state["pending"][0]
        attempt = _safe_int(state["attempts"].get(symbol), 0) + 1
        state["attempts"][symbol] = attempt
        _save_progress_state(progress_path, state)

        try:
            result = service.sync_symbol(symbol, full_refresh=bool(state.get("full_refresh")))
        except Exception as exc:
            error_text = str(exc)
            if attempt <= max_retries:
                state["summary"]["retried"] += 1
                state["pending"].append(state["pending"].pop(0))
                _save_progress_state(progress_path, state)
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "symbol": normalize_symbol(symbol),
                            "error": error_text,
                            "retrying": True,
                            "attempt": attempt,
                            "max_retries": max_retries,
                            "progress": _progress_view(state),
                        },
                        ensure_ascii=False,
                    )
                )
                continue

            state["pending"].pop(0)
            state["attempts"].pop(symbol, None)
            state["summary"]["failed"] += 1
            state["summary"]["processed"] += 1
            state["failed_symbols"].append(
                {
                    "symbol": normalize_symbol(symbol),
                    "error": error_text,
                    "attempts": attempt,
                }
            )
            _save_progress_state(progress_path, state)
            print(
                json.dumps(
                    {
                        "ok": False,
                        "symbol": normalize_symbol(symbol),
                        "error": error_text,
                        "retrying": False,
                        "attempt": attempt,
                        "max_retries": max_retries,
                        "progress": _progress_view(state),
                    },
                    ensure_ascii=False,
                )
            )
            continue

        state["pending"].pop(0)
        state["attempts"].pop(symbol, None)
        state["summary"]["succeeded"] += 1
        state["summary"]["processed"] += 1
        state["summary"]["updated"] += int(result.rows > 0)
        state["summary"]["rows"] += result.rows
        _save_progress_state(progress_path, state)
        print(json.dumps({"ok": True, **result.__dict__, "progress": _progress_view(state)}, ensure_ascii=False))

    summary = {
        **state["summary"],
        "mode": state["mode"],
        "market": state["market"],
        "fullRefresh": bool(state.get("full_refresh")),
        "remaining": len(state["pending"]),
        "progress_file": str(progress_path),
    }
    if state["failed_symbols"]:
        summary["failed_symbols"] = list(state["failed_symbols"])
        _save_progress_state(progress_path, state)
    else:
        _delete_progress_file(progress_path)
    print(json.dumps({"summary": summary}, ensure_ascii=False))

    if state["failed_symbols"] or state["summary"]["succeeded"] == 0:
        raise SystemExit(1)


def _run_merge_db(args: argparse.Namespace) -> None:
    target_store = CandleStore(args.target)
    merged_sources = 0
    total_rows = 0

    for source in args.sources:
        source_path = Path(source)
        if not source_path.exists():
            print(json.dumps({"ok": False, "source": str(source_path), "error": "not found"}, ensure_ascii=False))
            continue
        rows = target_store.merge_from(source_path)
        merged_sources += 1
        total_rows += rows
        print(json.dumps({"ok": True, "source": str(source_path), "rows": rows}, ensure_ascii=False))

    print(
        json.dumps(
            {
                "summary": {
                    "mode": "merge-db",
                    "target": str(Path(args.target)),
                    "sources": merged_sources,
                    "rows": total_rows,
                }
            },
            ensure_ascii=False,
        )
    )
    if merged_sources == 0:
        raise SystemExit(1)


def _run_create_admin(args: argparse.Namespace, kline_db: str) -> None:
    if not args.password:
        raise SystemExit("create-admin requires --password or ADMIN_PASSWORD/APP_ACCESS_PASSWORD")
    app_db = args.app_db or str(Path(kline_db).with_name("app.db"))
    store = AccountStore(app_db)
    store.bootstrap_admin(args.username, args.password)
    print(json.dumps({"ok": True, "username": args.username, "app_db": app_db}, ensure_ascii=False))


def _resolve_progress_path(db_path: str | Path, market: str, progress_file: str | None, prefixes: tuple[str, ...]) -> Path:
    if progress_file:
        return Path(progress_file)
    base_path = Path(db_path)
    suffix = "" if market == "all" else f"-{market}"
    if prefixes:
        suffix += "-" + "-".join(prefixes)
    return base_path.with_name(f"sync-progress{suffix}.json")


def _new_progress_state(
    symbols: list[str],
    market: str,
    max_retries: int,
    prefixes: tuple[str, ...],
    full_refresh: bool,
) -> dict[str, Any]:
    normalized = _normalize_symbols(symbols)
    return {
        "version": _PROGRESS_VERSION,
        "mode": "all",
        "market": market,
        "prefixes": list(prefixes),
        "full_refresh": full_refresh,
        "max_retries": max_retries,
        "status": "running",
        "started_at": _utc_now(),
        "updated_at": _utc_now(),
        "pending": normalized,
        "attempts": {},
        "failed_symbols": [],
        "summary": {
            "symbols": len(normalized),
            "processed": 0,
            "succeeded": 0,
            "failed": 0,
            "updated": 0,
            "rows": 0,
            "retried": 0,
        },
    }


def _load_progress_state(
    progress_path: Path,
    market: str,
    max_retries: int,
    prefixes: tuple[str, ...],
    full_refresh: bool,
) -> dict[str, Any] | None:
    if not progress_path.exists():
        return None
    try:
        payload = json.loads(progress_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    if (
        payload.get("version") != _PROGRESS_VERSION
        or payload.get("mode") != "all"
        or payload.get("market") != market
        or tuple(str(prefix) for prefix in payload.get("prefixes", [])) != prefixes
        or bool(payload.get("full_refresh", False)) != full_refresh
    ):
        return None

    pending = _normalize_symbols(payload.get("pending", []))
    if pending:
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
        attempts = payload.get("attempts") if isinstance(payload.get("attempts"), dict) else {}
        payload["pending"] = pending
        payload["attempts"] = {symbol: _safe_int(attempts.get(symbol), 0) for symbol in pending}
        payload["failed_symbols"] = list(payload.get("failed_symbols", []))
        payload["prefixes"] = list(prefixes)
        payload["full_refresh"] = full_refresh
        payload["summary"] = {
            "symbols": _safe_int(summary.get("symbols"), len(pending)),
            "processed": _safe_int(summary.get("processed"), 0),
            "succeeded": _safe_int(summary.get("succeeded"), 0),
            "failed": _safe_int(summary.get("failed"), 0),
            "updated": _safe_int(summary.get("updated"), 0),
            "rows": _safe_int(summary.get("rows"), 0),
            "retried": _safe_int(summary.get("retried"), 0),
        }
        payload["max_retries"] = max_retries
        payload["status"] = "running"
        payload["resume_source"] = "pending"
        return payload

    failed_symbols = [
        normalize_symbol(item.get("symbol", ""))
        for item in payload.get("failed_symbols", [])
        if isinstance(item, dict) and item.get("symbol")
    ]
    failed_symbols = [symbol for symbol in failed_symbols if symbol]
    if failed_symbols:
        state = _new_progress_state(
            symbols=failed_symbols,
            market=market,
            max_retries=max_retries,
            prefixes=prefixes,
            full_refresh=full_refresh,
        )
        state["resume_source"] = "failed_symbols"
        return state

    _delete_progress_file(progress_path)
    return None


def _save_progress_state(progress_path: Path, state: dict[str, Any]) -> None:
    payload = {
        **state,
        "status": "failed" if state["failed_symbols"] else ("running" if state["pending"] else "completed"),
        "updated_at": _utc_now(),
    }
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = progress_path.with_suffix(progress_path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(progress_path)


def _delete_progress_file(progress_path: Path) -> None:
    if progress_path.exists():
        progress_path.unlink()


def _progress_view(state: dict[str, Any]) -> dict[str, int]:
    return {
        "processed": int(state["summary"]["processed"]),
        "total": int(state["summary"]["symbols"]),
        "remaining": len(state["pending"]),
        "failed": int(state["summary"]["failed"]),
        "retried": int(state["summary"]["retried"]),
    }


def _normalize_symbols(symbols: object) -> list[str]:
    if not isinstance(symbols, list):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for symbol in symbols:
        code = normalize_symbol(str(symbol))
        if code and code not in seen:
            seen.add(code)
            normalized.append(code)
    return normalized


def _parse_prefixes(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    prefixes: list[str] = []
    seen: set[str] = set()
    for raw in value.split(","):
        prefix = raw.strip()
        if not prefix:
            continue
        if not prefix.isdigit():
            raise SystemExit("sync --prefixes only accepts digits and commas")
        if prefix not in seen:
            seen.add(prefix)
            prefixes.append(prefix)
    return tuple(prefixes)


def _filter_symbols_by_prefixes(symbols: list[str], prefixes: tuple[str, ...]) -> list[str]:
    if not prefixes:
        return symbols
    return [symbol for symbol in symbols if any(symbol.startswith(prefix) for prefix in prefixes)]


def _safe_int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


if __name__ == "__main__":
    main()
