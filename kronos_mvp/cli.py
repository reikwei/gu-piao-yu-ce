from __future__ import annotations

import argparse
import json
import os

from dotenv import load_dotenv

from .predictors import KronosPredictor
from .providers import build_default_providers
from .storage import CandleStore
from .sync import DataSyncService


load_dotenv()


def main() -> None:
    parser = argparse.ArgumentParser(description="A-share Kronos MVP")
    parser.add_argument("--db", default=os.getenv("KLINE_DB_PATH", "data/candles.db"))
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync_parser = subparsers.add_parser("sync", help="sync daily K-line data")
    sync_parser.add_argument("symbols", nargs="+")

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
        service = DataSyncService(store=store, providers=build_default_providers())
        for symbol in args.symbols:
            result = service.sync_symbol(symbol)
            print(json.dumps(result.__dict__, ensure_ascii=False))
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
