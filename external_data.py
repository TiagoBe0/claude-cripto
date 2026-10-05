"""Indicadores de BTC de fuentes públicas fuera de Binance.

- fear_greed.csv     Crypto Fear & Greed Index diario (alternative.me), 0 = miedo extremo, 100 = codicia extrema
- BTC_dvol_1h.csv    DVOL de Deribit: volatilidad implícita anualizada a 30 días de opciones BTC (en %)
- BTC_market_1d.csv  CoinGecko: precio, market cap y volumen de 24 h de BTC sumando todos los exchanges
- stablecoins_1d.csv DefiLlama: total de stablecoins atadas al dólar en circulación (US$)

Las dos diarias guardan cada fila con la apertura del día que resumen (como las velas de 1d):
CoinGecko y DefiLlama publican el dato de las 00:00 UTC, que es el cierre del día anterior.
La API gratis de CoinGecko solo da los últimos 365 días: la historia arranca ahí.
"""

import logging
import time
from pathlib import Path

import requests

from storage import Appender, last_timestamp

log = logging.getLogger("external_data")

HOUR = 60 * 60 * 1000
DAY = 24 * HOUR
FNG_COLUMNS = ["timestamp", "datetime_utc", "value", "classification"]
DVOL_COLUMNS = ["timestamp", "datetime_utc", "open", "high", "low", "close"]
MARKET_COLUMNS = ["timestamp", "datetime_utc", "price_usd", "market_cap_usd", "volume_24h_usd"]
STABLE_COLUMNS = ["timestamp", "datetime_utc", "stablecoins_usd"]


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


def _closed_day_start(now_ms: int) -> int:
    """Apertura del último día UTC ya cerrado (ayer)."""
    return now_ms // DAY * DAY - DAY


def update_btc_market(csv_path: Path, coin: str = "bitcoin") -> int:
    last = last_timestamp(csv_path)
    now = int(time.time() * 1000)
    newest = _closed_day_start(now)
    if last is not None and last >= newest:
        return 0  # ya está el día de ayer: no gastar un pedido (el límite gratis es bajo)
    days = 365 if last is None else min(365, (now - last) // DAY + 1)
    d = _get(f"https://api.coingecko.com/api/v3/coins/{coin}/market_chart",
             vs_currency="usd", days=days, interval="daily")
    caps, vols = dict(d["market_caps"]), dict(d["total_volumes"])
    rows = []
    for ts, price in d["prices"]:
        day = int(ts) - DAY  # el dato de las 00:00 cierra el día anterior
        # el último punto es "ahora" (no cae a las 00:00) y es un día a medias: se descarta
        if int(ts) % DAY == 0 and (last is None or day > last) and day <= newest:
            rows.append([day, price, caps.get(ts), vols.get(ts)])
    with Appender(csv_path, MARKET_COLUMNS) as app:
        app.write(rows)
        return app.added


def update_stablecoins(csv_path: Path, start_ms: int) -> int:
    last = last_timestamp(csv_path)
    newest = _closed_day_start(int(time.time() * 1000))
    if last is not None and last >= newest:
        return 0
    data = _get("https://stablecoins.llama.fi/stablecoincharts/all")
    rows = []
    for r in data:
        day = int(r["date"]) * 1000 - DAY
        usd = (r.get("totalCirculatingUSD") or {}).get("peggedUSD")
        if usd is not None and day >= start_ms and (last is None or day > last) and day <= newest:
            rows.append([day, usd])
    with Appender(csv_path, STABLE_COLUMNS) as app:
        app.write(sorted(rows))
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
    if ecfg.get("coingecko_market", True):
        p = data_dir / "BTC_market_1d.csv"
        out.append(("market cap", p, lambda p=p: update_btc_market(p)))
    if ecfg.get("stablecoins", True):
        p = data_dir / "stablecoins_1d.csv"
        out.append(("stablecoins", p, lambda p=p: update_stablecoins(p, start_ms)))
    return out
