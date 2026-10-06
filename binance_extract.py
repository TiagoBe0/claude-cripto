"""Descarga incremental de datos de BTC a CSV (y JSON opcional):

- velas spot de Binance + indicadores técnicos (indicators.py)
- futuros perpetuos de Binance: velas, premium, funding, open interest,
  ratios long/short y foto del libro (futures_data.py)
- Fear & Greed, DVOL de Deribit, market cap (CoinGecko y CoinMarketCap), stablecoins y flujos a
  exchanges (DefiLlama) (external_data.py)
- al final: estrategia, paper trading, modelo ML, cobertura sugerida (estrategia/cobertura.py) y los datos
  del panel, según lo que esté en config.json

Uso:
    python binance_extract.py                 # usa config.json
    python binance_extract.py --config otra.json

Cada CSV es la fuente de verdad de su serie: en cada corrida se lee el
último timestamp guardado y solo se piden datos nuevos. Las velas todavía
abiertas no se guardan, así los archivos nunca tienen datos parciales.
"""

import argparse
import fcntl
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import ccxt
import requests

import build_dashboard
import external_data
import futures_data
from estrategia.live_signal import update as update_strategy
from estrategia.paper import update as update_paper
from indicators import write_indicators
from ml.build_dataset import update as update_ml_dataset
from storage import Appender, export_json, last_timestamp

COLUMNS = ["timestamp", "datetime_utc", "open", "high", "low", "close", "volume"]
PAGE_LIMIT = 1000  # máximo de velas por request en Binance spot

log = logging.getLogger("binance_extract")


def load_config(path: Path) -> dict:
    with path.open() as f:
        return json.load(f)


def file_stem(symbol: str, timeframe: str) -> str:
    return f"{symbol.replace('/', '')}_{timeframe}"


def candle_close_ms(exchange, open_ms: int, timeframe: str) -> int:
    """Momento (ms) en que cierra la vela. Los meses tienen largo variable."""
    if timeframe.endswith("M"):
        dt = datetime.fromtimestamp(open_ms / 1000, tz=timezone.utc)
        months = dt.month - 1 + int(timeframe[:-1])
        nxt = dt.replace(year=dt.year + months // 12, month=months % 12 + 1)
        return int(nxt.timestamp() * 1000)
    return open_ms + exchange.parse_timeframe(timeframe) * 1000


def fetch_new_candles(exchange, symbol: str, timeframe: str, since: int):
    """Generador de páginas de velas cerradas con apertura >= `since` (ms)."""
    while True:
        batch = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=PAGE_LIMIT)
        now = exchange.milliseconds()
        closed = [c for c in batch if candle_close_ms(exchange, c[0], timeframe) <= now]
        if closed:
            yield closed
        if len(batch) < PAGE_LIMIT or not closed:
            return
        # Binance devuelve velas con apertura >= startTime: +1 ms basta y
        # evita suponer un largo fijo de vela.
        since = closed[-1][0] + 1


def update_csv(exchange, symbol: str, timeframe: str, csv_path: Path, start_ms: int) -> int:
    last = last_timestamp(csv_path)
    since = start_ms if last is None else last + 1
    with Appender(csv_path, COLUMNS) as app:
        for page in fetch_new_candles(exchange, symbol, timeframe, since):
            app.write(page)
            log.info("  %s %s: +%d velas (hasta %s)", symbol, timeframe, len(page),
                     exchange.iso8601(page[-1][0]))
        return app.added


def spot_tasks(exchange, cfg: dict, data_dir: Path, start_ms: int):
    out = []
    for symbol in cfg["symbols"]:
        for tf in cfg["timeframes"]:
            p = data_dir / f"{file_stem(symbol, tf)}.csv"
            # un timeframe puede pedir más historia (la EMA de la estrategia necesita calentar)
            since = cfg.get("spot_start_dates", {}).get(tf)
            since = exchange.parse8601(f"{since}T00:00:00Z") if since else start_ms
            out.append((f"spot {symbol} {tf}", p,
                        lambda s=symbol, tf=tf, p=p, since=since: update_csv(exchange, s, tf, p, since)))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=Path(__file__).with_name("config.json"), type=Path)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)

    data_dir = Path(cfg.get("data_dir", "data"))
    if not data_dir.is_absolute():
        data_dir = args.config.parent / data_dir
    data_dir.mkdir(parents=True, exist_ok=True)

    # Evita que dos corridas (p. ej. cron solapado) escriban el mismo CSV
    lock_file = (data_dir / ".lock").open("w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.warning("Ya hay otra corrida en curso, salgo.")
        return

    exchange = ccxt.binance({"enableRateLimit": True})
    start_ms = exchange.parse8601(f"{cfg['start_date']}T00:00:00Z")
    with_json = cfg.get("export_json", False)

    spot = spot_tasks(exchange, cfg, data_dir, start_ms)
    tasks = spot + futures_data.tasks(exchange, cfg, data_dir, start_ms) + external_data.tasks(cfg, data_dir, start_ms)

    t0 = time.time()
    failed = 0
    for name, csv_path, run in tasks:
        try:
            added = run()
            log.info("%s: %d filas nuevas -> %s", name, added, csv_path.name)
            if with_json:
                export_json(csv_path)
            if cfg.get("indicators", False) and (name, csv_path, run) in spot:
                dest = write_indicators(csv_path, with_json)
                log.info("%s: indicadores -> %s", name, dest.name)
        except (ccxt.BaseError, requests.RequestException) as e:
            failed += 1
            log.error("%s: falló la descarga (%s)", name, e)
        except Exception:  # cualquier otro error (p. ej. un zip corrupto de vision) no corta la corrida
            failed += 1
            log.exception("%s: falló al procesar %s", name, csv_path.name)
    if scfg := cfg.get("strategy"):
        try:
            st = update_strategy(data_dir, scfg["symbol"], scfg["timeframe"])
            log.info("estrategia: vela %s, posición %s, orden próxima apertura %s, stop %s, entrada si cierra > %s",
                     st["last_candle_open_utc"], "long" if st["in_position"] else "fuera",
                     st["action_next_open"], st["stop"], st["entry_trigger"])
        except Exception:
            failed += 1
            log.exception("estrategia: falló el cálculo de la señal")
    if pcfg := cfg.get("paper"):
        try:
            ps = update_paper(data_dir, pcfg["start"], pcfg.get("capital", 10_000))
            if ps["waiting"]:
                log.info("paper: empieza en %s", ps["start_utc"])
            for key, a in ps["accounts"].items():
                if not ps["waiting"]:
                    log.info("paper %s: capital %s, exposición %s, orden %s", key, a["equity_usd"],
                             a["exposure"], a["order"])
        except Exception:
            failed += 1
            log.exception("paper: falló la simulación")
    if (mcfg := cfg.get("ml")) is not None:
        try:
            log.info("ml: dataset -> %s", update_ml_dataset(data_dir, mcfg))
        except Exception:
            failed += 1
            log.exception("ml: falló al armar el dataset")
    if mcfg and (h := mcfg.get("live_horizon")):
        try:
            from ml.live_model import update as update_ml_model  # importa lightgbm: solo si está configurado

            pr = update_ml_model(data_dir, h, mcfg.get("threshold_pct", {}).get(str(h)))
            log.info("ml: vela %s, P(movimiento %dh) %.0f %% (percentil %.0f), P(long) %.0f %%, P(short) %.0f %%",
                     pr["candle_open_utc"], h, pr["p_move"] * 100, pr["p_move_pctile_90d"],
                     pr["p_long"] * 100, pr["p_short"] * 100)
        except Exception:
            failed += 1
            log.exception("ml: falló la predicción en vivo")
    if ccfg := cfg.get("cobertura"):
        try:
            from estrategia.cobertura import update as update_cobertura  # también importa lightgbm

            cs = update_cobertura(data_dir, ccfg["start"], ccfg.get("capital", 10_000))
            p = cs["paper"]
            log.info("cobertura: %s (percentil %s)%s", f"corto {cs['hedge']:.0%} en el perp hasta {cs['until_utc']}"
                     if cs["hedge"] else "sin cobertura", cs["pctl"],
                     "" if p.get("waiting") else f", paper {p['return_pct']:+.2f} % contra {p['buy_hold_return_pct']:+.2f} %")
        except Exception:
            failed += 1
            log.exception("cobertura: falló")
    if (ecfg := cfg.get("trading", {}).get("ejecutor", {})).get("enabled"):
        try:
            from estrategia.ejecutor import update as update_executor

            ej = update_executor(data_dir, ecfg)
            pos = ej["position"]
            log.info("ejecutor (%s): %s, capital %s USDT (%+.2f %%)", "REAL" if ej["real"] else "testnet",
                     f"{pos['qty']} BTC, stop {pos['stop_price']}" if pos else "afuera",
                     ej["equity_usdt"], ej["return_pct"])
        except Exception:
            failed += 1
            log.exception("ejecutor: falló")
    # Después de la estrategia: status.json lee state.json y paper.json recién calculados
    if cfg.get("dashboard", False):
        try:
            log.info("dashboard: datos -> %s", build_dashboard.build(data_dir))
        except Exception:
            log.exception("dashboard: falló al generar los datos")
    log.info("Listo en %.1f s (%d series, %d con error)", time.time() - t0, len(tasks), failed)


if __name__ == "__main__":
    main()
