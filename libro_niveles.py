"""Libro de órdenes nivel por nivel, cada minuto, para el mapa de calor del libro (liquidez.html).

Lo usa libro_1m.py con los mismos libros que ya pide (Binance spot, 5000 niveles, unos ±1 %; perpetuo, 1000
niveles, unos ±0,15 %: es el máximo que da Binance), así que no suma pedidos a la API.

Guarda en <data_dir>/libro/BTCUSDT_niveles_<día UTC>.csv.gz una línea por minuto, mercado y lado:
    timestamp,market,side,mid,start,usd
    side   b (bids, compras límite) o a (asks, ventas límite)
    start  precio del primer tramo; tramo i = start + i × BUCKET_USD, de abajo hacia arriba
    usd    US$ en órdenes límite de cada tramo, separados por ";" (vacío = 0)
Cada minuto se agrega como un miembro gzip propio: si el proceso se corta, se pierde a lo sumo ese minuto.

Y cada WRITE_EVERY minutos, <data_dir>/dashboard/book_heatmap.json con las últimas KEEP_H horas en la grilla
de BUCKET_USD (formato de liquidez.heatmap: un byte por celda en escala log, en base64), el precio medio de
cada minuto y las órdenes de ballenas (BTCUSDT_whale_orders.csv) ubicadas en el precio medio de su minuto.
"""

import base64
import gzip
import json
import logging
import math
import time
from collections import deque
from pathlib import Path

import numpy as np

SYMBOL = "BTCUSDT"
BUCKET_USD = 10
KEEP_H = 24
WRITE_EVERY = 5  # minutos
MARKETS = ("spot", "perp")
WHALE_MARKET = {"binance_spot": "spot", "binance_perp": "perp", "coinbase": "spot"}
HEADER = "timestamp,market,side,mid,start,usd\n"

log = logging.getLogger("libro_niveles")


def buckets(levels: list[tuple[float, float]]) -> tuple[int, list[int]]:
    """[(precio, cantidad BTC)] -> (precio del primer tramo, US$ por tramo de abajo hacia arriba)."""
    if not levels:
        return 0, []
    idx = np.floor(np.array([p for p, _ in levels]) / BUCKET_USD).astype(np.int64)
    usd = np.array([p * q for p, q in levels])
    lo = int(idx.min())
    out = np.bincount(idx - lo, weights=usd)
    return lo * BUCKET_USD, [int(round(x)) for x in out]


class Niveles:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.dir = data_dir / "libro"
        self.dir.mkdir(parents=True, exist_ok=True)
        # (minuto ms, mercado, mid, start, usd np.array): bids y asks ya sumados en la misma grilla
        self.recent: deque = deque()
        self._load_recent()

    def _path(self, minute_ms: int) -> Path:
        day = time.strftime("%Y-%m-%d", time.gmtime(minute_ms / 1000))
        return self.dir / f"{SYMBOL}_niveles_{day}.csv.gz"

    def add(self, minute_ms: int, market: str, bids, asks) -> None:
        mid = (bids[0][0] + asks[0][0]) / 2
        lines, sides = [], {}
        for side, lv in (("b", bids), ("a", asks)):
            start, usd = buckets(lv)
            sides[side] = (start, usd)
            lines.append(f"{minute_ms},{market},{side},{mid:.2f},{start},{';'.join(str(u) if u else '' for u in usd)}\n")
        path = self._path(minute_ms)
        new = not path.exists()
        with gzip.open(path, "at") as f:
            f.write((HEADER if new else "") + "".join(lines))
        self._remember(minute_ms, market, mid, sides)

    def _remember(self, minute_ms, market, mid, sides) -> None:
        (sb, ub), (sa, ua) = sides["b"], sides["a"]
        lo = min(sb, sa) if ub and ua else (sb if ub else sa)
        hi = max(sb + len(ub) * BUCKET_USD, sa + len(ua) * BUCKET_USD)
        arr = np.zeros((hi - lo) // BUCKET_USD)
        for s, u in ((sb, ub), (sa, ua)):
            if u:
                i = (s - lo) // BUCKET_USD
                arr[i:i + len(u)] += u
        self.recent.append((minute_ms, market, mid, lo, arr))
        cutoff = minute_ms - KEEP_H * 3600 * 1000
        while self.recent and self.recent[0][0] <= cutoff:
            self.recent.popleft()

    def _load_recent(self) -> None:
        """Al arrancar, recupera las últimas KEEP_H horas de los archivos del día y del anterior."""
        now = int(time.time() * 1000)
        pending: dict = {}
        for p in sorted({self._path(now - KEEP_H * 3600 * 1000), self._path(now)}):
            if not p.exists():
                continue
            try:
                with gzip.open(p, "rt") as f:
                    for line in f:
                        if line.startswith("timestamp"):
                            continue
                        ts, market, side, mid, start, usd = line.rstrip("\n").split(",", 5)
                        if int(ts) <= now - KEEP_H * 3600 * 1000:
                            continue
                        vals = [int(x) if x else 0 for x in usd.split(";")] if usd else []
                        key = (int(ts), market)
                        pending.setdefault(key, {"mid": float(mid)})[side] = (int(start), vals)
            except (OSError, EOFError, ValueError) as e:  # último minuto cortado a la mitad
                log.warning("%s: leído hasta donde se pudo (%s)", p.name, e)
        for (ts, market), v in sorted(pending.items()):
            if "b" in v and "a" in v:
                self._remember(ts, market, v["mid"], v)
        log.info("libro nivel por nivel: %d fotos recuperadas", len(self.recent))

    # --- mapa de calor para el panel ---------------------------------------------------------

    def heatmap(self) -> dict | None:
        if not self.recent:
            return None
        last = max(r[0] for r in self.recent)
        cols = KEEP_H * 60
        t0 = last - (cols - 1) * 60_000
        snaps = [r for r in self.recent if r[0] >= t0]
        lo = min(r[3] for r in snaps)
        hi = max(r[3] + len(r[4]) * BUCKET_USD for r in snaps)
        rows = (hi - lo) // BUCKET_USD
        grid = np.zeros((len(MARKETS), cols, rows), np.float32)
        mids = {m: [None] * cols for m in MARKETS}
        for ts, market, mid, start, arr in snaps:
            c, k, i = (ts - t0) // 60_000, MARKETS.index(market), (start - lo) // BUCKET_USD
            grid[k, c, i:i + len(arr)] = arr
            mids[market][c] = round(mid, 2)
        top = float(grid.max()) or 1.0
        hi_l, lo_l = math.log10(top), math.log10(top) - 4
        with np.errstate(divide="ignore"):
            q = np.where(grid > 0, np.clip(np.round(1 + 254 * (np.log10(grid) - lo_l) / (hi_l - lo_l)), 1, 255), 0)
        return {"generated": int(time.time()), "t0": t0 // 1000, "step_s": 60, "cols": cols, "rows": rows,
                "price0": lo, "bin": BUCKET_USD, "layers": list(MARKETS), "log10_lo": round(lo_l, 4),
                "log10_hi": round(hi_l, 4), "mids": mids, "whales": self._whales(t0, cols, lo, rows, mids),
                "data": base64.b64encode(q.astype(np.uint8).tobytes()).decode()}

    def _whales(self, t0, cols, lo, rows, mids) -> list:
        """Órdenes de ballenas de la ventana: [columna, fila, US$, 1 compra / 0 venta, mercado]."""
        path = self.data_dir / f"{SYMBOL}_whale_orders.csv"
        if not path.exists():
            return []
        with path.open("rb") as f:  # solo la cola del archivo: crece todo el tiempo
            f.seek(max(0, path.stat().st_size - 8_000_000))
            tail = f.read().decode(errors="ignore").splitlines()[1:]
        out = []
        for line in tail:
            try:
                ts, _, market, side, usd = line.split(",")
                c = (int(ts) - t0) // 60_000
            except ValueError:
                continue
            if not 0 <= c < cols or market not in WHALE_MARKET:
                continue
            mid = mids[WHALE_MARKET[market]][c] or mids["spot"][c] or mids["perp"][c]
            if mid is None:
                continue
            r = int((mid - lo) // BUCKET_USD)
            if 0 <= r < rows:
                out.append([int(c), r, round(float(usd)), int(side == "buy"), market])
        return out

    def write_heatmap(self) -> None:
        hm = self.heatmap()
        if hm is None:
            return
        dest = self.data_dir / "dashboard" / "book_heatmap.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(hm, separators=(",", ":")))
        tmp.replace(dest)
