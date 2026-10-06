"""Foto del libro de órdenes de BTCUSDT cada minuto (Binance spot y perp): profundidad y spread.

Uso (proceso permanente, lo corre el servicio systemd cripto-libro):
    python libro_1m.py [data_dir]

Escribe <data_dir>/BTCUSDT_book_1m.csv, una fila por minuto (timestamp = minuto en que se pidió):
    <m>_mid, <m>_spread_bps            precio medio y spread en puntos básicos
    <m>_bid_<b>, <m>_ask_<b>           US$ en órdenes límite a ±b % del precio medio
    <m>_cover_pct                      hasta qué distancia llegó el libro devuelto: si es menor que una
                                       banda, la profundidad de esa banda está subestimada
para m = spot (5000 niveles, bandas 0,05 / 0,1 / 0,5 %) y perp (1000 niveles, bandas 0,05 / 0,1 %).

Además guarda cada libro nivel por nivel (tramos de US$ 10) para el mapa de calor del libro: libro_niveles.py.

Sirve para ver cuándo se retiran los market makers (bots de alta frecuencia): el libro se adelgaza y el
spread se abre. Hipótesis P5 y P6 de investigacion/HIPOTESIS.md. No hay historia: solo lo capturado.
Peso en la API de Binance: 250 + 20 por minuto, lejos del límite de 6000.
"""

import logging
import sys
import time
from pathlib import Path

import requests

from libro_niveles import WRITE_EVERY, Niveles
from storage import Appender

SYMBOL = "BTCUSDT"
BOOKS = {
    "spot": ("https://api.binance.com/api/v3/depth", 5000, (0.0005, 0.001, 0.005)),
    "perp": ("https://fapi.binance.com/fapi/v1/depth", 1000, (0.0005, 0.001)),
}


def _band(b: float) -> str:
    return f"{b * 100:g}".replace(".", "")  # 0,05 % -> "005", 0,1 % -> "01", 0,5 % -> "05"


COLUMNS = ["timestamp", "datetime_utc"]
for _m, (_, _, _bands) in BOOKS.items():
    COLUMNS += [f"{_m}_mid", f"{_m}_spread_bps"]
    COLUMNS += [f"{_m}_{side}_{_band(b)}" for b in _bands for side in ("bid", "ask")]
    COLUMNS += [f"{_m}_cover_pct"]

log = logging.getLogger("libro_1m")


def measure(url: str, limit: int, bands: tuple) -> tuple[list, list, list]:
    r = requests.get(url, params={"symbol": SYMBOL, "limit": limit}, timeout=10)
    r.raise_for_status()
    book = r.json()
    bids = [(float(p), float(q)) for p, q in book["bids"]]
    asks = [(float(p), float(q)) for p, q in book["asks"]]
    mid = (bids[0][0] + asks[0][0]) / 2
    out = [round(mid, 2), round((asks[0][0] - bids[0][0]) / mid * 1e4, 4)]
    for b in bands:
        out += [round(sum(p * q for p, q in bids if p >= mid * (1 - b)), 0),
                round(sum(p * q for p, q in asks if p <= mid * (1 + b)), 0)]
    out.append(round(min(mid - bids[-1][0], asks[-1][0] - mid) / mid * 100, 4))
    return out, bids, asks


def snapshot(minute: int, niveles: Niveles | None = None) -> list:
    row = []
    for name, (url, limit, bands) in BOOKS.items():
        n = 3 + 2 * len(bands)
        try:
            out, bids, asks = measure(url, limit, bands)
        except (requests.RequestException, KeyError, IndexError, ValueError) as e:
            log.warning("%s: %s", name, e)
            row += [""] * n  # vacío = sin dato (no confundir con libro vacío)
            continue
        row += out
        if niveles is not None:
            try:
                niveles.add(minute, name, bids, asks)
            except Exception:  # noqa: BLE001 - el mapa del libro nunca corta la captura de bandas
                log.exception("%s: nivel por nivel", name)
    return row


def main(data_dir: Path) -> None:
    try:
        niveles = Niveles(data_dir)
    except Exception:  # noqa: BLE001
        log.exception("libro nivel por nivel desactivado")
        niveles = None
    with Appender(data_dir / f"{SYMBOL}_book_1m.csv", COLUMNS) as app:
        while True:
            # al segundo 2 de cada minuto, para no coincidir con el cierre de vela
            time.sleep(60 - time.time() % 60 + 2)
            minute = int(time.time() // 60 * 60 * 1000)
            app.write([[minute, *snapshot(minute, niveles)]])
            if niveles is not None and (minute // 60_000) % WRITE_EVERY == 0:
                try:
                    niveles.write_heatmap()
                except Exception:  # noqa: BLE001
                    log.exception("mapa del libro")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("data"))
