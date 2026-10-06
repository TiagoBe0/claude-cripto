"""Indicadores de BTC de fuentes públicas fuera de Binance.

- fear_greed.csv     Crypto Fear & Greed Index diario (alternative.me), 0 = miedo extremo, 100 = codicia extrema
- BTC_dvol_1h.csv    DVOL de Deribit: volatilidad implícita anualizada a 30 días de opciones BTC (en %)
- BTC_market_1d.csv  CoinGecko: precio, market cap y volumen de 24 h de BTC sumando todos los exchanges
- stablecoins_1d.csv DefiLlama: stablecoins atadas al dólar en circulación (unidades ≈ US$), total y
                     USDT y USDC por separado. La diferencia día a día es la emisión neta (creadas − quemadas)
- crypto_market_1d.csv CoinMarketCap: market cap de todo el mercado cripto y de las altcoins (todo menos
                     BTC), dominancia de BTC y ETH y volumen de 24 h, desde 2015
- cex_flows_1h.csv   DefiLlama: entradas netas a 88 exchanges (Binance, Coinbase, OKX...) según sus billeteras
                     públicas, en las últimas 24 h, 7 d y 30 d, y lo que tienen en custodia. DefiLlama solo da
                     el dato del momento gratis: se guarda una foto por hora y la historia arranca con el
                     recolector. cex_flows.json guarda además el detalle por exchange de la última foto

Las diarias guardan cada fila con la apertura del día que resumen (como las velas de 1d):
CoinGecko, DefiLlama y CoinMarketCap publican el dato de las 00:00 UTC (CoinMarketCap, 00:30), que es el
cierre del día anterior. La API gratis de CoinGecko solo da los últimos 365 días: la historia arranca ahí.
La de CoinMarketCap es la que usa su propia web (la API oficial gratis no da historia): no está
documentada y puede cambiar sin aviso.
"""

import calendar
import json
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
STABLE_COLUMNS = ["timestamp", "datetime_utc", "stablecoins_usd", "usdt", "usdc"]
# id de DefiLlama de cada stablecoin que se sigue por separado (None = todas)
STABLE_IDS = {"stablecoins_usd": None, "usdt": 1, "usdc": 2}
CRYPTO_COLUMNS = ["timestamp", "datetime_utc", "total_mcap_usd", "altcoin_mcap_usd", "btc_dominance",
                  "eth_dominance", "volume_24h_usd", "altcoin_volume_24h_usd"]
CMC_HISTORY = "https://api.coinmarketcap.com/data-api/v3/global-metrics/quotes/historical"
CMC_START = 1420070400000  # 2015-01-01: lo más viejo que tiene con dominancia de BTC
CMC_MAX_DAYS = 2000  # rechaza pedidos de más de 2200 puntos
CEX_COLUMNS = ["timestamp", "datetime_utc", "exchanges", "inflows_24h_usd", "inflows_7d_usd", "inflows_30d_usd",
               "assets_usd", "clean_assets_usd"]


def _get(url: str, **params) -> dict:
    # CoinMarketCap rechaza pedidos sin User-Agent de navegador
    r = requests.get(url, params=params, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
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
    """Oferta diaria en unidades (no en US$: si una stablecoin se desacopla un poco de 1 dólar, su valor
    cambia sin que se cree ni se queme nada)."""
    last = last_timestamp(csv_path)
    newest = _closed_day_start(int(time.time() * 1000))
    if last is not None and last >= newest:
        return 0
    series = {}
    for col, sid in STABLE_IDS.items():
        params = {} if sid is None else {"stablecoin": sid}
        data = _get("https://stablecoins.llama.fi/stablecoincharts/all", **params)
        series[col] = {int(r["date"]) * 1000 - DAY: (r.get("totalCirculating") or {}).get("peggedUSD") for r in data}
    days = sorted(d for d in series["stablecoins_usd"]
                  if d >= start_ms and (last is None or d > last) and d <= newest)
    rows = [[d, *(series[col].get(d) for col in STABLE_IDS)] for d in days]
    with Appender(csv_path, STABLE_COLUMNS) as app:
        app.write(rows)
        return app.added


def update_crypto_market(csv_path: Path) -> int:
    last = last_timestamp(csv_path)
    now = int(time.time() * 1000)
    newest = _closed_day_start(now)
    if last is not None and last >= newest:
        return 0
    since = CMC_START if last is None else last + DAY
    rows = {}
    # De a ventanas de CMC_MAX_DAYS; cada punto (00:00 o 00:30 UTC) cierra el día anterior
    for start in range(since + DAY, newest + 2 * DAY, CMC_MAX_DAYS * DAY):
        end = min(start + CMC_MAX_DAYS * DAY, now)
        quotes = _get(CMC_HISTORY, format="chart", interval="1d",
                      timeStart=start // 1000, timeEnd=end // 1000)["data"]["quotes"]
        for q in quotes:
            u = q["quote"][0]
            if u["timestamp"][11:13] not in ("00", "01"):
                continue  # el último punto puede ser "ahora", a mitad del día: no es un cierre
            day = calendar.timegm(time.strptime(u["timestamp"][:10], "%Y-%m-%d")) * 1000 - DAY
            if since <= day <= newest and day not in rows and u.get("totalMarketCap"):
                rows[day] = [day, u["totalMarketCap"], u.get("altcoinMarketCap"), q.get("btcDominance"),
                             q.get("ethDominance") or None, u.get("totalVolume24H"), u.get("altcoinVolume24H")]
    with Appender(csv_path, CRYPTO_COLUMNS) as app:
        app.write([rows[d] for d in sorted(rows)])
        return app.added


def update_cex_flows(csv_path: Path) -> int:
    """Una foto por hora: la fila lleva la hora en que se tomó (redondeada para abajo)."""
    hour = int(time.time() * 1000) // HOUR * HOUR
    last = last_timestamp(csv_path)
    if last is not None and last >= hour:
        return 0
    cexs = [c for c in _get("https://api.llama.fi/cexs")["cexs"] if c.get("currentTvl")]
    total = lambda k: sum(c.get(k) or 0 for c in cexs)
    row = [hour, len(cexs), total("inflows_24h"), total("inflows_1w"), total("inflows_1m"),
           total("currentTvl"), total("cleanAssetsTvl")]
    detail = [{"name": c["name"], "assets_usd": c.get("currentTvl"), "clean_assets_usd": c.get("cleanAssetsTvl"),
               "inflows_24h_usd": c.get("inflows_24h"), "inflows_7d_usd": c.get("inflows_1w"),
               "inflows_30d_usd": c.get("inflows_1m")} for c in cexs]
    detail_path = csv_path.with_name("cex_flows.json")
    tmp = detail_path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"timestamp": hour, "exchanges": detail}))
    tmp.replace(detail_path)
    with Appender(csv_path, CEX_COLUMNS) as app:
        app.write([row])
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
    if ecfg.get("crypto_market", True):
        p = data_dir / "crypto_market_1d.csv"
        out.append(("mercado cripto", p, lambda p=p: update_crypto_market(p)))
    if ecfg.get("cex_flows", True):
        p = data_dir / "cex_flows_1h.csv"
        out.append(("flujos a exchanges", p, lambda p=p: update_cex_flows(p)))
    return out
