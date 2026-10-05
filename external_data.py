"""Indicadores de BTC de fuentes públicas fuera de Binance.

- fear_greed.csv     Crypto Fear & Greed Index diario (alternative.me), 0 = miedo extremo, 100 = codicia extrema
- BTC_dvol_1h.csv    DVOL de Deribit: volatilidad implícita anualizada a 30 días de opciones BTC (en %)
"""

import logging
import time
from pathlib import Path

import requests

from storage import Appender, last_timestamp

log = logging.getLogger("external_data")

HOUR = 60 * 60 * 1000
FNG_COLUMNS = ["timestamp", "datetime_utc", "value", "classification"]
DVOL_COLUMNS = ["timestamp", "datetime_utc", "open", "high", "low", "close"]


def _get(url: str, **params) -> dict:
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def update_fear_greed(csv_path: Path, start_ms: int) -> int:
    last = last_timestamp(csv_path)
    since = start_ms if last is None else last + 1
    data = _get("https://api.alternative.me/fng/", limit=0)["data"]
    rows = sorted([int(d["timestamp"]) * 1000, int(d["value"]), d["value_classification"]] for d in data)
    with Appender(csv_path, FNG_COLUMNS) as app:
        app.write([r for r in rows if r[0] >= since])
        return app.added


def update_dvol(csv_path: Path, start_ms: int, currency: str = "BTC") -> int:
    last = last_timestamp(csv_path)
    since = start_ms if last is None else last + 1
    now = int(time.time() * 1000)
    points: dict[int, list] = {}
    end = now
    # Deribit devuelve hasta 1000 puntos desde el final hacia atrás y un
    # "continuation" con el siguiente end_timestamp a pedir.
    while True:
        res = _get("https://www.deribit.com/api/v2/public/get_volatility_index_data",
                   currency=currency, start_timestamp=since, end_timestamp=end, resolution="3600")["result"]
        for ts, o, h, l, c in res["data"]:
            points[int(ts)] = [o, h, l, c]
        if not res.get("continuation"):
            break
        end = int(res["continuation"])
    closed = sorted(ts for ts in points if ts >= since and ts + HOUR <= now)
    with Appender(csv_path, DVOL_COLUMNS) as app:
        app.write([[ts, *points[ts]] for ts in closed])
        return app.added


def tasks(cfg: dict, data_dir: Path, start_ms: int):
    ecfg = cfg.get("external", {})
    out = []
    if ecfg.get("fear_greed", True):
        p = data_dir / "fear_greed.csv"
        out.append(("fear & greed", p, lambda p=p: update_fear_greed(p, start_ms)))
    if ecfg.get("deribit_dvol", True):
        p = data_dir / "BTC_dvol_1h.csv"
        out.append(("DVOL", p, lambda p=p: update_dvol(p, start_ms)))
    return out
