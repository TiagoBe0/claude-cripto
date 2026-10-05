"""Captura en vivo las órdenes grandes ("ballenas") de BTC en Binance spot, Binance perp y Coinbase.

Uso (proceso permanente, lo corre el servicio systemd cripto-ballenas):
    python whale_trades_ws.py [data_dir]

Escribe <data_dir>/BTCUSDT_whale_trades_1m.csv con una fila por minuto cerrado y,
por mercado (binance_spot, binance_perp, coinbase):
    <m>_whale_buy_usd, <m>_whale_sell_usd   volumen de órdenes taker >= MIN_USD
    <m>_whale_buy_n,   <m>_whale_sell_n     cantidad de esas órdenes
    <m>_buy_usd,       <m>_sell_usd         volumen taker total (para normalizar)
Compra = el agresor compró (taker buy). Las columnas de un mercado quedan vacías
si ese minuto no llegó ningún trade (conexión caída), para no confundir con cero.

Una orden de mercado grande se ejecuta contra varios makers y llega como varios
trades. Se reagrupan antes de compararlas con el umbral:
- Binance: aggTrades con el mismo tiempo de ejecución (T) y el mismo lado.
- Coinbase: matches con el mismo taker_order_id.
Las ballenas que parten la orden en pedazos chicos (TWAP, icebergs) no se ven.

Además escribe <data_dir>/BTCUSDT_whale_orders.csv con cada orden >= MIN_USD por
separado (minuto, mercado, lado, USD): de ahí salen los avisos de órdenes muy
grandes (alertas/ballenas.py) y, con el tiempo, el estudio de qué hace el precio
después de cada una.

No hay historia: solo existe lo capturado mientras el proceso corre.
"""

import asyncio
import json
import logging
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import aiohttp

from storage import Appender

SYMBOL = "BTCUSDT"
MIN_USD = 100_000
MARKETS = ["binance_spot", "binance_perp", "coinbase"]
FIELDS = ["whale_buy_usd", "whale_sell_usd", "whale_buy_n", "whale_sell_n", "buy_usd", "sell_usd"]
COLUMNS = ["timestamp", "datetime_utc"] + [f"{m}_{f}" for m in MARKETS for f in FIELDS]
URLS = {
    "binance_spot": f"wss://stream.binance.com:9443/ws/{SYMBOL.lower()}@aggTrade",
    # en futuros la ruta /ws/ vieja conecta pero no manda nada (igual que en liquidations_ws.py)
    "binance_perp": f"wss://fstream.binance.com/market/ws/{SYMBOL.lower()}@aggTrade",
    "coinbase": "wss://ws-feed.exchange.coinbase.com",
}
ORDER_COLUMNS = ["timestamp", "datetime_utc", "market", "side", "usd"]
MINUTE = 60_000

log = logging.getLogger("whale_trades_ws")


class Buckets:
    """Órdenes reagrupadas por minuto y mercado: {minuto: {mercado: {clave: [es_compra, usd]}}}."""

    def __init__(self):
        self.data = defaultdict(lambda: defaultdict(dict))
        self.written = -1  # último minuto escrito: lo que llegue más tarde se descarta

    def add(self, market: str, ts_ms: int, key, is_buy: bool, usd: float) -> None:
        minute = ts_ms - ts_ms % MINUTE
        if minute <= self.written:
            return
        orders = self.data[minute][market]
        order = orders.setdefault(key, [is_buy, 0.0])
        order[1] += usd

    def flush(self, app: Appender, orders_app: Appender, now_ms: int) -> None:
        """Escribe los minutos ya cerrados (con 5 s de margen para trades que llegan tarde)."""
        for minute in sorted(m for m in self.data if m + MINUTE + 5000 <= now_ms):
            by_market = self.data.pop(minute)
            row = [minute]
            for m in MARKETS:
                orders = by_market.get(m)
                if not orders:
                    row += [""] * len(FIELDS)
                    continue
                wb = [u for b, u in orders.values() if b and u >= MIN_USD]
                ws = [u for b, u in orders.values() if not b and u >= MIN_USD]
                big = [[minute, m, "buy" if b else "sell", round(u, 2)] for b, u in orders.values() if u >= MIN_USD]
                if big:
                    orders_app.write(sorted(big, key=lambda r: -r[3]))
                row += [round(sum(wb), 2), round(sum(ws), 2), len(wb), len(ws),
                        round(sum(u for b, u in orders.values() if b), 2),
                        round(sum(u for b, u in orders.values() if not b), 2)]
            app.write([row])
            self.written = minute


async def binance(market: str, session: aiohttp.ClientSession, buckets: Buckets) -> None:
    async with session.ws_connect(URLS[market], heartbeat=60) as ws:
        log.info("%s: conectado", market)
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                break
            d = json.loads(msg.data)
            # m = el comprador es maker -> el agresor vendió
            buckets.add(market, d["T"], (d["T"], d["m"]), not d["m"], float(d["p"]) * float(d["q"]))


async def coinbase(market: str, session: aiohttp.ClientSession, buckets: Buckets) -> None:
    async with session.ws_connect(URLS[market], heartbeat=30) as ws:
        await ws.send_json({"type": "subscribe", "product_ids": ["BTC-USD"], "channels": ["matches"]})
        log.info("%s: conectado", market)
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                break
            d = json.loads(msg.data)
            if d.get("type") != "match":
                continue  # last_match repite el último trade al suscribirse
            ts = int(datetime.fromisoformat(d["time"].replace("Z", "+00:00")).timestamp() * 1000)
            # side es el lado del maker: "sell" = el agresor compró
            buckets.add(market, ts, d["taker_order_id"], d["side"] == "sell",
                        float(d["price"]) * float(d["size"]))


async def forever(market, fn, session, buckets) -> None:
    delay = 1
    while True:
        try:
            await fn(market, session, buckets)
            delay = 1  # cierre normal (Binance corta cada 24 h): reconectar ya
        except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, ValueError) as e:
            log.warning("%s: %s; reconecto en %d s", market, e, delay)
            delay = min(delay * 2, 300)
        await asyncio.sleep(delay)


async def flusher(buckets: Buckets, app: Appender, orders_app: Appender) -> None:
    while True:
        await asyncio.sleep(5)
        buckets.flush(app, orders_app, int(time.time() * 1000))


async def main(data_dir: Path) -> None:
    buckets = Buckets()
    with Appender(data_dir / f"{SYMBOL}_whale_trades_1m.csv", COLUMNS) as app, \
            Appender(data_dir / f"{SYMBOL}_whale_orders.csv", ORDER_COLUMNS) as orders_app:
        async with aiohttp.ClientSession() as session:
            await asyncio.gather(flusher(buckets, app, orders_app),
                                 forever("binance_spot", binance, session, buckets),
                                 forever("binance_perp", binance, session, buckets),
                                 forever("coinbase", coinbase, session, buckets))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    d = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("data")
    asyncio.run(main(d))
