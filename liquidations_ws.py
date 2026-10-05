"""Captura en vivo las liquidaciones de BTCUSDT perpetuo en Binance y Bybit.

Uso (proceso permanente, lo corre el servicio systemd cripto-liquidaciones):
    python liquidations_ws.py [data_dir]

Escribe <data_dir>/BTCUSDT_liquidations.csv con una fila por liquidación:
    timestamp, datetime_utc, exchange, side, price, qty, usd
`side` es la posición liquidada: "long" (venta forzada) o "short" (compra forzada).

No hay historia: solo existe lo capturado mientras el proceso corre.
Binance manda como máximo una liquidación por segundo y por símbolo (la última),
así que en cascadas subestima; Bybit manda todas.
"""

import asyncio
import json
import logging
import sys
from pathlib import Path

import aiohttp

from storage import Appender

SYMBOL = "BTCUSDT"
COLUMNS = ["timestamp", "datetime_utc", "exchange", "side", "price", "qty", "usd"]
BINANCE_URL = f"wss://fstream.binance.com/market/ws/{SYMBOL.lower()}@forceOrder"
BYBIT_URL = "wss://stream.bybit.com/v5/public/linear"

log = logging.getLogger("liquidations_ws")


def _row(ts, exchange, side, price, qty):
    price, qty = float(price), float(qty)
    return [int(ts), exchange, side, price, qty, round(price * qty, 2)]


async def binance(session: aiohttp.ClientSession, app: Appender) -> None:
    async with session.ws_connect(BINANCE_URL, heartbeat=60) as ws:
        log.info("binance: conectado")
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                break
            o = json.loads(msg.data)["o"]
            # S = lado de la orden de liquidación: SELL cierra un largo
            app.write([_row(o["T"], "binance", "long" if o["S"] == "SELL" else "short", o["ap"], o["z"])])


async def bybit(session: aiohttp.ClientSession, app: Appender) -> None:
    async with session.ws_connect(BYBIT_URL) as ws:
        await ws.send_json({"op": "subscribe", "args": [f"allLiquidation.{SYMBOL}"]})
        log.info("bybit: conectado")

        async def ping():  # Bybit corta la conexión sin un ping propio cada ~20 s
            while True:
                await asyncio.sleep(20)
                await ws.send_json({"op": "ping"})

        pinger = asyncio.create_task(ping())
        try:
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                m = json.loads(msg.data)
                if not m.get("topic", "").startswith("allLiquidation"):
                    continue
                # En Bybit S es el lado de la posición: Buy = se liquidó un largo
                app.write([_row(r["T"], "bybit", "long" if r["S"] == "Buy" else "short", r["p"], r["v"])
                           for r in m["data"]])
        finally:
            pinger.cancel()


async def forever(name, fn, session, app) -> None:
    delay = 1
    while True:
        try:
            await fn(session, app)
            delay = 1  # cierre normal (Binance corta cada 24 h): reconectar ya
        except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, ValueError) as e:
            log.warning("%s: %s; reconecto en %d s", name, e, delay)
            delay = min(delay * 2, 300)
        await asyncio.sleep(delay)


async def main(data_dir: Path) -> None:
    with Appender(data_dir / f"{SYMBOL}_liquidations.csv", COLUMNS) as app:
        async with aiohttp.ClientSession() as session:
            await asyncio.gather(forever("binance", binance, session, app),
                                 forever("bybit", bybit, session, app))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    d = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("data")
    asyncio.run(main(d))
