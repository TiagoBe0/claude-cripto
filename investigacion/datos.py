"""Historia completa de derivados para investigar estrategias propias, en data/research/.

Uso:
    python -m investigacion.datos

Es aparte del pipeline en vivo (que baja desde config.json → start_date, 2024) para no tocarlo:
mismas funciones de futures_data.py, desde el lanzamiento del perpetuo BTCUSDT (2019-09).

- BTCUSDT_perp_1h.csv / _4h.csv     velas del perpetuo con volumen taker comprador
- BTCUSDT_premium_1h.csv            premium del perpetuo sobre el índice spot
- BTCUSDT_funding.csv               funding cobrado cada 8 h
- BTCUSDT_metrics_5m.csv            open interest y ratios long/short (data.binance.vision; arranca
                                    cuando Binance empezó a publicarlos, ~2021-12)
- fear_greed.csv                    Fear & Greed diario desde 2018 (alternative.me)

Incremental como el resto: volver a correrlo solo agrega lo nuevo.
"""

import logging
from pathlib import Path

import ccxt
import requests

import futures_data
from storage import Appender, last_timestamp

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "research"
SYMBOL = "BTCUSDT"
PERP_START = "2019-09-08T00:00:00Z"
METRICS_START = "2020-09-01T00:00:00Z"  # lo que no esté en vision se saltea con un aviso

log = logging.getLogger("investigacion.datos")


def fear_greed(path: Path) -> int:
    rows = requests.get("https://api.alternative.me/fng/", params={"limit": 0, "format": "json"}, timeout=30).json()["data"]
    last = last_timestamp(path) or 0
    new = sorted((int(r["timestamp"]) * 1000, int(r["value"]), r["value_classification"]) for r in rows)
    new = [r for r in new if r[0] > last]
    with Appender(path, ["timestamp", "datetime_utc", "value", "classification"]) as app:
        app.write([list(r) for r in new])
    return len(new)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    OUT.mkdir(parents=True, exist_ok=True)
    ex = ccxt.binance({"enableRateLimit": True})
    start, mstart = ex.parse8601(PERP_START), ex.parse8601(METRICS_START)
    jobs = [
        *[(f"perp {tf}", lambda tf=tf: futures_data.update_klines(
            ex, "fapiPublicGetKlines", SYMBOL, tf, OUT / f"{SYMBOL}_perp_{tf}.csv", start,
            futures_data.PERP_COLUMNS, futures_data.perp_fields)) for tf in ("1h", "4h")],
        ("premium 1h", lambda: futures_data.update_klines(
            ex, "fapiPublicGetPremiumIndexKlines", SYMBOL, "1h", OUT / f"{SYMBOL}_premium_1h.csv", start,
            futures_data.PREMIUM_COLUMNS, futures_data.premium_fields)),
        ("funding", lambda: futures_data.update_funding(ex, SYMBOL, OUT / f"{SYMBOL}_funding.csv", start)),
        ("metrics 5m", lambda: futures_data.update_metrics(ex, SYMBOL, OUT / f"{SYMBOL}_metrics_5m.csv", mstart)),
        ("fear & greed", lambda: fear_greed(OUT / "fear_greed.csv")),
    ]
    for name, run in jobs:
        try:
            log.info("%s: %d filas nuevas", name, run())
        except Exception:
            log.exception("%s: falló", name)


if __name__ == "__main__":
    main()
