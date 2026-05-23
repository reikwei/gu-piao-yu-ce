from __future__ import annotations

import argparse
import json
import os

from dotenv import load_dotenv

from .predictors import KronosPredictor
from .providers import build_default_providers, list_a_share_symbols
from .storage import CandleStore, normalize_symbol
from .sync import DataSyncService


load_dotenv()


def main() -> None:
    parser = argparse.ArgumentParser(description="A-share Kronos MVP")
    parser.add_argument("--db", default=os.getenv("KLINE_DB_PATH", "data/candles.db"))
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync_parser = subparsers.add_parser("sync", help="sync daily K-line data")
    sync_parser.add_argument("symbols", nargs="*")
    sync_parser.add_argument("--all", action="store_true", dest="sync_all", help="sync all A-share symbols")

    predict_parser = subparsers.add_parser("predict", help="run Kronos prediction from local cache")
    predict_parser.add_argument("symbol")
    predict_parser.add_argument("--horizon", type=int, default=5)
    predict_parser.add_argument("--paths", type=int, default=3)
    predict_parser.add_argument("--lookback", type=int, default=512)

    serve_parser = subparsers.add_parser("serve", help="start FastAPI dev server")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8000)

    args = parser.parse_args()
    store = CandleStore(args.db)

    if args.command == "sync":
        if args.sync_all and args.symbols:
            parser.error("sync --all cannot be combined with explicit symbols")
        if args.sync_all:
            symbols = list_a_share_symbols()
        else:
            symbols = args.symbols
        if not symbols:
            parser.error("sync requires one or more symbols or --all")

        service = DataSyncService(store=store, providers=build_default_providers())
        succeeded = 0
        failed = 0
        updated = 0
        total_rows = 0
        for symbol in symbols:
            try:
                result = service.sync_symbol(symbol)
                succeeded += 1
                updated += int(result.rows > 0)
                total_rows += result.rows
                print(json.dumps({"ok": True, **result.__dict__}, ensure_ascii=False))
            except Exception as exc:
                failed += 1
                print(json.dumps({"ok": False, "symbol": normalize_symbol(symbol), "error": str(exc)}, ensure_ascii=False))
                if not args.sync_all:
                    raise
        print(
            json.dumps(
                {
                    "summary": {
                        "mode": "all" if args.sync_all else "symbols",
                        "symbols": len(symbols),
                        "succeeded": succeeded,
                        "failed": failed,
                        "updated": updated,
                        "rows": total_rows,
                    }
                },
                ensure_ascii=False,
            )
        )
        if succeeded == 0:
            raise SystemExit(1)
    elif args.command == "predict":
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

        uvicorn.run("kronos_mvp.api:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
