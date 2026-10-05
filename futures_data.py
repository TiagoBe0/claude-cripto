"""Datos del mercado de futuros perpetuos USDⓈ-M de Binance (API pública).

Series que genera (prefijo = símbolo, p. ej. BTCUSDT):
- <s>_perp_<tf>.csv       velas del perpetuo con volumen en USDT, trades y volumen taker comprador
- <s>_premium_<tf>.csv    velas del premium index (perp vs índice spot)
- <s>_funding.csv         historial de funding rate (cada 8 h)
- <s>_metrics_5m.csv      open interest y ratios long/short cada 5 min
- <s>_snapshot.csv        una fila por corrida: funding estimado, OI en vivo y profundidad del libro

Los endpoints de open interest y ratios long/short solo devuelven los
últimos 30 días. Para el historial anterior se usan los archivos diarios de
data.binance.vision, que tienen las mismas series (con más decimales).
Los timestamps se guardan con la convención de la API: en vision, el open
interest y los ratios long/short están 5 min antes que en la API, y el ratio
taker coincide (verificado comparando un día completo de ambas fuentes).
"""

import csv
import io
import logging
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from storage import Appender, last_timestamp

log = logging.getLogger("futures_data")

FIVE_MIN = 5 * 60 * 1000
DAY = 24 * 60 * 60 * 1000
API_WINDOW = 29 * DAY  # la API rechaza startTime con 30 días o más de antigüedad
VISION_URL = "https://data.binance.vision/data/futures/um/daily/metrics/{s}/{s}-metrics-{d}.zip"

PERP_COLUMNS = ["timestamp", "datetime_utc", "open", "high", "low", "close", "volume",
                "quote_volume", "trades", "taker_buy_volume", "taker_buy_quote_volume"]
PREMIUM_COLUMNS = ["timestamp", "datetime_utc", "premium_open", "premium_high", "premium_low", "premium_close"]
FUNDING_COLUMNS = ["timestamp", "datetime_utc", "funding_rate", "mark_price"]

# columna, método CCXT, campo de la API, columna en vision, corrimiento vision -> API (ms)
METRICS = [
    ("open_interest", "fapiDataGetOpenInterestHist", "sumOpenInterest", "sum_open_interest", FIVE_MIN),
    ("open_interest_usd", "fapiDataGetOpenInterestHist", "sumOpenInterestValue", "sum_open_interest_value", FIVE_MIN),
    ("top_trader_account_ls_ratio", "fapiDataGetTopLongShortAccountRatio", "longShortRatio",
     "count_toptrader_long_short_ratio", FIVE_MIN),
    ("top_trader_position_ls_ratio", "fapiDataGetTopLongShortPositionRatio", "longShortRatio",
     "sum_toptrader_long_short_ratio", FIVE_MIN),
    ("global_account_ls_ratio", "fapiDataGetGlobalLongShortAccountRatio", "longShortRatio",
     "count_long_short_ratio", FIVE_MIN),
    ("taker_buy_sell_ratio", "fapiDataGetTakerlongshortRatio", "buySellRatio", "sum_taker_long_short_vol_ratio", 0),
]
METRICS_COLUMNS = ["timestamp", "datetime_utc"] + [m[0] for m in METRICS]

SNAPSHOT_COLUMNS = ["timestamp", "datetime_utc", "mark_price", "index_price", "est_funding_rate",
                    "next_funding_time", "open_interest",
                    "spot_bid", "spot_ask", "spot_spread_bps",
                    "spot_bid_depth_usd_05pct", "spot_ask_depth_usd_05pct", "spot_imbalance_05pct",
                    "perp_bid_depth_usd_01pct", "perp_ask_depth_usd_01pct", "perp_imbalance_01pct"]


# --- velas (perpetuo y premium index) ---------------------------------------

def update_klines(exchange, method: str, symbol: str, timeframe: str, csv_path: Path,
                  start_ms: int, columns: list[str], pick) -> int:
    """Descarga velas cerradas de un endpoint tipo /klines a partir de la última guardada."""
    last = last_timestamp(csv_path)
    since = start_ms if last is None else last + 1
    fetch = getattr(exchange, method)
    with Appender(csv_path, columns) as app:
        while True:
            batch = fetch({"symbol": symbol, "interval": timeframe, "startTime": since, "limit": 1500})
            now = exchange.milliseconds()
            closed = [k for k in batch if int(k[6]) < now]  # k[6] = closeTime
            if closed:
                app.write([[int(k[0]), *pick(k)] for k in closed])
            if len(batch) < 1500 or not closed:
                return app.added
            since = int(closed[-1][0]) + 1


def perp_fields(k):
    return [k[1], k[2], k[3], k[4], k[5], k[7], k[8], k[9], k[10]]


def premium_fields(k):
    return [k[1], k[2], k[3], k[4]]


# --- funding rate -------------------------------------------------------------

def update_funding(exchange, symbol: str, csv_path: Path, start_ms: int) -> int:
    last = last_timestamp(csv_path)
    since = start_ms if last is None else last + 1
    with Appender(csv_path, FUNDING_COLUMNS) as app:
        while True:
            batch = exchange.fapiPublicGetFundingRate(
                {"symbol": symbol, "startTime": since, "endTime": exchange.milliseconds(), "limit": 1000})
            if batch:
                app.write([[int(r["fundingTime"]), r["fundingRate"], r.get("markPrice", "")] for r in batch])
            if len(batch) < 1000:
                return app.added
            since = int(batch[-1]["fundingTime"]) + 1


# --- open interest y ratios long/short (5 min) ------------------------------

def _vision_day(symbol: str, day: str) -> list[dict]:
    r = requests.get(VISION_URL.format(s=symbol, d=day), timeout=60)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        return list(csv.DictReader(io.StringIO(z.read(z.namelist()[0]).decode())))


def _metrics_from_vision(symbol: str, since: int, until: int, table: dict) -> None:
    # Se pide desde el día anterior porque las columnas corridas 5 min de la
    # primera fila vienen del archivo del día previo.
    first = datetime.fromtimestamp((since - DAY) / 1000, tz=timezone.utc).date()
    last = datetime.fromtimestamp(until / 1000, tz=timezone.utc).date()
    days = [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]
    log.info("  metrics: bajando %d días de data.binance.vision (%s a %s)", len(days), days[0], days[-1])
    with ThreadPoolExecutor(max_workers=8) as pool:
        for day, rows in zip(days, pool.map(lambda d: _vision_day(symbol, d), days)):
            if not rows:
                log.info("  metrics: %s no está en vision", day)
            for row in rows:
                created = datetime.strptime(row["create_time"], "%Y-%m-%d %H:%M:%S")
                ts = int(created.replace(tzinfo=timezone.utc).timestamp() * 1000)
                for col, _, _, vcol, shift in METRICS:
                    if row.get(vcol) not in (None, ""):
                        table.setdefault(ts + shift, {}).setdefault(col, row[vcol])


def _metrics_from_api(exchange, symbol: str, since: int, table: dict) -> None:
    by_method: dict[str, list] = {}
    for col, method, field, _, _ in METRICS:
        by_method.setdefault(method, []).append((col, field))
    for method, fields in by_method.items():
        # Estos endpoints devuelven las 500 filas MÁS RECIENTES del rango,
        # sin importar startTime: hay que paginar hacia atrás con endTime.
        end = exchange.milliseconds()
        while end >= since:
            batch = getattr(exchange, method)(
                {"symbol": symbol, "period": "5m", "startTime": since, "endTime": end, "limit": 500})
            for r in batch:
                for col, field in fields:
                    # vision tiene más decimales: si ya está, no se pisa
                    table.setdefault(int(r["timestamp"]), {}).setdefault(col, r[field])
            if len(batch) < 500:
                break
            # Se superpone una fila: el ratio taker filtra endTime por el
            # cierre del período y con "- 1" se perdería una fila por página.
            end = min(int(r["timestamp"]) for r in batch)


def update_metrics(exchange, symbol: str, csv_path: Path, start_ms: int) -> int:
    last = last_timestamp(csv_path)
    since = start_ms if last is None else last + 1
    now = exchange.milliseconds()
    api_start = max(since, now - API_WINDOW)

    table: dict[int, dict] = {}
    if since < api_start:
        _metrics_from_vision(symbol, since, api_start, table)
    _metrics_from_api(exchange, symbol, api_start, table)

    # Solo se escriben filas hasta el último instante que tienen todas las
    # series; así la fila más nueva nunca queda a medio completar.
    newest = [max((ts for ts, row in table.items() if col in row), default=None) for col, *_ in METRICS]
    if None in newest:
        return 0
    cutoff = min(newest)
    timestamps = sorted(ts for ts in table if since <= ts <= cutoff)
    if timestamps and timestamps[0] > since + FIVE_MIN and last is not None:
        log.warning("  metrics: hueco entre %d y %d (más de 30 días sin correr y sin datos en vision)",
                    last, timestamps[0])
    with Appender(csv_path, METRICS_COLUMNS) as app:
        app.write([[ts, *(table[ts].get(col, "") for col, *_ in METRICS)] for ts in timestamps])
        return app.added


# --- foto del momento ---------------------------------------------------------

def _depth(book: dict, band: float) -> tuple:
    """Suma en USD de bids y asks dentro de ±band del precio medio."""
    bids = [(float(p), float(q)) for p, q in book["bids"]]
    asks = [(float(p), float(q)) for p, q in book["asks"]]
    mid = (bids[0][0] + asks[0][0]) / 2
    lo, hi = mid * (1 - band), mid * (1 + band)
    if bids[-1][0] > lo or asks[-1][0] < hi:
        log.warning("  snapshot: el libro devuelto no cubre ±%.1f%%, profundidad subestimada", band * 100)
    bid_usd = sum(p * q for p, q in bids if p >= lo)
    ask_usd = sum(p * q for p, q in asks if p <= hi)
    return round(bid_usd, 2), round(ask_usd, 2), round((bid_usd - ask_usd) / (bid_usd + ask_usd), 6)


def append_snapshot(exchange, symbol: str, csv_path: Path) -> int:
    prem = exchange.fapiPublicGetPremiumIndex({"symbol": symbol})
    oi = exchange.fapiPublicGetOpenInterest({"symbol": symbol})
    spot_book = exchange.publicGetDepth({"symbol": symbol, "limit": 5000})
    perp_book = exchange.fapiPublicGetDepth({"symbol": symbol, "limit": 1000})

    bid, ask = float(spot_book["bids"][0][0]), float(spot_book["asks"][0][0])
    spread_bps = round((ask - bid) / ((ask + bid) / 2) * 1e4, 4)
    row = [exchange.milliseconds(), prem["markPrice"], prem["indexPrice"], prem["lastFundingRate"],
           int(prem["nextFundingTime"]), oi["openInterest"], bid, ask, spread_bps,
           *_depth(spot_book, 0.005), *_depth(perp_book, 0.001)]
    with Appender(csv_path, SNAPSHOT_COLUMNS) as app:
        app.write([row])
        return app.added


# --- punto de entrada -----------------------------------------------------------

def tasks(exchange, cfg: dict, data_dir: Path, start_ms: int):
    """Devuelve (nombre, archivo, función) por cada serie habilitada en config."""
    fcfg = cfg.get("futures", {})
    s = fcfg.get("symbol", "BTCUSDT")
    out = []
    for tf in cfg["timeframes"]:
        if fcfg.get("klines", True):
            p = data_dir / f"{s}_perp_{tf}.csv"
            out.append((f"perp {tf}", p, lambda p=p, tf=tf: update_klines(
                exchange, "fapiPublicGetKlines", s, tf, p, start_ms, PERP_COLUMNS, perp_fields)))
        if fcfg.get("premium_index", True):
            p = data_dir / f"{s}_premium_{tf}.csv"
            out.append((f"premium {tf}", p, lambda p=p, tf=tf: update_klines(
                exchange, "fapiPublicGetPremiumIndexKlines", s, tf, p, start_ms, PREMIUM_COLUMNS, premium_fields)))
    if fcfg.get("funding_rate", True):
        p = data_dir / f"{s}_funding.csv"
        out.append(("funding", p, lambda p=p: update_funding(exchange, s, p, start_ms)))
    if fcfg.get("metrics_5m", True):
        p = data_dir / f"{s}_metrics_5m.csv"
        out.append(("metrics 5m", p, lambda p=p: update_metrics(exchange, s, p, start_ms)))
    if fcfg.get("snapshot", True):
        p = data_dir / f"{s}_snapshot.csv"
        out.append(("snapshot", p, lambda p=p: append_snapshot(exchange, s, p)))
    return out
